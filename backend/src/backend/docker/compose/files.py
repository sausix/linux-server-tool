from common.identity import APP_NAME, STORAGE_ID, SESSION_COOKIE, SESSION_SALT, SOURCE_KEY, MANIFEST_NAME, DEFINITIONS_DIRECTORY, TEMP_PREFIX, MANAGED_HEADER
"""Explicit, permission-checked edits of discovered Compose source files."""

import difflib
import fcntl
import hashlib
import os
from pathlib import Path
import stat
import uuid

import yaml

from backend.docker.compose.definitions import MAX_DEFINITION_BYTES
from backend.docker.compose.metadata import COMPOSE_NAMES, local_compose_path, parse_document, validate_metadata, compose_path_diagnostic


def digest(content):
    return hashlib.sha256(content).hexdigest()


def discover_paths(stack, settings):
    paths = list(stack.get('config_files') or [])
    directory = stack.get('working_directory')
    if paths:
        return [str(Path(directory) / path) if directory and not Path(path).is_absolute() else path for path in paths]
    directories = ([Path(directory)] if directory else []) + [path / stack['name'] for path in settings.compose_search_directories]
    for directory in directories:
        for name in COMPOSE_NAMES:
            path = directory / name
            try:
                if local_compose_path(path, settings).is_file():
                    return [str(path)]
            except (OSError, ValueError):
                continue
    return []


def permitted(info, write=False, execute=False):
    uid, groups = os.geteuid(), {*os.getgroups(), os.getegid()}
    owner_bit, group_bit, other_bit = (stat.S_IXUSR, stat.S_IXGRP, stat.S_IXOTH) if execute else (stat.S_IWUSR, stat.S_IWGRP, stat.S_IWOTH) if write else (stat.S_IRUSR, stat.S_IRGRP, stat.S_IROTH)
    bit = owner_bit if uid == info.st_uid else group_bit if info.st_gid in groups else other_bit
    return bool(info.st_mode & bit)


def can_preserve_owner(info, parent):
    groups = {*os.getgroups(), os.getegid()}
    created_group = parent.st_gid if parent.st_mode & stat.S_ISGID else os.getegid()
    if info.st_uid == os.geteuid() and (info.st_gid == created_group or info.st_gid in groups):
        return True
    try:
        for line in Path('/proc/self/status').read_text().splitlines():
            if line.startswith('CapEff:'):
                return bool(int(line.split()[1], 16) & 1)  # CAP_CHOWN
    except (OSError, ValueError):
        pass
    return False


def source_path(stack, requested, settings):
    if stack.get('base_kind', stack['kind']) != 'compose':
        raise ValueError('Compose source editing is only available for Compose stacks')
    paths = discover_paths(stack, settings)
    if requested not in paths:
        raise ValueError('This file is not a discovered Compose source for this stack')
    return local_compose_path(requested, settings)


def read_source(stack, requested, settings):
    try:
        return _read_source(stack, requested, settings)
    except OSError as exc:
        diagnostic = compose_path_diagnostic(requested, settings)
        if diagnostic:
            raise ValueError(diagnostic) from exc
        raise


def _read_source(stack, requested, settings):
    path = source_path(stack, requested, settings)
    with os.fdopen(os.open(path, os.O_RDONLY | os.O_NONBLOCK | os.O_NOFOLLOW), 'rb') as file:
        info = os.fstat(file.fileno())
        if not stat.S_ISREG(info.st_mode):
            raise ValueError('Source must be a regular file readable by the daemon identity')
        if not permitted(info):
            raise ValueError(compose_path_diagnostic(requested, settings) or f'No access to {requested}')
        raw = file.read(MAX_DEFINITION_BYTES + 1)
    if len(raw) > MAX_DEFINITION_BYTES:
        raise ValueError('Compose files are limited to 1 MiB')
    parent = path.parent.stat()
    ownership = can_preserve_owner(info, parent)
    host_read_only = settings.compose_host_read_only and not path.is_relative_to(settings.compose_root.resolve())
    writable = (not host_read_only and settings.docker_access == 'readwrite' and permitted(info, True) and ownership
                and permitted(parent, True) and permitted(parent, execute=True) and os.access(path, os.W_OK, effective_ids=True)
                and os.access(path.parent, os.W_OK | os.X_OK, effective_ids=True))
    return {'path': requested, 'content': raw.decode('utf-8'), 'hash': digest(raw),
            'uid': info.st_uid, 'gid': info.st_gid, 'mode': f'{stat.S_IMODE(info.st_mode):04o}',
            'writable': writable, 'write_reason': None if writable else 'External Compose sources are read-only (COMPOSE_HOST_READ_ONLY)' if host_read_only else 'The daemon cannot preserve the original owner/group during atomic replacement' if not ownership else
            'Docker write access, matching owner/group write permissions, a writable parent directory and a writable mount/systemd ReadWritePaths are required'}


def validate_source(text, suffix):
    if not isinstance(text, str) or len(text.encode()) > MAX_DEFINITION_BYTES:
        raise ValueError('Compose text must be UTF-8 and at most 1 MiB')
    # Syntax validation accepts Compose-specific tags without constructing them.
    # Full Compose interpretation is performed by the Compose CLI on deployment.
    if suffix == '.json':
        return validate_metadata(parse_document(text, suffix))
    try:
        node = yaml.compose(text, Loader=yaml.SafeLoader)
    except yaml.YAMLError as exc:
        mark = getattr(exc, 'problem_mark', None)
        location = f' at line {mark.line + 1}, column {mark.column + 1}' if mark else ''
        raise ValueError('Invalid YAML syntax' + location) from exc
    if not isinstance(node, yaml.MappingNode):
        raise ValueError('A Compose file must contain an object')
    try:
        return validate_metadata(parse_document(text, suffix))
    except yaml.constructor.ConstructorError as exc:
        problem = getattr(exc, 'problem', '') or ''
        if 'could not determine a constructor' in problem and any(tag in problem for tag in ("'!reset'", "'!override'")):
            return {'present': False, 'eligible': False, 'invalid_fields': [],
                    'warnings': ['Compose-specific syntax is preserved; metadata assessment cannot resolve it.']}
        raise ValueError('Invalid Compose YAML keys or unsupported tags') from None
    except (ValueError, yaml.YAMLError):
        raise ValueError('Compose YAML contains invalid, recursive or oversized data') from None


def save_source(stack, requested, text, expected_hash, settings, apply=False):
    if type(apply) is not bool:
        raise ValueError('apply must be a boolean')
    current = read_source(stack, requested, settings)
    if not current['writable']:
        raise ValueError(current['write_reason'])
    if current['hash'] != expected_hash:
        raise ValueError('The source file changed on disk; the draft is retained. Review and rebase before saving.')
    path = source_path(stack, requested, settings)
    assessment = validate_source(text, path.suffix.lower())
    # Browser textarea normalization must not silently change CRLF files to LF.
    if '\r\n' in current['content'] and '\n' not in current['content'].replace('\r\n', ''):
        text = text.replace('\r\n', '\n').replace('\n', '\r\n')
    raw = text.encode('utf-8')
    result = {'path': requested, 'hash': current['hash'], 'new_hash': digest(raw), 'assessment': assessment,
              'diff': ''.join(difflib.unified_diff(current['content'].splitlines(keepends=True), text.splitlines(keepends=True), fromfile=requested, tofile='draft')), 'applied': apply}
    if not apply:
        return result
    directory = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    temporary = TEMP_PREFIX + 'compose-' + uuid.uuid4().hex
    try:
        descriptor = os.open(path.name, os.O_RDONLY | os.O_NONBLOCK | os.O_NOFOLLOW, dir_fd=directory)
        with os.fdopen(descriptor, 'rb') as source:
            fcntl.flock(source.fileno(), fcntl.LOCK_EX)
            info = os.fstat(source.fileno())
            if (not stat.S_ISREG(info.st_mode) or not permitted(info, True)
                    or digest(source.read(MAX_DEFINITION_BYTES + 1)) != expected_hash):
                raise ValueError('Source or write permissions changed after preview; draft retained')
            output = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600, dir_fd=directory)
            with os.fdopen(output, 'wb') as target:
                target_info = os.fstat(target.fileno())
                if (target_info.st_uid, target_info.st_gid) != (info.st_uid, info.st_gid):
                    os.fchown(target.fileno(), info.st_uid, info.st_gid)
                os.fchmod(target.fileno(), stat.S_IMODE(info.st_mode))
                for attribute in os.listxattr(source.fileno()):
                    os.setxattr(target.fileno(), attribute, os.getxattr(source.fileno(), attribute))
                target.write(raw); target.flush(); os.fsync(target.fileno())
            latest = os.stat(path.name, dir_fd=directory, follow_symlinks=False)
            if (latest.st_dev, latest.st_ino, latest.st_mtime_ns, latest.st_size) != (info.st_dev, info.st_ino, info.st_mtime_ns, info.st_size):
                raise ValueError('Source changed during save; draft retained')
            os.replace(temporary, path.name, src_dir_fd=directory, dst_dir_fd=directory)
            os.fsync(directory)
    finally:
        try:
            os.unlink(temporary, dir_fd=directory)
        except FileNotFoundError:
            pass
        os.close(directory)
    return result
