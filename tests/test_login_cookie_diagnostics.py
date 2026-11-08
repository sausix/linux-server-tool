"""Secure-cookie transport mismatches must be explained without bypassing CSRF."""
import re

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from frontend.app import create_app
from frontend.config import Settings
from frontend.models import User
from frontend.security import set_password


@pytest.mark.parametrize('policy,scheme,expected', [('always', 'http', 403), ('auto', 'http', 303), ('always', 'https', 303)])
def test_login_cookie_transport_and_diagnostics(tmp_path, policy, scheme, expected):
    app = create_app(Settings(_env_file=None, environment='test', public_url=f'{scheme}://testserver',
        cookie_secure=policy, trusted_hosts='testserver', secret_key='test-secret-key-that-is-long-enough-123456',
        database_url=f'sqlite:///{tmp_path/"users.db"}'))
    with TestClient(app, base_url=f'{scheme}://testserver') as client:
        with app.state.session_factory() as db:
            user = db.scalar(select(User)); set_password(user, 'test-current-password-12345', 90); db.commit()
        page = client.get('/login')
        token = re.search(r'name="csrf_token" value="([^"]+)"', page.text).group(1)
        response = client.post('/login', data={'username': 'admin', 'password': 'test-current-password-12345',
                                             'csrf_token': token}, follow_redirects=False)
        assert response.status_code == expected
        if expected == 403:
            assert 'session cookies require HTTPS' in page.text
            assert 'sign-in session is missing or expired' in response.text
            assert 'FRONTEND_COOKIE_SECURE=auto' in response.text
            assert response.headers['content-type'].startswith('text/html')
            assert 'Secure' in response.headers['set-cookie']
            assert client.get('/api/session').status_code == 401
        else:
            assert 'session cookies require HTTPS' not in page.text
            assert client.get('/api/session').status_code == 200


def test_stale_login_form_gets_html_error_and_keeps_csrf_enforced(tmp_path):
    app = create_app(Settings(_env_file=None, environment='test', trusted_hosts='testserver',
        secret_key='test-secret-key-that-is-long-enough-123456', database_url=f'sqlite:///{tmp_path/"users.db"}'))
    with TestClient(app) as client:
        client.get('/login')
        response = client.post('/login', data={'username': 'admin', 'password': 'never-echo-this-password',
            'csrf_token': 'wrong-token'}, follow_redirects=False)
        assert response.status_code == 403
        assert 'sign-in form expired or changed' in response.text
        assert 'never-echo-this-password' not in response.text
        assert response.headers['cache-control'] == 'no-store'
