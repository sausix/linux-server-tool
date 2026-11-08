import pytest

from common.interface_scope import interface_scope
from frontend.inventory import filter_inventory, interface_models


@pytest.mark.parametrize('addresses,flags,expected', [
    (['127.0.0.1/8', '::1/128'], [], 'loopback'),
    (['::ffff:127.0.0.1'], [], 'loopback'),
    (['10.1.2.3/24', 'fd12::1/64', 'fe80::1/64'], [], 'private'),
    (['127.0.0.1', '192.168.1.1'], [], 'private'),
    (['192.168.1.1', '2606:4700:4700::1111'], [], 'public'),
    (['8.8.8.8'], ['loopback'], 'public'),
    (['::ffff:8.8.8.8'], [], 'public'),
    (['100.64.0.1'], [], 'public'),
    ([], ['loopback'], 'loopback'),
    ([], [], 'unknown'),
    (['invalid'], [], 'unknown'),
])
def test_runtime_interface_scope(addresses, flags, expected):
    assert interface_scope(addresses, flags) == expected


def test_scope_filter_highlights_field_and_ignores_persistent_only_addresses():
    data = {'interfaces': [{'name': 'eth0', 'flags': []}, {'name': 'lo', 'flags': ['loopback']},
                           {'name': 'br0', 'kind': 'bridge', 'ports': [{'name': 'eth0'}]}],
            'addresses': [{'interface': 'eth0', 'address': '192.168.1.1'}, {'interface': 'lo', 'address': '127.0.0.1'}],
            'configuration': {'eth0': {'addresses': ['8.8.8.8/32']}}}
    result = filter_inventory('interfaces', data, [('interface.scope', 'private')])
    assert [link['name'] for link in result['interfaces']] == ['eth0']
    assert result['interfaces'][0]['scope'] == 'private'
    assert result['interfaces'][0]['_matches']['interface.scope'] == ['private']
    all_links = filter_inventory('interfaces', data, [])['interfaces']
    assert all_links[2]['ports'][0]['scope'] == 'private'
    assert interface_models(data)['br0'].scope == 'unknown'
