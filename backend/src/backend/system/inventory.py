"""Read Linux resource counters without invoking external utilities."""

import os
import platform
import posixpath
import re
import stat
import threading
from pathlib import Path


def unescape_mount(value):
    return re.sub(r'\\([0-7]{3})', lambda match: chr(int(match[1], 8)), value)


def mapped_mount_matches(text, target, device, root, filesystem):
    """Compare mount identities across namespaces, including Btrfs subvolume roots."""
    candidates = []
    for line in text.splitlines():
        left, separator, right = line.partition(' - ')
        fields, details = left.split(), right.split()
        if not separator or len(fields) < 6 or len(details) < 2:
            continue
        mountpoint = Path(unescape_mount(fields[4]))
        if not target.is_relative_to(mountpoint):
            continue
        relative = target.relative_to(mountpoint).as_posix()
        mapped_root = posixpath.normpath(posixpath.join(unescape_mount(fields[3]), relative))
        candidates.append((len(mountpoint.parts), fields[2] == device and details[0] == filesystem
                           and mapped_root == posixpath.normpath(root)))
    if not candidates:
        return False
    deepest = max(depth for depth, _ in candidates)
    return all(matches for depth, matches in candidates if depth == deepest)


def memory_accounting(memory):
    """Disjoint physical-memory segments; MemAvailable is an overlapping estimate."""
    total = memory.get('MemTotal')
    if not total or any(key not in memory for key in ('MemFree', 'Buffers', 'Cached')):
        return None
    free, buffers = memory['MemFree'], memory['Buffers']
    cache = memory['Cached'] + memory.get('SReclaimable', 0)
    used = total - free - buffers - cache
    if min(used, free, buffers, cache) < 0:
        return None
    return {'total': total, 'used': used, 'buffers': buffers, 'cache': cache, 'free': free}


class SystemInventory:
    def __init__(self, proc=Path('/proc'), sys=Path('/sys'), host_root=None):
        self.host_root = Path(host_root) if host_root else None
        self.proc = self.host_root / 'proc' if self.host_root else proc
        self.sys = self.host_root / 'sys' if self.host_root else sys
        self.previous = {}
        self.lock = threading.Lock()

    def snapshot(self):
        errors = []
        def read(path):
            try:
                return path.read_text()
            except OSError as exc:
                errors.append(f'{path}: {exc}')
                return ''
        memory = {}
        for line in read(self.proc / 'meminfo').splitlines():
            key, _, value = line.partition(':')
            parts = value.split()
            if parts and parts[0].isdigit():
                memory[key] = int(parts[0]) * (1024 if len(parts) > 1 and parts[1] == 'kB' else 1)
        cpu = []
        with self.lock:
            for line in read(self.proc / 'stat').splitlines():
                parts = line.split()
                if not parts or not re.fullmatch(r'cpu\d*', parts[0]):
                    continue
                values = [int(value) for value in parts[1:9]]  # guest time is already included in user/nice.
                total, idle = sum(values), values[3] + (values[4] if len(values) > 4 else 0)
                previous = self.previous.get(parts[0])
                delta = total - previous[0] if previous else 0
                usage = round(100 * max(0, min(delta, delta - (idle - previous[1]))) / delta, 2) if delta > 0 else None
                cpu.append({'name': parts[0], 'usage': usage})
                self.previous[parts[0]] = (total, idle)
        cpu_info = read(self.proc / 'cpuinfo')
        model = next((line.split(':', 1)[1].strip() for line in cpu_info.splitlines()
                      if line.split(':', 1)[0].strip() in {'model name', 'Hardware', 'Processor'} and ':' in line), platform.machine())
        uptime = read(self.proc / 'uptime').split()
        load = read(self.proc / 'loadavg').split()
        devices = []
        try:
            paths = sorted((self.sys / 'class/block').iterdir())
        except OSError as exc:
            paths = []
            errors.append(f'Block devices: {exc}')
        for path in paths:
            if path.name.startswith(('loop', 'ram', 'zram')):
                continue
            size = read(path / 'size').strip()
            partition = (path / 'partition').exists()
            device = {'name': path.name, 'device': '/dev/' + path.name, 'major_minor': read(path / 'dev').strip(),
                      'type': 'partition' if partition else 'disk', 'parent': path.resolve().parent.name if partition else None,
                      'size': int(size) * 512 if size.isdigit() else None,
                      'read_only': read(path / 'ro').strip() == '1'}
            devices.append(device)
        known = {item['major_minor']: item['name'] for item in devices}
        mounts = []
        local_mountinfo = None
        unescape = unescape_mount
        mountinfo = self.proc / ('1/mountinfo' if self.host_root else 'self/mountinfo')
        for line in read(mountinfo).splitlines():
            left, separator, right = line.partition(' - ')
            parts, details = left.split(), right.split()
            if not separator or len(parts) < 6 or len(details) < 2:
                continue
            major_minor = parts[2]
            if major_minor not in known and not (self.sys / 'dev/block' / major_minor).exists():
                try:
                    source_path = Path(unescape(details[1]))
                    if self.host_root:
                        source_path = self.host_root / str(source_path).lstrip('/')
                    source = os.stat(source_path)
                    if not stat.S_ISBLK(source.st_mode):
                        continue
                    major_minor = f'{os.major(source.st_rdev)}:{os.minor(source.st_rdev)}'
                except OSError:
                    continue
            mountpoint = unescape(parts[4])
            mount = {'device': unescape(details[1]), 'block_device': known.get(major_minor, major_minor),
                     'mountpoint': mountpoint, 'root': unescape(parts[3]), 'filesystem': details[0], 'options': parts[5],
                     'total': None, 'used': None, 'available': None, 'usage': None}
            try:
                target = Path(mountpoint)
                if self.host_root:
                    target = self.host_root / mountpoint.lstrip('/')
                    if not target.resolve().is_relative_to(self.host_root.resolve()):
                        raise OSError('Host mount path escapes the configured host root')
                    identity = os.stat(target).st_dev
                    if f'{os.major(identity)}:{os.minor(identity)}' != parts[2]:
                        # Btrfs stat() can report a subvolume-specific anonymous device
                        # different from the superblock device in mountinfo.
                        if local_mountinfo is None:
                            local_mountinfo = read(self.proc / 'self/mountinfo')
                        if not mapped_mount_matches(local_mountinfo, target.resolve(), parts[2], unescape(parts[3]), details[0]):
                            raise OSError('Host mount is not visible at the mapped path; refusing container filesystem statistics '
                                          f'(path={target}, mount device={parts[2]}, stat device={os.major(identity)}:{os.minor(identity)})')
                stats = os.statvfs(target)
                mount['total'] = stats.f_blocks * stats.f_frsize
                mount['used'] = (stats.f_blocks - stats.f_bfree) * stats.f_frsize
                mount['available'] = stats.f_bavail * stats.f_frsize
                denominator = mount['used'] + mount['available']
                mount['usage'] = round(mount['used'] / denominator * 100, 2) if denominator else 0
            except OSError as exc:
                mount['error'] = str(exc)
                errors.append(f'Mount {mountpoint}: {exc}')
            mounts.append(mount)
        if self.host_root:
            release = {}
            release_path = self.host_root / 'etc/os-release'
            if release_path.resolve().is_relative_to(self.host_root.resolve()):
                for line in read(release_path).splitlines():
                    key, _, value = line.partition('=')
                    release[key] = value.strip('"\'')
            hostname = read(self.host_root / 'etc/hostname').strip()
        else:
            try:
                release = platform.freedesktop_os_release()
            except OSError:
                release = {}
            hostname = platform.node()
        return {'os': {'hostname': hostname, 'name': release.get('PRETTY_NAME', platform.system()),
                       'kernel': platform.release(), 'architecture': platform.machine()},
                'uptime_seconds': float(uptime[0]) if uptime else None, 'load': load[:3],
                'cpu_model': model, 'cpus': cpu, 'memory': memory, 'memory_accounting': memory_accounting(memory), 'devices': devices,
                'mounts': sorted(mounts, key=lambda item: (item['mountpoint'], item['device'])), 'errors': errors,
                'mount_source': 'host' if self.host_root else 'daemon',
                'note': ('Resources and mount tables are read from the configured host root. ' if self.host_root else 'Resources describe the daemon’s Linux namespace. In containers, OS and mounts may differ from the host. ') + 'CPU usage needs two samples; mount usage includes only kernel block devices. Bind mounts may share the same filesystem; sizes must not be summed. Cgroup quotas are not host capacity.'}
