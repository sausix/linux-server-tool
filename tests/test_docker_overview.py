from common.identity import SESSION_COOKIE, SOURCE_KEY, MANIFEST_NAME, DEFINITIONS_DIRECTORY, MANAGED_HEADER
from session_helpers import session_cookie
import base64
import json
from unittest.mock import Mock

import pytest
from fastapi.testclient import TestClient
from itsdangerous import TimestampSigner
from sqlalchemy import select

from backend.daemon import DaemonServer
from backend.daemon_config import DaemonSettings
from backend.docker.client import DockerError
from frontend.app import create_app
from frontend.config import Settings
from frontend.models import User
from common.protocol import ProtocolError


def test_docker_overview_partial_failure_and_field_selection():
    server = DaemonServer(DaemonSettings(_env_file=None, docker_access='ro'))
    server.docker = Mock()
    server.docker.ping.return_value = {'reachable': True}
    server.docker.version.return_value = {'Version': 'test-engine', 'ApiVersion': '1.47', 'UnexpectedSecret': 'hidden'}
    server.docker.info.side_effect = DockerError('Access denied')
    result = server.dispatch('docker.overview', {})
    assert result['reachable'] is True
    assert result['version'] == {'Version': 'test-engine', 'ApiVersion': '1.47'}
    assert result['info'] == {}
    assert result['errors'] == ['info: Access denied']
    server.docker.ping.side_effect = DockerError('Socket missing')
    result = server.dispatch('docker.overview', {})
    assert not result['reachable']
    assert result['errors'] == ['Connection: Socket missing']


def test_disabled_docker_blocks_overview():
    server = DaemonServer(DaemonSettings(_env_file=None, docker_access='disabled'))
    with pytest.raises(ProtocolError, match='disabled'):
        server.dispatch('docker.overview', {})


def test_frontend_docker_status_and_navigation(tmp_path):
    settings = Settings(_env_file=None, database_url=f'sqlite:///{tmp_path / "app.db"}',
                        secret_key='test-secret-key-that-is-long-enough-123456', trusted_hosts='testserver')
    app = create_app(settings)
    with TestClient(app) as client:
        assert client.get('/docker', follow_redirects=False).status_code == 303
        with app.state.session_factory() as db:
            user = db.scalar(select(User).where(User.username == 'admin'))
            session = base64.b64encode(json.dumps({'user_id': user.id}).encode())
        client.cookies.set(SESSION_COOKIE, session_cookie(app, session))
        app.state.daemon_client.ping = lambda: {'docker_access': 'readonly', 'network_access': 'disabled'}
        app.state.daemon_client.call = lambda method, params=None: {
            'reachable': True, 'endpoint': 'unix:///var/run/docker.sock',
            'version': {'Version': 'test-engine'}, 'info': {'IPv4Forwarding': False, 'Containers': 0}, 'errors': []}
        page = client.get('/docker')
        assert page.status_code == 200
        assert 'test-engine' in page.text
        assert 'Connected' in page.text
        assert 'unix:///var/run/docker.sock' in page.text
        assert 'data-inventory-search' not in page.text
        assert 'href="/docker" aria-current="page"' in page.text
        assert 'Docker monitoring</a>' in page.text
        assert '<p class="page-permission">Read only</p>' in page.text
        assert '<small>readonly</small>' not in page.text
        icons = client.get('/static/icons.svg')
        assert icons.status_code == 200
        assert 'id="containers"' in icons.text
        assert 'stroke="var(--icon-color, currentColor)"' in icons.text
        import re
        assert re.search(r'icons\.svg\?v=[a-f0-9]+#containers', page.text)
        assert re.search(r'<h1><svg[^>]*>.*?#docker', page.text)
        app.state.daemon_client.call = lambda method, params=None: {'stacks': [{
            'name': 'demo', 'kind': 'compose', 'state': 'running', 'services': 1,
            'containers': 1, 'running': 1, 'stopped': 0, 'unhealthy': 0, 'networks': 1, 'volumes': 0,
        }], 'errors': [], 'note': 'Observed resources'}
        stack_page = client.get('/docker/stacks')
        assert stack_page.status_code == 200
        assert 'action="/docker/stacks"' in stack_page.text
        assert 'data-stack-menu' in stack_page.text
        assert 'stack.state' in stack_page.text
        assert client.get('/api/docker/stacks?stack.state=stopped').json()['stacks'] == []
        assert client.get('/api/docker/stacks?stack.name=demo').json()['stacks'][0]['name'] == 'demo'
        details = client.get('/docker/stacks/compose/demo')
        assert details.status_code == 200
        assert 'data-inventory-search' not in details.text
        assert 'Engine configuration' not in page.text
        container = {'id': 'a' * 64, 'name': 'web', 'stack': 'demo', 'stack_kind': 'compose', 'state': 'running', 'labels': {'owner': 'ops'}}
        app.state.daemon_client.call = lambda method, params=None: {'containers': [container]} if method == 'docker.containers' else container
        container_page = client.get('/docker/containers')
        assert container_page.status_code == 200
        assert 'container.stack' in container_page.text
        assert 'action="/docker/containers"' in container_page.text
        assert client.get('/api/docker/containers?container.stack=demo').json()['containers'][0]['name'] == 'web'
        assert client.get('/api/docker/containers?container.state=exited').json()['containers'] == []
        assert len(client.get('/api/docker/containers?label.key=owner&label.value=ops').json()['containers']) == 1
        assert client.get('/api/docker/containers?label.key=owner&label.value=other').json()['containers'] == []
        assert client.get('/api/docker/containers?stack.name=demo').status_code == 422
        detail = client.get('/docker/containers/' + 'a' * 64)
        assert detail.status_code == 200
        assert 'data-inventory-search' not in detail.text
        assert client.get('/api/docker/containers/' + 'a' * 64).json()['name'] == 'web'
        assert client.get('/api/docker/containers/invalid').status_code == 404
        image = {'id': 'sha256:' + 'a' * 64, 'name': 'web:latest', 'tags': ['web:latest'], 'usage': 'active', 'size': 100, 'labels': {'owner': 'ops'}}
        app.state.daemon_client.call = lambda method, params=None: {'images': [image], 'errors': []} if method == 'docker.images' else image
        images_page = client.get('/docker/images')
        assert images_page.status_code == 200
        assert 'image.usage' in images_page.text
        assert 'action="/docker/images"' in images_page.text
        assert client.get('/api/docker/images?image.usage=unused').json()['images'] == []
        assert client.get('/api/docker/images?image.tags=web:latest').json()['images'][0]['name'] == 'web:latest'
        assert len(client.get('/api/docker/images?label.key=owner&label.value=ops').json()['images']) == 1
        assert client.get('/api/docker/images?container.state=running').status_code == 422
        detail = client.get('/docker/images/detail', params={'image': image['id']})
        assert detail.status_code == 200
        assert 'data-inventory-search' not in detail.text
        assert client.get('/api/docker/images/detail', params={'image': image['id']}).json()['id'] == image['id']
        app.state.daemon_client.call = lambda method, params=None: {'labels': [{'key': 'owner', 'value': 'ops', 'images': 2}], 'errors': [], 'note': 'Counts'}
        label_page = client.get('/docker/labels')
        assert label_page.status_code == 200
        assert 'action="/docker/labels"' in label_page.text
        assert 'label.images' in label_page.text
        assert client.get('/api/docker/labels?label.key=owner').json()['labels'][0]['images'] == 2
        assert client.get('/api/docker/labels?label.value=other').json()['labels'] == []
        assert client.get('/api/docker/labels?image.name=web').status_code == 422
        app.state.daemon_client.call = lambda method, params=None: {'resources': [{'id': 'network', 'name': 'demo', 'labels': {'owner': 'ops'}}]}
        resources_page = client.get('/docker/resources/networks?label.key=owner&label.value=ops')
        assert resources_page.status_code == 200
        assert 'action="/docker/resources/networks"' in resources_page.text
        assert len(client.get('/api/docker/resources/networks?label.key=owner&label.value=ops').json()['resources']) == 1
        assert client.get('/api/docker/resources/networks?label.key=owner&label.value=other').json()['resources'] == []
        assert client.get('/api/docker/resources/invalid').status_code == 404
        app.state.daemon_client.call = lambda method, params=None: (
            {'local': True, 'systemd': {}, 'processes': [], 'warnings': [],
             'daemon_json': {'path': '/etc/docker/daemon.json', 'exists': False, 'error': None, 'settings': {}},
             'additional_configs': []}
            if method == 'docker.configuration' else {
                'reachable': True, 'endpoint': 'unix:///var/run/docker.sock', 'version': {},
                'info': {'Driver': 'overlay2'}, 'errors': []})
        page = client.get('/docker/configuration')
        assert page.status_code == 200
        assert 'File does not exist.' in page.text
        assert 'overlay2' in page.text
        assert 'href="/docker/configuration" aria-current="page"' in page.text
        app.state.daemon_client.call = lambda method, params=None: {'reachable': False, 'endpoint': 'unix:///missing.sock', 'version': {}, 'info': {}, 'errors': ['Socket missing']}
        page = client.get('/docker')
        assert page.status_code == 200
        assert 'Socket missing' in page.text
        app.state.daemon_client.ping = lambda: {'docker_access': 'disabled'}
        assert client.get('/docker').status_code == 403
