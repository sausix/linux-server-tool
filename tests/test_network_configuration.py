from backend.network.configuration import declarations


def test_static_addresses_and_dynamic_sources(tmp_path):
    (tmp_path / '10-static.network').write_text(
        '[Match]\nName=eth0\n[Network]\nAddress=192.0.2.10/24\nAddress=2001:db8::1/64\n'
    )
    (tmp_path / '20-dynamic.network').write_text('[Match]\nName=eth1\n[Network]\nDHCP=yes\n')
    result = declarations([{'name': name} for name in ['eth0', 'eth1', 'other']], tmp_path, tmp_path / 'interfaces')
    assert result['eth0']['addresses'] == ['192.0.2.10/24', '2001:db8::1/64']
    assert result['eth0']['complete']
    assert result['eth1']['dynamic']
    assert not result['other']['complete']
