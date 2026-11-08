from common.identity import SESSION_COOKIE, SOURCE_KEY, MANIFEST_NAME, DEFINITIONS_DIRECTORY, MANAGED_HEADER
from session_helpers import session_cookie
import base64
import json

from fastapi.testclient import TestClient
from itsdangerous import TimestampSigner
from sqlalchemy import select

from frontend.app import create_app
from frontend.config import Settings
from frontend.models import User
from frontend.security import set_password


def test_system_pages_auth_navigation_and_independent_sysctl(tmp_path):
    settings = Settings(_env_file=None, environment='test', database_url=f'sqlite:///{tmp_path / "app.db"}',
                        secret_key='test-secret-key-that-is-long-enough-123456', trusted_hosts='testserver')
    app = create_app(settings)
    capabilities = {'network_access': 'disabled', 'system_access': 'readonly', 'docker_access': 'disabled',
                    'features': {'interfaces': 'disabled', 'routing': 'disabled', 'firewall': 'disabled', 'sysctl': 'ro'}}
    app.state.daemon_client.ping = lambda: capabilities
    calls = []
    def rpc(method, params=None):
        calls.append(method)
        return {'options': []} if 'sysctl' in method else {'cpu_model': 'example'}
    app.state.daemon_client.call = rpc
    with TestClient(app) as client:
        assert client.get('/api/system').status_code == 401
        assert client.get('/api/system/sysctl').status_code == 401
        assert client.get('/api/system/configuration').status_code == 401
        with app.state.session_factory() as db:
            user = db.scalar(select(User).where(User.username == 'admin'))
            set_password(user, 'test-new-password', 90)
            db.commit()
            payload = base64.b64encode(json.dumps({'user_id': user.id}).encode())
        client.cookies.set(SESSION_COOKIE, session_cookie(app, payload))
        page = client.get('/system')
        assert page.status_code == 200 and 'data-category="system"' in page.text
        assert 'data-inventory-search' not in page.text
        assert page.text.index('href="/system/configuration"') < page.text.index('href="/system/sysctl"')
        review = client.get('/system/configuration')
        assert review.status_code == 200 and '<p class="page-permission">Read only</p>' in review.text
        assert 'data-inventory-search' not in review.text
        assert client.get('/api/system/configuration').status_code == 200
        assert calls[-1] == 'system.configuration'
        assert client.post('/api/system/configuration', json={'apply': True}).status_code == 405
        assert '/system/sysctl' in page.text and 'System control' in page.text
        assert 'Network monitoring' not in page.text
        assert client.get('/api/system').json()['cpu_model'] == 'example'
        assert calls[-1] == 'system.snapshot'
        page = client.get('/system/sysctl')
        assert page.status_code == 200 and 'action="/system/sysctl"' in page.text
        assert client.get('/api/system/sysctl').status_code == 200
        assert calls[-1] == 'system.sysctl'
        redirect = client.get('/network/sysctl?class=routing', follow_redirects=False)
        assert redirect.status_code == 303 and redirect.headers['location'] == '/system/sysctl?class=routing'
        capabilities['system_access'] = 'disabled'
        assert client.get('/api/system').status_code == 403
        assert client.get('/api/system/sysctl').status_code == 403
        assert client.get('/api/system/configuration').status_code == 403
        capabilities['network_access'] = 'readwrite'
        review = client.get('/network/configuration')
        assert review.status_code == 200 and '<p class="page-permission">Read only</p>' in review.text
        assert 'href="/network/configuration" aria-current="page"' in review.text
        assert 'data-inventory-search' not in review.text
        assert client.get('/api/network/configuration').status_code == 200
        assert calls[-1] == 'network.configuration'
        capabilities['network_access'] = 'disabled'
        assert client.get('/api/network/configuration').status_code == 403
