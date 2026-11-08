from common.identity import SESSION_COOKIE, SOURCE_KEY, MANIFEST_NAME, DEFINITIONS_DIRECTORY, MANAGED_HEADER
from session_helpers import session_cookie
from pathlib import Path
import re

from fastapi.testclient import TestClient
from sqlalchemy import select

from frontend.app import create_app
from frontend.config import Settings
from frontend.models import User


def test_network_views_require_login(tmp_path):
    app = make_app(tmp_path)
    with TestClient(app) as client:
        for path in ('/network', '/api/network/overview', '/network/interfaces', '/api/network/interfaces', '/api/events'):
            response = client.get(path, follow_redirects=False)
            assert response.status_code == (401 if path.startswith('/api/') else 303)
            if not path.startswith('/api/'):
                assert response.headers['location'].startswith('/login?reason=session')


def test_network_navigation_and_inventory_follow_capabilities(tmp_path, monkeypatch):
    app = make_app(tmp_path)
    with TestClient(app) as client:
        with app.state.session_factory() as db:
            user = db.scalar(select(User).where(User.username == 'admin'))
            user_id = user.id
        # Sign a test session using the same middleware secret.
        import base64
        import json
        from itsdangerous import TimestampSigner
        session = base64.b64encode(json.dumps({'user_id': user_id}).encode())
        client.cookies.set(SESSION_COOKIE, session_cookie(app, session))
        app.state.daemon_client.ping = lambda: {
            'network_access': 'readonly', 'docker_access': 'disabled', 'events': True,
        }
        app.state.daemon_client.call = lambda method: {'interfaces': [], 'addresses': [], 'routes': []}
        dashboard = client.get('/')
        assert 'class="dashboard-cards"' in dashboard.text
        assert '<h2>General</h2>' in dashboard.text
        assert '<h2>Network monitoring</h2>' in dashboard.text
        assert '<h2>Docker monitoring</h2>' in dashboard.text
        assert 'data-inventory-search' not in dashboard.text
        page = client.get('/network/interfaces')
        assert page.status_code == 200
        assert '<p class="page-permission">Read only</p>' in page.text
        assert '/network/firewall' in page.text
        assert 'href="/network/interfaces" aria-current="page"' in page.text
        assert 'name="q"' in page.text
        import html
        filtered = client.get('/network/interfaces?interface.name=eth0&interface.ip4=192.168.0.1')
        assert 'value="interface.name=eth0 interface.ip4=192.168.0.1"' in html.unescape(filtered.text)
        assert 'autocomplete="off"' in filtered.text
        assert 'aria-controls="search-help"' in filtered.text
        assert '>Search</button>' not in filtered.text
        assert 'type="hidden" name="interface.name"' not in filtered.text
        for category, present, absent in [
            ('interfaces', ['interface.subnet', 'interface.ip'], ['route.family', 'sysctl.key', 'firewall.chain']),
            ('routing', ['interface.subnet', 'route.family'], ['sysctl.key', 'firewall.chain']),
            ('sysctl', ['sysctl.key', 'class', 'prefix'], ['route.family', 'firewall.chain']),
            ('firewall', ['firewall.chain', 'firewall.expression'], ['interface.subnet', 'route.family', 'sysctl.key']),
        ]:
            help_page = client.get('/network/' + category).text
            help_text = help_page.split('id="search-help"', 1)[1].split('</div>', 1)[0]
            assert all(field in help_text for field in present)
            assert not any(field in help_text for field in absent)
            assert 'Press Enter or leave' not in help_text
        assert '<h2>Docker</h2>' not in page.text
        assert client.get('/api/network/interfaces').status_code == 200
        overview = client.get('/network')
        assert overview.status_code == 200
        assert 'href="/network" aria-current="page"' in overview.text
        assert 'data-inventory-search' not in overview.text
        assert 'data-view="overview"' in overview.text
        app.state.daemon_client.call = lambda method: {'interfaces': [
            {'name': 'eth0', 'mac_address': 'aa:bb:cc:dd:ee:ff', 'operational_state': 'up', 'mtu': 1500}
        ], 'addresses': [{'interface': 'eth0', 'address': '8.8.8.8'}]}
        result = client.get('/api/network/overview?interface.name=not-a-filter').json()
        assert result['ui'] == {'interfaces': {}, 'address_accents': []}
        assert {'interfaces': result['interfaces']} == {'interfaces': [{'name': 'eth0', 'mac_address': 'aa:bb:cc:dd:ee:ff', 'operational_state': 'up',
                                          'scope': 'public', 'kind': None, 'master': None}]}
        app.state.daemon_client.ping = lambda: {
            'network_access': 'readonly', 'features': {'interfaces': 'disabled'},
        }
        assert client.get('/api/network/overview').status_code == 403
        assert 'data-view="overview"' not in client.get('/network').text
        app.state.daemon_client.ping = lambda: {
            'network_access': 'readonly', 'docker_access': 'disabled', 'events': True,
        }
        import asyncio

        class Writer:
            def write(self, data):
                assert b'events.subscribe' in data

            async def drain(self):
                pass

            def close(self):
                pass

            async def wait_closed(self):
                pass

        async def connect(*args, **kwargs):
            reader = asyncio.StreamReader()
            reader.feed_data(b'{"jsonrpc":"2.0","id":"subscription","result":{}}\n')
            reader.feed_data(b'{"jsonrpc":"2.0","method":"event","params":{"topic":"network.link.changed"}}\n')
            reader.feed_eof()
            return reader, Writer()

        monkeypatch.setattr(asyncio, 'open_unix_connection', connect)
        events = client.get('/api/events')
        assert events.status_code == 200
        assert 'event: ready' in events.text
        assert 'network.link.changed' in events.text
        app.state.daemon_client.ping = lambda: {'network_access': 'disabled'}
        assert client.get('/api/network/interfaces').status_code == 403


def make_app(tmp_path: Path):
    return create_app(
        Settings(
            _env_file=None,
            environment="test",
            database_url=f"sqlite:///{tmp_path / 'test.db'}",
            secret_key="test-secret-key-that-is-long-enough-123456",
            trusted_hosts="testserver",
        )
    )


def test_startup_creates_temporary_admin(tmp_path: Path):
    app = make_app(tmp_path)
    with TestClient(app) as client:
        with app.state.session_factory() as db:
            admin = db.scalar(select(User).where(User.username == "admin"))
            assert admin is not None
            assert admin.is_admin
            assert admin.password_is_temporary
        assert client.get("/login").status_code == 200


def csrf_from(response) -> str:
    match = re.search(r'name="csrf_token" value="([^"]+)"', response.text)
    assert match
    return match.group(1)


def test_admin_can_login_and_replace_initial_password(tmp_path: Path, caplog):
    app = make_app(tmp_path)
    with TestClient(app) as client:
        password_record = next(
            record for record in caplog.records if record.msg == "Initial admin password: %s"
        )
        initial_password = password_record.args[0]
        login_page = client.get("/login")
        response = client.post(
            "/login",
            data={
                "username": "admin",
                "password": initial_password,
                "csrf_token": csrf_from(login_page),
            },
            follow_redirects=False,
        )
        assert response.status_code == 303
        assert response.headers["location"] == "/settings/user"

        password_page = client.get("/password")
        response = client.post(
            "/password",
            data={
                "current_password": initial_password,
                "new_password": "a-secure-new-password",
                "confirm_password": "a-secure-new-password",
                "csrf_token": csrf_from(password_page),
            },
            follow_redirects=False,
        )
        assert response.status_code == 303
        assert response.headers["location"] == "/login?reason=password-changed"
        assert client.get('/api/session').status_code == 401

    caplog.clear()
    restarted_app = make_app(tmp_path)
    with TestClient(restarted_app):
        assert not any(
            record.msg == "Initial admin password: %s" for record in caplog.records
        )


def test_invalid_csrf_is_rejected(tmp_path: Path):
    app = make_app(tmp_path)
    with TestClient(app) as client:
        client.get("/login")
        response = client.post(
            "/login",
            data={"username": "admin", "password": "wrong", "csrf_token": "wrong"},
        )
    assert response.status_code == 403


def test_health_endpoint(tmp_path: Path):
    app = make_app(tmp_path)
    with TestClient(app) as client:
        response = client.get("/healthz")
    assert response.status_code == 200
    assert response.json() == {"status": "ok"}

