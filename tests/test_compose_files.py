import os
import stat

import pytest

from backend.docker.compose.files import read_source, save_source, validate_source
from backend.daemon_config import DaemonSettings


@pytest.fixture
def source(tmp_path):
    path = tmp_path / 'compose.yaml'
    path.write_text('services:\n  web:\n    image: web:1\n')
    path.chmod(0o640)
    stack = {'name': 'demo', 'kind': 'compose', 'config_files': [str(path)]}
    settings = DaemonSettings(_env_file=None, docker_access='rw', compose_host_read_only=False,
                              compose_root=tmp_path / 'managed', compose_allowed_roots=[tmp_path], stack_database=tmp_path / 'stacks.sqlite3')
    return path, stack, settings


def test_compose_save_is_previewed_atomic_and_preserves_permissions(source):
    path, stack, settings = source
    original = path.read_bytes(); info = path.stat()
    opened = read_source(stack, str(path), settings)
    assert opened['writable'] and opened['mode'] == '0640'
    new = opened['content'].replace('web:1', 'web:2')
    preview = save_source(stack, str(path), new, opened['hash'], settings)
    assert 'web:2' in preview['diff'] and path.read_bytes() == original
    result = save_source(stack, str(path), new, opened['hash'], settings, True)
    assert result['applied'] and 'web:2' in path.read_text()
    assert stat.S_IMODE(path.stat().st_mode) == 0o640
    assert (path.stat().st_uid, path.stat().st_gid) == (info.st_uid, info.st_gid)
    assert not list(path.parent.glob('.lsdfm-compose-*'))


def test_source_change_and_readonly_mode_prevent_overwrite(source):
    path, stack, settings = source
    opened = read_source(stack, str(path), settings)
    path.write_text('services: {new: {image: other}}')
    with pytest.raises(ValueError, match='changed on disk'):
        save_source(stack, str(path), opened['content'], opened['hash'], settings, True)
    path.chmod(0o440)
    assert not read_source(stack, str(path), settings)['writable']
    with pytest.raises(ValueError, match='write permissions'):
        save_source(stack, str(path), opened['content'], read_source(stack, str(path), settings)['hash'], settings, True)


def test_editor_rejects_undeclared_sources_and_keeps_crlf(source):
    path, stack, settings = source
    other = path.parent / 'other.yaml'; other.write_text('services: {}')
    with pytest.raises(ValueError, match='not a discovered'):
        read_source(stack, str(other), settings)
    path.write_bytes(b'services:\r\n  web: {image: web:1}\r\n')
    opened = read_source(stack, str(path), settings)
    save_source(stack, str(path), opened['content'].replace('\r\n', '\n').replace('web:1', 'web:2'), opened['hash'], settings, True)
    assert path.read_bytes() == b'services:\r\n  web: {image: web:2}\r\n'
    settings.docker_access = 'readonly'
    assert not read_source(stack, str(path), settings)['writable']


def test_invalid_source_syntax_does_not_write_or_echo_secret(source):
    path, stack, settings = source
    opened = read_source(stack, str(path), settings)
    with pytest.raises(ValueError, match='Invalid YAML syntax') as error:
        save_source(stack, str(path), 'services: [hidden-secret', opened['hash'], settings, True)
    assert 'hidden-secret' not in str(error.value)
    assert path.read_text() == opened['content']
    with pytest.raises(ValueError):
        validate_source('services: {}\nservices: {}', '.yaml')
    assert validate_source('services: {web: {ports: !reset []}}', '.yaml')['warnings']
