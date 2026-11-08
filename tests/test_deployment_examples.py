"""Public deployment examples must resolve independently of the docs location."""

import json
import os
from pathlib import Path
import re
import shutil
import subprocess

import pytest

from frontend.config import Settings
from backend.daemon_config import DaemonSettings


ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture
def compose_environment():
    if not shutil.which('docker'):
        pytest.skip('Docker Compose CLI is not installed')
    checked = subprocess.run(['docker', 'compose', 'version'], capture_output=True, text=True)
    if checked.returncode:
        pytest.skip('Docker Compose plugin is not installed')
    environment = {key: value for key, value in os.environ.items() if not key.startswith(('FRONTEND_', 'BACKEND_'))}
    environment['FRONTEND_SECRET_KEY'] = 'validation-only-placeholder-secret-123456'
    return environment


def configuration(name, environment):
    result = subprocess.run(['docker', 'compose', '--project-directory', str(ROOT), '--env-file', os.devnull,
        '-f', str(ROOT / 'docs/deployment' / f'compose.{name}.yaml'), 'config', '--format', 'json'],
        env=environment, check=True, capture_output=True, text=True)
    return json.loads(result.stdout)


@pytest.mark.parametrize('name,services', [('web', {'frontend'}), ('daemon', {'backend'}), ('all', {'frontend', 'backend'})])
def test_public_compose_files_keep_checkout_build_and_shared_socket_paths(compose_environment, name, services):
    data = configuration(name, compose_environment)
    assert set(data['services']) == services
    for name, service in data['services'].items():
        assert Path(service['build']['context']) == ROOT
        assert service['build']['dockerfile'] == 'docker/Dockerfile'
        assert service['image'] == 'application:local'
        assert service['command'] == [name]
        socket = next(mount for mount in service['volumes'] if mount['target'] == '/run/lsdfm')
        assert Path(socket['source']) == ROOT / 'lsdfm-run'
        assert socket.get('read_only', False) == (name == 'frontend')


def test_custom_managed_stack_path_matches_host_container_and_environment(compose_environment):
    compose_environment['BACKEND_COMPOSE_ROOT'] = '/srv/example-managed-stacks'
    service = configuration('all', compose_environment)['services']['backend']
    mount = next(item for item in service['volumes'] if item.get('source') == '/srv/example-managed-stacks')
    assert mount['target'] == '/srv/example-managed-stacks'
    assert service['environment']['BACKEND_COMPOSE_ROOT'] == mount['target']


def test_environment_reference_tables_cover_every_application_setting():
    for filename, prefix, model in [('backend/README.md', 'BACKEND_', DaemonSettings), ('frontend/README.md', 'FRONTEND_', Settings)]:
        text = (ROOT / 'docs' / filename).read_text()
        documented = set(re.findall(r'\| `(' + prefix + r'[A-Z0-9_]+)` \|', text))
        assert documented == {prefix + name.upper() for name in model.model_fields if not name.endswith('_support')}
        for name in ('docker', 'network', 'system'):
            assert '| `' + name.upper() + '_SUPPORT` |' in text


def test_native_environment_example_keeps_effective_access_readonly():
    # Constructor values avoid inheriting any real operator access flags.
    settings = DaemonSettings(_env_file=ROOT / 'backend/systemd/backend.env.example', docker_access='ro', network_access='ro', system_access='ro')
    assert settings.docker_access == 'readonly'
    assert set(settings.features.values()) == {'ro'}


@pytest.mark.parametrize('prefix', ['BACKEND_', 'HOST_'])
def test_docker_focused_example_exposes_only_managed_resources(compose_environment, prefix, monkeypatch):
    compose_environment.update(BACKEND_ENV_PREFIX=prefix, BACKEND_COMPOSE_ROOT='/srv/managed-stacks',
                               NETWORK_SUPPORT='true', SYSTEM_SUPPORT='true',
                               BACKEND_COMPOSE_ALLOWED_ROOTS='["/"]',
                               BACKEND_COMPOSE_SEARCH_DIRECTORIES='["/root"]')
    data = configuration('docker', compose_environment)
    backend = data['services']['backend']
    assert not backend.get('pid')
    assert backend.get('network_mode') != 'host'
    assert not backend.get('cap_add')
    assert backend['cap_drop'] == ['ALL']
    targets = {mount['target'] for mount in backend['volumes']}
    assert targets == {'/run/lsdfm', '/run/docker.sock', '/srv/managed-stacks', '/var/lib/lsdfm/backend'}
    managed = next(mount for mount in backend['volumes'] if mount['target'] == '/srv/managed-stacks')
    assert managed['source'] == managed['target']
    assert managed['bind']['create_host_path'] is False
    for key, value in backend['environment'].items():
        monkeypatch.setenv(key, value)
    settings = DaemonSettings(_env_file=None, _env_prefix=prefix)
    assert settings.compose_source_root == Path('/unavailable/compose-sources')
    assert settings.compose_allowed_roots == [Path('/srv/managed-stacks')]
    assert settings.compose_search_directories == []
    for service in data['services'].values():
        assert service['environment']['NETWORK_SUPPORT'] == 'false'
        assert service['environment']['SYSTEM_SUPPORT'] == 'false'
        assert service['environment']['DOCKER_SUPPORT'] == 'true'
