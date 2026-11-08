"""ENV references are complete defaults, not hidden deployment overrides."""
from importlib.util import module_from_spec, spec_from_file_location
import json
import os
from pathlib import Path
import subprocess
import shutil
from types import SimpleNamespace
from unittest.mock import MagicMock

from dotenv import dotenv_values
import pytest

from backend.daemon_config import DaemonSettings
from frontend.config import Settings

ROOT = Path(__file__).resolve().parents[1]
SPEC = spec_from_file_location('configuration_examples', ROOT/'scripts/config_examples.py')
examples = module_from_spec(SPEC)
SPEC.loader.exec_module(examples)


@pytest.fixture
def isolated_environment(monkeypatch):
    for key in list(os.environ):
        if key.startswith(('FRONTEND_', 'BACKEND_', 'LSDFM_', 'LSDFMD_')) or key in ('DOCKER_SUPPORT', 'NETWORK_SUPPORT', 'SYSTEM_SUPPORT'):
            monkeypatch.delenv(key)


def test_generated_references_are_current():
    for path, expected in examples.outputs().items():
        assert path.read_text() == expected, path


@pytest.mark.parametrize('component,model,path', [
    ('FRONTEND', Settings, 'docs/frontend/frontend.env.example'),
    ('BACKEND', DaemonSettings, 'backend/systemd/backend.env.example'),
])
def test_complete_native_examples_equal_missing_file_defaults(component, model, path, tmp_path, isolated_environment):
    contents = dotenv_values(ROOT/path)
    keys = {name.upper() if name.endswith('_support') else component+'_'+name.upper() for name in model.model_fields}
    assert set(contents) == keys | {component+'_ENV_PREFIX'}
    assert model(_env_file=ROOT/path).model_dump() == model(_env_file=tmp_path/'missing.env').model_dump()


@pytest.fixture
def compose_cli():
    if not shutil.which('docker'):
        pytest.skip('Docker Compose CLI is not installed')
    if subprocess.run(['docker', 'compose', 'version'], capture_output=True).returncode:
        pytest.skip('Docker Compose plugin is not installed')


def compose(path, environment):
    result = subprocess.run(['docker', 'compose', '--project-directory', str(ROOT), '--env-file', str(path),
                             '-f', str(ROOT/'docs/deployment/compose.all.yaml'), 'config', '--format', 'json'],
                            env=environment, capture_output=True, text=True)
    assert result.returncode == 0, 'Compose configuration failed (values suppressed)'
    return json.loads(result.stdout)


def test_complete_compose_example_equals_no_overrides_and_forwards_every_setting(isolated_environment, monkeypatch, compose_cli):
    environment = dict(os.environ, FRONTEND_SECRET_KEY='test-only-unique-secret-with-at-least-32-characters')
    defaults = compose(os.devnull, environment)
    complete = compose(ROOT/'docs/deployment/compose.env.example', environment)
    assert defaults == complete
    entries = dotenv_values(ROOT/'docs/deployment/compose.env.example')
    for component, model in examples.MODELS.items():
        expected = {name.upper() if name.endswith('_support') else component+'_'+name.upper() for name in model.model_fields}
        assert expected <= set(entries)
        assert set(defaults['services'][component.lower()]['environment']) == expected | {component+'_ENV_PREFIX'}
        prefixed = defaults['services'][component.lower()]['environment']
        # Empty optional TLS paths/tokens retain None, never Path('.').
        for key, value in prefixed.items():
            monkeypatch.setenv(key, value)
        actual = model(_env_file=None)
        assert actual.env_prefix == component+'_'
        if component == 'BACKEND':
            assert actual.docker_tls_ca is None and actual.access_token is None
        else:
            assert actual.ssl_certfile is None and actual.daemon_token is None


def test_production_requires_a_unique_secret_even_with_default_reference(isolated_environment, compose_cli):
    environment = dict(os.environ)
    for file in (os.devnull, ROOT/'docs/deployment/compose.env.example'):
        result = subprocess.run(['docker', 'compose', '--project-directory', str(ROOT), '--env-file', str(file),
                                 '-f', str(ROOT/'docs/deployment/compose.web.yaml'), 'config', '--quiet'],
                                env=environment, capture_output=True, text=True)
        assert result.returncode != 0
        assert 'FRONTEND_SECRET_KEY' in result.stderr


def test_frontend_healthcheck_follows_listener_port(monkeypatch):
    from frontend import healthcheck
    monkeypatch.setattr(healthcheck, 'Settings', lambda: SimpleNamespace(bind_host='0.0.0.0', port=8123, ssl_certfile=None))
    probe = MagicMock()
    probe.return_value.__enter__.return_value.status = 200
    monkeypatch.setattr(healthcheck, 'urlopen', probe)
    healthcheck.main()
    probe.assert_called_once_with('http://127.0.0.1:8123/healthz', timeout=3, context=None)


@pytest.mark.parametrize('transport,expected', [('unix', 'unix:///run/custom/service.sock'), ('tcp', 'tcp://127.0.0.1:9988')])
def test_backend_healthcheck_uses_configured_endpoint_and_token(monkeypatch, transport, expected):
    from backend import healthcheck
    settings = SimpleNamespace(transport=transport, unix_socket='/run/custom/service.sock', listen_host='0.0.0.0', listen_port=9988, access_token='test-token')
    monkeypatch.setattr(healthcheck, 'DaemonSettings', lambda: settings)
    client = MagicMock()
    monkeypatch.setattr(healthcheck, 'DaemonClient', client)
    healthcheck.main()
    client.assert_called_once_with(expected, token='test-token', timeout=2)
    client.return_value.ping.assert_called_once_with()


def test_empty_file_secret_blocks_legacy_but_explicit_constructor_wins(tmp_path, isolated_environment, monkeypatch):
    from common.identity import LEGACY_FRONTEND_PREFIX
    from pydantic import ValidationError
    monkeypatch.setenv(LEGACY_FRONTEND_PREFIX+'SECRET_KEY', 'legacy-secret-that-must-not-return-123456')
    path = tmp_path/'overrides.env'
    path.write_text('FRONTEND_ENVIRONMENT=production\nFRONTEND_SECRET_KEY=\n')
    with pytest.raises(ValidationError, match='SECRET_KEY'):
        Settings(_env_file=path)
    assert Settings(_env_file=path, secret_key='explicit-secret-that-wins-1234567890').secret_key == 'explicit-secret-that-wins-1234567890'


def test_empty_tcp_token_cannot_reactivate_legacy_token(isolated_environment, monkeypatch):
    from common.identity import LEGACY_BACKEND_PREFIX
    from pydantic import ValidationError
    monkeypatch.setenv(LEGACY_BACKEND_PREFIX+'ACCESS_TOKEN', 'legacy-token-that-must-not-return-123456')
    monkeypatch.setenv('BACKEND_TRANSPORT', 'tcp')
    monkeypatch.setenv('BACKEND_ACCESS_TOKEN', '')
    with pytest.raises(ValidationError, match='ACCESS_TOKEN'):
        DaemonSettings(_env_file=None)
