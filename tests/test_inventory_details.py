from backend.network.inventory import annotate_bridges
from backend.system.sysctl import snapshot
from frontend.inventory import filter_inventory


def test_bridge_ports_and_mac_matches_are_snapshot_metadata():
    links = [
        {'name': 'br0', 'kind': 'bridge', 'mac_address': 'aa:bb:cc:dd:ee:ff'},
        {'name': 'eth0', 'master': 'br0', 'mac_address': 'AA:BB:CC:DD:EE:FF', 'operational_state': 'up', 'flags': ['up']},
        {'name': 'eth1', 'master': 'br0', 'mac_address': None, 'operational_state': 'down'},
    ]
    annotate_bridges(links)
    assert [port['name'] for port in links[0]['ports']] == ['eth0', 'eth1']
    assert links[0]['mac_source']['matching_ports'] == ['eth0']
    result = filter_inventory('interfaces', {'interfaces': links}, [('interface.name', 'br0')])
    assert len(result['interfaces']) == 1
    assert len(result['interfaces'][0]['ports']) == 2


def test_sysctl_whitespace_classification_and_unknown_default(tmp_path):
    proc = tmp_path / 'proc'
    base = proc / 'net/ipv4'
    base.mkdir(parents=True)
    (base / 'ping_group_range').write_text('0\t2147483647\n')
    (base / 'ip_forward').write_text('1')
    (base / 'ip_default_ttl').write_text('64')
    config = tmp_path / 'sysctl.conf'
    config.write_text('net.ipv4.ping_group_range=0 2147483647\nnet.ipv4.ip_forward=0\n')
    entries = {item['key']: item for item in snapshot(proc, [], config, False)['options']}
    assert entries['net.ipv4.ping_group_range']['state'] == 'configured'
    assert entries['net.ipv4.ip_forward']['state'] == 'changed'
    assert entries['net.ipv4.ip_default_ttl']['state'] == 'default'
    assert entries['net.ipv4.ip_default_ttl']['kernel_default'] is None
    assert 'configured' in entries['net.ipv4.ping_group_range']['classes']


def test_filter_highlights_only_the_qualifying_address():
    data = {'interfaces': [{'name': 'eth0'}], 'addresses': [
        {'interface': 'eth0', 'address': '192.0.2.1'},
        {'interface': 'eth0', 'address': '192.0.2.2'},
    ]}
    result = filter_inventory('interfaces', data, [('interface.ip', '192.0.2.1')])
    assert result['interfaces'][0]['_matches'] == {'interface.ip': ['192.0.2.1']}


def test_route_filter_highlight_metadata():
    result = filter_inventory('routing', {'routes': [
        {'family': 'ipv6', 'table': 254, 'type': 'unicast'}]},
        [('route.family', 'ipv6'), ('route.type', 'unicast')])
    assert result['routes'][0]['_matches'] == {'route.family': ['ipv6'], 'route.type': ['unicast']}
