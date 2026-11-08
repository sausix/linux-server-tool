from unittest.mock import Mock

import pytest

from backend.daemon_config import DaemonSettings
from backend.daemon import DaemonServer
from backend.events import EventBroker, EventRuntime
from common.protocol import ProtocolError
from frontend.inventory import filter_inventory


def test_feature_modes_are_capped_and_aliased():
    settings = DaemonSettings(_env_file=None, network_access='ro', routing_access='rw', sysctl_access='disabled')
    assert settings.features['routing'] == 'ro'
    assert settings.features['sysctl'] == 'disabled'
    assert DaemonSettings(_env_file=None, network_access='rw', routing_access='rw').features['routing'] == 'rw'
    disabled = DaemonSettings(_env_file=None, network_access='disabled')
    assert all(disabled.features[feature] == 'disabled' for feature in ('interfaces', 'routing', 'firewall'))
    assert disabled.features['sysctl'] == 'ro'


def test_disabled_features_block_rpc_and_monitors():
    settings = DaemonSettings(_env_file=None, docker_access='disabled', interfaces_access='disabled',
        routing_access='disabled', sysctl_access='disabled', firewall_access='disabled')
    server = DaemonServer(settings)
    for method in ('network.snapshot', 'network.routing', 'network.sysctl', 'firewall.snapshot'):
        with pytest.raises(ProtocolError, match='disabled'):
            server.dispatch(method, {})
    runtime = EventRuntime(EventBroker(), Mock(), settings)
    runtime.start()
    assert runtime.sources == []


def test_route_feature_readonly_blocks_apply():
    server = DaemonServer(DaemonSettings(_env_file=None, network_access='rw', routing_access='ro'))
    with pytest.raises(ProtocolError, match='read-only'):
        server.dispatch('network.route.change', {'apply': True})


@pytest.mark.parametrize('network,interfaces', [('ro', 'rw'), ('rw', 'ro'), ('rw', 'disabled')])
def test_address_configuration_respects_network_and_feature_access(network, interfaces):
    server = DaemonServer(DaemonSettings(_env_file=None, network_access=network, interfaces_access=interfaces))
    with pytest.raises(ProtocolError, match='read-only|disabled'):
        server.dispatch('network.address.configuration', {'apply': True, 'interface': 'eth0',
                                                        'address': '192.0.2.1/24', 'action': 'persist'})


def test_connection_string_redacts_secrets():
    assert DaemonServer.safe_endpoint('https://user:password@example.org:2376/api?token=secret') == 'https://example.org:2376/api'


def test_container_host_configuration_does_not_use_image_sysctl_files():
    from pathlib import Path
    settings = DaemonSettings(_env_file=None, host_etc_directory=Path('/host/etc'))
    assert settings.sysctl_sources == {'directories': [Path('/host/etc/sysctl.d')],
                                       'legacy': Path('/host/etc/sysctl.conf')}


def test_sysctl_class_prefix_and_interface_filters():
    data = {'options': [
        {'key': 'net.ipv6.route.max_size', 'prefix': 'net.ipv6.route', 'classes': ['routing'], 'value': '1'},
        {'key': 'net.ipv6.conf.eth0.forwarding', 'prefix': 'net.ipv6.conf.eth0', 'interface': 'eth0', 'classes': ['interface', 'routing'], 'value': '1'},
    ]}
    result = filter_inventory('sysctl', data, [('class', 'routing'), ('prefix', 'net.ipv6.route')])
    assert len(result['options']) == 1
    assert result['options'][0]['key'] == 'net.ipv6.route.max_size'
    assert len(filter_inventory('sysctl', data, [('sysctl.interface', 'eth0')])['options']) == 1
