from common.identity import APP_NAME, STORAGE_ID, SESSION_COOKIE, SESSION_SALT, SOURCE_KEY, MANIFEST_NAME, DEFINITIONS_DIRECTORY, TEMP_PREFIX, MANAGED_HEADER
"""Preview-first address edits restricted to existing application-owned files."""

import difflib
import fcntl
import hashlib
import ipaddress
import os
from pathlib import Path
import stat
import tempfile

from backend.network.configuration import declarations
from backend.network.management import INTERFACE_PATTERN, NetworkManagementError


def change(interface, address, action, networkd_directory, interfaces_file, *, apply=False, expected_hash=None):
    if not isinstance(interface, str) or not INTERFACE_PATTERN.fullmatch(interface):
        raise ValueError('Invalid interface name')
    if action not in {'persist', 'remove-config'}:
        raise ValueError('Invalid configuration action')
    if '/' not in address:
        raise ValueError('An address prefix is required')
    parsed = ipaddress.ip_interface(address)
    address = str(parsed)
    config = declarations([{'name': interface}], networkd_directory, interfaces_file)[interface]
    sources = set(config['sources'])
    if len(sources) != 1:
        raise NetworkManagementError('A single unambiguous configuration file is required')
    target = Path(next(iter(sources)))
    networkd = target == networkd_directory / f'00-{STORAGE_ID}-{interface}.network'
    legacy = target == interfaces_file.parent / 'interfaces.d' / f'90-{STORAGE_ID}-{interface}'
    if not (networkd or legacy):
        raise NetworkManagementError('Administrator-managed configuration cannot be edited here. Only application-owned files can be changed.')
    descriptor = os.open(target, os.O_RDONLY | os.O_NOFOLLOW)
    with os.fdopen(descriptor, 'rb') as stream:
        fcntl.flock(stream, fcntl.LOCK_EX)
        metadata = os.fstat(stream.fileno())
        if not stat.S_ISREG(metadata.st_mode) or metadata.st_size > 1024 * 1024:
            raise ValueError('Configuration must be a regular file smaller than 1 MiB')
        original = stream.read()
        content = original.decode('utf-8')
        if not content.startswith(MANAGED_HEADER + '\n'):
            raise ValueError('Configuration is not marked as managed by application')
        if networkd and [line.strip() for line in content.splitlines() if line.strip().startswith('Name=')] != [f'Name={interface}']:
            raise ValueError('Configuration must match this interface exclusively')
        digest = hashlib.sha256(original).hexdigest()
        if apply and expected_hash != digest:
            raise ValueError('Configuration changed; review the change again')
        lines = content.splitlines(keepends=True)
        output, section, insertion, found = [], '', None, False
        for line in lines:
            stripped = line.strip()
            if networkd:
                if stripped.startswith('['):
                    section = stripped
                if section == '[Network]':
                    insertion = len(output) + 1
                candidate = stripped.partition('=')[2] if stripped.startswith('Address=') else None
                if candidate is not None and section != '[Network]':
                    raise ValueError('Address sections require manual configuration review')
            else:
                words = stripped.split()
                if words[:1] == ['iface']:
                    if len(words) < 4 or words[1] != interface:
                        raise ValueError('Configuration contains another interface')
                    section = words[2]
                if section == ('inet' if parsed.version == 4 else 'inet6'):
                    insertion = len(output) + 1
                candidate = words[1] if len(words) == 2 and words[0] == 'address' else None
            if candidate is not None:
                try:
                    matches = ipaddress.ip_interface(candidate) == parsed
                except ValueError:
                    matches = False
                if matches:
                    found = True
                    if action == 'remove-config':
                        continue
            output.append(line)
        if action == 'persist':
            if found:
                raise ValueError('Address is already configured')
            if insertion is None:
                raise ValueError('No matching address section; configure the address family first')
            output.insert(insertion, f'Address={address}\n' if networkd else f'    address {address}\n')
        elif not found:
            raise ValueError('Address is no longer configured')
        updated = ''.join(output)
        result = {'target': str(target), 'content_hash': digest, 'applied': False,
                  'diff': ''.join(difflib.unified_diff(content.splitlines(True), updated.splitlines(True),
                                                      fromfile=str(target), tofile=str(target)))}
        if apply:
            current = os.stat(target, follow_symlinks=False)
            if (current.st_ino, current.st_dev, current.st_mtime_ns, current.st_size) != (metadata.st_ino, metadata.st_dev, metadata.st_mtime_ns, metadata.st_size):
                raise ValueError('Configuration changed; review again')
            fd, temporary = tempfile.mkstemp(prefix=TEMP_PREFIX + 'address-', dir=target.parent)
            try:
                with os.fdopen(fd, 'wb') as replacement:
                    replacement.write(updated.encode('utf-8'))
                    replacement.flush()
                    os.fchown(replacement.fileno(), metadata.st_uid, metadata.st_gid)
                    os.fchmod(replacement.fileno(), stat.S_IMODE(metadata.st_mode))
                    os.fsync(replacement.fileno())
                os.replace(temporary, target)
            finally:
                if os.path.exists(temporary):
                    os.unlink(temporary)
            result['applied'] = True
        return result
