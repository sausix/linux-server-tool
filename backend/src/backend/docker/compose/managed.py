from common.identity import APP_NAME, STORAGE_ID, SESSION_COOKIE, SESSION_SALT, SOURCE_KEY, MANIFEST_NAME, DEFINITIONS_DIRECTORY, TEMP_PREFIX, MANAGED_HEADER
"""Dedicated stack workspaces, safe publication and discovery of saved projects."""

import ctypes
import hashlib
import json
import logging
import os
from pathlib import Path
import shutil
import uuid

from common.stack_files import COMPOSE_FILES, validate_bundle
from backend.docker.compose.files import validate_source
from backend.docker.compose.definitions import validate_definition


def workspace(settings, name):
    validate_definition(name, {'services': {}})
    return settings.compose_root / name


def projects(settings):
    root = settings.compose_root
    if not root.is_dir():
        return []
    result = []
    try:
        directories = sorted(root.iterdir())
    except OSError as exc:
        logging.getLogger(__name__).warning('Managed stack root cannot be read: %s', exc)
        return []
    for directory in directories:
        if directory.is_symlink() or not directory.is_dir() or directory.name.startswith('.'):
            continue
        try:
            workspace(settings, directory.name)
            manifest = directory / MANIFEST_NAME
            if manifest.is_file() and manifest.stat().st_size <= 4096:
                main = json.loads(manifest.read_text())['main_file']
                from common.stack_files import safe_path
                main = safe_path(main)
                paths = [directory / main]
            else:
                paths = [directory / name for name in COMPOSE_FILES if (directory / name).is_file()][:1]
            paths = [path for path in paths if path.is_file() and not path.is_symlink() and path.resolve().is_relative_to(directory.resolve())]
            if paths:
                result.append({'name': directory.name, 'config_files': [str(path) for path in paths],
                               'working_directory': str(paths[0].parent)})
        except (OSError, ValueError, KeyError, TypeError):
            continue
    return result


def create(settings, name, files, main_file, *, apply=False, expected_hash=None):
    target = workspace(settings, name)
    if os.path.lexists(target):
        raise ValueError('The stack directory already exists; choose a free stack name')
    decoded = validate_bundle(files, main_file)
    try:
        source = decoded[main_file].decode('utf-8')
    except UnicodeDecodeError:
        raise ValueError('The Compose definition must be UTF-8 text') from None
    assessment = validate_source(source, Path(main_file).suffix.lower())
    manifest = [{'path': path, 'size': len(data), 'sha256': hashlib.sha256(data).hexdigest()}
                for path, data in sorted(decoded.items())]
    fingerprint = hashlib.sha256(json.dumps([str(target), main_file, manifest], sort_keys=True).encode()).hexdigest()
    result = {'name': name, 'directory': str(target), 'main_file': main_file, 'files': manifest,
              'hash': fingerprint, 'assessment': assessment, 'applied': False}
    if not apply:
        return result
    if fingerprint != expected_hash:
        raise ValueError('Bundle changed after preview; review again')
    settings.compose_root.mkdir(parents=True, exist_ok=True, mode=0o750)
    root_fd = os.open(settings.compose_root, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    temporary = TEMP_PREFIX + 'new-' + uuid.uuid4().hex
    stage = settings.compose_root / temporary
    try:
        os.mkdir(temporary, mode=0o700, dir_fd=root_fd)
        for path, data in decoded.items():
            output = stage / path
            output.parent.mkdir(parents=True, exist_ok=True, mode=0o750)
            fd = os.open(output, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
            with os.fdopen(fd, 'wb') as stream:
                stream.write(data); stream.flush(); os.fsync(stream.fileno())
        metadata = stage / MANIFEST_NAME
        with metadata.open('x', encoding='utf-8') as stream:
            os.chmod(metadata, 0o600)
            json.dump({'main_file': main_file}, stream); stream.flush(); os.fsync(stream.fileno())
        os.chmod(stage, 0o750)
        for parent, _, _ in os.walk(stage, topdown=False):
            descriptor = os.open(parent, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
            try:
                os.fsync(descriptor)
            finally:
                os.close(descriptor)
        rename = getattr(ctypes.CDLL(None, use_errno=True), 'renameat2', None)
        if rename is None:
            raise ValueError('Atomic stack creation requires Linux renameat2 support')
        rename.argtypes = [ctypes.c_int, ctypes.c_char_p, ctypes.c_int, ctypes.c_char_p, ctypes.c_uint]
        rename.restype = ctypes.c_int
        if rename(root_fd, temporary.encode(), root_fd, name.encode(), 1) != 0:
            error = ctypes.get_errno()
            raise OSError(error, os.strerror(error))
        os.fsync(root_fd)
        result['applied'] = True
        return result
    finally:
        shutil.rmtree(stage, ignore_errors=True)
        os.close(root_fd)
