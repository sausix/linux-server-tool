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


def test_image_deletion_requires_csrf_preview_and_one_time_confirmation(tmp_path):
    settings = Settings(_env_file=None, environment='test', database_url=f'sqlite:///{tmp_path / "app.db"}',
                        secret_key='test-secret-key-that-is-long-enough-123456', trusted_hosts='testserver')
    app = create_app(settings)
    image_id = 'sha256:' + 'a' * 64
    item = {'id': image_id, 'name': 'web:latest', 'tags': ['web:latest'], 'usage': 'unused'}
    calls = []
    app.state.daemon_client.ping = lambda: {'docker_access': 'readwrite', 'network_access': 'disabled'}
    def rpc(method, params=None):
        calls.append((method, params))
        if method == 'docker.images':
            return {'images': [item]}
        return {'items': [item], 'errors': [], 'deleted': [image_id] if params['apply'] else [], 'applied': params['apply']}
    app.state.daemon_client.call = rpc
    with TestClient(app) as client:
        assert client.post('/docker/images/delete', data={'csrf_token': 'bad'}, follow_redirects=False).status_code in {303, 401}
        with app.state.session_factory() as db:
            user = db.scalar(select(User).where(User.username == 'admin'))
            set_password(user, 'test-new-password', 90)
            db.commit()
            payload = base64.b64encode(json.dumps({'user_id': user.id}).encode())
        client.cookies.set(SESSION_COOKIE, session_cookie(app, payload))
        page = client.get('/docker/images')
        csrf = html.unescape(re.search(r'data-csrf="([^"]+)"', page.text).group(1))
        assert 'data-write="true"' in page.text
        assert client.post('/docker/images/delete', data={'csrf_token': 'bad', 'ids': [image_id]}).status_code == 403
        assert not calls
        single = client.get('/docker/images/delete', params={'image': image_id})
        assert single.status_code == 200
        assert 'Confirm Delete' in single.text
        assert '/docker/images/detail?' in single.text
        assert calls[-1] == ('docker.images.delete', {'ids': [image_id], 'apply': False})
        assert 'action="http://testserver/docker/images/delete"' in single.text
        assert not any(params and params.get('apply') is True for _, params in calls)
        preview = client.post('/docker/images/delete', data={'csrf_token': csrf, 'mode': 'unused'})
        assert preview.status_code == 200
        assert 'data-inventory-search' not in preview.text
        assert calls[-1][1]['apply'] is False
        token = html.unescape(re.search(r'name="confirmation" value="([^"]+)"', preview.text).group(1))
        result = client.post('/docker/images/delete', data={'csrf_token': csrf, 'confirmation': token})
        assert result.status_code == 200
        assert '/docker/images/detail?' not in result.text
        assert calls[-1] == ('docker.images.delete', {'ids': [image_id], 'expected': {image_id: ['web:latest']}, 'apply': True})
        assert client.post('/docker/images/delete', data={'csrf_token': csrf, 'confirmation': token}).status_code == 409
        app.state.daemon_client.ping = lambda: {'docker_access': 'readonly', 'network_access': 'disabled'}
        assert client.post('/docker/images/delete', data={'csrf_token': csrf, 'ids': [image_id]}).status_code == 403
