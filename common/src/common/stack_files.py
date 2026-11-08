from common.identity import APP_NAME, STORAGE_ID, SESSION_COOKIE, SESSION_SALT, SOURCE_KEY, MANIFEST_NAME, DEFINITIONS_DIRECTORY, TEMP_PREFIX, MANAGED_HEADER
"""Bounded, portable stack bundles; archives never create filesystem entries."""

import base64
import io
from pathlib import PurePosixPath
import stat
import tarfile
import zipfile

MAX_BUNDLE_BYTES = 4 * 1024 * 1024
MAX_FILES = 64
COMPOSE_FILES = ('compose.yaml', 'compose.yml', 'compose.json', 'docker-compose.yaml', 'docker-compose.yml', 'docker-compose.json')


def safe_path(name):
    if (not isinstance(name, str) or not name or len(name) > 512 or len(name.split('/')) > 16 or '\\' in name or '\0' in name
            or name.startswith('/') or any(part in {'', '.', '..'} for part in name.split('/'))
            or any(part.startswith(TEMP_PREFIX) or len(part.encode()) > 255 for part in name.split('/')) or ':' in name):
        raise ValueError('Bundle paths must be relative, unique and contain no traversal or reserved names')
    return str(PurePosixPath(name))


def validate_bundle(files, main_file, *, max_bytes=MAX_BUNDLE_BYTES, max_files=MAX_FILES):
    if not isinstance(files, dict) or not files or len(files) > max_files:
        raise ValueError(f'A stack bundle must contain 1–{max_files} files')
    if not 1 <= max_bytes <= MAX_BUNDLE_BYTES or not 1 <= max_files <= MAX_FILES:
        raise ValueError('Upload limits exceed the supported bundle budget')
    decoded, total = {}, 0
    for name, encoded in files.items():
        name = safe_path(name)
        if not isinstance(encoded, str) or len(encoded) > MAX_BUNDLE_BYTES * 2:
            raise ValueError('Invalid bundle encoding')
        try:
            data = base64.b64decode(encoded, validate=True)
        except (ValueError, TypeError):
            raise ValueError('Invalid bundle encoding') from None
        total += len(data)
        if total > max_bytes:
            raise ValueError(f'Expanded stack bundles exceed the {max_bytes}-byte limit')
        decoded[name] = data
    for name in decoded:
        if any(str(parent) in decoded for parent in PurePosixPath(name).parents if str(parent) != '.'):
            raise ValueError('A bundle path cannot be both a file and a directory')
    main_file = safe_path(main_file)
    if main_file not in decoded or PurePosixPath(main_file).suffix.lower() not in {'.yaml', '.yml', '.json'}:
        raise ValueError('Select an existing YAML or JSON Compose file in the bundle')
    return decoded


def upload_bundle(filename, data, main_file='', *, max_bytes=MAX_BUNDLE_BYTES, max_files=MAX_FILES):
    if len(data) > max_bytes:
        raise ValueError(f'Uploads exceed the {max_bytes}-byte limit')
    files = {}
    total = 0
    def archive_path(name):
        while name.startswith('./'):
            name = name[2:]
        return safe_path(name.rstrip('/'))
    def add(name, size, read):
        nonlocal total
        name = archive_path(name)
        if name in files or len(files) >= max_files or size > max_bytes or size < 0:
            raise ValueError('Archive contains duplicate paths, too many files or oversized entries')
        total += size
        if total > max_bytes:
            raise ValueError(f'Expanded stack bundles exceed the {max_bytes}-byte limit')
        value = read(size + 1)
        if len(value) != size:
            raise ValueError('Archive entry size does not match its content')
        files[name] = base64.b64encode(value).decode('ascii')
    try:
        if filename.lower().endswith('.zip'):
            with zipfile.ZipFile(io.BytesIO(data)) as archive:
                if len(archive.infolist()) > max_files * 2:
                    raise ValueError('Archive contains too many entries')
                for entry in archive.infolist():
                    archive_path(entry.filename)
                    kind = stat.S_IFMT(entry.external_attr >> 16)
                    if kind not in {0, stat.S_IFREG, stat.S_IFDIR} or entry.flag_bits & 1:
                        raise ValueError('Archive links, special files and encrypted entries are not supported')
                    if not entry.is_dir():
                        with archive.open(entry) as stream:
                            add(entry.filename, entry.file_size, stream.read)
        elif filename.lower().endswith(('.tar', '.tar.gz', '.tgz', '.tar.bz2', '.tar.xz')):
            with tarfile.open(fileobj=io.BytesIO(data), mode='r:*') as archive:
                for count, entry in enumerate(archive):
                    if count >= max_files * 2:
                        raise ValueError('Archive contains too many entries')
                    if entry.isdir() and entry.name in {'.', './'}:
                        continue
                    archive_path(entry.name)
                    if entry.isdir():
                        continue
                    if not entry.isfile() or entry.issparse():
                        raise ValueError('Archive links, special files and sparse files are not supported')
                    with archive.extractfile(entry) as stream:
                        add(entry.name, entry.size, stream.read)
        else:
            name = safe_path(filename)
            add(name, len(data), io.BytesIO(data).read)
    except (zipfile.BadZipFile, tarfile.TarError, OSError, EOFError):
        raise ValueError('Unable to read this archive') from None
    if not main_file:
        candidates = [name for name in COMPOSE_FILES if name in files]
        if not candidates:
            candidates = [name for name in files if PurePosixPath(name).name in COMPOSE_FILES]
        if not candidates and len(files) == 1:
            candidates = list(files)
        if len(candidates) != 1:
            raise ValueError('Specify the main Compose file path when the bundle has no unique default')
        main_file = candidates[0]
    validate_bundle(files, main_file, max_bytes=max_bytes, max_files=max_files)
    return {'files': files, 'main_file': main_file}
