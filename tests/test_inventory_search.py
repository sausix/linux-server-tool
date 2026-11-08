import pytest

from frontend.inventory import filter_inventory, parse_filters


def inventory():
    return {'interfaces': [
        {'name': 'eth0', 'type': 1, 'flags': ['up'], 'mac_address': 'aa:bb:cc:dd:ee:ff'},
        {'name': 'eth1', 'type': 1, 'flags': ['down']},
    ], 'addresses': [
        {'interface': 'eth0', 'family': 'ipv4', 'address': '192.168.0.1'},
        {'interface': 'eth0', 'family': 'ipv6', 'address': '2001:db8::1'},
    ], 'configuration': {'eth1': {'addresses': ['192.0.2.1/24']}}}


@pytest.mark.parametrize('field,value,name', [
    ('interface.name', 'eth1', 'eth1'),
    ('interface.ip', '192.168.0.1', 'eth0'),
    ('interface.ip4', '192.168.0.1', 'eth0'),
    ('interface.ip6', '2001:0db8:0:0:0:0:0:1', 'eth0'),
    ('interface.mac', 'AA:BB:CC:DD:EE:FF', 'eth0'),
    ('interface.ip', '192.0.2.1', 'eth1'),
])
def test_interface_fields(field, value, name):
    result = filter_inventory('interfaces', inventory(), [(field, value)])
    assert [entry['name'] for entry in result['interfaces']] == [name]


def test_combined_search_and_invalid_fields():
    assert not filter_inventory('interfaces', inventory(), [('q', 'interface.name=eth0 interface.flag=down')])['interfaces']
    assert len(filter_inventory('interfaces', inventory(), [('q', 'Ethernet interface.flag=up')])['interfaces']) == 1
    with pytest.raises(ValueError, match='Unknown filter'):
        parse_filters([('interface.password', 'test')])
    with pytest.raises(ValueError):
        parse_filters([('q', '"unterminated')])


@pytest.mark.parametrize('field,value,names', [
    ('interface.ip', '172.22.1.0', ['eth0']),
    ('interface.ip', '172.22.1.99', ['eth0']),
    ('interface.ip4', '172.22.1.255', ['eth0']),
    ('interface.ip', '172.22.2.1', []),
    ('interface.ip6', '172.22.1.1', []),
    ('interface.ip', '2001:db8::abcd', ['eth0']),
    ('interface.ip6', '2001:db8::abcd', ['eth0']),
    ('interface.subnet', '172.22.1.0/24', ['eth0']),
    ('interface.subnet', '172.22.1.77/24', ['eth0']),
    ('interface.subnet', '172.22.0.0/16', []),
    ('interface.subnet', '172.22.1.0', []),
    ('interface.subnet', '2001:0db8::123/64', ['eth0']),
    ('interface.ip', '192.0.2.99', ['eth1']),
    ('interface.subnet', '192.0.2.0/24', ['eth1']),
    ('interface.ip', 'invalid', []),
])
def test_subnet_and_host_membership_filters(field, value, names):
    data = inventory()
    data['addresses'] = [
        {'interface': 'eth0', 'address': '172.22.1.1', 'prefix_length': 24},
        {'interface': 'eth0', 'address': '2001:db8::1', 'prefix_length': 64},
    ]
    result = filter_inventory('interfaces', data, [(field, value)])
    assert [link['name'] for link in result['interfaces']] == names
    if names and names[0] == 'eth0':
        assert result['interfaces'][0]['_matches']['interface.ip' if field == 'interface.subnet' else field]


def test_subnet_join_in_routing_and_sysctl():
    data = inventory()
    data['addresses'][0]['prefix_length'] = 24
    result = filter_inventory('routing', {'routes': [{'interface': 'eth0'}, {'interface': 'eth1'}]},
                              [('interface.subnet', '192.168.0.0/24')], data)
    assert [route['interface'] for route in result['routes']] == ['eth0']
    result = filter_inventory('sysctl', {'options': [{'key': 'net.ipv4.conf.eth0.forwarding', 'interface': 'eth0'},
                                                    {'key': 'net.ipv4.ip_forward'}]},
                              [('interface.ip', '192.168.0.99')], data)
    assert [option['interface'] for option in result['options']] == ['eth0']


def test_routing_destination_subnet_filter_is_local_and_prefix_specific():
    routes = {'routes': [
        {'interface': 'br-demo', 'destination': '172.22.1.0', 'prefix_length': 24, 'gateway': '172.22.1.1'},
        {'interface': 'eth0', 'destination': '172.22.1.0', 'prefix_length': 25},
        {'interface': 'eth0', 'destination': 'default', 'prefix_length': 0},
    ]}
    result = filter_inventory('routing', routes, [('route.subnet', '172.22.1.77/24')])
    assert [route['interface'] for route in result['routes']] == ['br-demo']
    assert result['routes'][0]['_matches']['route.subnet'] == ['172.22.1.0/24']
    assert len(filter_inventory('routing', routes, [('route.gateway', '172.22.1.1')])['routes']) == 1
    assert len(filter_inventory('routing', routes, [('route.interface', 'eth0')])['routes']) == 2


def test_routing_interface_join_and_option_fields():
    routes = {'routes': [
        {'interface': 'eth0', 'family': 'ipv4', 'destination': 'default'},
        {'interface': 'eth1', 'family': 'ipv4', 'destination': '192.0.2.0'},
    ], 'options': [{'key': 'net.ipv4.ip_forward', 'value': '1', 'sources': []}]}
    result = filter_inventory('routing', routes, [('interface.ip4', '192.168.0.1')], inventory())
    assert len(result['routes']) == 1
    assert result['routes'][0]['interface'] == 'eth0'
    assert result['options'] == []
    result = filter_inventory('routing', routes, [('option.key', 'net.ipv4.ip_forward')])
    assert result['routes'] == []
    assert len(result['options']) == 1


def test_firewall_search_retains_parent_table_and_chain():
    data = {'ruleset': {'nftables': [
        {'table': {'family': 'inet', 'name': 'filter'}},
        {'chain': {'family': 'inet', 'table': 'filter', 'name': 'input'}},
        {'rule': {'family': 'inet', 'table': 'filter', 'chain': 'input', 'expr': [{'accept': None}]}},
        {'chain': {'family': 'inet', 'table': 'filter', 'name': 'output'}},
    ]}}
    result = filter_inventory('firewall', data, [('q', 'accept')])
    assert [next(iter(entry)) for entry in result['ruleset']['nftables']] == ['table', 'chain', 'rule']


def test_legacy_firewall_search_hides_unmatched_chains():
    data = {'ipv4_rules': '*filter\n:INPUT ACCEPT [0:0]\n:OUTPUT ACCEPT [0:0]\n-A INPUT -s 192.0.2.1 -j DROP\nCOMMIT\n'}
    result = filter_inventory('firewall', data, [('firewall.chain', 'INPUT')])
    assert ':INPUT' in result['ipv4_rules']
    assert ':OUTPUT' not in result['ipv4_rules']
    assert '-A INPUT' in result['ipv4_rules']
