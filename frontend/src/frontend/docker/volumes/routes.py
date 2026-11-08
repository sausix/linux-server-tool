"""Volume inventory and session-bound deletion confirmations."""

import secrets
import time

from fastapi import Depends, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse
from sqlalchemy.orm import Session

from common.client import DaemonUnavailable
from common.protocol import ProtocolError
from frontend.inventory import matches_resource, parse_filters
from frontend.docker.models import VolumeEntry
from frontend.security import csrf_is_valid, csrf_token


def install_volume_routes(app, daemon_client, render, get_db, require_docker):
    previews = {}

    @app.get('/docker/volumes', response_class=HTMLResponse)
    @app.get('/docker/volumes/detail', response_class=HTMLResponse)
    def volumes_page(request: Request, volume: str = '', db: Session = Depends(get_db)):
        user, capabilities = require_docker(request, db)
        detail = request.url.path.endswith('/detail')
        if detail and not volume:
            raise HTTPException(422, 'A volume name is required')
        return render(request, 'docker_volumes.html', user=user, daemon_status=capabilities,
                      volume_reference=volume if detail else '', hide_search=detail,
                      view='volumes', search_action='/docker/volumes', show_search=not detail,
                      search_fields=[f'volume.{key}' for key in VolumeEntry.model_fields] + ['label.key', 'label.value'])

    @app.get('/api/docker/volumes/detail')
    def volume_details(request: Request, volume: str, db: Session = Depends(get_db)):
        require_docker(request, db)
        try:
            return JSONResponse(daemon_client.call('docker.volume', {'name': volume}), headers={'Cache-Control': 'no-store'})
        except (DaemonUnavailable, ProtocolError, ValueError) as exc:
            raise HTTPException(503, str(exc)) from exc

    @app.get('/api/docker/volumes')
    def volume_inventory(request: Request, db: Session = Depends(get_db)):
        require_docker(request, db)
        try:
            filters, words = parse_filters(request.query_params.multi_items())
            if any(not key.startswith('volume.') and key not in {'label.key', 'label.value'} for key, _ in filters):
                raise ValueError('Only volume.* and label.key/value fields are supported here')
        except ValueError as exc:
            raise HTTPException(422, str(exc)) from exc
        try:
            data = daemon_client.call('docker.volumes')
            found = []
            for item in data['volumes']:
                highlights = {}
                if matches_resource('volume', VolumeEntry(**item), item.get('labels'), filters, words, highlights):
                    found.append({**item, '_matches': highlights})
            return JSONResponse({**data, 'volumes': found}, headers={'Cache-Control': 'no-store'})
        except (DaemonUnavailable, ProtocolError, ValueError) as exc:
            raise HTTPException(503, str(exc)) from exc

    @app.get('/api/docker/volumes/storage')
    def volume_storage(request: Request, db: Session = Depends(get_db)):
        require_docker(request, db)
        try:
            return JSONResponse(daemon_client.call('docker.volumes.storage'), headers={'Cache-Control': 'no-store'})
        except (DaemonUnavailable, ProtocolError, ValueError) as exc:
            raise HTTPException(503, str(exc)) from exc

    @app.get('/docker/volumes/delete', response_class=HTMLResponse)
    def volume_deletion_preview(request: Request, volume: str, db: Session = Depends(get_db)):
        return volume_deletion(request, ids=[volume], mode='selected', confirmation='',
                               csrf=csrf_token(request.session), db=db)

    @app.post('/docker/volumes/delete', response_class=HTMLResponse)
    def volume_deletion(request: Request, ids: list[str] = Form(default=[]), mode: str = Form('selected'),
                        confirmation: str = Form(''), csrf: str = Form(..., alias='csrf_token'),
                        db: Session = Depends(get_db)):
        user, capabilities = require_docker(request, db)
        if capabilities.get('docker_access') not in {'rw', 'readwrite'} or not user.is_admin or user.password_change_required():
            raise HTTPException(403, 'Volume deletion requires administrator write access and a current password')
        if not csrf_is_valid(request.session, csrf):
            raise HTTPException(403, 'Invalid CSRF token')
        now = time.monotonic()
        for token, pending in list(previews.items()):
            if pending['expires'] < now:
                previews.pop(token, None)
        result, next_confirmation = {'items': [], 'errors': [], 'deleted': [], 'applied': False}, None
        try:
            if confirmation:
                pending = previews.pop(confirmation, None)
                if request.session.pop('volume_delete_token', None) != confirmation or not pending or pending['user'] != user.id:
                    raise HTTPException(409, 'Confirmation expired or does not match this session. Review again.')
                result = daemon_client.call('docker.volumes.delete', {**pending['params'], 'apply': True})
            else:
                if mode == 'unused':
                    data = daemon_client.call('docker.volumes')
                    ids = [item['name'] for item in data['volumes'] if item['usage'] == 'unused']
                elif mode != 'selected':
                    raise HTTPException(422, 'Unknown deletion mode')
                result = daemon_client.call('docker.volumes.delete', {'ids': ids, 'apply': False})
                if result['items'] and not result['errors']:
                    next_confirmation = secrets.token_urlsafe(32)
                    previews.pop(request.session.get('volume_delete_token'), None)
                    if len(previews) >= 128:
                        previews.pop(next(iter(previews)))
                    previews[next_confirmation] = {'user': user.id, 'expires': now + 600,
                        'params': {'ids': [item['name'] for item in result['items']],
                                   'expected': {item['name']: item['fingerprint'] for item in result['items']}}}
                    request.session['volume_delete_token'] = next_confirmation
        except (DaemonUnavailable, ProtocolError, ValueError) as exc:
            result['errors'].append(str(exc))
        return render(request, 'volume_delete.html', user=user, daemon_status=capabilities,
                      hide_search=True, result=result, confirmation=next_confirmation)
