from common.identity import SESSION_COOKIE, SOURCE_KEY, MANIFEST_NAME, DEFINITIONS_DIRECTORY, MANAGED_HEADER
from session_helpers import session_cookie
import base64
import html
import json
import re
from unittest.mock import Mock
import pytest

from fastapi.testclient import TestClient
from itsdangerous import TimestampSigner
from sqlalchemy import select

from frontend.app import create_app
from frontend.config import Settings
from frontend.models import User
from frontend.security import set_password
from backend.daemon import DaemonServer
from backend.daemon_config import DaemonSettings
from common.protocol import ProtocolError


def hidden(page, name):
    return html.unescape(re.search(rf'name="{name}" value="([^"]+)"', page.text).group(1))


def test_virtual_stack_forms_preview_save_edit_and_concurrent_change(tmp_path):
    app = create_app(Settings(_env_file=None, environment='test', database_url=f'sqlite:///{tmp_path / "users.db"}',
                             secret_key='test-secret-key-that-is-long-enough-123456', trusted_hosts='testserver'))
    server = DaemonServer(DaemonSettings(_env_file=None, docker_access='rw', compose_root=tmp_path / 'managed', stack_database=tmp_path / 'stacks.sqlite3'))
    server.docker = Mock()
    server.docker.request.side_effect = lambda path, **kwargs: {'Volumes': []} if path == '/volumes' else []
    server.docker.networks.return_value = []
    server.docker.info.return_value = {'Swarm': {}}
    app.state.daemon_client.ping = lambda: server.dispatch('ping', {})
    app.state.daemon_client.call = lambda method, params=None: server.dispatch(method, params or {})
    with TestClient(app) as client:
        assert client.get('/docker/stack-definitions/new', follow_redirects=False).status_code == 303
        with app.state.session_factory() as db:
            user = db.scalar(select(User).where(User.username == 'admin'))
            set_password(user, 'new-test-password', 90); db.commit()
            cookie = base64.b64encode(json.dumps({'user_id': user.id}).encode())
        client.cookies.set(SESSION_COOKIE, session_cookie(app, cookie))
        page = client.get('/docker/stack-definitions/new')
        assert page.status_code == 200 and 'data-inventory-search' not in page.text
        csrf = hidden(page, 'csrf_token')
        text = json.dumps({'services': {'web': {'image': 'web:1'}}, 'x-stackmeta': {'description': 'Virtual test'}})
        params = {'name': 'demo', 'definition_text': text, 'csrf_token': csrf}
        assert client.post('/docker/stack-definitions/save', data={**params, 'csrf_token': 'bad'}).status_code == 403
        preview = client.post('/docker/stack-definitions/save', data=params)
        assert 'Confirm Save' in preview.text and not server.settings.stack_database.exists()
        token = hidden(preview, 'confirmation')
        edited = client.post('/docker/stack-definitions/save', data={**params, 'confirmation': token, 'edit': 'true'})
        assert 'Review definition' in edited.text and 'Virtual test' in edited.text
        preview = client.post('/docker/stack-definitions/save', data=params)
        token = hidden(preview, 'confirmation')
        saved = client.post('/docker/stack-definitions/save', data={**params, 'confirmation': token}, follow_redirects=False)
        assert saved.status_code == 303
        assert server.stack_store.get('demo')['revision'] == 1
        assert client.post('/docker/stack-definitions/save', data={**params, 'confirmation': token}).status_code == 409
        stack = client.get('/api/docker/stacks?stack.base_kind=compose&stack.name=demo').json()['stacks'][0]
        assert stack['kind'] == 'compose-meta' and stack['definition_origin'] == 'virtual'
        edit_page = client.get('/docker/stack-definitions/edit/demo')
        assert 'Virtual test' in edit_page.text and hidden(edit_page, 'revision') == '1'
        params['revision'] = '1'
        preview = client.post('/docker/stack-definitions/save', data=params)
        server.stack_store.save('demo', {'services': {}, 'x-stackmeta': {'description': 'Concurrent change'}}, 1)
        result = client.post('/docker/stack-definitions/save', data={**params, 'confirmation': hidden(preview, 'confirmation')})
        assert 'changed' in result.text and server.stack_store.get('demo')['revision'] == 2
        server.stack_store.save('demo', {'services': {}, 'x-stackmeta': {'labels': ['owner=ops']}}, 2)
        filtered = client.get('/api/docker/stacks?label.key=owner&label.value=ops').json()
        assert [item['name'] for item in filtered['stacks']] == ['demo']
        assert not client.get('/api/docker/stacks?label.key=owner&label.value=other').json()['stacks']
        label = next(item for item in client.get('/api/docker/labels').json()['labels'] if item['key'] == 'owner')
        assert label['stacks'] == 1 and label['containers'] == 0
        model = client.get('/docker/stack-models/demo')
        assert model.status_code == 200 and 'Desired Compose definition' not in model.text
        assert 'Definition and diff' not in model.text
        assert client.get('/docker/stack-models/demo', follow_redirects=False).headers['cache-control'] == 'no-store'
        assert 'data-inventory-search' not in model.text
        server.settings.docker_access = 'readonly'
        assert client.get('/docker/stack-definitions/new').status_code == 403
        assert client.get('/docker/stack-models/demo').status_code == 200
        with app.state.session_factory() as db:
            user = db.scalar(select(User).where(User.username == 'admin'))
            user.is_admin = False
            db.commit()
        assert client.get('/docker/stack-models/demo').status_code == 200
        assert client.get('/docker/stack-models/demo/edit/active').status_code == 403


def test_readonly_file_can_have_previewed_layers_without_source_writes(tmp_path):
    host = tmp_path / 'host'
    directory = host / 'root/docker/app'; directory.mkdir(parents=True)
    source = directory / 'docker-compose.yml'; source.write_text('services: {web: {image: "web:1"}}\n')
    source_text = source.read_text()
    app = create_app(Settings(_env_file=None, environment='test', database_url=f'sqlite:///{tmp_path / "users.db"}',
                             secret_key='test-secret-key-that-is-long-enough-123456', trusted_hosts='testserver'))
    server = DaemonServer(DaemonSettings(_env_file=None, docker_access='rw', system_host_root=host,
                          compose_allowed_roots=['/root/docker'], compose_root=tmp_path / 'managed', stack_database=tmp_path / 'stacks.sqlite3'))
    server.docker = Mock()
    container = {'Id': 'a' * 64, 'State': 'running', 'Names': ['/app-web-1'], 'Labels': {
        'com.docker.compose.project': 'app', 'com.docker.compose.service': 'web',
        'com.docker.compose.project.config_files': '/root/docker/app/docker-compose.yml',
        'com.docker.compose.project.working_dir': '/root/docker/app'}}
    server.docker.request.side_effect = lambda path, **kwargs: [container] if path == '/containers/json' else {'Volumes': []} if path == '/volumes' else {}
    server.docker.networks.return_value = []
    server.docker.info.return_value = {'Swarm': {}}
    app.state.daemon_client.ping = lambda: server.dispatch('ping', {})
    app.state.daemon_client.call = lambda method, params=None: server.dispatch(method, params or {})
    with TestClient(app) as client:
        assert client.get('/docker/stack-models/app/edit/active', follow_redirects=False).status_code == 303
        with app.state.session_factory() as db:
            user = db.scalar(select(User).where(User.username == 'admin'))
            set_password(user, 'new-test-password', 90); db.commit()
            cookie = base64.b64encode(json.dumps({'user_id': user.id}).encode())
        client.cookies.set(SESSION_COOKIE, session_cookie(app, cookie))
        file_page = client.get('/docker/stack-files/edit?name=app&path=/root/docker/app/docker-compose.yml')
        assert 'External Compose sources are read-only' in file_page.text
        inventory = client.get('/api/docker/stacks?stack.name=app').json()
        model = inventory['stacks'][0]['compose_model']
        assert model['tree'] is None
        csrf = hidden(file_page, 'csrf_token')
        assert client.post('/docker/stack-models/app/change', data={'action': 'initialize', 'expected_hash': model['definition_hash'], 'csrf_token': 'invalid'}).status_code == 403
        assert server.compose_models.tree('app') is None
        init = client.post('/docker/stack-models/app/change', data={'action': 'initialize', 'expected_hash': model['definition_hash'], 'csrf_token': csrf}, follow_redirects=False)
        assert init.status_code == 303 and init.headers['location'] == '/docker/stacks/compose/app'
        assert len(server.compose_models.tree('app')['nodes']) == 1
        assert server.compose_models.tree('app')['nodes'][0]['label'] == '/root/docker/app/docker-compose.yml'
        editor = client.get('/docker/stack-models/app/edit/active?mode=derive')
        assert editor.status_code == 200 and editor.headers['cache-control'] == 'no-store'
        assert 'data-inventory-search' not in editor.text
        csrf = hidden(editor, 'csrf_token')
        root = hidden(editor, 'node')
        params = {'action': 'add', 'node': root, 'revision': hidden(editor, 'revision'), 'label': 'Security update',
                  'definition_text': json.dumps({'services': {'web': {'image': 'web:2'}}}), 'csrf_token': csrf}
        assert client.post('/docker/stack-models/app/change', data={**params, 'csrf_token': 'bad'}).status_code == 403
        preview = client.post('/docker/stack-models/app/change', data=params)
        assert 'web:2' in preview.text and 'Derive definition' in preview.text
        assert server.compose_models.tree('app')['active'] == root
        # Saving a newer draft while the confirmation is open must preserve it.
        from frontend.docker.stacks.routes import draft_key
        draft = draft_key('definition-derive', 'app', root)
        saved_draft = client.post('/api/docker/stack-drafts/' + draft, data={'content': 'newer draft', 'stack_name': 'app',
                                  'base_hash': params['revision'], 'revision': 0, 'csrf_token': csrf})
        assert saved_draft.status_code == 200
        saved = client.post('/docker/stack-models/app/change', data={'confirmation': hidden(preview, 'confirmation'), 'csrf_token': csrf}, follow_redirects=False)
        assert saved.status_code == 303
        assert client.get('/api/docker/stack-drafts/' + draft).json()['draft']['content'] == 'newer draft'
        assert source.read_text() == source_text
        tree = server.compose_models.tree('app')
        assert tree['active'] == tree['running'] == root
        child = tree['nodes'][-1]['id']
        server.compose_models.change_tree('app', {'action': 'activate', 'node': child, 'expected_revision': tree['revision'], 'apply': True})
        tree = server.compose_models.tree('app')
        assert server.compose_models.details('app')['pending_changes']
        assert 'web:2' in server.compose_models.details('app')['definition']['services']['web']['image']
        view = client.get('/docker/stack-models/app')
        assert 'Definition and diff' not in view.text
        assert client.get('/api/docker/stacks').json()['stacks'][0]['compose_model']['tree']['nodes'][-1]['label'] == 'Security update'
        # A changed hierarchy invalidates already-reviewed selections.
        select_params = {'action': 'activate', 'node': root, 'revision': tree['revision'], 'csrf_token': csrf}
        select_preview = client.post('/docker/stack-models/app/change', data=select_params)
        server.compose_models.change_tree('app', {'action': 'activate', 'node': root, 'expected_revision': tree['revision'], 'apply': True})
        stale = client.post('/docker/stack-models/app/change', data={'confirmation': hidden(select_preview, 'confirmation'), 'csrf_token': csrf})
        assert 'hierarchy changed' in stale.text
        tree = server.compose_models.tree('app')
        duplicate_form = client.get('/docker/stack-models/app/duplicate/' + root)
        assert duplicate_form.status_code == 200 and duplicate_form.headers['cache-control'] == 'no-store'
        assert hidden(duplicate_form, 'label') == '/root/docker/app/docker-compose.yml (copy)'
        duplicate = client.post('/docker/stack-models/app/change', data={'action': 'duplicate', 'node': root,
            'revision': hidden(duplicate_form, 'revision'), 'label': 'Custom copy', 'csrf_token': csrf})
        assert 'Duplicate' in duplicate.text
        assert client.post('/docker/stack-models/app/change', data={'confirmation': hidden(duplicate, 'confirmation'), 'csrf_token': csrf}, follow_redirects=False).status_code == 303
        copy = server.compose_models.tree('app')['nodes'][-1]
        assert copy['label'] == 'Custom copy'
        assert copy['parent'] is None and copy['id'] != root
        assert server.compose_models.tree('app')['active'] == root
        copy_editor = client.get('/docker/stack-models/app/edit/' + copy['id'])
        assert 'Edit definition' in copy_editor.text and 'Review changes' in copy_editor.text
        edit = client.post('/docker/stack-models/app/change', data={'action': 'edit', 'node': copy['id'],
            'revision': hidden(copy_editor, 'revision'), 'label': 'Edited copy',
            'definition_text': json.dumps({'services': {'web': {'image': 'copy:2'}}}), 'csrf_token': csrf})
        assert 'copy:2' in edit.text
        assert client.post('/docker/stack-models/app/change', data={'confirmation': hidden(edit, 'confirmation'), 'csrf_token': csrf}, follow_redirects=False).status_code == 303
        options = client.get('/docker/stack-models/app/new')
        assert options.status_code == 200 and 'New Definition' in options.text
        assert 'Upload a Compose file or archive' in options.text and 'Paste text or start an empty definition' in options.text
        assert 'Choose another name' not in options.text
        imported = client.post('/docker/stack-models/app/import', data={'revision': hidden(options, 'revision'),
            'mode': 'paste', 'label': 'Replacement', 'definition_text': 'services: {web: {image: "replacement:1"}}', 'csrf_token': csrf})
        assert 'compose.yaml' in imported.text and 'replacement:1' in imported.text
        assert 'web:1' not in imported.text and '--- current' not in imported.text and '+++ proposed' not in imported.text
        assert client.post('/docker/stack-models/app/change', data={'confirmation': hidden(imported, 'confirmation'), 'csrf_token': csrf}, follow_redirects=False).status_code == 303
        added = server.compose_models.tree('app')['nodes'][-1]
        assert added['parent'] is None and added['label'] == 'Replacement'
        assert server.compose_models.tree('app')['active'] == root
        assert source.read_text() == source_text
        deletion = client.post('/docker/stack-models/app/change', data={'action': 'delete', 'node': copy['id'],
            'revision': server.compose_models.tree('app')['revision'], 'csrf_token': csrf})
        assert 'Edited copy' in deletion.text
        assert client.post('/docker/stack-models/app/change', data={'confirmation': hidden(deletion, 'confirmation'), 'csrf_token': csrf}, follow_redirects=False).status_code == 303
        assert not any(item['id'] == copy['id'] for item in server.compose_models.tree('app')['nodes'])
        server.settings.docker_access = 'readonly'
        assert client.get('/docker/stack-models/app/duplicate/' + root).status_code == 403
        assert client.get('/docker/stack-models/app/new').status_code == 403
        assert client.get('/docker/stack-models/app/edit/active?mode=derive').status_code == 403
        readonly = client.get('/docker/stack-models/app/edit/' + child)
        assert readonly.status_code == 200 and 'definition is read-only' in readonly.text
        with pytest.raises(ProtocolError, match='read-only'):
            server.dispatch('docker.stack.model.change', {'name': 'app'})
