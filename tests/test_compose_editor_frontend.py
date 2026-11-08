from common.identity import SESSION_COOKIE, SOURCE_KEY, MANIFEST_NAME, DEFINITIONS_DIRECTORY, MANAGED_HEADER
from session_helpers import session_cookie
import base64
import html
import json
import re
import threading
from types import SimpleNamespace
from unittest.mock import Mock

import pytest
from fastapi.testclient import TestClient
from itsdangerous import TimestampSigner
from sqlalchemy import select

from frontend.app import create_app
from frontend.config import Settings
from frontend.models import User, ComposeDraft
from frontend.security import set_password
from backend.daemon import DaemonServer
from backend.daemon_config import DaemonSettings


def hidden(page, name):
    return html.unescape(re.search(rf'name="{name}" value="([^"]*)"', page.text).group(1))


@pytest.fixture
def editor(tmp_path, monkeypatch):
    class ImmediateThread:
        def __init__(self, target, args, **kwargs):
            self.target, self.args = target, args
        def start(self):
            self.target(*self.args)
    import backend.docker.compose.control as controls
    monkeypatch.setattr(controls, 'threading', SimpleNamespace(Thread=ImmediateThread, Lock=threading.Lock))
    path = tmp_path / 'compose.yaml'; path.write_text('services: {web: {image: web:1}}\n')
    server = DaemonServer(DaemonSettings(_env_file=None, docker_access='rw', compose_host_read_only=False,
                                       compose_root=tmp_path / 'managed', stack_database=tmp_path / 'stacks.sqlite3', compose_allowed_roots=[tmp_path]))
    server.docker = Mock()
    containers = [{'Id': 'a' * 64, 'Names': ['/demo-web'], 'State': 'running', 'Labels': {
        'com.docker.compose.project': 'demo', 'com.docker.compose.service': 'web',
        'com.docker.compose.project.config_files': str(path)}}]
    server.docker.request.side_effect = lambda url, **kwargs: containers if url == '/containers/json' else {'Volumes': []} if url == '/volumes' else {}
    server.docker.networks.return_value = []; server.docker.info.return_value = {'Swarm': {}}
    server.stack_control._run = Mock(side_effect=lambda project, args: json.dumps({'name': 'demo', 'services': {'web': {'image': 'web:1'}}}) if args[0] == 'config' else '')
    app = create_app(Settings(_env_file=None, environment='test', database_url=f'sqlite:///{tmp_path / "users.db"}',
                             trusted_hosts='testserver', secret_key='test-secret-key-that-is-long-enough-123456'))
    app.state.daemon_client.ping = lambda: server.dispatch('ping', {})
    app.state.daemon_client.call = lambda method, params=None: server.dispatch(method, params or {})
    with TestClient(app) as client:
        with app.state.session_factory() as db:
            user = db.scalar(select(User).where(User.username == 'admin'))
            set_password(user, 'new-test-password', 90); db.commit()
            payload = base64.b64encode(json.dumps({'user_id': user.id}).encode())
        client.cookies.set(SESSION_COOKIE, session_cookie(app, payload))
        yield client, app, server, path


def open_editor(client, path):
    page = client.get('/docker/stack-files/edit', params={'name': 'demo', 'path': str(path)})
    assert page.status_code == 200
    key = re.search('data-draft-key="([^"]+)"', page.text).group(1)
    return page, key, hidden(page, 'csrf_token')


def test_drafts_are_durable_private_and_do_not_write_sources(editor):
    client, app, server, path = editor
    page, key, csrf = open_editor(client, path)
    assert 'no-store' in page.headers['cache-control'] and 'data-inventory-search' not in page.text
    assert 'role="toolbar"' in page.text
    draft = {'content': 'services: [unfinished', 'base_hash': hidden(page, 'expected_hash'), 'stack_name': 'demo', 'revision': '0', 'csrf_token': csrf}
    assert client.post('/api/docker/stack-drafts/' + key, data={**draft, 'csrf_token': 'bad'}).status_code == 403
    result = client.post('/api/docker/stack-drafts/' + key, data=draft)
    assert result.json()['revision'] == 1
    assert path.read_text() == 'services: {web: {image: web:1}}\n'
    assert client.get('/api/docker/stack-drafts/' + key).json()['draft']['content'] == draft['content']
    with app.state.session_factory() as db:
        assert db.scalar(select(ComposeDraft)).content == draft['content']
        other = User(username='other', is_admin=True); set_password(other, 'other-password', 90); db.add(other); db.commit()
        payload = base64.b64encode(json.dumps({'user_id': other.id}).encode())
    # Drafts remain on disk and are scoped to the authenticated user, not just a key.
    client.cookies.clear(); client.cookies.set(SESSION_COOKIE, session_cookie(app, payload))
    assert client.get('/api/docker/stack-drafts/' + key).json()['draft'] is None


def test_file_save_retains_newer_drafts_and_rejects_stale_sources(editor):
    client, app, server, path = editor
    page, key, csrf = open_editor(client, path)
    original_hash = hidden(page, 'expected_hash')
    new_text = 'services: {web: {image: web:2}}\n'
    draft = {'content': new_text, 'base_hash': original_hash, 'stack_name': 'demo', 'revision': '0', 'csrf_token': csrf}
    assert client.post('/api/docker/stack-drafts/' + key, data=draft).json()['revision'] == 1
    params = {'name': 'demo', 'path': str(path), 'content': new_text, 'expected_hash': original_hash, 'draft_revision': '1', 'csrf_token': csrf}
    preview = client.post('/docker/stack-files/save', data=params)
    assert 'Confirm Save' in preview.text and 'web:2' not in path.read_text()
    assert client.post('/api/docker/stack-drafts/' + key, data={**draft, 'content': 'newer unsaved text', 'revision': '1'}).json()['revision'] == 2
    saved = client.post('/docker/stack-files/save', data={**params, 'confirmation': hidden(preview, 'confirmation')})
    assert saved.status_code == 200 and path.read_text() == new_text
    assert client.get('/api/docker/stack-drafts/' + key).json()['draft']['content'] == 'newer unsaved text'
    stale = client.post('/docker/stack-files/save', data=params)
    assert 'changed on disk' in stale.text and 'name="confirmation"' not in stale.text
    assert client.post('/api/docker/stack-drafts/' + key, data={**draft, 'revision': '1'}).status_code == 409
    assert client.delete('/api/docker/stack-drafts/' + key, params={}).status_code == 422


def test_readonly_and_non_admin_editor_access(editor):
    client, app, server, path = editor
    page, key, csrf = open_editor(client, path)
    server.settings.docker_access = 'readonly'
    readonly = client.get('/docker/stack-files/edit', params={'name': 'demo', 'path': str(path)})
    assert readonly.status_code == 200 and 'Read-only:' in readonly.text
    assert client.post('/api/docker/stack-drafts/' + key, data={'content': 'x', 'base_hash': 'x', 'csrf_token': csrf}).status_code == 200
    assert client.post('/docker/stack-files/save', data={'name': 'demo', 'path': str(path), 'content': 'x', 'expected_hash': 'x', 'csrf_token': csrf}).status_code == 403
    assert client.get('/docker/stack-actions/compose/demo/stop').status_code == 403
    with app.state.session_factory() as db:
        db.scalar(select(User).where(User.username == 'admin')).is_admin = False; db.commit()
    assert client.get('/docker/stack-files/edit', params={'name': 'demo', 'path': str(path)}).status_code == 403


def test_stack_controls_and_conversion_require_csrf_and_confirmation(editor):
    client, app, server, path = editor
    page, _, csrf = open_editor(client, path)
    preview = client.get('/docker/stack-actions/compose/demo/start')
    assert preview.status_code == 200 and 'Confirm Start' in preview.text
    assert all(call.args[1][0] == 'config' for call in server.stack_control._run.call_args_list)
    params = {'confirmation': hidden(preview, 'confirmation'), 'csrf_token': csrf}
    assert client.post('/docker/stack-actions/confirm', data={**params, 'csrf_token': 'bad'}).status_code == 403
    response = client.post('/docker/stack-actions/confirm', data=params, follow_redirects=False)
    assert response.status_code == 303 and '/docker/stack-jobs/' in response.headers['location']
    job_page = client.get(response.headers['location']); assert 'succeeded' in job_page.text
    assert client.post('/docker/stack-actions/confirm', data=params).status_code == 409
    conversion = client.get('/docker/stack-files/convert?name=demo')
    assert 'Confirm conversion' in conversion.text and not server.stack_store.get('demo')
    result = client.post('/docker/stack-files/convert', data={'confirmation': hidden(conversion, 'confirmation'), 'csrf_token': csrf})
    assert 'Structured definition saved' in result.text
    assert server.stack_store.get('demo')['definition'][SOURCE_KEY]['type'] == 'compose-files'
    assert path.read_text() == 'services: {web: {image: web:1}}\n'


def test_saved_drafts_remain_accessible_during_daemon_outage(editor):
    from common.client import DaemonUnavailable
    client, app, server, path = editor
    page, key, csrf = open_editor(client, path)
    params = {'content': 'unfinished draft', 'base_hash': hidden(page, 'expected_hash'), 'stack_name': 'demo', 'csrf_token': csrf}
    assert client.post('/api/docker/stack-drafts/' + key, data=params).status_code == 200
    def unavailable(*args, **kwargs):
        raise DaemonUnavailable('Backend temporarily unavailable')
    app.state.daemon_client.ping = unavailable; app.state.daemon_client.call = unavailable
    assert client.get('/api/docker/stack-drafts/' + key).json()['draft']['content'] == 'unfinished draft'
    assert client.post('/api/docker/stack-drafts/' + key, data={**params, 'content': 'draft kept through outage', 'revision': '1'}).status_code == 200
    page = client.get('/docker/stack-files/edit', params={'name': 'demo', 'path': str(path)})
    assert page.status_code == 200 and 'saved drafts remain accessible' in page.text.lower()
