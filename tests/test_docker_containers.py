from unittest.mock import Mock

import pytest

from backend.docker.containers import inventory, details
from backend.daemon import DaemonServer
from backend.daemon_config import DaemonSettings
from common.protocol import ProtocolError
from backend.docker.networks import associate_endpoints


def test_inventory_includes_unmanaged_and_stopped_containers_sorted_by_stack():
    client = Mock()
    client.request.return_value = [
        {'Id': 'a' * 64, 'Names': ['/first'], 'Labels': {'com.docker.compose.project': 'zebra'}, 'State': 'running', 'Status': 'Up (unhealthy)'},
        {'Id': 'b' * 64, 'Names': ['/unmanaged'], 'State': 'exited'},
        {'Id': 'c' * 64, 'Names': ['/last'], 'Labels': {'com.docker.stack.namespace': 'Alpha'}, 'State': 'running'},
    ]
    result = inventory(client)['containers']
    client.request.assert_called_once_with('/containers/json', query={'all': 'true'})
    assert [item['stack'] for item in result] == ['', 'Alpha', 'zebra']
    assert result[0]['display_state'] == 'stopped'
    assert result[1]['stack_kind'] == 'swarm'
    assert result[2]['display_state'] == 'unhealthy'


def test_detail_selects_fields_without_exposing_engine_secrets():
    client = Mock()
    client.request.return_value = {'Id': 'a' * 64, 'Name': '/web',
        'Config': {'Image': 'web:1', 'Env': ['PASSWORD=hidden'], 'Cmd': ['hidden'],
                   'Labels': {'com.docker.compose.project': 'demo', 'secret': 'hidden'}},
        'State': {'Status': 'running', 'Health': {'Status': 'starting', 'Log': [{'Output': 'hidden'}]}},
        'NetworkSettings': {'Networks': {'zebra': {}, 'alpha': {}}},
        'Mounts': [{'Destination': '/data', 'Source': '/secret', 'RW': True}]}
    result = details(client, 'a' * 64)
    assert result['name'] == 'web'
    assert result['display_state'] == 'starting'
    assert result['stack'] == 'demo'
    assert [item['name'] for item in result['networks']] == ['alpha', 'zebra']
    assert result['labels']['secret'] == 'hidden'
    assert 'Env' not in result and 'Cmd' not in result and 'Log' not in result
    assert '/secret' not in str(result)
    client.request.reset_mock()
    with pytest.raises(ValueError):
        details(client, '../../info')
    client.request.assert_not_called()


def test_disabled_docker_denies_container_rpcs():
    server = DaemonServer(DaemonSettings(_env_file=None, docker_access='disabled'))
    for method in ['docker.containers', 'docker.container']:
        with pytest.raises(ProtocolError, match='disabled'):
            server.dispatch(method, {'id': 'a' * 64})


def test_network_endpoint_details_include_prefixes_gateways_and_scoped_interface_mapping():
    client = Mock()
    client.request.return_value = {'Id': 'a' * 64, 'Name': '/demo-web-1',
        'Config': {'Labels': {'com.docker.compose.project': 'demo', 'com.docker.compose.service': 'web'}},
        'NetworkSettings': {'SandboxKey': '/private/netns', 'Networks': {'demo_default': {
            'NetworkID': 'network1', 'EndpointID': 'private-endpoint', 'MacAddress': '02:42:ac:16:01:f9',
            'IPAddress': '172.22.1.249', 'IPPrefixLen': 24, 'Gateway': '172.22.1.1',
            'GlobalIPv6Address': 'fd12::f9', 'GlobalIPv6PrefixLen': 64, 'IPv6Gateway': 'fd12::1'}}}}
    endpoint = details(client, 'a' * 64)['networks'][0]
    assert (endpoint['ip4'], endpoint['ip4_prefix_length'], endpoint['gateway4']) == ('172.22.1.249', 24, '172.22.1.1')
    assert (endpoint['ip6'], endpoint['ip6_prefix_length'], endpoint['gateway6']) == ('fd12::f9', 64, 'fd12::1')
    assert endpoint['interface'] is None
    assert '/private/netns' not in str(endpoint) and 'private-endpoint' not in str(endpoint)
    networks = [{'Id': 'network1', 'Name': 'demo_default', 'Driver': 'bridge',
                 'Options': {'com.docker.network.bridge.name': 'br-demo'}, 'Labels': {'com.docker.compose.project': 'owner'}}]
    links = [{'name': 'br-demo'}, {'name': 'veth-web', 'kind': 'veth', 'master': 'br-demo',
                                  'index': 100, 'peer_index': 2, 'link_netnsid': 0, 'mac_address': '02:42:00:00:00:01'},
             {'name': 'veth-unrelated', 'master': 'br-other', 'mac_address': '02:42:ac:16:01:f9'}]
    peers = {'veth-web': {'namespace_id': 0, 'internal_index': 2, 'port_index': 100,
                           'port_mac': '02:42:00:00:00:01', 'internal_mac': endpoint['mac']}}
    associate_endpoints([endpoint], networks, links, peers)
    assert endpoint['interface'] == 'veth-web'
    assert endpoint['bridge_interface'] == 'br-demo'
    assert endpoint['stack'] == 'owner' and endpoint['service_stack'] == 'demo'


def test_matching_mac_alone_does_not_prove_a_veth_peer_mapping():
    from backend.docker.networks import container_endpoints
    raw = {'NetworkSettings': {'Networks': {'demo': {'MacAddress': '02:42:00:00:00:01'}}}}
    networks = [{'Name': 'demo', 'Driver': 'bridge', 'Options': {'com.docker.network.bridge.name': 'br-demo'}}]
    links = [{'name': 'br-demo'}, {'name': 'veth-a', 'kind': 'veth', 'master': 'br-demo',
                                 'index': 100, 'peer_index': 2, 'link_netnsid': 0, 'mac_address': '02:42:00:00:00:01'}]
    endpoint = associate_endpoints(container_endpoints(raw), networks, links)[0]
    assert endpoint['interface'] is None
    peers = {'veth-a': {'namespace_id': 0, 'internal_index': 2, 'port_index': 100,
                        'internal_mac': endpoint['mac'], 'port_mac': '02:42:00:00:00:02'}}
    assert associate_endpoints(container_endpoints(raw), networks, links, peers)[0]['interface'] == 'veth-a'
    links[1]['link_netnsid'] = 1
    assert associate_endpoints(container_endpoints(raw), networks, links, peers)[0]['interface'] is None
