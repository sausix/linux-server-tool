"""Explicit stack management and previewed definition edits."""
import base64
from datetime import datetime, timezone
import json
import secrets
import time
from urllib.parse import quote, urlencode

from fastapi import Depends, Form, HTTPException, Request
from fastapi.responses import RedirectResponse
from sqlalchemy.orm import Session
from starlette.concurrency import run_in_threadpool
from starlette.datastructures import UploadFile

from common.client import DaemonUnavailable
from common.protocol import ProtocolError
from common.stack_files import MAX_BUNDLE_BYTES, upload_bundle, validate_bundle
from frontend.security import csrf_is_valid, csrf_token
from frontend.docker.stacks.routes import draft_key, discard_matching_draft


def install_tree_routes(app, daemon, render, get_db, require_write, require_docker):
    previews = {}

    def require_read(request, db):
        user, status = require_docker(request, db)
        if not user.is_admin or user.password_change_required():
            raise HTTPException(403, 'Definition editing requires an administrator with a current password')
        return user, status

    def remember(request, user, params, review, draft_revision):
        now = time.monotonic()
        for token, pending in list(previews.items()):
            if pending['expires'] < now:
                previews.pop(token, None)
        while len(previews) >= 128:
            previews.pop(next(iter(previews)))
        if params['action'] == 'new':
            previous = [key for key, item in previews.items() if item['params']['action'] == 'new']
            for key in previous[:-7]:
                previews.pop(key, None)
        if review.get('assets'):
            params['expected_asset_hash'] = review['assets']['hash']
        token = secrets.token_urlsafe(32)
        kind = 'definition-new' if params['action'] == 'new' else 'definition-edit' if params['action'] == 'edit' else 'definition-derive'
        previews[token] = {'user': user.id, 'csrf': csrf_token(request.session), 'params': params,
                           'expires': now + 600, 'draft_revision': draft_revision,
                           'draft_key': draft_key(kind, params['name'], '' if params['action'] == 'new' else params.get('node', ''))}
        return token

    @app.get('/docker/stack-models/{name}/edit/{node}')
    def layer_editor(name: str, node: str, request: Request, mode: str = 'edit', db: Session = Depends(get_db)):
        if mode not in {'edit', 'derive'}:
            raise HTTPException(422, 'Unknown definition action')
        user, status = require_write(request, db) if mode == 'derive' else require_read(request, db)
        definition, revision, error, writable, label = {}, 0, None, False, ''
        try:
            result = daemon.call('docker.stack.model.node', {'name': name, 'node': node})
            definition, revision, node = result['definition'], result['revision'], result['node']
            if mode == 'edit' and result.get('file'):
                return RedirectResponse('/docker/stack-files/edit?' + urlencode({'name': name, 'path': result['file']}), status_code=303)
            writable = mode == 'derive' or result['writable'] and status.get('docker_access') in {'rw', 'readwrite'}
            label = datetime.now(timezone.utc).isoformat(timespec='seconds') if mode == 'derive' else result['label']
        except (DaemonUnavailable, ProtocolError, ValueError) as exc:
            error = str(exc)
        return render(request, 'stack_layer_editor.html', user=user, daemon_status=status, hide_search=True,
                      stack_name=name, node=node, revision=revision, definition_text=json.dumps(definition, indent=2),
                      layer_name=label, error=error, readonly=not writable, mode=mode,
                      draft_key=draft_key('definition-derive' if mode == 'derive' else 'definition-edit', name, node), draft_revision=None)

    @app.get('/docker/stack-models/{name}/new')
    def new_definition(name: str, request: Request, db: Session = Depends(get_db)):
        user, status = require_write(request, db)
        try:
            current = daemon.call('docker.stack.model.node', {'name': name, 'node': 'active'})
        except (DaemonUnavailable, ProtocolError, ValueError) as exc:
            return render(request, 'stack_layer_review.html', user=user, daemon_status=status, hide_search=True,
                          stack_name=name, error=str(exc), confirmation=None, action='new', node='', review=None)
        return render(request, 'stack_create.html', user=user, daemon_status=status, hide_search=True,
                      step='options', stack_name=name, definition_management=True, revision=current['revision'],
                      layer_name=datetime.now(timezone.utc).isoformat(timespec='seconds'),
                      draft_key=draft_key('definition-new', name), definition_text='services: {}\n', error=None)

    @app.get('/docker/stack-models/{name}/duplicate/{node}')
    def duplicate_definition(name: str, node: str, request: Request, db: Session = Depends(get_db)):
        user, status = require_write(request, db)
        current, error = None, None
        try:
            current = daemon.call('docker.stack.model.node', {'name': name, 'node': node})
        except (DaemonUnavailable, ProtocolError, ValueError) as exc:
            error = str(exc)
        return render(request, 'stack_duplicate.html', user=user, daemon_status=status, hide_search=True,
                      stack_name=name, current=current, error=error,
                      suggested_name=current['label'][:153] + ' (copy)' if current else '')

    @app.post('/docker/stack-models/{name}/import')
    async def import_definition(name: str, request: Request, db: Session = Depends(get_db)):
        user, status = await run_in_threadpool(require_write, request, db)
        body, size = [], 0
        async for chunk in request.stream():
            size += len(chunk)
            if size > app.state.settings.upload_max_bytes + 65536:
                raise HTTPException(413, 'Definition upload exceeds the configured limit')
            body.append(chunk)
        raw = b''.join(body)
        delivered = False

        async def receive():
            nonlocal delivered
            if delivered:
                return {'type': 'http.disconnect'}
            delivered = True
            return {'type': 'http.request', 'body': raw, 'more_body': False}
        bounded = Request(request.scope, receive)
        error, review, token, label = None, None, None, ''
        try:
            async with bounded.form(max_files=1, max_fields=16, max_part_size=app.state.settings.upload_max_bytes) as form:
                if not csrf_is_valid(request.session, str(form.get('csrf_token', ''))):
                    raise HTTPException(403, 'Invalid CSRF token')
                main_file = str(form.get('main_file', '')).strip()
                mode = str(form.get('mode', 'paste'))
                if mode == 'upload':
                    upload = form.get('upload')
                    if not isinstance(upload, UploadFile) or not upload.filename:
                        raise ValueError('Choose a Compose file or archive')
                    bundle = await run_in_threadpool(upload_bundle, upload.filename, await upload.read(app.state.settings.upload_max_bytes + 1), main_file,
                        max_bytes=app.state.settings.upload_max_bytes, max_files=app.state.settings.upload_max_files)
                elif mode == 'paste':
                    main_file = main_file or 'compose.yaml'
                    text = str(form.get('definition_text', '')) or 'services: {}\n'
                    bundle = {'main_file': main_file, 'files': {main_file: base64.b64encode(text.encode()).decode()}}
                else:
                    raise ValueError('Stack Builder and Stack Templates are planned and not available yet')
                validate_bundle(bundle['files'], bundle['main_file'], max_bytes=app.state.settings.upload_max_bytes, max_files=app.state.settings.upload_max_files)
                label = str(form.get('label', ''))
                params = {'name': name, 'action': 'new', 'expected_revision': int(str(form.get('revision', '0'))),
                          'label': label, 'identity': secrets.token_hex(16), **bundle}
                review = await run_in_threadpool(daemon.call, 'docker.stack.model.change', {**params, 'apply': False})
                draft_revision = str(form.get('draft_revision', ''))
                token = remember(request, user, params, review, int(draft_revision) if draft_revision.isdigit() else None)
        except (DaemonUnavailable, ProtocolError, ValueError) as exc:
            error = str(exc)
        return render(request, 'stack_layer_review.html', user=user, daemon_status=status, hide_search=True,
                      stack_name=name, action='new', node='', label=label, error=error, review=review, confirmation=token)

    @app.post('/docker/stack-models/{name}/change')
    def layer_change(name: str, request: Request, action: str = Form(''), node: str = Form(''),
                     revision: int = Form(0), expected_hash: str = Form(''), label: str = Form(''), definition_text: str = Form(''),
                     confirmation: str = Form(''), draft_revision: int | None = Form(None),
                     csrf: str = Form(..., alias='csrf_token'), db: Session = Depends(get_db)):
        user, status = require_write(request, db)
        if not csrf_is_valid(request.session, csrf):
            raise HTTPException(403, 'Invalid CSRF token')
        error, review, token = None, None, None
        try:
            if confirmation:
                pending = previews.pop(confirmation, None)
                if (not pending or pending['expires'] < time.monotonic() or pending['user'] != user.id
                        or pending['params']['name'] != name or not csrf_is_valid(request.session, pending['csrf'])):
                    raise HTTPException(409, 'Confirmation expired or changed; review again. Drafts are retained.')
                action, node = pending['params']['action'], pending['params'].get('node', '')
                daemon.call('docker.stack.model.change', {**pending['params'], 'apply': True})
                if action in {'add', 'edit', 'new'}:
                    discard_matching_draft(db, user.id, pending['draft_key'], pending['draft_revision'])
                return RedirectResponse('/docker/stacks/compose/' + quote(name, safe=''), status_code=303)
            params = {'name': name, 'action': action, 'node': node, 'expected_revision': revision, 'expected_hash': expected_hash}
            if action == 'initialize':
                daemon.call('docker.stack.model.change', {**params, 'apply': True})
                return RedirectResponse('/docker/stacks/compose/' + quote(name, safe=''), status_code=303)
            if action == 'duplicate':
                params['label'] = label
            if action in {'add', 'edit'}:
                if len(definition_text.encode()) > 1024 * 1024:
                    raise ValueError('Compose definitions are limited to 1 MiB')
                params.update(definition=json.loads(definition_text), label=label)
            review = daemon.call('docker.stack.model.change', {**params, 'apply': False})
            token = remember(request, user, params, review, draft_revision)
        except (DaemonUnavailable, ProtocolError, ValueError) as exc:
            error = str(exc)
            if not confirmation and action in {'add', 'edit'}:
                mode = 'derive' if action == 'add' else 'edit'
                return render(request, 'stack_layer_editor.html', user=user, daemon_status=status, hide_search=True,
                              stack_name=name, node=node, revision=revision, definition_text=definition_text,
                              layer_name=label, error=error, readonly=False, mode=mode,
                              draft_key=draft_key('definition-derive' if action == 'add' else 'definition-edit', name, node), draft_revision=draft_revision)
        return render(request, 'stack_layer_review.html', user=user, daemon_status=status, hide_search=True,
                      stack_name=name, action=action, node=node, label=label, error=error, review=review, confirmation=token)
