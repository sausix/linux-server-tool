from common.identity import SESSION_COOKIE, SOURCE_KEY, MANIFEST_NAME, DEFINITIONS_DIRECTORY, MANAGED_HEADER
from session_helpers import session_cookie
import base64
import html
import json
import re

import pytest
from fastapi.testclient import TestClient
from itsdangerous import TimestampSigner
from sqlalchemy import select

from frontend.app import create_app
from frontend.config import Settings
from frontend.models import User
from frontend.security import set_password
from common.ip_validation import validate_ip


@pytest.mark.parametrize('value', ['192.0.2.1', '2001:db8::1', '192.0.2.1/33', '2001:db8::1/129', 'invalid/24'])
def test_interface_address_requires_valid_prefix(value):
    with pytest.raises(ValueError):
        validate_ip(value, 'required')


def test_subnet_help_and_forbidden_prefix():
    value = validate_ip('192.0.2.10/24', 'required')
    assert value['network'] == '192.0.2.0/24'
    assert value['total_addresses'] == '256'
    assert value['usable_addresses'] == '254'
    assert value['first'] == '192.0.2.1'
    assert value['last'] == '192.0.2.254'
    assert validate_ip('2001:db8::1/64', 'required')['total_addresses'] == str(2**64)
    assert validate_ip('192.0.2.0/31', 'required')['usable_addresses'] == '2'
    assert validate_ip('192.0.2.1/32', 'required')['usable_addresses'] == '1'
    assert validate_ip('192.0.2.1', 'forbidden')['ip'] == '192.0.2.1'
    with pytest.raises(ValueError, match='not allowed'):
        validate_ip('192.0.2.1/24', 'forbidden')


@pytest.fixture
def address_client(tmp_path):
    settings = Settings(_env_file=None, environment='test', database_url=f'sqlite:///{tmp_path / "test.db"}',
                        secret_key='test-secret-key-that-is-long-enough-123456', trusted_hosts='testserver')
    app = create_app(settings)
    calls = []
    app.state.daemon_client.ping = lambda: {'network_access': 'readwrite', 'features': {'interfaces': 'rw'}}

    def rpc(method, params=None):
        calls.append((method, params))
        if method == 'network.snapshot':
            return {'interfaces': [{'name': 'eth0'}], 'addresses': [
                {'interface': 'eth0', 'address': '192.0.2.1', 'prefix_length': 24}]}
        if method == 'network.address.configuration':
            return {'applied': params['apply'], 'content_hash': 'preview-hash', 'target': '/etc/systemd/network/00-lsdfm-eth0.network', 'diff': '+Address=192.0.2.1/24'}
        return {'applied': params['apply']}

    app.state.daemon_client.call = rpc
    with TestClient(app) as client:
        with app.state.session_factory() as db:
            user = db.scalar(select(User).where(User.username == 'admin'))
            set_password(user, 'test-new-password', 90)
            db.commit()
            payload = base64.b64encode(json.dumps({'user_id': user.id}).encode())
        client.cookies.set(SESSION_COOKIE, session_cookie(app, payload))
        yield client, app, calls


def hidden(page, name):
    return html.unescape(re.search(rf'name="{name}" value="([^"]+)"', page.text).group(1))


def test_add_requires_preview_csrf_and_matching_confirmation(address_client):
    client, app, calls = address_client
    url = '/network/interfaces/address/add'
    page = client.get(url + '?interface=eth0')
    params = {'interface': 'eth0', 'address': '192.0.2.2/24', 'csrf_token': hidden(page, 'csrf_token')}
    assert client.post(url, data={**params, 'csrf_token': 'wrong'}).status_code == 403
    assert not calls
    assert 'id="subnet-info"' not in page.text
    preview = client.post(url, data=params)
    assert preview.status_code == 200
    assert 'Confirm Add' in preview.text
    assert 'id="subnet-info"' in preview.text
    assert 'data-inventory-search' not in preview.text
    assert calls[-1][1]['apply'] is False
    confirmation = hidden(preview, 'confirmation')
    assert client.post(url, data={**params, 'confirmation': confirmation, 'address': '192.0.2.3/24'}).status_code == 409
    preview = client.post(url, data=params)
    response = client.post(url, data={**params, 'confirmation': hidden(preview, 'confirmation')}, follow_redirects=False)
    assert response.status_code == 303
    assert calls[-1] == ('network.address.change', {'action': 'add', 'interface': 'eth0', 'address': '192.0.2.2/24', 'apply': True})


def test_delete_get_does_not_mutate_and_readonly_blocks(address_client):
    client, app, calls = address_client
    url = '/network/interfaces/address/delete?interface=eth0&address=192.0.2.1%2F24'
    page = client.get(url)
    assert page.status_code == 200
    assert 'Confirm Delete' in page.text
    assert 'data-inventory-search' not in page.text
    assert all(method != 'network.address.change' for method, _ in calls)
    app.state.daemon_client.ping = lambda: {'network_access': 'readonly', 'features': {'interfaces': 'ro'}}
    assert client.get(url).status_code == 403


def test_address_validation_returns_specific_prefix_error(address_client):
    client, _, _ = address_client
    result = client.get('/api/address/validate?address=192.0.2.1')
    assert result.status_code == 422
    assert 'prefix is required' in result.json()['detail']


@pytest.mark.parametrize('action', ['persist', 'remove-config'])
def test_configuration_changes_require_confirmation_hash_and_csrf(address_client, action):
    client, app, calls = address_client
    url = '/network/interfaces/address/' + action
    preview = client.get(url, params={'interface': 'eth0', 'address': '192.0.2.1/24'})
    assert preview.status_code == 200
    assert 'No network reload is performed' in preview.text
    assert not any(params and params.get('apply') for _, params in calls)
    params = {'interface': 'eth0', 'address': '192.0.2.1/24',
              'csrf_token': hidden(preview, 'csrf_token'), 'confirmation': hidden(preview, 'confirmation')}
    assert client.post(url, data={**params, 'csrf_token': 'wrong'}).status_code == 403
    result = client.post(url, data=params, follow_redirects=False)
    assert result.status_code == 303
    assert calls[-1] == ('network.address.configuration', {'interface': 'eth0', 'address': '192.0.2.1/24',
                                                         'action': action, 'apply': True, 'expected_hash': 'preview-hash'})
    app.state.daemon_client.ping = lambda: {'network_access': 'readonly', 'features': {'interfaces': 'ro'}}
    assert client.get(url, params={'interface': 'eth0', 'address': '192.0.2.1/24'}).status_code == 403
