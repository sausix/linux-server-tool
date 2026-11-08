from common.identity import SESSION_COOKIE, SOURCE_KEY, MANIFEST_NAME, DEFINITIONS_DIRECTORY, MANAGED_HEADER
import base64
import html
import json
import re
import time
from urllib.parse import parse_qs, urlsplit

import pytest
from fastapi.testclient import TestClient
from itsdangerous import TimestampSigner
from sqlalchemy import select

from frontend.app import create_app
from frontend.config import Settings
from frontend.models import User
from frontend.security import set_password


def application(tmp_path):
    return create_app(Settings(_env_file=None, environment='test',
        database_url=f'sqlite:///{tmp_path / "test.db"}', trusted_hosts='testserver',
        session_max_age_seconds=60, secret_key='test-secret-key-that-is-long-enough-123456'))


def test_expired_cookie_redirects_pages_but_preserves_api_401(tmp_path):
    app = application(tmp_path)

    class OldSigner(TimestampSigner):
        def get_timestamp(self):
            return int(time.time()) - 120

    with TestClient(app) as client:
        payload = base64.b64encode(json.dumps({'user_id': 1}).encode())
        cookie = OldSigner(app.state.settings.secret_key).sign(payload).decode()
        client.cookies.set(SESSION_COOKIE, cookie)
        target = '/network/interfaces?interface.name=eth0'
        response = client.get(target, follow_redirects=False)
        assert response.status_code == 303
        login = response.headers['location']
        assert parse_qs(urlsplit(login).query)['next'] == [target]
        page = client.get(login)
        assert page.status_code == 200
        assert 'Your session has expired' in page.text
        assert 'interface.name=eth0' in html.unescape(page.text)
        for path in ('/api/session', '/api/events', '/api/network/interfaces'):
            assert client.get(path, follow_redirects=False).status_code == 401


@pytest.mark.parametrize('target,expected', [
    ('/network/interfaces?interface.name=eth0', '/network/interfaces?interface.name=eth0'),
    ('https://external.example/', '/'), ('//external.example/', '/'),
    ('/\\external.example/', '/'), ('/login', '/'),
])
def test_login_return_target_is_local_and_validated_on_post(tmp_path, target, expected):
    app = application(tmp_path)
    with TestClient(app) as client:
        with app.state.session_factory() as db:
            user = db.scalar(select(User).where(User.username == 'admin'))
            set_password(user, 'new-secure-password', 90)
            db.commit()
        page = client.get('/login')
        csrf = re.search(r'name="csrf_token" value="([^"]+)"', page.text).group(1)
        response = client.post('/login', data={'username': 'admin', 'password': 'new-secure-password',
                              'csrf_token': csrf, 'next': target}, follow_redirects=False)
        assert response.status_code == 303
        assert response.headers['location'] == expected
