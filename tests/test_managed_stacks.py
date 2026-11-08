from common.identity import SESSION_COOKIE, SOURCE_KEY, MANIFEST_NAME, DEFINITIONS_DIRECTORY, MANAGED_HEADER
import base64
import html
import io
import json
import re
import stat
import tarfile
import zipfile
from unittest.mock import Mock

import pytest
from fastapi.testclient import TestClient
from session_helpers import session_cookie
from sqlalchemy import select

from frontend.app import create_app
from frontend.config import Settings
from frontend.models import User
from frontend.security import set_password
from common.stack_files import upload_bundle
from backend.daemon import DaemonServer, build_parser
from backend.daemon_config import DaemonSettings
from backend.docker.compose.files import read_source
from common.protocol import ProtocolError


def hidden(page, name):
    return html.unescape(re.search(rf'name="{name}" value="([^"]+)"', page.text).group(1))


@pytest.fixture
def creation(tmp_path):
    server = DaemonServer(DaemonSettings(_env_file=None, docker_access='rw', network_access='disabled',
        compose_root=tmp_path/'managed', stack_database=tmp_path/'stacks.sqlite3'))
    server.docker = Mock()
    server.docker.request.side_effect = lambda path, **kwargs: {'Volumes': []} if path == '/volumes' else []
    server.docker.networks.return_value = []
    server.docker.info.return_value = {'Swarm': {}}
    app = create_app(Settings(_env_file=None, environment='test', database_url=f'sqlite:///{tmp_path/"users.db"}',
        secret_key='test-secret-key-that-is-long-enough-123456', trusted_hosts='testserver'))
    app.state.daemon_client.ping = lambda: server.dispatch('ping', {})
    app.state.daemon_client.call = server.dispatch
    with TestClient(app) as client:
        with app.state.session_factory() as db:
            user = db.scalar(select(User).where(User.username == 'admin'))
            set_password(user, 'new-test-password', 90); db.commit()
            cookie = session_cookie(app, base64.b64encode(json.dumps({'user_id': user.id}).encode()))
        client.cookies.set(SESSION_COOKIE, cookie, domain='testserver.local', path='/')
        yield client, server


def test_name_options_preview_confirmation_and_offline_discovery(creation):
    client, server = creation
    page = client.get('/docker/stack-create')
    assert 'data-inventory-search' not in page.text
    csrf = hidden(page, 'csrf_token')
    assert client.post('/docker/stack-create/choose', data={'name': 'demo', 'csrf_token': 'wrong'}).status_code == 403
    assert client.post('/docker/stack-create/choose', data={'name': 'demo', 'csrf_token': csrf}).status_code == 200
    options = client.get('/docker/stack-create/options?name=demo')
    assert 'Stack Builder' in options.text and 'Stack Templates' in options.text
    assert 'data-compose-editor' in options.text
    preview = client.post('/docker/stack-create/review', data={'name': 'demo', 'mode': 'paste', 'csrf_token': csrf,
        'definition_text': 'services: {}\n', 'main_file': 'compose.yaml'})
    assert preview.status_code == 200 and 'Create stack' in preview.text
    assert not server.settings.compose_root.exists()
    token = hidden(preview, 'confirmation')
    assert client.post('/docker/stack-create/confirm', data={'confirmation': token, 'csrf_token': 'wrong'}).status_code == 403
    saved = client.post('/docker/stack-create/confirm', data={'confirmation': token, 'csrf_token': csrf}, follow_redirects=False)
    assert saved.status_code == 303
    source = server.settings.compose_root/'demo'/'compose.yaml'
    assert source.read_text() == 'services: {}\n' and source.stat().st_mode & 0o777 == 0o600
    stack = server.dispatch('docker.stacks', {})['stacks'][0]
    assert stack['name'] == 'demo' and stack['state'] == 'empty' and stack['definition_origin'] == 'file'
    assert read_source(stack, str(source), server.settings)['writable']
    assert 'already' in client.get('/docker/stack-create/options?name=demo').text
    assert client.post('/docker/stack-create/confirm', data={'confirmation': token, 'csrf_token': csrf}).status_code == 409
    assert not any(call.kwargs.get('method') in {'POST', 'DELETE'} for call in server.docker.request.call_args_list)


def archive(entries):
    output = io.BytesIO()
    with zipfile.ZipFile(output, 'w') as file:
        for name, data in entries:
            file.writestr(name, data)
    return output.getvalue()


def test_upload_preserves_multiple_files_and_rechecks_name_before_commit(creation):
    client, server = creation
    csrf = hidden(client.get('/docker/stack-create'), 'csrf_token')
    data = archive([('compose.yaml', 'services: {}\n'), ('assets/cert.bin', b'\0\xff'), ('.env', 'PASSWORD=private')])
    preview = client.post('/docker/stack-create/review', data={'name': 'upload', 'mode': 'upload', 'csrf_token': csrf},
                          files={'upload': ('stack.zip', data, 'application/zip')})
    assert 'assets/cert.bin' in preview.text and 'PASSWORD=private' not in preview.text
    token = hidden(preview, 'confirmation')
    server.settings.compose_root.mkdir()
    occupied = server.settings.compose_root/'upload'; occupied.mkdir(); (occupied/'keep').write_text('original')
    failure = client.post('/docker/stack-create/confirm', data={'confirmation': token, 'csrf_token': csrf})
    assert 'already' in failure.text and (occupied/'keep').read_text() == 'original'
    preview = client.post('/docker/stack-create/review', data={'name': 'other', 'mode': 'upload', 'csrf_token': csrf},
                          files={'upload': ('stack.zip', data, 'application/zip')})
    result = client.post('/docker/stack-create/confirm', data={'confirmation': hidden(preview, 'confirmation'), 'csrf_token': csrf}, follow_redirects=False)
    assert result.status_code == 303
    assert (server.settings.compose_root/'other/assets/cert.bin').read_bytes() == b'\0\xff'
    assert (server.settings.compose_root/'other/.env').stat().st_mode & 0o777 == 0o600


@pytest.mark.parametrize('name', ['../escape', '/absolute', 'a/../../escape', 'a\\b', MANIFEST_NAME])
def test_archive_rejects_traversal_and_reserved_paths(name):
    with pytest.raises(ValueError):
        upload_bundle('stack.zip', archive([('compose.yaml', 'services: {}'), (name, 'bad')]))


def test_archive_rejects_symlinks_and_expansion_bombs():
    output = io.BytesIO()
    with zipfile.ZipFile(output, 'w') as file:
        link = zipfile.ZipInfo('secret'); link.external_attr = (stat.S_IFLNK | 0o777) << 16
        file.writestr(link, '/etc/shadow')
    with pytest.raises(ValueError, match='links'):
        upload_bundle('stack.zip', output.getvalue())


def test_tar_supports_common_dot_prefixes_but_rejects_hardlinks():
    data = io.BytesIO()
    with tarfile.open(fileobj=data, mode='w:gz') as archive:
        content = b'services: {}\n'
        entry = tarfile.TarInfo('./project/compose.yaml'); entry.size = len(content)
        archive.addfile(entry, io.BytesIO(content))
    bundle = upload_bundle('stack.tar.gz', data.getvalue())
    assert bundle['main_file'] == 'project/compose.yaml'
    data = io.BytesIO()
    with tarfile.open(fileobj=data, mode='w') as archive:
        entry = tarfile.TarInfo('linked'); entry.type = tarfile.LNKTYPE; entry.linkname = '/etc/shadow'
        archive.addfile(entry)
    with pytest.raises(ValueError, match='links'):
        upload_bundle('stack.tar', data.getvalue())
    output = io.BytesIO()
    with zipfile.ZipFile(output, 'w', compression=zipfile.ZIP_DEFLATED) as file:
        file.writestr('compose.yaml', b'x' * (4 * 1024 * 1024 + 1))
    with pytest.raises(ValueError):
        upload_bundle('stack.zip', output.getvalue())


def test_readonly_and_authentication_cannot_create_stacks(creation):
    client, server = creation
    csrf = hidden(client.get('/docker/stack-create'), 'csrf_token')
    server.settings.docker_access = 'readonly'
    assert client.get('/docker/stack-create').status_code == 403
    with pytest.raises(ProtocolError, match='read-only'):
        server.dispatch('docker.stack.create', {'name': 'demo', 'files': {}, 'main_file': 'compose.yaml'})
    client.cookies.clear()
    assert client.post('/docker/stack-create/review', data={'csrf_token': csrf}, follow_redirects=False).status_code == 303
    assert not server.settings.compose_root.exists()


def test_managed_root_and_external_readonly_env_cli(tmp_path, monkeypatch):
    monkeypatch.setenv('BACKEND_COMPOSE_ROOT', str(tmp_path/'env'))
    monkeypatch.setenv('BACKEND_COMPOSE_HOST_READ_ONLY', 'true')
    args = build_parser().parse_args(['--compose-root', str(tmp_path/'cli'), '--no-compose-host-read-only'])
    configured = DaemonSettings(_env_file=None, compose_root=args.compose_root, compose_host_read_only=args.compose_host_read_only)
    assert configured.compose_root == tmp_path/'cli' and not configured.compose_host_read_only
    assert DaemonSettings(_env_file=None).compose_host_read_only
    file = tmp_path/'external.yaml'; file.write_text('services: {}')
    settings = DaemonSettings(_env_file=None, docker_access='rw', compose_allowed_roots=[tmp_path])
    source = read_source({'name': 'external', 'kind': 'compose', 'config_files': [str(file)]}, str(file), settings)
    assert not source['writable'] and 'COMPOSE_HOST_READ_ONLY' in source['write_reason']
