from common.identity import SESSION_COOKIE, SOURCE_KEY, MANIFEST_NAME, DEFINITIONS_DIRECTORY, MANAGED_HEADER
from session_helpers import session_cookie
import base64
import json
import html
import re

import pytest
from fastapi.testclient import TestClient
from itsdangerous import TimestampSigner
from sqlalchemy import select

from frontend.app import create_app
from frontend.config import Settings
from frontend.models import User
from frontend.cli import build_parser


@pytest.mark.parametrize('persistent', [True, False])
def test_session_renews_on_use_but_expires_after_inactivity(tmp_path, monkeypatch, persistent):
    clock = [1000]
    monkeypatch.setattr('frontend.sessions.now', lambda: clock[0])
    monkeypatch.setattr(TimestampSigner, 'get_timestamp', lambda self: clock[0])
    settings = Settings(_env_file=None, environment='test', session_max_age_seconds=60,
                        session_persistent=persistent, database_url=f'sqlite:///{tmp_path / "app.db"}',
                        secret_key='test-secret-key-that-is-long-enough-123456', trusted_hosts='testserver')
    app = create_app(settings)
    with TestClient(app) as client:
        with app.state.session_factory() as db:
            user = db.scalar(select(User).where(User.username == 'admin'))
            payload = base64.b64encode(json.dumps({'user_id': user.id}).encode())
        original = session_cookie(app, payload)
        client.cookies.set(SESSION_COOKIE, original, domain='testserver.local', path='/')
        clock[0] = 1040
        response = client.get('/api/session')
        assert response.status_code == 200
        cookie = response.headers['set-cookie']
        assert ('max-age=60' in cookie.lower()) is persistent
        assert 'httponly' in cookie.lower() and 'samesite=lax' in cookie.lower()
        assert client.cookies.get(SESSION_COOKIE) != original
        clock[0] = 1080  # Original login expired, renewed session remains valid.
        assert client.get('/api/session').status_code == 200
        assert 'set-cookie' not in client.get('/healthz').headers
        clock[0] = 1141
        assert client.get('/api/session').status_code == 401


def test_session_cookie_configuration_cli_overrides_env(monkeypatch):
    monkeypatch.setenv('FRONTEND_SESSION_PERSISTENT', 'false')
    assert Settings(_env_file=None).session_persistent is False
    args = build_parser().parse_args(['--session-persistent'])
    assert Settings(_env_file=None, session_persistent=args.session_persistent).session_persistent is True
    assert build_parser().parse_args(['--no-session-persistent']).session_persistent is False


def test_live_refresh_preference_is_private_csrf_protected_and_persists(tmp_path):
    settings = Settings(_env_file=None, environment='test', database_url=f'sqlite:///{tmp_path / "app.db"}',
                        secret_key='test-secret-key-that-is-long-enough-123456', trusted_hosts='testserver')
    app = create_app(settings)
    app.state.daemon_client.ping = lambda: {'network_access': 'readonly', 'docker_access': 'disabled', 'events': True}
    with TestClient(app) as client:
        assert client.post('/api/session/refresh', data={'enabled': False, 'csrf_token': 'wrong'}).status_code == 401
        with app.state.session_factory() as db:
            user = db.scalar(select(User).where(User.username == 'admin'))
            payload = base64.b64encode(json.dumps({'user_id': user.id}).encode())
        client.cookies.set(SESSION_COOKIE, session_cookie(app, payload),
                           domain='testserver.local', path='/')
        page = client.get('/network/interfaces')
        csrf = html.unescape(re.search(r'name="csrf_token" value="([^"]+)"', page.text).group(1))
        assert '>Live refresh</button>' in page.text
        assert page.text.index('data-refresh-toggle') < page.text.index('>Settings</a>')
        assert client.post('/api/session/refresh', data={'enabled': False, 'csrf_token': 'wrong'}).status_code == 403
        result = client.post('/api/session/refresh', data={'enabled': False, 'csrf_token': csrf})
        assert result.json() == {'enabled': False}
        assert 'no-store' in result.headers['cache-control']
        assert 'data-live-refresh="false"' in client.get('/network/interfaces').text
        assert '>No refresh</button>' in client.get('/network/routing').text
        assert client.get('/api/events').status_code == 409
        app.state.daemon_client.call = lambda method: {'interfaces': [], 'addresses': []}
        assert client.get('/api/network/interfaces').status_code == 200  # Explicit navigation can still load a snapshot.
        assert client.post('/api/session/refresh', data={'enabled': True, 'csrf_token': csrf}).json() == {'enabled': True}
        assert 'data-live-refresh="true"' in client.get('/network/interfaces').text
