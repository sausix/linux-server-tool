from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from backend.system.inventory import SystemInventory, memory_accounting, mapped_mount_matches
from backend.daemon_config import DaemonSettings
from backend.daemon import DaemonServer, build_parser
from common.protocol import ProtocolError


def test_proc_sys_inventory_block_mounts_and_cpu_delta(tmp_path, monkeypatch):
    proc, sys = tmp_path / 'proc', tmp_path / 'sys'
    (proc / 'self').mkdir(parents=True)
    def put(name, value):
        (proc / name).write_text(value)
    put('meminfo', 'MemTotal: 1000 kB\nMemAvailable: 600 kB\nSwapTotal: 500 kB\nSwapFree: 400 kB\n')
    put('stat', 'cpu 10 0 10 80 0 0 0 0 9 0\ncpu0 10 0 10 80 0 0 0 0 9 0\n')
    put('cpuinfo', 'model name : Test CPU\n')
    put('uptime', '12345.0 10000.0\n')
    put('loadavg', '0.10 0.20 0.30 1/100 1\n')
    put('self/mountinfo', '1 0 8:1 / /disk\\040space rw - ext4 /dev/sda1 rw\n2 0 0:1 / /run rw - tmpfs tmpfs rw\n')
    for name, dev, partition in [('sda', '8:0', False), ('sda1', '8:1', True), ('loop0', '7:0', False)]:
        path = sys / 'class/block' / name
        path.mkdir(parents=True)
        (path / 'dev').write_text(dev)
        (path / 'size').write_text('1000')
        (path / 'ro').write_text('0')
        if partition:
            (path / 'partition').write_text('1')
    monkeypatch.setattr('backend.system.inventory.os.statvfs', lambda path: SimpleNamespace(f_blocks=100, f_bfree=40, f_bavail=30, f_frsize=4096))
    reader = SystemInventory(proc, sys)
    first = reader.snapshot()
    assert first['cpus'][0]['usage'] is None
    assert first['memory']['MemTotal'] == 1024000
    assert first['cpu_model'] == 'Test CPU'
    assert len(first['devices']) == 2 and first['devices'][1]['type'] == 'partition'
    assert len(first['mounts']) == 1 and first['mounts'][0]['mountpoint'] == '/disk space'
    assert first['mounts'][0]['used'] == 60 * 4096
    assert first['mounts'][0]['usage'] == 66.67
    put('stat', 'cpu 20 0 20 160 0 0 0 0 19 0\ncpu0 20 0 20 160 0 0 0 0 19 0\n')
    assert reader.snapshot()['cpus'][0]['usage'] == 20
    monkeypatch.setattr('backend.system.inventory.os.statvfs', Mock(side_effect=PermissionError('denied')))
    failed = reader.snapshot()
    assert failed['mounts'][0]['total'] is None and failed['errors']


def test_system_access_env_cli_and_sysctl_independent_from_network(monkeypatch):
    monkeypatch.setenv('BACKEND_SYSTEM_ACCESS', 'disabled')
    assert DaemonSettings(_env_file=None).system_access == 'disabled'
    args = build_parser().parse_args(['--system-access', 'rw'])
    settings = DaemonSettings(_env_file=None, system_access=args.system_access, sysctl_access='rw', network_access='disabled')
    assert settings.system_access == 'readwrite' and settings.feature_access('sysctl') == 'rw'
    server = DaemonServer(DaemonSettings(_env_file=None))
    for method in ['system.snapshot', 'system.sysctl', 'network.sysctl']:
        with pytest.raises(ProtocolError, match='disabled'):
            server.dispatch(method, {})
    server = DaemonServer(settings)
    server.system_inventory = Mock()
    server.system_inventory.snapshot.return_value = {'test': True}
    assert server.dispatch('system.snapshot', {}) == {'test': True}
    monkeypatch.setattr('backend.daemon.routing_snapshot', lambda **kwargs: {'options': [], 'source_note': ''})
    assert server.dispatch('system.sysctl', {}).get('interface_origins', {}) == {}
    assert server.dispatch('network.sysctl', {}).get('interface_origins', {}) == {}


def test_memory_segments_are_disjoint_and_do_not_add_available():
    memory = {'MemTotal': 1000, 'MemFree': 100, 'Buffers': 50, 'Cached': 200, 'SReclaimable': 50, 'MemAvailable': 350}
    accounting = memory_accounting(memory)
    assert accounting == {'total': 1000, 'used': 600, 'buffers': 50, 'cache': 250, 'free': 100}
    assert sum(value for key, value in accounting.items() if key != 'total') == accounting['total']
    assert memory_accounting({'MemTotal': 1000}) is None
    assert memory_accounting({**memory, 'Cached': 2000}) is None


@pytest.mark.parametrize('filesystem, mount_device, subvolume', [('ext4', None, '/'), ('btrfs', '0:4242', '/@root')])
def test_host_mounts_use_host_pid1_and_mapped_paths_and_reject_wrong_filesystem(tmp_path, monkeypatch, filesystem, mount_device, subvolume):
    import os
    root = tmp_path / 'host'
    (root / 'proc/1').mkdir(parents=True)
    (root / 'proc/self').mkdir()
    (root / 'etc').mkdir()
    (root / 'etc/os-release').write_text('PRETTY_NAME="Host Linux"\n')
    (root / 'etc/hostname').write_text('host-name\n')
    for name, value in [('meminfo', ''), ('stat', ''), ('cpuinfo', ''), ('uptime', '100 20'), ('loadavg', '1 2 3')]:
        (root / 'proc' / name).write_text(value)
    device = root.stat().st_dev
    major_minor = mount_device or f'{os.major(device)}:{os.minor(device)}'
    for name, number in [('sda', major_minor), ('sdb', '255:254')]:
        path = root / 'sys/class/block' / name
        path.mkdir(parents=True)
        for key, value in [('size', '1000'), ('ro', '0'), ('dev', number)]:
            (path / key).write_text(value)
    (root / 'wrong').mkdir()
    (root / 'proc/1/mountinfo').write_text(f'1 0 {major_minor} {subvolume} / rw - {filesystem} /dev/sda rw\n2 0 255:254 / /wrong rw - ext4 /dev/sdb rw\n')
    (root / 'proc/self/mountinfo').write_text(f'3 0 {major_minor} {subvolume} {root} ro - {filesystem} /dev/sda ro\n')
    statvfs = Mock(return_value=SimpleNamespace(f_blocks=100, f_bfree=40, f_bavail=30, f_frsize=4096))
    monkeypatch.setattr('backend.system.inventory.os.statvfs', statvfs)
    result = SystemInventory(host_root=root).snapshot()
    assert result['mount_source'] == 'host'
    assert result['os']['name'] == 'Host Linux' and result['os']['hostname'] == 'host-name'
    assert result['mounts'][0]['total'] == 409600
    assert result['mounts'][1]['total'] is None
    assert 'refusing container filesystem statistics' in result['mounts'][1]['error']
    assert statvfs.call_args_list[0].args == (root,)
    assert statvfs.call_count == 1


def test_host_root_config_env_and_cli(monkeypatch):
    monkeypatch.setenv('BACKEND_SYSTEM_HOST_ROOT', '/host/root')
    assert DaemonSettings(_env_file=None).system_host_root == Path('/host/root')
    args = build_parser().parse_args(['--system-host-root', '/other/root'])
    assert DaemonSettings(_env_file=None, system_host_root=args.system_host_root).system_host_root == Path('/other/root')
    with pytest.raises(ValueError, match='absolute'):
        DaemonSettings(_env_file=None, system_host_root='relative')


def test_btrfs_mount_identity_uses_namespace_mount_table_and_subvolume_root():
    text = ('1 0 0:1 / / rw - overlay overlay rw\n'
            '2 1 0:42 /@root /host/root ro - btrfs /dev/sda2 ro\n'
            '3 2 0:42 /@mail /host/root/vmail ro - btrfs /dev/sda2 ro\n'
            '4 2 0:42 /@sea\\040file /host/root/sea\\040file ro - btrfs /dev/sda2 ro\n')
    assert mapped_mount_matches(text, Path('/host/root'), '0:42', '/@root', 'btrfs')
    assert mapped_mount_matches(text, Path('/host/root/vmail'), '0:42', '/@mail', 'btrfs')
    assert mapped_mount_matches(text, Path('/host/root/sea file'), '0:42', '/@sea file', 'btrfs')
    assert mapped_mount_matches(text, Path('/host/root/var/lib/docker/btrfs'), '0:42', '/@root/var/lib/docker/btrfs', 'btrfs')
    assert not mapped_mount_matches(text, Path('/host/root/vmail'), '0:42', '/@root/vmail', 'btrfs')
    assert not mapped_mount_matches(text, Path('/host/root/vmail'), '0:99', '/@mail', 'btrfs')
    assert not mapped_mount_matches(text, Path('/host/root'), '0:1', '/', 'overlay')
    assert not mapped_mount_matches(text, Path('/host/rooted'), '0:42', '/@root', 'btrfs')
    assert not mapped_mount_matches('', Path('/host/root'), '0:42', '/@root', 'btrfs')
