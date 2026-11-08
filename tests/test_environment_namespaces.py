"""Configurable namespaces must preserve precedence and isolate secrets."""
import json
from unittest.mock import Mock

import pytest
from pydantic import ValidationError
from common.identity import LEGACY_FRONTEND_PREFIX, LEGACY_BACKEND_PREFIX, SESSION_COOKIE, APP_NAME
from frontend.config import Settings
from backend.daemon_config import DaemonSettings


@pytest.mark.parametrize('model,selector', [(Settings, 'FRONTEND_ENV_PREFIX'), (DaemonSettings, 'BACKEND_ENV_PREFIX')])
def test_prefix_from_file_environment_and_cli(model, selector, tmp_path, monkeypatch):
    path = tmp_path/'settings.env'
    path.write_text(selector+'=FILE_\nFILE_PORT=8123\nFILE_LISTEN_PORT=8124\n')
    value = model(_env_file=path)
    field = 'port' if model is Settings else 'listen_port'
    assert value.env_prefix == 'FILE_'
    assert getattr(value, field) == (8123 if model is Settings else 8124)
    monkeypatch.setenv(selector, 'PROCESS_')
    monkeypatch.setenv('PROCESS_'+field.upper(), '8234')
    assert getattr(model(_env_file=path), field) == 8234
    monkeypatch.setenv('CLI_'+field.upper(), '8345')
    value = model(_env_file=path, _env_prefix='CLI_', **{field: 8456})
    assert value.env_prefix == 'CLI_' and getattr(value, field) == 8456
    assert value.env_name('docker_support') == 'DOCKER_SUPPORT'
    assert value.env_name(field) == 'CLI_'+field.upper()


def test_default_namespace_accepts_legacy_as_fallback_but_new_names_win(monkeypatch, tmp_path):
    monkeypatch.setenv(LEGACY_FRONTEND_PREFIX+'PORT', '8111')
    assert Settings(_env_file=None).port == 8111
    path = tmp_path/'settings.env'
    path.write_text('FRONTEND_PORT=8222\n')
    assert Settings(_env_file=path).port == 8222
    monkeypatch.setenv('FRONTEND_PORT', '8333')
    assert Settings(_env_file=path).port == 8333
    monkeypatch.setenv(LEGACY_BACKEND_PREFIX+'DOCKER_ACCESS', 'rw')
    assert DaemonSettings(_env_file=None).docker_access == 'readwrite'


def test_custom_namespace_does_not_pick_up_legacy_credentials(monkeypatch):
    monkeypatch.setenv(LEGACY_FRONTEND_PREFIX+'SECRET_KEY', 'legacy-secret-that-must-not-be-used-123456')
    monkeypatch.setenv('FRONTEND_ENV_PREFIX', 'ISOLATED_')
    monkeypatch.setenv('ISOLATED_ENVIRONMENT', 'production')
    with pytest.raises(ValidationError, match='SECRET_KEY'):
        Settings(_env_file=None)
    monkeypatch.setenv(LEGACY_BACKEND_PREFIX+'ACCESS_TOKEN', 'legacy-token-that-must-not-be-used-123456')
    with pytest.raises(ValidationError, match='ACCESS_TOKEN'):
        DaemonSettings(_env_file=None, _env_prefix='ISOLATED_', transport='tcp')


def test_custom_namespace_complex_fields_and_global_component_switches(monkeypatch):
    monkeypatch.setenv('FRONTEND_ENV_PREFIX', 'CUSTOM_')
    monkeypatch.setenv('CUSTOM_LOGIN_ALLOWED_NETWORKS', '["192.0.2.0/24"]')
    monkeypatch.setenv('CUSTOM_LOGIN_RULES', '[{"users":["admin"],"webauthn":"required"}]')
    monkeypatch.setenv('DOCKER_SUPPORT', 'false')
    settings = Settings(_env_file=None)
    assert settings.login_allowed_networks == ['192.0.2.0/24']
    assert settings.login_rules[0].webauthn == 'required' and not settings.docker_support
    assert 'CUSTOM_PUBLIC_URL' in settings.format_env_references('Set FRONTEND_PUBLIC_URL')


def test_reload_exports_values_in_selected_namespace(monkeypatch, tmp_path):
    import frontend.cli as cli
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr('sys.argv', ['frontend', '--reload', '--env-prefix', 'RELOAD_', '--port', '8123',
        '--secret-key', 'validation-only-placeholder-secret-123456', '--database-url', 'sqlite:///:memory:'])
    # Isolate the environment writes from following tests.
    monkeypatch.setattr(cli.os, 'environ', dict(cli.os.environ))
    monkeypatch.setattr(cli.uvicorn.Config, 'bind_socket', lambda self: Mock())
    monkeypatch.setattr('uvicorn.supervisors.ChangeReload', Mock())
    cli.main()
    assert cli.os.environ['FRONTEND_ENV_PREFIX'] == 'RELOAD_'
    assert cli.os.environ['RELOAD_PORT'] == '8123'
    assert json.loads(cli.os.environ['RELOAD_LOGIN_RULES']) == []


def test_brand_is_read_from_metadata_and_can_be_overridden(monkeypatch):
    assert Settings(_env_file=None).app_name == APP_NAME
    monkeypatch.setenv('FRONTEND_APP_NAME', 'Another Console')
    assert Settings(_env_file=None).app_name == 'Another Console'
    assert SESSION_COOKIE  # Compatibility identity remains independent of display name.


def test_compose_translates_custom_prefix_keys_and_healthcheck_token(monkeypatch):
    from test_deployment_examples import configuration, compose_environment
    import os
    environment = {key: value for key, value in os.environ.items() if not key.startswith(('FRONTEND_', 'BACKEND_'))}
    environment.update(FRONTEND_ENV_PREFIX='WEBAPP_', BACKEND_ENV_PREFIX='HOSTAPP_',
        FRONTEND_SECRET_KEY='validation-only-placeholder-secret-123456', FRONTEND_APP_NAME='A Different Console',
        FRONTEND_DAEMON_TOKEN='validation-only-daemon-token-1234567890')
    data = configuration('all', environment)
    web = data['services']['frontend']['environment']
    backend = data['services']['backend']['environment']
    assert web['WEBAPP_SECRET_KEY'] == environment['FRONTEND_SECRET_KEY']
    assert web['WEBAPP_APP_NAME'] == 'A Different Console'
    assert 'FRONTEND_SECRET_KEY' not in web
    assert backend['HOSTAPP_ACCESS_TOKEN'] == environment['FRONTEND_DAEMON_TOKEN']
    assert backend['BACKEND_ENV_PREFIX'] == 'HOSTAPP_'
    assert data['services']['backend']['healthcheck']['test'] == ['CMD', 'python', '-m', 'backend.healthcheck']


def test_configuration_validation_errors_do_not_echo_secret_values():
    secret = 'must-not-appear-in-error-output-1234567890'
    with pytest.raises(ValidationError) as error:
        Settings(_env_file=None, secret_key=secret, port=-1)
    assert secret not in str(error.value)
    with pytest.raises(ValidationError) as error:
        DaemonSettings(_env_file=None, access_token=secret, listen_port=-1)
    assert secret not in str(error.value)


def test_configured_brand_is_rendered_as_text(tmp_path):
    from fastapi.testclient import TestClient
    from frontend.app import create_app
    app = create_app(Settings(_env_file=None, app_name='Another <Console>',
        secret_key='validation-only-placeholder-secret-123456',
        database_url=f'sqlite:///{tmp_path/"web.db"}', trusted_hosts='testserver'))
    with TestClient(app) as client:
        response = client.get('/login')
        assert 'Another &lt;Console&gt;' in response.text
        assert 'Another <Console>' not in response.text


def test_explicit_empty_standard_secret_does_not_use_legacy_secret(monkeypatch):
    monkeypatch.setenv(LEGACY_FRONTEND_PREFIX+'SECRET_KEY', 'legacy-secret-that-must-not-be-used-123456')
    monkeypatch.setenv('FRONTEND_SECRET_KEY', '')
    monkeypatch.setenv('FRONTEND_ENVIRONMENT', 'production')
    with pytest.raises(ValidationError, match='SECRET_KEY'):
        Settings(_env_file=None)


@pytest.mark.parametrize('prefix', ['NO_SUFFIX', 'invalid-prefix_', 'BAD PREFIX_', 'X_$(command)'])
def test_invalid_prefix_is_rejected_without_using_its_value(prefix):
    with pytest.raises(ValueError, match='Environment prefixes'):
        Settings(_env_file=None, _env_prefix=prefix)
