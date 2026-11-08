from common.identity import SESSION_COOKIE, SOURCE_KEY, MANIFEST_NAME, DEFINITIONS_DIRECTORY, MANAGED_HEADER
from session_helpers import session_cookie
import base64
import json
from unittest.mock import AsyncMock, Mock

from fastapi.testclient import TestClient
from itsdangerous import TimestampSigner
from sqlalchemy import select

from frontend.app import create_app
from frontend.config import Settings
from frontend.models import User


def application(tmp_path):
    return create_app(Settings(_env_file=None, environment='test', database_url=f'sqlite:///{tmp_path/"users.db"}',
        trusted_hosts='testserver', secret_key='test-secret-key-that-is-long-enough-123456'))


def login(client, app):
    with app.state.session_factory() as db:
        user = db.scalar(select(User).where(User.username == 'admin'))
        value = base64.b64encode(json.dumps({'user_id': user.id}).encode())
    client.cookies.set(SESSION_COOKIE, session_cookie(app, value), domain='testserver.local', path='/')


def test_log_pages_and_history_require_login_and_allow_docker_readonly(tmp_path):
    app = application(tmp_path)
    with TestClient(app) as client:
        params = {'scope': 'container', 'id': 'a'*64}
        assert client.get('/docker/logs', params=params, follow_redirects=False).status_code == 303
        assert client.get('/api/docker/logs', params=params).status_code == 401
        assert client.get('/api/docker/logs/live', params=params).status_code == 401
        login(client, app)
        app.state.daemon_client.ping = lambda: {'docker_access': 'readonly', 'network_access': 'disabled', 'logs': {'max_lines': 5000}}
        app.state.daemon_client.call = Mock(return_value={'lines': [], 'sources': [], 'errors': [], 'note': ''})
        page = client.get('/docker/logs', params=params)
        assert page.status_code == 200 and 'Fetch last lines' in page.text and 'value="200"' in page.text
        assert 'tool-sidebar' not in page.text and 'data-inventory-search' not in page.text
        result = client.get('/api/docker/logs', params=params)
        assert result.status_code == 200 and result.headers['cache-control'] == 'no-store'
        assert app.state.daemon_client.call.call_args.args[0] == 'docker.logs.tail'
        assert app.state.daemon_client.call.call_args.args[1]['limit'] == 200
        app.state.daemon_client.ping = lambda: {'docker_access': 'readonly', 'network_access': 'disabled', 'logs': {'max_lines': 5000, 'default_lines': 321}}
        assert 'value="321"' in client.get('/docker/logs', params=params).text
        assert 'value="27"' in client.get('/docker/logs', params={**params, 'limit': 27}).text
        assert client.get('/api/docker/logs', params={**params, 'limit': 0}).status_code == 422
        app.state.daemon_client.ping = lambda: {'docker_access': 'disabled'}
        assert client.get('/api/docker/logs', params=params).status_code == 403


def test_log_sse_forwards_records_and_closes_on_shutdown(tmp_path, monkeypatch):
    app = application(tmp_path)
    writer = Mock(); writer.drain = AsyncMock(); writer.wait_closed = AsyncMock()
    monkeypatch.setattr('frontend.docker.logs.routes.asyncio.open_unix_connection', AsyncMock(return_value=(Mock(), writer)))
    count = 0
    async def read(*args, **kwargs):
        nonlocal count
        count += 1
        if count == 1:
            outgoing = json.loads(writer.write.call_args.args[0])
            assert outgoing['method'] == 'docker.logs.subscribe'
            assert outgoing['params']['cursors']['a'*64]['timestamp'] == '2026-11-01T10:00:00Z'
            return json.dumps({'jsonrpc': '2.0', 'id': outgoing['id'], 'result': {'sources': []}}).encode()
        if count == 2:
            return json.dumps({'jsonrpc': '2.0', 'method': 'docker.log', 'params': {'type': 'line',
                'line': {'text': '<script>untrusted</script>', 'stream': 'stderr', 'timestamp': '2026-11-01T10:00:00Z'}}}).encode()
        return None
    monkeypatch.setattr('frontend.docker.logs.routes.read_stream_line', read)
    with TestClient(app) as client:
        login(client, app)
        app.state.daemon_client.ping = lambda: {'docker_access': 'readonly', 'network_access': 'disabled'}
        app.state.daemon_client.call = Mock(return_value={'lines': [], 'sources': [], 'errors': [],
            'cursors': {'a'*64: {'timestamp': '2026-11-01T10:00:00Z', 'count': 1, 'started_at': '2026-11-01T10:00:00Z'}}})
        snapshot = client.get('/api/docker/logs?scope=container&id='+'a'*64).json()
        assert 'cursors' not in snapshot
        url = '/api/docker/logs/live?scope=container&id='+'a'*64+'&checkpoint='+snapshot['checkpoint']
        response = client.get(url)
        assert response.status_code == 200
        assert 'event: ready' in response.text and 'event: log\n' in response.text and 'event: shutdown' in response.text
        assert response.headers['x-accel-buffering'] == 'no'
        writer.close.assert_called_once(); writer.wait_closed.assert_awaited_once()
        assert client.get(url).status_code == 409
