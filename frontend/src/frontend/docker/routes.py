from __future__ import annotations
import logging
import json
import time
import re
from fastapi import Depends, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from sqlalchemy.orm import Session
from frontend.docker.stacks.routes import install_stack_routes, draft_key, discard_matching_draft
from frontend.docker.stacks.tree_routes import install_tree_routes
from frontend.docker.logs.routes import install_log_routes
from frontend.docker.volumes.routes import install_volume_routes
from common.client import DaemonUnavailable
from common.protocol import ProtocolError
from frontend.inventory import parse_filters, matches, matches_resource
from frontend.docker.models import StackEntry, ContainerEntry, ImageEntry, LabelEntry, ResourceEntry
import secrets
from frontend.security import csrf_is_valid, csrf_token
logger = logging.getLogger("frontend.docker")

def install(app, settings, daemon_client, render, get_db, current_user, service_status, safe_return_path):
    inventories = ('overview', 'configuration', 'stacks', 'containers', 'container', 'images', 'image', 'images.delete',
        'labels', 'resources', 'volumes', 'volume', 'volumes.delete', 'stack.definition.get', 'stack.definition.save',
        'stack.model', 'stack.model.node', 'stack.model.change', 'stack.file.get', 'stack.file.save', 'stack.job',
        'stack.name.check', 'stack.create', 'logs.tail')
    daemon_client.method_timeouts.update({'docker.' + method: settings.daemon_inventory_timeout_seconds for method in inventories})
    daemon_client.method_timeouts.update({'docker.' + method: settings.daemon_storage_timeout_seconds for method in ('images.storage', 'volumes.storage')})
    daemon_client.method_timeouts.update({'docker.' + method: settings.daemon_control_timeout_seconds for method in ('stack.action', 'stack.convert')})
    def require_docker(request, db):
        user = current_user(request, db)
        if not user:
            raise HTTPException(401, 'Sign in required')
        capabilities = service_status()
        if not capabilities.get('available'):
            raise HTTPException(503, 'backend is unavailable')
        if capabilities.get('docker_access', 'disabled') == 'disabled':
            raise HTTPException(403, 'Docker access is disabled')
        return user, capabilities

    @app.get('/docker/stacks', response_class=HTMLResponse)
    def stack_list(request: Request, db: Session = Depends(get_db)):
        user, capabilities = require_docker(request, db)
        return render(request, 'docker_stacks.html', user=user, daemon_status=capabilities,
                      view='stacks', search_action='/docker/stacks', show_search=True,
                      search_fields=[f'stack.{key}' for key in StackEntry.model_fields] + ['label.key', 'label.value'],
                      stack_kind=None, stack_name=None)

    @app.get('/docker/stacks/{kind}/{name}', response_class=HTMLResponse)
    def stack_details(kind: str, name: str, request: Request, db: Session = Depends(get_db)):
        user, capabilities = require_docker(request, db)
        if kind not in {'compose', 'compose-meta', 'swarm'}:
            raise HTTPException(404, 'Unknown stack kind')
        return render(request, 'docker_stacks.html', user=user, daemon_status=capabilities,
                      hide_search=True, stack_kind=kind, stack_name=name)

    def require_definition_write(request, db):
        user, capabilities = require_docker(request, db)
        if capabilities.get('docker_access') not in {'rw', 'readwrite'} or not user.is_admin or user.password_change_required():
            raise HTTPException(403, 'Stack editing and control require administrator Docker write access and a current password')
        return user, capabilities

    install_stack_routes(app, daemon_client, render, get_db, require_docker, require_definition_write, current_user)
    install_tree_routes(app, daemon_client, render, get_db, require_definition_write, require_docker)
    install_log_routes(app, daemon_client, settings, render, get_db, require_docker)
    install_volume_routes(app, daemon_client, render, get_db, require_docker)

    @app.get('/docker/stack-definitions/new', response_class=HTMLResponse)
    @app.get('/docker/stack-definitions/edit/{name}', response_class=HTMLResponse)
    def definition_editor(request: Request, name: str = '', db: Session = Depends(get_db)):
        user, capabilities = require_definition_write(request, db)
        name = '' if request.url.path.endswith('/new') else name
        definition = {'services': {}, 'x-stackmeta': {'autostart': False, 'description': '', 'dependencies': {'requires': []}}}
        revision, error = None, None
        if name:
            try:
                stored = daemon_client.call('docker.stack.definition.get', {'name': name})
                definition, revision = stored['definition'], stored['revision']
            except (DaemonUnavailable, ProtocolError, ValueError) as exc:
                error = str(exc)
        return render(request, 'stack_definition.html', user=user, daemon_status=capabilities,
                      hide_search=True, stack_name=name, definition_text=json.dumps(definition, indent=2),
                      revision=revision, error=error, confirmation=None, assessment=None,
                      draft_key=draft_key('virtual', name or 'new'), draft_revision=None)

    @app.post('/docker/stack-definitions/save', response_class=HTMLResponse)
    def save_definition(request: Request, name: str = Form(...), definition_text: str = Form(''),
                        revision: int | None = Form(None), confirmation: str = Form(''),
                        edit: bool = Form(False),
                        draft_id: str = Form(''), draft_revision: int | None = Form(None),
                        csrf: str = Form(..., alias='csrf_token'), db: Session = Depends(get_db)):
        user, capabilities = require_definition_write(request, db)
        if not csrf_is_valid(request.session, csrf):
            raise HTTPException(403, 'Invalid CSRF token')
        previews = app.state.stack_definition_previews
        now = time.monotonic()
        for token, pending in list(previews.items()):
            if pending['expires'] < now:
                previews.pop(token, None)
        error, assessment, next_confirmation = None, None, None
        try:
            if confirmation:
                pending = previews.pop(confirmation, None)
                if not pending or pending['user'] != user.id or not csrf_is_valid(request.session, pending['csrf']):
                    raise HTTPException(409, 'Confirmation expired or does not match this session; review again')
                if pending['params']['name'] != name:
                    raise HTTPException(409, 'Stack name changed after preview')
                if edit:
                    definition_text = json.dumps(pending['params']['definition'], indent=2)
                    revision = pending['params']['expected_revision']
                else:
                    daemon_client.call('docker.stack.definition.save', {**pending['params'], 'apply': True})
                    discard_matching_draft(db, user.id, pending.get('draft_id'), pending.get('draft_revision'))
                    from urllib.parse import quote
                    return RedirectResponse('/docker/stacks/compose/' + quote(name, safe=''), status_code=303)
            elif edit:
                raise HTTPException(409, 'Review the definition before editing a preview')
            elif len(definition_text.encode()) > 1024 * 1024:
                raise ValueError('Compose definitions are limited to 1 MiB')
            else:
                definition = json.loads(definition_text)
                params = {'name': name, 'definition': definition, 'expected_revision': revision}
                assessment = daemon_client.call('docker.stack.definition.save', {**params, 'apply': False})
                next_confirmation = secrets.token_urlsafe(32)
                previews[next_confirmation] = {'user': user.id, 'expires': now + 600, 'params': params,
                                               'csrf': csrf_token(request.session), 'draft_id': draft_id, 'draft_revision': draft_revision}
        except (DaemonUnavailable, ProtocolError, ValueError) as exc:
            error = str(exc)
        return render(request, 'stack_definition.html', user=user, daemon_status=capabilities,
                      hide_search=True, stack_name=name, definition_text=definition_text, revision=revision,
                      error=error, confirmation=next_confirmation, assessment=assessment,
                      draft_key=draft_id or draft_key('virtual', name or 'new'), draft_revision=draft_revision)

    @app.get('/docker/stack-models/{name}', response_class=HTMLResponse)
    def stack_model_page(name: str, request: Request, db: Session = Depends(get_db)):
        require_docker(request, db)
        from urllib.parse import quote
        return RedirectResponse('/docker/stacks/compose/' + quote(name, safe=''), status_code=303)

    @app.get('/api/docker/stacks')
    def stack_inventory(request: Request, db: Session = Depends(get_db)):
        require_docker(request, db)
        try:
            filters, words = parse_filters(request.query_params.multi_items())
            if any(not key.startswith('stack.') and key not in {'label.key', 'label.value'} for key, _ in filters):
                raise ValueError('Only stack.* and label.key/value fields are supported here')
        except ValueError as exc:
            raise HTTPException(422, str(exc)) from exc
        try:
            runtime_name = next((value for key, value in filters if key == 'stack.name'), None)
            data = daemon_client.call('docker.stacks', {'runtime': runtime_name}) if runtime_name else daemon_client.call('docker.stacks')
            found = []
            for item in data['stacks']:
                highlights = {}
                if matches_resource('stack', StackEntry(**item), item.get('labels'), filters, words, highlights):
                    found.append({**item, '_matches': highlights})
            return JSONResponse({**data, 'stacks': found}, headers={'Cache-Control': 'no-store'})
        except (DaemonUnavailable, ProtocolError, ValueError) as exc:
            raise HTTPException(503, f'docker.stacks failed: {exc}') from exc

    @app.get('/docker/containers', response_class=HTMLResponse)
    @app.get('/docker/containers/{container_id}', response_class=HTMLResponse)
    def containers_page(request: Request, container_id: str = '', db: Session = Depends(get_db)):
        user, capabilities = require_docker(request, db)
        if container_id and not re.fullmatch(r'[a-fA-F0-9]{12,64}', container_id):
            raise HTTPException(404, 'Invalid container ID')
        return render(request, 'docker_containers.html', user=user, daemon_status=capabilities,
                      container_id=container_id, hide_search=bool(container_id), show_search=not container_id,
                      view='containers', search_action='/docker/containers',
                      search_fields=[f'container.{key}' for key in ContainerEntry.model_fields] + ['label.key', 'label.value'])

    @app.get('/api/docker/containers')
    def containers_inventory(request: Request, db: Session = Depends(get_db)):
        require_docker(request, db)
        try:
            filters, words = parse_filters(request.query_params.multi_items())
            if any(not key.startswith('container.') and key not in {'label.key', 'label.value'} for key, _ in filters):
                raise ValueError('Only container.* and label.key/value fields are supported here')
        except ValueError as exc:
            raise HTTPException(422, str(exc)) from exc
        try:
            data = daemon_client.call('docker.containers')
            found = []
            for item in data['containers']:
                highlights = {}
                if matches_resource('container', ContainerEntry(**item), item.get('labels'), filters, words, highlights):
                    found.append({**item, '_matches': highlights})
            return JSONResponse({**data, 'containers': found}, headers={'Cache-Control': 'no-store'})
        except (DaemonUnavailable, ProtocolError, ValueError) as exc:
            raise HTTPException(503, f'docker.containers failed: {exc}') from exc

    @app.get('/api/docker/containers/{container_id}')
    def container_data(container_id: str, request: Request, db: Session = Depends(get_db)):
        require_docker(request, db)
        if not re.fullmatch(r'[a-fA-F0-9]{12,64}', container_id):
            raise HTTPException(404, 'Invalid container ID')
        try:
            return JSONResponse(daemon_client.call('docker.container', {'id': container_id}), headers={'Cache-Control': 'no-store'})
        except (DaemonUnavailable, ProtocolError, ValueError) as exc:
            raise HTTPException(503, f'docker.container failed: {exc}') from exc

    @app.get('/docker/images', response_class=HTMLResponse)
    @app.get('/docker/images/detail', response_class=HTMLResponse)
    def images_page(request: Request, image: str = '', db: Session = Depends(get_db)):
        user, capabilities = require_docker(request, db)
        detail = request.url.path.endswith('/detail')
        if detail and not image:
            raise HTTPException(422, 'An image reference is required')
        return render(request, 'docker_images.html', user=user, daemon_status=capabilities,
                      image_reference=image if detail else '', hide_search=detail, show_search=not detail,
                      view='images', search_action='/docker/images',
                      search_fields=[f'image.{key}' for key in ImageEntry.model_fields] + ['label.key', 'label.value'])

    @app.get('/api/docker/images')
    def images_inventory(request: Request, db: Session = Depends(get_db)):
        require_docker(request, db)
        try:
            filters, words = parse_filters(request.query_params.multi_items())
            if any(not key.startswith('image.') and key not in {'label.key', 'label.value'} for key, _ in filters):
                raise ValueError('Only image.* and label.key/value fields are supported here')
        except ValueError as exc:
            raise HTTPException(422, str(exc)) from exc
        try:
            data = daemon_client.call('docker.images')
            found = []
            for item in data['images']:
                highlights = {}
                if matches_resource('image', ImageEntry(**item), item.get('labels'), filters, words, highlights):
                    found.append({**item, '_matches': highlights})
            return JSONResponse({**data, 'images': found}, headers={'Cache-Control': 'no-store'})
        except (DaemonUnavailable, ProtocolError, ValueError) as exc:
            raise HTTPException(503, f'docker.images failed: {exc}') from exc

    @app.get('/api/docker/images/storage')
    def image_storage(request: Request, db: Session = Depends(get_db)):
        require_docker(request, db)
        try:
            return JSONResponse(daemon_client.call('docker.images.storage'), headers={'Cache-Control': 'no-store'})
        except (DaemonUnavailable, ProtocolError, ValueError) as exc:
            raise HTTPException(503, 'Image storage statistics unavailable: ' + str(exc)) from exc

    @app.get('/api/docker/images/detail')
    def image_data(request: Request, image: str, db: Session = Depends(get_db)):
        require_docker(request, db)
        try:
            return JSONResponse(daemon_client.call('docker.image', {'image': image}), headers={'Cache-Control': 'no-store'})
        except (DaemonUnavailable, ProtocolError, ValueError) as exc:
            raise HTTPException(503, f'docker.image failed: {exc}') from exc

    @app.get('/docker/images/delete', response_class=HTMLResponse)
    def image_deletion_preview(request: Request, image: str, db: Session = Depends(get_db)):
        # A GET can only build a preview; execution always requires the confirmed POST.
        return image_deletion(request, ids=[image], mode='selected', confirmation='',
                              csrf=csrf_token(request.session), db=db)

    @app.post('/docker/images/delete', response_class=HTMLResponse)
    def image_deletion(request: Request, ids: list[str] = Form(default=[]), mode: str = Form('selected'),
                       confirmation: str = Form(''), csrf: str = Form(..., alias='csrf_token'),
                       db: Session = Depends(get_db)):
        user, capabilities = require_docker(request, db)
        if capabilities.get('docker_access') not in {'rw', 'readwrite'} or not user.is_admin or user.password_change_required():
            raise HTTPException(403, 'Image deletion requires administrator write access and a current password')
        if not csrf_is_valid(request.session, csrf):
            raise HTTPException(403, 'Invalid CSRF token')
        previews = app.state.image_deletion_previews
        now = time.monotonic()
        for token, pending in list(previews.items()):
            if pending['expires'] < now:
                previews.pop(token, None)
        result, next_confirmation = {'items': [], 'errors': [], 'deleted': [], 'applied': False}, None
        try:
            if confirmation:
                pending = previews.pop(confirmation, None)
                if request.session.pop('image_delete_token', None) != confirmation or not pending or pending['user'] != user.id:
                    raise HTTPException(409, 'Confirmation expired or does not match this session. Review again.')
                result = daemon_client.call('docker.images.delete', {**pending['params'], 'apply': True})
            else:
                if mode == 'unused':
                    data = daemon_client.call('docker.images')
                    ids = [item['id'] for item in data['images'] if item['usage'] == 'unused']
                elif mode != 'selected':
                    raise HTTPException(422, 'Unknown deletion mode')
                result = daemon_client.call('docker.images.delete', {'ids': ids, 'apply': False})
                if result['items'] and not result['errors']:
                    next_confirmation = secrets.token_urlsafe(32)
                    previous = request.session.get('image_delete_token')
                    previews.pop(previous, None)
                    previews[next_confirmation] = {'user': user.id, 'expires': now + 600,
                        'params': {'ids': [item['id'] for item in result['items']],
                                   'expected': {item['id']: item['tags'] for item in result['items']}}}
                    request.session['image_delete_token'] = next_confirmation
        except (DaemonUnavailable, ProtocolError, ValueError) as exc:
            result['errors'].append(str(exc))
        return render(request, 'image_delete.html', user=user, daemon_status=capabilities,
                      hide_search=True, result=result, confirmation=next_confirmation)

    @app.get('/docker/labels', response_class=HTMLResponse)
    def labels_page(request: Request, db: Session = Depends(get_db)):
        user, capabilities = require_docker(request, db)
        return render(request, 'docker_labels.html', user=user, daemon_status=capabilities,
                      view='labels', search_action='/docker/labels', show_search=True,
                      search_fields=[f'label.{key}' for key in LabelEntry.model_fields])

    @app.get('/api/docker/labels')
    def labels_inventory(request: Request, db: Session = Depends(get_db)):
        require_docker(request, db)
        try:
            filters, words = parse_filters(request.query_params.multi_items())
            if any(not key.startswith('label.') for key, _ in filters):
                raise ValueError('Only label.* fields are supported here')
        except ValueError as exc:
            raise HTTPException(422, str(exc)) from exc
        try:
            data = daemon_client.call('docker.labels')
            found = []
            for item in data['labels']:
                highlights = {}
                if matches({'label': LabelEntry(**item)}, filters, words, highlights):
                    found.append({**item, '_matches': highlights})
            return JSONResponse({**data, 'labels': found}, headers={'Cache-Control': 'no-store'})
        except (DaemonUnavailable, ProtocolError, ValueError) as exc:
            raise HTTPException(503, f'docker.labels failed: {exc}') from exc

    resource_kinds = {'services', 'container_templates', 'networks', 'volumes', 'nodes', 'engines'}

    @app.get('/docker/resources/{kind}', response_class=HTMLResponse)
    def resource_page(kind: str, request: Request, db: Session = Depends(get_db)):
        user, capabilities = require_docker(request, db)
        if kind == 'volumes':
            return RedirectResponse('/docker/volumes' + ('?' + request.url.query if request.url.query else ''), status_code=303)
        if kind not in resource_kinds:
            raise HTTPException(404)
        return render(request, 'docker_resources.html', user=user, daemon_status=capabilities,
                      resource_kind=kind, view='resources', search_action=f'/docker/resources/{kind}', show_search=True,
                      search_fields=[f'resource.{key}' for key in ResourceEntry.model_fields] + ['label.key', 'label.value'])

    @app.get('/api/docker/resources/{kind}')
    def resource_data(kind: str, request: Request, db: Session = Depends(get_db)):
        require_docker(request, db)
        if kind not in resource_kinds:
            raise HTTPException(404)
        try:
            filters, words = parse_filters(request.query_params.multi_items())
            if any(not key.startswith('resource.') and key not in {'label.key', 'label.value'} for key, _ in filters):
                raise ValueError('Only resource.* and label.key/value fields are supported here')
        except ValueError as exc:
            raise HTTPException(422, str(exc)) from exc
        try:
            data = daemon_client.call('docker.resources', {'kind': kind})
            found = []
            for item in data['resources']:
                highlights = {}
                if matches_resource('resource', ResourceEntry(**item), item.get('labels'), filters, words, highlights):
                    found.append({**item, '_matches': highlights})
            return JSONResponse({'resources': found}, headers={'Cache-Control': 'no-store'})
        except (DaemonUnavailable, ProtocolError, ValueError) as exc:
            raise HTTPException(503, f'docker.resources failed: {exc}') from exc

    @app.get('/docker', response_class=HTMLResponse)
    @app.get('/docker/configuration', response_class=HTMLResponse)
    def docker_overview(request: Request, db: Session = Depends(get_db)):
        user = current_user(request, db)
        if not user:
            raise HTTPException(401, 'Sign in required')
        capabilities = service_status()
        if capabilities.get('available') and capabilities.get('docker_access', 'disabled') == 'disabled':
            raise HTTPException(403, 'Docker access is disabled')
        overview = {'reachable': False, 'endpoint': capabilities.get('connections', {}).get('docker', 'Unknown'),
                    'version': {}, 'info': {}, 'errors': []}
        if capabilities.get('available'):
            try:
                overview = daemon_client.call('docker.overview')
            except (DaemonUnavailable, ProtocolError, ValueError) as exc:
                overview['errors'].append(str(exc))
        else:
            overview['errors'].append('backend is unavailable')
        configuration = None
        if request.url.path == '/docker/configuration' and capabilities.get('available'):
            try:
                configuration = daemon_client.call('docker.configuration')
            except (DaemonUnavailable, ProtocolError, ValueError) as exc:
                overview['errors'].append(f'Configuration sources: {exc}')
        template = 'docker_configuration.html' if request.url.path == '/docker/configuration' else 'docker_overview.html'
        return render(request, template, user=user, daemon_status=capabilities,
                      overview=overview, configuration=configuration, hide_search=True)

    return require_docker
