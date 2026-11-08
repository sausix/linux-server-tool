import pytest

from common.ip_classification import classify_ip
from frontend.inventory import filter_inventory


@pytest.mark.parametrize('address,kind', [
    ('127.0.0.1', 'loopback'), ('::1', 'loopback'),
    ('192.168.1.1', 'private'), ('8.8.8.8', 'public'),
    ('169.254.1.1', 'link-local'), ('100.64.0.1', 'shared'),
    ('224.0.0.1', 'multicast'), ('255.255.255.255', 'broadcast'),
    ('0.0.0.0', 'unspecified'), ('::', 'unspecified'),
    ('fe80::1', 'link-local'), ('fd12::1', 'unique-local'),
    ('2001:4860:4860::8888', 'public'), ('2001:db8::1', 'documentation'),
    ('3fff::1', 'documentation'), ('fec0::1', 'site-local'),
    ('::ffff:192.168.1.1', 'ipv4-mapped'), ('2002:c000:0201::1', '6to4'),
    ('2001:0000::1', 'teredo'), ('64:ff9b::c000:201', 'nat64'),
    ('64:ff9b:1::1', 'nat64'), ('100::1', 'discard'),
])
def test_address_types(address, kind):
    assert classify_ip(address)['type'] == kind


def test_multicast_scope_and_address_type_filter():
    assert classify_ip('ff02::1')['scope'] == 'link-local'
    assert classify_ip('ff0e::1')['scope'] == 'global'
    data = {'interfaces': [{'name': 'eth0'}, {'name': 'eth1'}], 'addresses': [
        {'interface': 'eth0', 'address': 'fd12::1'},
        {'interface': 'eth1', 'address': '8.8.8.8'},
    ]}
    result = filter_inventory('interfaces', data, [('interface.address_type', 'unique-local')])
    assert [item['name'] for item in result['interfaces']] == ['eth0']
    assert result['address_types']['fd12::1']['label'].startswith('Unique local')
