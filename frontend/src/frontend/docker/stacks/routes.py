"""Compose source editors, durable per-user drafts and stack action previews."""

import hashlib
import json
import re
import secrets
import time
import base64

from fastapi import Depends, Form, HTTPException, Request
from fastapi.responses import JSONResponse, RedirectResponse
from sqlalchemy import delete, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from frontend.models import ComposeDraft, utcnow
from frontend.security import csrf_is_valid, csrf_token
from common.client import DaemonUnavailable
from common.protocol import ProtocolError
from common.stack_files import MAX_BUNDLE_BYTES, upload_bundle, validate_bundle
from starlette.concurrency import run_in_threadpool
from starlette.datastructures import UploadFile


def draft_key(kind, name, path=''):
    return hashlib.sha256(json.dumps([kind, name, path]).encode()).hexdigest()


def discard_matching_draft(db, user_id, key, revision):
    if key and revision is not None:
        db.execute(delete(ComposeDraft).where(ComposeDraft.user_id == user_id, ComposeDraft.draft_key == key, ComposeDraft.revision == revision))
        db.commit()


def install_stack_routes(app, daemon, render, get_db, require_docker, require_write, current_user):
    previews = {}

    def editor_access(request, db):
        user = current_user(request, db)
        if not user:
            raise HTTPException(401, 'Sign in required')
        if not user.is_admin or user.password_change_required():
            raise HTTPException(403, 'Compose sources require an administrator with a current password')
        return user

    def backend_status():
        try:
            return {**daemon.ping(), 'available': True}
        except (DaemonUnavailable, ProtocolError, ValueError):
            return {'available': False, 'docker_access': 'disabled', 'network_access': 'disabled', 'events': False}

    def check_csrf(request, csrf):
        if not csrf_is_valid(request.session, csrf):
            raise HTTPException(403, 'Invalid CSRF token')

    def remember(request, user, kind, params, extra=None):
        now = time.monotonic()
        for token, pending in list(previews.items()):
            if pending['expires'] < now:
                previews.pop(token, None)
        while len(previews) >= 128:
            previews.pop(next(iter(previews)))
        token = secrets.token_urlsafe(32)
        binding = csrf_token(request.session)
        previews[token] = {'user': user.id, 'csrf': binding, 'kind': kind, 'params': params, 'extra': extra or {}, 'expires': now + 600}
        return token

    def consume(request, user, token, kind):
        pending = previews.pop(token, None)
        if (not pending or pending['user'] != user.id or not csrf_is_valid(request.session, pending['csrf'])
                or pending['kind'] != kind or pending['expires'] < time.monotonic()):
            raise HTTPException(409, 'Preview expired or changed; review again. Drafts are retained.')
        return pending

    def validate_key(key):
        if not re.fullmatch(r'[a-f0-9]{64}', key):
            raise HTTPException(422, 'Invalid draft key')

    @app.get('/docker/stack-create')
    def new_stack(request: Request, db: Session = Depends(get_db)):
        user, status = require_write(request, db)
        return render(request, 'stack_create.html', user=user, daemon_status=status, hide_search=True,
                      step='name', stack_name='', error=None)

    @app.post('/docker/stack-create/choose')
    def choose_stack(request: Request, name: str = Form(...), csrf: str = Form(..., alias='csrf_token'), db: Session = Depends(get_db)):
        user, status = require_write(request, db); check_csrf(request, csrf)
        try:
            daemon.call('docker.stack.name.check', {'name': name})
            from urllib.parse import urlencode
            return RedirectResponse('/docker/stack-create/options?' + urlencode({'name': name}), status_code=303)
        except (DaemonUnavailable, ProtocolError, ValueError) as exc:
            return render(request, 'stack_create.html', user=user, daemon_status=status, hide_search=True,
                          step='name', stack_name=name, error=str(exc))

    @app.get('/docker/stack-create/options')
    def stack_options(request: Request, name: str, db: Session = Depends(get_db)):
        user, status = require_write(request, db)
        try:
            available = daemon.call('docker.stack.name.check', {'name': name})
        except (DaemonUnavailable, ProtocolError, ValueError) as exc:
            return render(request, 'stack_create.html', user=user, daemon_status=status, hide_search=True,
                          step='name', stack_name=name, error=str(exc))
        return render(request, 'stack_create.html', user=user, daemon_status=status, hide_search=True,
                      step='options', stack_name=name, directory=available['directory'], error=None,
                      draft_key=draft_key('create', name), definition_text='services: {}\n')

    @app.post('/docker/stack-create/review')
    async def review_stack(request: Request, db: Session = Depends(get_db)):
        user, status = await run_in_threadpool(require_write, request, db)
        # Bound the complete request before the multipart parser spools uploads.
        body, size = [], 0
        async for chunk in request.stream():
            size += len(chunk)
            if size > app.state.settings.upload_max_bytes + 65536:
                raise HTTPException(413, 'Stack upload exceeds the configured limit')
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
        name, error, assessment, token = '', None, None, None
        try:
            async with bounded.form(max_files=1, max_fields=12, max_part_size=app.state.settings.upload_max_bytes) as form:
                check_csrf(request, str(form.get('csrf_token', '')))
                name, mode = str(form.get('name', '')), str(form.get('mode', 'paste'))
                main_file = str(form.get('main_file', '')).strip()
                if mode == 'upload':
                    upload = form.get('upload')
                    if not isinstance(upload, UploadFile) or not upload.filename:
                        raise ValueError('Choose a Compose file or an archive')
                    bundle = await run_in_threadpool(upload_bundle, upload.filename, await upload.read(app.state.settings.upload_max_bytes + 1), main_file,
                        max_bytes=app.state.settings.upload_max_bytes, max_files=app.state.settings.upload_max_files)
                elif mode == 'paste':
                    text = str(form.get('definition_text', '')) or 'services: {}\n'
                    main_file = main_file or 'compose.yaml'
                    bundle = {'main_file': main_file, 'files': {main_file: base64.b64encode(text.encode()).decode()}}
                else:
                    raise ValueError('Stack Builder and Stack Templates are planned and not available yet')
                validate_bundle(bundle['files'], bundle['main_file'], max_bytes=app.state.settings.upload_max_bytes, max_files=app.state.settings.upload_max_files)
                params = {'name': name, **bundle}
                assessment = await run_in_threadpool(daemon.call, 'docker.stack.create', {**params, 'apply': False})
                # Uploaded binary bundles are short-lived, bounded server previews.
                creation_tokens = [key for key, value in previews.items() if value['kind'] == 'create']
                for old in creation_tokens[:-7]:
                    previews.pop(old, None)
                revision = str(form.get('draft_revision', ''))
                extra = {'draft_key': draft_key('create', name), 'draft_revision': int(revision) if revision.isdigit() else None}
                token = remember(request, user, 'create', {**params, 'expected_hash': assessment['hash']}, extra)
        except (DaemonUnavailable, ProtocolError, ValueError) as exc:
            error = str(exc)
        return render(request, 'stack_create_review.html', user=user, daemon_status=status, hide_search=True,
                      stack_name=name, assessment=assessment, confirmation=token, error=error)

    @app.post('/docker/stack-create/confirm')
    def confirm_stack(request: Request, confirmation: str = Form(...), csrf: str = Form(..., alias='csrf_token'), db: Session = Depends(get_db)):
        user, status = require_write(request, db); check_csrf(request, csrf)
        pending = consume(request, user, confirmation, 'create')
        try:
            daemon.call('docker.stack.create', {**pending['params'], 'apply': True})
            discard_matching_draft(db, user.id, pending['extra']['draft_key'], pending['extra']['draft_revision'])
            from urllib.parse import quote
            return RedirectResponse('/docker/stacks/compose/' + quote(pending['params']['name'], safe=''), status_code=303)
        except (DaemonUnavailable, ProtocolError, ValueError) as exc:
            return render(request, 'stack_create_review.html', user=user, daemon_status=status, hide_search=True,
                          stack_name=pending['params']['name'], assessment=None, confirmation=None, error=str(exc))

    @app.get('/api/docker/stack-drafts/{key}')
    def get_draft(key: str, request: Request, db: Session = Depends(get_db)):
        user = editor_access(request, db); validate_key(key)
        row = db.scalar(select(ComposeDraft).where(ComposeDraft.user_id == user.id, ComposeDraft.draft_key == key))
        return JSONResponse({'draft': {'content': row.content, 'base_hash': row.base_hash, 'stack_name': row.stack_name,
                                      'revision': row.revision, 'updated_at': row.updated_at.isoformat()} if row else None}, headers={'Cache-Control': 'no-store'})

    @app.post('/api/docker/stack-drafts/{key}')
    def save_draft(key: str, request: Request, content: str = Form(...), base_hash: str = Form(...),
                   stack_name: str = Form(''), revision: int = Form(0), csrf: str = Form(..., alias='csrf_token'),
                   db: Session = Depends(get_db)):
        user = editor_access(request, db); validate_key(key); check_csrf(request, csrf)
        if len(content.encode()) > 1024 * 1024 or len(base_hash) > 128 or len(stack_name) > 128:
            raise HTTPException(413, 'Draft is too large')
        try:
            if revision == 0:
                row = ComposeDraft(user_id=user.id, draft_key=key, stack_name=stack_name, content=content, base_hash=base_hash)
                db.add(row); db.flush(); next_revision = row.revision
            else:
                result = db.execute(update(ComposeDraft).where(ComposeDraft.user_id == user.id, ComposeDraft.draft_key == key,
                    ComposeDraft.revision == revision).values(content=content, base_hash=base_hash, stack_name=stack_name,
                                                             revision=revision + 1, updated_at=utcnow()))
                if result.rowcount != 1:
                    db.rollback(); raise HTTPException(409, 'Draft changed in another tab; this text has not been overwritten')
                next_revision = revision + 1
            db.commit()
        except IntegrityError:
            db.rollback(); raise HTTPException(409, 'A newer draft exists in another tab') from None
        return JSONResponse({'revision': next_revision}, headers={'Cache-Control': 'no-store'})

    @app.delete('/api/docker/stack-drafts/{key}')
    def remove_draft(key: str, request: Request, revision: int = Form(...), csrf: str = Form(..., alias='csrf_token'), db: Session = Depends(get_db)):
        user = editor_access(request, db); validate_key(key); check_csrf(request, csrf)
        result = db.execute(delete(ComposeDraft).where(ComposeDraft.user_id == user.id, ComposeDraft.draft_key == key, ComposeDraft.revision == revision))
        if result.rowcount != 1:
            db.rollback(); raise HTTPException(409, 'Draft changed; reload before discarding')
        db.commit(); return {'deleted': True}

    @app.get('/docker/stack-files/edit')
    def file_editor(request: Request, name: str, path: str, db: Session = Depends(get_db)):
        user, status = editor_access(request, db), backend_status()
        error = None
        try:
            source = daemon.call('docker.stack.file.get', {'name': name, 'path': path})
        except (DaemonUnavailable, ProtocolError, ValueError) as exc:
            error = str(exc)
            source = {'path': path, 'hash': '', 'content': '', 'writable': False, 'kind': 'compose', 'write_reason': 'Source unavailable; saved drafts remain accessible'}
        return render(request, 'compose_editor.html', user=user, daemon_status=status, hide_search=True,
                      stack_name=name, source=source, content=source['content'], source_hash=source['hash'],
                      draft_key=draft_key('file', name, path), draft_revision=None, confirmation=None, review=None, error=error)

    @app.post('/docker/stack-files/save')
    def file_save(request: Request, name: str = Form(...), path: str = Form(...), content: str = Form(''),
                  expected_hash: str = Form(...), draft_revision: int | None = Form(None),
                  confirmation: str = Form(''), csrf: str = Form(..., alias='csrf_token'), db: Session = Depends(get_db)):
        user, status = require_write(request, db); check_csrf(request, csrf)
        params = {'name': name, 'path': path, 'content': content, 'expected_hash': expected_hash}
        error, review, next_token, source = None, None, None, None
        try:
            if confirmation:
                pending = consume(request, user, confirmation, 'file')
                if pending['params']['name'] != name or pending['params']['path'] != path:
                    raise HTTPException(409, 'Source selection changed')
                params = pending['params']; content = params['content']; expected_hash = params['expected_hash']
                review = daemon.call('docker.stack.file.save', {**params, 'apply': True})
                discard_matching_draft(db, user.id, draft_key('file', name, path), pending['extra'].get('draft_revision'))
            else:
                review = daemon.call('docker.stack.file.save', {**params, 'apply': False})
                next_token = remember(request, user, 'file', params, {'draft_revision': draft_revision})
            source = daemon.call('docker.stack.file.get', {'name': name, 'path': path})
        except (DaemonUnavailable, ProtocolError, ValueError) as exc:
            error = str(exc)
            try:
                source = daemon.call('docker.stack.file.get', {'name': name, 'path': path})
            except (DaemonUnavailable, ProtocolError, ValueError):
                source = {'path': path, 'hash': expected_hash, 'content': '', 'writable': False, 'kind': 'compose', 'write_reason': 'Source unavailable'}
        if review and review.get('applied'):
            content, expected_hash = source['content'], source['hash']
        return render(request, 'compose_editor.html', user=user, daemon_status=status, hide_search=True,
                      stack_name=name, source=source, content=content, source_hash=expected_hash,
                      draft_key=draft_key('file', name, path), draft_revision=draft_revision,
                      confirmation=next_token, review=review, error=error)

    @app.get('/docker/stack-files/convert')
    def conversion_page(request: Request, name: str, db: Session = Depends(get_db)):
        user, status = require_write(request, db)
        error, conversion, token = None, None, None
        try:
            conversion = daemon.call('docker.stack.convert', {'name': name, 'apply': False})
            token = remember(request, user, 'convert', {'name': name, 'expected': conversion['expected']})
        except (DaemonUnavailable, ProtocolError, ValueError) as exc:
            error = str(exc)
        return render(request, 'stack_conversion.html', user=user, daemon_status=status, hide_search=True,
                      stack_name=name, error=error, conversion=conversion, confirmation=token, applied=False)

    @app.post('/docker/stack-files/convert')
    def convert_stack(request: Request, confirmation: str = Form(...), csrf: str = Form(..., alias='csrf_token'), db: Session = Depends(get_db)):
        user, status = require_write(request, db); check_csrf(request, csrf)
        pending = consume(request, user, confirmation, 'convert')
        error, conversion = None, None
        try:
            conversion = daemon.call('docker.stack.convert', {**pending['params'], 'apply': True})
        except (DaemonUnavailable, ProtocolError, ValueError) as exc:
            error = str(exc)
        return render(request, 'stack_conversion.html', user=user, daemon_status=status, hide_search=True,
                      stack_name=pending['params']['name'], error=error, conversion=conversion, confirmation=None, applied=not error)

    @app.get('/docker/stack-actions/{kind}/{name}/{action}')
    def stack_action_page(kind: str, name: str, action: str, request: Request, db: Session = Depends(get_db)):
        user, status = require_write(request, db)
        error, plan, token = None, None, None
        try:
            plan = daemon.call('docker.stack.action', {'name': name, 'kind': kind, 'action': action, 'apply': False})
            token = remember(request, user, 'action', {'name': name, 'kind': kind, 'action': action, 'expected': plan['fingerprint']})
        except (DaemonUnavailable, ProtocolError, ValueError) as exc:
            error = str(exc)
        return render(request, 'stack_action.html', user=user, daemon_status=status, hide_search=True,
                      stack_name=name, kind=kind, action=action, error=error, plan=plan, confirmation=token, job=None)

    @app.post('/docker/stack-actions/confirm')
    def confirm_action(request: Request, confirmation: str = Form(...), csrf: str = Form(..., alias='csrf_token'), db: Session = Depends(get_db)):
        user, status = require_write(request, db); check_csrf(request, csrf)
        pending = consume(request, user, confirmation, 'action'); params = pending['params']
        error, job = None, None
        try:
            job = daemon.call('docker.stack.action', {**params, 'apply': True})
        except (DaemonUnavailable, ProtocolError, ValueError) as exc:
            error = str(exc)
        if job:
            return RedirectResponse('/docker/stack-jobs/' + job['id'], status_code=303)
        return render(request, 'stack_action.html', user=user, daemon_status=status, hide_search=True,
                      stack_name=params['name'], kind=params['kind'], action=params['action'], error=error, plan=None, confirmation=None, job=job)

    @app.get('/docker/stack-jobs/{identity}')
    def stack_job_page(identity: str, request: Request, db: Session = Depends(get_db)):
        user, status = require_write(request, db)
        try:
            job = daemon.call('docker.stack.job', {'id': identity})
        except (DaemonUnavailable, ProtocolError, ValueError) as exc:
            raise HTTPException(503, str(exc)) from exc
        return render(request, 'stack_action.html', user=user, daemon_status=status, hide_search=True,
                      stack_name=job['name'], kind=job['kind'], action=job['action'], error=None, plan=None, confirmation=None, job=job)

    @app.get('/api/docker/stack-jobs/{identity}')
    def stack_job(identity: str, request: Request, db: Session = Depends(get_db)):
        require_write(request, db)
        try:
            return JSONResponse(daemon.call('docker.stack.job', {'id': identity}), headers={'Cache-Control': 'no-store'})
        except (DaemonUnavailable, ProtocolError, ValueError) as exc:
            raise HTTPException(503, str(exc)) from exc
