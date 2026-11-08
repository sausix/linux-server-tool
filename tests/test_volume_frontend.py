from common.identity import SESSION_COOKIE, SOURCE_KEY, MANIFEST_NAME, DEFINITIONS_DIRECTORY, MANAGED_HEADER
from session_helpers import session_cookie
import base64
import html
import json
import re

from fastapi.testclient import TestClient
from itsdangerous import TimestampSigner
from sqlalchemy import select

from frontend.app import create_app
from frontend.config import Settings
from frontend.models import User
from frontend.security import set_password


def test_volume_filtering_and_session_bound_delete_confirmation(tmp_path):
    settings = Settings(_env_file=None, environment='test', database_url=f'sqlite:///{tmp_path / "app.db"}',
                        secret_key='test-secret-key-that-is-long-enough-123456', trusted_hosts='testserver')
    app = create_app(settings)
    item = {'id': 'data', 'name': 'data', 'driver': 'local', 'usage': 'unused', 'fingerprint': 'reviewed',
            'container': ['web'], 'stack': ['demo'], 'labels': {'owner': 'demo'}}
    calls = []
    app.state.daemon_client.ping = lambda: {'docker_access': 'readwrite', 'network_access': 'disabled'}
    def rpc(method, params=None):
        calls.append((method, params))
        if method == 'docker.volumes':
            return {'volumes': [item], 'errors': []}
        if method == 'docker.volume':
            return {**item, 'inspect': {'Name': 'data', 'Mountpoint': '/volume/data', 'Options': {'type': 'tmpfs'}}}
        if method == 'docker.volumes.storage':
            return {'volumes': {'data': 123}}
        return {'items': [item], 'errors': [], 'deleted': ['data'] if params['apply'] else [], 'applied': params['apply']}
    app.state.daemon_client.call = rpc
    with TestClient(app) as client:
        assert client.get('/api/docker/volumes').status_code == 401
        assert client.get('/api/docker/volumes/storage').status_code == 401
        assert client.get('/api/docker/volumes/detail?volume=data').status_code == 401
        with app.state.session_factory() as db:
            user = db.scalar(select(User).where(User.username == 'admin'))
            set_password(user, 'test-new-password', 90)
            db.commit()
            payload = base64.b64encode(json.dumps({'user_id': user.id}).encode())
        client.cookies.set(SESSION_COOKIE, session_cookie(app, payload))
        page = client.get('/docker/volumes')
        assert page.status_code == 200 and 'data-write="true"' in page.text
        script = html.unescape(re.search(r'src="([^"]+/docker_links.js\?v=[^"]+)"', page.text).group(1))
        assert 'function dockerVolumeTable(' in client.get(script).text
        assert 'function dockerVolumeLink(' in client.get(script).text
        detail = client.get('/docker/volumes/detail?volume=data')
        assert detail.status_code == 200 and 'data-reference="data"' in detail.text
        assert 'data-inventory-search' not in detail.text
        assert client.get('/docker/volumes/detail').status_code == 422
        data = client.get('/api/docker/volumes/detail?volume=data')
        assert data.status_code == 200 and data.json()['inspect']['Options'] == {'type': 'tmpfs'}
        csrf = html.unescape(re.search(r'data-csrf="([^"]+)"', page.text).group(1))
        response = client.get('/api/docker/volumes', params={'q': 'volume.stack=demo volume.container=web label.key=owner label.value=demo'})
        assert response.status_code == 200 and len(response.json()['volumes']) == 1
        assert client.get('/api/docker/volumes', params={'volume.usage': 'active'}).json()['volumes'] == []
        assert client.get('/api/docker/volumes', params={'container.name': 'web'}).status_code == 422
        assert client.get('/api/docker/volumes/storage').json() == {'volumes': {'data': 123}}
        assert client.post('/docker/volumes/delete', data={'csrf_token': 'bad', 'ids': ['data']}).status_code == 403
        preview = client.get('/docker/volumes/delete', params={'volume': 'data'})
        assert preview.status_code == 200 and 'permanently removes their data' in preview.text
        assert '/docker/volumes/detail?volume=data' in preview.text
        assert 'data-inventory-search' not in preview.text
        assert not any(params and params.get('apply') for _, params in calls)
        token = html.unescape(re.search(r'name="confirmation" value="([^"]+)"', preview.text).group(1))
        response = client.post('/docker/volumes/delete', data={'csrf_token': csrf, 'confirmation': token})
        assert response.status_code == 200 and 'Deleted 1 volume(s)' in response.text
        assert '/docker/volumes/detail?volume=data' not in response.text
        assert calls[-1] == ('docker.volumes.delete', {'ids': ['data'], 'expected': {'data': 'reviewed'}, 'apply': True})
        assert client.post('/docker/volumes/delete', data={'csrf_token': csrf, 'confirmation': token}).status_code == 409
        def partial_rpc(method, params=None):
            return {'items': [item, {**item, 'id': 'kept', 'name': 'kept'}],
                    'errors': ['kept: volume is in use'] if params['apply'] else [],
                    'deleted': ['data'] if params['apply'] else [], 'applied': params['apply']}
        app.state.daemon_client.call = partial_rpc
        preview = client.post('/docker/volumes/delete', data={'csrf_token': csrf, 'ids': ['data', 'kept']})
        token = html.unescape(re.search(r'name="confirmation" value="([^"]+)"', preview.text).group(1))
        partial = client.post('/docker/volumes/delete', data={'csrf_token': csrf, 'confirmation': token})
        assert partial.status_code == 200
        assert '/docker/volumes/detail?volume=data' not in partial.text
        assert '/docker/volumes/detail?volume=kept' in partial.text
        app.state.daemon_client.ping = lambda: {'docker_access': 'readonly', 'network_access': 'disabled'}
        assert client.get('/docker/volumes/delete', params={'volume': 'data'}).status_code == 403
        assert 'data-write="false"' in client.get('/docker/volumes').text
