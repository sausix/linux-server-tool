"""Workspace resolution must use local members without changing wheel metadata."""
from pathlib import Path
import tomllib

ROOT = Path(__file__).resolve().parents[1]


def read(path):
    return tomllib.loads(path.read_text())


def test_workspace_members_and_common_dependency_source():
    config = read(ROOT/'pyproject.toml')
    assert config['tool']['uv']['package'] is False
    members = config['tool']['uv']['workspace']['members']
    assert set(members) == {'common', 'frontend', 'backend'}
    common = read(ROOT/'common/pyproject.toml')['project']
    assert config['tool']['uv']['sources'][common['name']] == {'workspace': True}
    for member in ('frontend', 'backend'):
        config = read(ROOT/member/'pyproject.toml')
        assert common['name'] + '==' + common['version'] in config['project']['dependencies']
        assert not any('file:' in dependency for dependency in config['project']['dependencies'])


def test_lockfile_keeps_common_as_local_editable_and_has_all_members():
    lock = read(ROOT/'uv.lock')
    packages = {item['name']: item for item in lock['package']}
    identities = {directory: read(ROOT/directory/'pyproject.toml')['project']['name'] for directory in ('common', 'frontend', 'backend')}
    for directory, name in identities.items():
        assert packages[name]['source'] == {'editable': directory}
    for directory in ('frontend', 'backend'):
        dependency = next(item for item in packages[identities[directory]]['metadata']['requires-dist'] if item['name'] == identities['common'])
        assert dependency['editable'] == 'common'


def test_dev_tools_and_python_default_are_explicit_without_test_path_overrides():
    config = read(ROOT/'pyproject.toml')
    assert (ROOT/'.python-version').read_text().strip() == '3.14'
    dependencies = config['dependency-groups']['dev']
    for name in ('pytest', 'httpx', 'websockets', 'cryptography', 'cbor2'):
        assert any(item.startswith(name + '>=') for item in dependencies)
    assert 'pythonpath' not in config['tool']['pytest']['ini_options']
