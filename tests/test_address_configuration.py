from common.identity import SESSION_COOKIE, SOURCE_KEY, MANIFEST_NAME, DEFINITIONS_DIRECTORY, MANAGED_HEADER
import os

import pytest

from backend.network.address_configuration import change
from backend.network.management import NetworkManagementError


def test_preview_hash_confirmation_and_preservation(tmp_path):
    target = tmp_path / '00-lsdfm-eth0.network'
    original = MANAGED_HEADER + '\n[Match]\nName=eth0\n[Network]\nDHCP=no\nAddress=192.0.2.1/24\nDNS=192.0.2.53\n'
    target.write_text(original)
    target.chmod(0o640)
    args = ('eth0', '192.0.2.2/24', 'persist', tmp_path, tmp_path / 'interfaces')
    preview = change(*args)
    assert target.read_text() == original
    assert '+Address=192.0.2.2/24' in preview['diff']
    with pytest.raises(ValueError, match='changed'):
        change(*args, apply=True, expected_hash='stale')
    result = change(*args, apply=True, expected_hash=preview['content_hash'])
    assert result['applied']
    assert 'DNS=192.0.2.53' in target.read_text()
    assert target.stat().st_mode & 0o777 == 0o640
    assert target.stat().st_uid == os.getuid()
    deletion = change('eth0', '192.0.2.1/24', 'remove-config', tmp_path, tmp_path/'interfaces')
    change('eth0', '192.0.2.1/24', 'remove-config', tmp_path, tmp_path/'interfaces',
           apply=True, expected_hash=deletion['content_hash'])
    assert 'Address=192.0.2.1/24' not in target.read_text()
    assert 'Address=192.0.2.2/24' in target.read_text()


def test_administrator_source_and_unmarked_owned_path_are_protected(tmp_path):
    target = tmp_path/'10-admin.network'
    target.write_text('[Match]\nName=eth0\n[Network]\nAddress=192.0.2.1/24\n')
    with pytest.raises(NetworkManagementError, match='Administrator-managed'):
        change('eth0', '192.0.2.2/24', 'persist', tmp_path, tmp_path/'interfaces', apply=True)
    target.rename(tmp_path/'00-lsdfm-eth0.network')
    with pytest.raises(ValueError, match='not marked'):
        change('eth0', '192.0.2.2/24', 'persist', tmp_path, tmp_path/'interfaces')


def test_ifupdown_edits_only_matching_family(tmp_path):
    main = tmp_path/'interfaces'
    main.write_text('source interfaces.d/*\n')
    directory = tmp_path/'interfaces.d'
    directory.mkdir()
    target = directory/'90-lsdfm-eth0'
    target.write_text(MANAGED_HEADER + '\nauto eth0\niface eth0 inet static\n    address 192.0.2.1/24\niface eth0 inet6 static\n    address 2001:db8::1/64\n')
    preview = change('eth0', '192.0.2.2/24', 'persist', tmp_path/'networkd', main)
    change('eth0', '192.0.2.2/24', 'persist', tmp_path/'networkd', main,
           apply=True, expected_hash=preview['content_hash'])
    assert target.read_text().index('address 192.0.2.2/24') < target.read_text().index('iface eth0 inet6')
