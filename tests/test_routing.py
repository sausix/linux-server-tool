from backend.system.sysctl import snapshot, normalize_key
from pathlib import Path
import errno


def test_routing_sources_precedence_and_runtime_differences(tmp_path):
    proc = tmp_path / 'proc'
    config = tmp_path / 'etc'
    vendor = tmp_path / 'usr'
    config.mkdir()
    vendor.mkdir()
    values = {'net/ipv4/ip_forward': '1', 'net/ipv6/conf/eth0/forwarding': '1',
              'net/ipv4/conf/eth0/rp_filter': '2'}
    for key, value in values.items():
        path = proc / key
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(value)
    (vendor / '50-routing.conf').write_text('net.ipv4.ip_forward = 0\n')
    (config / '50-routing.conf').write_text('net.ipv4.ip_forward = 1\nnet.ipv6.conf.*.forwarding = 0\n')
    (config / '60-routing.conf').write_text('net.ipv6.conf.eth0.forwarding = 1\n')
    legacy = tmp_path / 'sysctl.conf'
    legacy.write_text('net.ipv4.ip_forward = 0\n')
    result = snapshot(proc, [config, vendor], legacy, include_routes=False)
    options = {option['key']: option for option in result['options']}
    forward = options['net.ipv4.ip_forward']
    assert forward['value'] == '1'
    assert forward['systemd_candidate']['value'] == '1'
    assert forward['procps_candidate']['value'] == '0'
    assert forward['systemd_candidate']['file'] == str(config / '50-routing.conf')
    assert options['net.ipv6.conf.eth0.forwarding']['systemd_candidate']['value'] == '1'
    assert options['net.ipv4.conf.eth0.rp_filter']['sources'] == []


def test_sysctl_slash_notation_preserves_dotted_interface_names():
    assert normalize_key('net/ipv4/conf/eth0.100/forwarding') == 'net/ipv4/conf/eth0.100/forwarding'
    assert normalize_key('net.ipv4.conf.eth0/100.forwarding') == 'net/ipv4/conf/eth0.100/forwarding'


def test_procfs_directory_errors_do_not_abort_other_families(tmp_path, monkeypatch):
    proc = tmp_path / 'proc'
    ipv4 = proc / 'net/ipv4'
    ipv4.mkdir(parents=True)
    (ipv4 / 'ip_forward').write_text('1')
    original = Path.glob

    def guarded_glob(path, pattern):
        if path == proc / 'net/ipv6':
            raise PermissionError('IPv6 access denied by container policy')
        return original(path, pattern)

    monkeypatch.setattr(Path, 'glob', guarded_glob)
    result = snapshot(proc, [], tmp_path / 'missing', False, {})
    assert result['options'][0]['key'] == 'net.ipv4.ip_forward'
    assert any('IPv6 access denied' in error for error in result['errors'])


def test_action_and_secret_sysctls_are_never_read_or_exposed(tmp_path, monkeypatch):
    proc = tmp_path / 'proc'
    excluded = ['net/ipv4/route/flush', 'net/ipv6/route/flush', 'net/ipv4/tcp_fastopen_key',
                'net/ipv6/conf/all/stable_secret', 'net/ipv6/conf/eth0/stable_secret']
    for key in [*excluded, 'net/ipv4/ip_forward', 'net/ipv4/conf/eth0/rp_filter']:
        path = proc / key
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text('1')
    config = tmp_path / 'sysctl.conf'
    config.write_text('net.ipv6.conf.all.stable_secret = private-value\n')
    read_text = Path.read_text

    def guarded_read(path, *args, **kwargs):
        assert path not in [proc / key for key in excluded], 'Excluded sysctl was read'
        if path == proc / 'net/ipv4/conf/eth0/rp_filter':
            raise PermissionError(errno.EACCES, 'Unexpected access failure')
        return read_text(path, *args, **kwargs)

    monkeypatch.setattr(Path, 'read_text', guarded_read)
    result = snapshot(proc, [], config, include_routes=False)
    assert [option['key'] for option in result['options']] == ['net.ipv4.ip_forward']
    assert len(result['errors']) == 1
    assert 'rp_filter' in result['errors'][0]
    assert 'private-value' not in str(result)
