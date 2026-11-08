from unittest.mock import Mock
import pytest

from backend.docker.stacks import snapshot
from backend.docker.client import DockerError
from frontend.inventory import matches, parse_filters
from frontend.docker.models import StackEntry
from backend.docker.networks import annotate_interfaces, network_interface, docker_subnets


def engine(containers=None, networks=None, volumes=None, services=None, swarm=None):
    client = Mock()
    def request(path, **kwargs):
        return {'/containers/json': containers or [], '/volumes': {'Volumes': volumes or []}, '/services': services or []}[path]
    client.request.side_effect = request
    client.networks.return_value = networks or []
    client.info.return_value = {'Swarm': swarm or {'LocalNodeState': 'inactive'}}
    return client


def test_compose_projects_include_stopped_resources_and_service_details():
    labels = {'com.docker.compose.project': 'demo', 'com.docker.compose.service': 'web',
              'com.docker.compose.project.config_files': '/srv/demo/compose.yaml'}
    client = engine(containers=[
        {'Id': 'one', 'Names': ['/web-1'], 'Labels': labels, 'State': 'running', 'Status': 'Up (healthy)', 'Image': 'web:1'},
        {'Id': 'two', 'Names': ['/web-2'], 'Labels': labels, 'State': 'exited', 'Status': 'Exited', 'Image': 'web:1'},
        {'Id': 'unmanaged', 'Labels': {}, 'State': 'running'},
    ], networks=[{'Name': 'demo_default', 'Labels': labels, 'Driver': 'bridge'}])
    stack = snapshot(client)['stacks'][0]
    assert stack['name'] == 'demo'
    assert stack['state'] == 'partial'
    assert stack['containers'] == 2
    assert stack['services'] == 1
    assert stack['running'] == 1
    assert stack['stopped'] == 1
    assert stack['networks'] == 1
    assert stack['desired_replicas'] is None
    assert stack['config_files'] == ['/srv/demo/compose.yaml']
    assert stack['service_items'][0]['state'] == 'partial'
    assert stack['container_items'][0]['display_state'] == 'running'
    assert stack['container_items'][0]['labels'] == labels
    assert stack['network_items'][0]['labels'] == labels


def test_swarm_replica_status_and_same_name_do_not_merge_with_compose():
    client = engine(containers=[{'Labels': {'com.docker.compose.project': 'demo'}, 'State': 'running'}],
        swarm={'ControlAvailable': True, 'LocalNodeState': 'active'}, services=[{
            'ID': 'service', 'Spec': {'Name': 'demo_api', 'Labels': {'com.docker.stack.namespace': 'demo'},
            'Mode': {'Replicated': {'Replicas': 3}}, 'TaskTemplate': {'ContainerSpec': {'Image': 'api:1'}}},
            'ServiceStatus': {'RunningTasks': 2, 'DesiredTasks': 3}}])
    result = snapshot(client)
    assert len(result['stacks']) == 2
    stack = next(item for item in result['stacks'] if item['kind'] == 'swarm')
    assert stack['desired_replicas'] == 3
    assert stack['running_replicas'] == 2
    assert stack['state'] == 'degraded'
    assert stack['service_items'][0]['state'] == 'degraded'


def test_missing_access_marks_inventory_incomplete():
    client = engine(networks=[{'Name': 'demo', 'Labels': {'com.docker.compose.project': 'demo'}}])
    original = client.request.side_effect
    def request(path, **kwargs):
        if path == '/containers/json':
            raise DockerError('Access denied')
        return original(path, **kwargs)
    client.request.side_effect = request
    result = snapshot(client)
    assert result['errors']
    assert result['stacks'][0]['incomplete']
    assert result['stacks'][0]['state'] == 'unknown'


def test_stack_search_uses_model_fields_and_text():
    filters, words = parse_filters([('q', 'stack.kind=compose stack.state=running demo')])
    assert matches({'stack': StackEntry(name='demo', kind='compose', state='running')}, filters, words)
    assert not matches({'stack': StackEntry(name='demo', kind='swarm', state='running')}, filters, words)


def test_docker_interface_associations_require_visible_interfaces():
    networks = [
        {'Name': 'demo_default', 'Id': '123456789abc999', 'Driver': 'bridge', 'Labels': {'com.docker.compose.project': 'demo'}},
        {'Driver': 'bridge', 'Options': {'com.docker.network.bridge.name': 'custom'},
         'Labels': {'com.docker.stack.namespace': 'other'}},
    ]
    links = [{'name': 'br-123456789abc'}, {'name': 'custom'}, {'name': 'lo'}]
    annotate_interfaces(links, networks)
    assert links[0]['docker_stacks'] == [{'kind': 'compose', 'name': 'demo'}]
    assert links[0]['docker_networks'] == ['demo_default']
    assert links[1]['docker_stacks'] == [{'kind': 'swarm', 'name': 'other'}]
    assert links[2]['docker_stacks'] == []
    assert links[2]['docker_networks'] == []
    assert network_interface(networks[0], set())['interface'] is None
    assert network_interface({'Driver': 'overlay', 'Id': '123456789abc999'}, {'br-123456789abc'})['interface'] is None
    assert network_interface({'Driver': 'macvlan', 'Options': {'parent': 'eth0'}}, {'eth0'})['interface'] == 'eth0'


def test_bridge_ports_correlate_namespace_scoped_peer_indices_and_endpoint_mac():
    links = [{'name': 'br-demo', 'kind': 'bridge', 'ports': [{'name': 'veth1'}, {'name': 'veth2'}]},
             {'name': 'veth1', 'kind': 'veth', 'master': 'br-demo', 'index': 100, 'peer_index': 2, 'link_netnsid': 0},
             {'name': 'veth2', 'kind': 'veth', 'master': 'br-demo', 'index': 101, 'peer_index': 2, 'link_netnsid': 1},
             {'name': 'other', 'master': 'br-other'}]
    networks = [{'Id': 'net1', 'Name': 'demo_default', 'Driver': 'bridge',
                 'Options': {'com.docker.network.bridge.name': 'br-demo'},
                 'Labels': {'com.docker.compose.project': 'demo'}}]
    containers = [{'Id': 'container1', 'Labels': {'com.docker.compose.project': 'demo', 'com.docker.compose.service': 'web'},
                   'NetworkSettings': {'Networks': {'demo_default': {'NetworkID': 'net1', 'MacAddress': '02:42:ac:16:01:02',
                                                                    'IPAddress': '172.22.1.249', 'IPPrefixLen': 24, 'Gateway': '172.22.1.1'}}}}]
    peers = {'veth1': {'internal_mac': '02:42:ac:16:01:02', 'internal_index': 2, 'port_index': 100,
                      'namespace_id': 0, 'port_mac': '02:42:00:00:00:01'}}
    annotate_interfaces(links, networks, containers, peers)
    service = links[0]['ports'][0]['docker_services'][0]
    assert (service['stack'], service['service'], service['kind']) == ('demo', 'web', 'compose')
    assert links[0]['ports'][1]['docker_services'] == []
    assert links[3]['docker_services'] == []
    assert links[1]['docker_stacks'] == [{'kind': 'compose', 'name': 'demo'}]
    client = engine(containers=containers, networks=networks)
    network = snapshot(client, {'br-demo', 'veth1', 'veth2'}, interface_links=links, peer_links=peers)['stacks'][0]['network_items'][0]
    assert [port['name'] for port in network['bridge_ports']] == ['veth1', 'veth2']
    assert network['bridge_ports'][0]['docker_services'][0]['service'] == 'web'
    endpoint = network['endpoints'][0]
    assert endpoint['interface'] == 'veth1'
    assert endpoint['mac'] == '02:42:ac:16:01:02'
    assert endpoint['port_mac'] == '02:42:00:00:00:01'
    assert (endpoint['ip4'], endpoint['ip4_prefix_length'], endpoint['gateway4']) == ('172.22.1.249', 24, '172.22.1.1')
    # Duplicate endpoint MACs across namespaces remain ambiguous on one bridge.
    peers['veth2'] = {**peers['veth1'], 'port_index': 101, 'namespace_id': 1}
    annotate_interfaces(links, networks, containers, peers)
    assert not links[1]['docker_services'] and not links[2]['docker_services']


def test_resource_tables_are_sorted_and_network_interface_is_included():
    labels = {'com.docker.compose.project': 'demo'}
    client = engine(
        containers=[{'Names': ['/' + name], 'Labels': labels} for name in ['zebra', 'Alpha']],
        networks=[{'Name': name, 'Driver': 'bridge', 'Options': {'com.docker.network.bridge.name': 'br-test'}, 'Labels': labels} for name in ['zebra', 'Alpha']],
        volumes=[{'Name': name, 'Labels': labels} for name in ['zebra', 'Alpha']])
    stack = snapshot(client, {'br-test'})['stacks'][0]
    for field in ['container_items', 'network_items', 'volume_items']:
        assert [item['name'] for item in stack[field]] == ['Alpha', 'zebra']
    assert stack['network_items'][0]['interface'] == 'br-test'


@pytest.mark.parametrize(('state', 'status', 'expected'), [
    ('running', 'Up (healthy)', 'running'),
    ('running', 'Up (health: starting)', 'starting'),
    ('created', '', 'starting'),
    ('restarting', '', 'starting'),
    ('running', 'Up (unhealthy)', 'unhealthy'),
    ('dead', '', 'failed'),
    ('exited', '', 'stopped'),
])
def test_compose_health_state_for_status_colors(state, status, expected):
    client = engine(containers=[{'Labels': {'com.docker.compose.project': 'demo'},
                                 'State': state, 'Status': status}])
    assert snapshot(client, set())['stacks'][0]['state'] == expected


def test_swarm_containers_keep_service_identity_and_health():
    client = engine(containers=[{'Id': 'container', 'Labels': {
        'com.docker.stack.namespace': 'demo', 'com.docker.swarm.service.name': 'demo_web',
        'com.docker.swarm.service.id': 'service'}, 'State': 'running', 'Status': 'Up (unhealthy)'}],
        swarm={'ControlAvailable': True}, services=[{'ID': 'service', 'Spec': {
            'Name': 'demo_web', 'Labels': {'com.docker.stack.namespace': 'demo'}, 'Mode': {'Replicated': {}}},
            'ServiceStatus': {'RunningTasks': 1, 'DesiredTasks': 1}}])
    stack = snapshot(client, set())['stacks'][0]
    assert stack['container_items'][0]['service'] == 'demo_web'
    assert stack['container_items'][0]['service_id'] == 'service'
    assert stack['container_items'][0]['display_state'] == 'unhealthy'
    assert stack['service_items'][0]['state'] == 'unhealthy'
    assert stack['service_items'][0]['labels'] == {'com.docker.stack.namespace': 'demo'}


def test_interface_origins_respect_disabled_docker_and_tolerate_outages(monkeypatch):
    from backend.daemon import DaemonServer
    from backend.daemon_config import DaemonSettings

    server = DaemonServer(DaemonSettings(docker_access='disabled'))
    server.docker = engine(networks=[{'Driver': 'bridge', 'Options': {'com.docker.network.bridge.name': 'custom'}}])
    monkeypatch.setattr('backend.docker.component.interfaces', lambda: [{'name': 'custom'}])
    assert server.components['docker'].interface_origins() == {}
    server.docker.networks.assert_not_called()
    server.settings.docker_access = 'readonly'
    assert server.components['docker'].interface_origins() == {'custom': 'docker'}
    server.docker.networks.side_effect = DockerError('Unavailable')
    assert server.components['docker'].interface_origins() == {}


def test_subnets_require_visible_docker_interface_and_include_live_addresses():
    networks = [{'Driver': 'bridge', 'Options': {'com.docker.network.bridge.name': 'br-test'},
                 'IPAM': {'Config': [{'Subnet': '172.18.0.0/16'}, {'Subnet': 'fd12::/64'}, {'Subnet': 'invalid'}]}}]
    live = [{'interface': 'br-test', 'address': '192.168.12.1', 'prefix_length': 24},
            {'interface': 'eth0', 'address': '10.1.1.1', 'prefix_length': 8}]
    assert docker_subnets(networks, {'br-test'}, live) == ['172.18.0.0/16', '192.168.12.0/24', 'fd12::/64']
    assert docker_subnets(networks, {'eth0'}, live) == []


def test_runtime_details_use_engine_timestamps_and_running_definition_directory():
    from unittest.mock import Mock
    from backend.docker.stacks import runtime_info
    engine = Mock()
    engine.request.return_value = {'Created': '2026-11-05T10:00:00.123Z', 'State': {'StartedAt': '2026-11-05T11:00:00Z'}}
    stack = {'container_items': [{'id': 'a' * 64, 'state': 'running', 'created': 1}],
             'working_directory': '/wrong-active-directory',
             'compose_model': {'tree': {'running': 'old', 'nodes': [{'id': 'old', 'context': {'directory': '/running-directory'}}]}}}
    result = runtime_info(engine, stack)
    assert result == {'directory': '/running-directory', 'created': '2026-11-05T10:00:00.123Z', 'started': '2026-11-05T11:00:00Z'}
    engine.request.return_value = {'State': {'StartedAt': '0001-01-01T00:00:00Z'}}
    assert runtime_info(engine, stack)['started'] is None
