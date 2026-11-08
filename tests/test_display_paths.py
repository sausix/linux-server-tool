import pytest

from common.display_paths import display_host_path


@pytest.mark.parametrize('value,expected', [
    ('/host/etc/sysctl.d/ipv6-seperate.conf', '/etc/sysctl.d/ipv6-seperate.conf'),
    ('/host/root/etc/sysctl.conf', '/etc/sysctl.conf'),
    ('/host/root', '/'),
    ('Source: /host/etc/test.conf:3', 'Source: /etc/test.conf:3'),
    ('path=/host/root/proc/sys/net/a', 'path=/proc/sys/net/a'),
    ('/host/etc-other/file', '/host/etc-other/file'),
    ('/other/host/etc/test', '/other/host/etc/test'),
    ('/host/root/etc/a and /host/etc/b', '/etc/a and /etc/b'),
])
def test_host_paths_normalize_display_only_at_path_boundaries(value, expected):
    mappings = {'/host/root': '/', '/host/etc': '/etc'}
    assert display_host_path(value, mappings) == expected
    assert mappings == {'/host/root': '/', '/host/etc': '/etc'}
