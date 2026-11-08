from common.identity import SESSION_COOKIE, SOURCE_KEY, MANIFEST_NAME, DEFINITIONS_DIRECTORY, MANAGED_HEADER
"""Component gates and provider-owned interface presentation contracts."""
import json
import os
from pathlib import Path
import subprocess
import sys

import pytest
from fastapi.testclient import TestClient
from frontend.config import Settings
from backend.daemon_config import DaemonSettings
from frontend.ui.models import UIModels, badge
from frontend.docker.presentation import extend_interfaces, ACCENT


def test_shared_environment_switches_and_cli_override(monkeypatch):
    monkeypatch.setenv('DOCKER_SUPPORT', 'false')
    monkeypatch.setenv('NETWORK_SUPPORT', 'false')
    monkeypatch.setenv('SYSTEM_SUPPORT', 'true')
    assert not Settings(_env_file=None).docker_support
    settings = DaemonSettings(_env_file=None, docker_access='rw', network_access='rw')
    assert settings.docker_access == settings.network_access == 'disabled'
    assert settings.feature_access('sysctl') != 'disabled'
    assert Settings(_env_file=None, docker_support=True).docker_support
    assert DaemonSettings(_env_file=None, docker_support=True).docker_support
    from frontend.cli import build_parser
    args = build_parser().parse_args(['--docker-support', '--no-network-support'])
    assert args.docker_support and not args.network_support


def test_docker_disabled_process_does_not_import_optional_implementation(tmp_path):
    script = '''
import sys
from frontend.app import create_app
from frontend.config import Settings
from backend.daemon import DaemonServer
from backend.daemon_config import DaemonSettings
app = create_app(Settings(_env_file=None, docker_support=False, secret_key='test-secret-key-that-is-long-enough-123456', database_url='sqlite:///:memory:'))
daemon = DaemonServer(DaemonSettings(_env_file=None, docker_support=False))
assert daemon.docker is None and daemon.stack_control is None
assert daemon.dispatch('ping', {})['docker_access'] == 'disabled'
assert not any(name.startswith(('frontend.docker', 'backend.docker')) for name in sys.modules), [name for name in sys.modules if 'docker' in name]
assert not any(route.path.startswith(('/docker', '/api/docker')) for route in app.routes)
'''
    environment = dict(os.environ, DOCKER_SUPPORT='false', PYTHONPATH=os.pathsep.join(str(Path(__file__).resolve().parents[1]/name/'src') for name in ['common','frontend','backend']))
    subprocess.run([sys.executable, '-c', script], cwd=tmp_path, env=environment, capture_output=True, text=True, check=True)


def test_ui_provider_replacement_clear_and_fast_lookup():
    ui = UIModels()
    ui.replace('network', {'br0': {'accentcolor': '#55d6a9', 'badges': [badge('Bridge', '#55d6a9')]}})
    ui.replace('integration', {'br0': {'accentcolor': '#77bdff', 'links': [badge('Stack: test', '#77bdff', '/docker/stacks/compose/test')]}})
    assert ui.interface('br0')['accentcolor'] == '#77bdff'
    assert ui.interface('br0')['badges'][0]['text'] == 'Bridge'
    ui.replace('integration', {'eth0': {'badges': [badge('Managed', '#77bdff')]}})
    assert ui.interface('br0')['accentcolor'] == '#55d6a9'
    assert ui.interface('br0')['links'] == []
    assert 'href' not in ui.interface('eth0')['badges'][0]
    ui.clear('integration')
    assert ui.interface('eth0') == {}
    # Callers cannot mutate the registry through a returned view.
    view = ui.interface('br0'); view['accentcolor'] = '#000000'
    assert ui.interface('br0')['accentcolor'] == '#55d6a9'


@pytest.mark.parametrize('url', ['javascript:alert(1)', '//outside.example/path', '/bad\\path', '/bad\npath'])
def test_ui_links_cannot_escape_application(url):
    with pytest.raises(ValueError):
        badge('Unsafe', ACCENT, url)


def test_docker_maps_interface_badges_links_and_extension_columns():
    ui = UIModels()
    data = {'interfaces': [{'name': 'br0', 'docker_networks': ['test_default'],
        'docker_stacks': [{'name': 'test', 'kind': 'compose'}], 'ports': [{'name': 'veth0', 'docker_services': [
            {'stack': 'test', 'kind': 'compose', 'service': 'web', 'internal_mac': '02:42:00:00:00:01'}]}]}], 'docker_subnets': ['172.18.0.0/16']}
    extend_interfaces(data, ui)
    assert ui.interface('br0')['accentcolor'] == ACCENT
    assert ui.interface('br0')['links'][0]['href'] == '/docker/stacks/compose/test'
    assert ui.interface('veth0')['columns']['docker.service']['items'][0]['text'] == 'web'
    assert ui.snapshot()['address_accents'][0]['subnet'] == '172.18.0.0/16'
    extend_interfaces({'interfaces': []}, ui)
    assert ui.interface('br0') == {} and ui.snapshot()['address_accents'] == []


def test_network_renderer_has_no_docker_field_or_route_dependency():
    text = (Path(__file__).resolve().parents[1]/'frontend/src/frontend/static/network.js').read_text().lower()
    assert 'docker' not in text and '/stacks/' not in text
    assert 'data.ui?.interfaces' in text


def test_disabled_docker_does_not_require_usable_endpoint_settings():
    settings = DaemonSettings(_env_file=None, docker_support=False, docker_endpoint='unconfigured', docker_tls_cert='/nonexistent', docker_tls_key=None)
    assert settings.docker_access == 'disabled'


def test_sysctl_web_access_remains_independent_of_network_support(tmp_path):
    from test_auth_hardening import application, login
    # Build directly because the shared fixture is intentionally HTTPS-only.
    from frontend.app import create_app
    from frontend.models import User
    from frontend.security import set_password
    from sqlalchemy import select
    app = create_app(Settings(_env_file=None, network_support=False, docker_support=False,
        database_url=f'sqlite:///{tmp_path/"web.db"}', secret_key='test-secret-key-that-is-long-enough-123456', trusted_hosts='testserver'))
    app.state.daemon_client.ping = lambda: {'system_access': 'readonly', 'features': {'sysctl': 'ro'}}
    app.state.daemon_client.call = lambda method: {'options': [], 'routes': [], 'errors': []}
    with TestClient(app) as client:
        with app.state.session_factory() as db:
            user = db.scalar(select(User)); set_password(user, 'test-current-password-12345', 90); db.commit()
        login(client)
        assert client.get('/api/system/sysctl').status_code == 200
        assert client.get('/api/network/interfaces').status_code == 404
        assert client.get('/docker').status_code == 404


def test_component_theme_is_public_and_available_during_required_password_change(tmp_path):
    from frontend.app import create_app
    from frontend.models import User
    from sqlalchemy import select
    import re
    app = create_app(Settings(_env_file=None, docker_support=False, secret_key='test-secret-key-that-is-long-enough-123456',
        database_url=f'sqlite:///{tmp_path/"web.db"}', trusted_hosts='testserver'))
    with TestClient(app) as client:
        theme = client.get('/assets/component-theme.css')
        assert theme.status_code == 200 and theme.headers['content-type'].startswith('text/css')
        assert '--category-network:' in theme.text and '--category-docker:' not in theme.text
        page = client.get('/login')
        csrf = re.search(r'name="csrf_token" value="([^"]+)"', page.text).group(1)
        # Use a real restricted session without exposing generated credentials.
        with app.state.session_factory() as db:
            uid = db.scalar(select(User.id))
        cookie, _, _ = app.state.sessions.create(uid, {'user_id': uid, 'csrf_token': csrf})
        client.cookies.set(SESSION_COOKIE, cookie, domain='testserver.local', path='/')
        assert client.get('/assets/component-theme.css').status_code == 200
        assert client.get('/network', follow_redirects=False).headers['location'] == '/settings/user'
