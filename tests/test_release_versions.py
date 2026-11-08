"""Coordinated releases preserve standalone metadata and roll back lock failures."""
from importlib import import_module
from importlib.metadata import version
from importlib.util import spec_from_file_location, module_from_spec
from pathlib import Path
from types import SimpleNamespace
import shutil
import tomllib

import pytest
from common.metadata import package_version

ROOT = Path(__file__).resolve().parents[1]
SPEC = spec_from_file_location('release_version_tool', ROOT/'scripts/version.py')
release = module_from_spec(SPEC)
SPEC.loader.exec_module(release)


@pytest.fixture
def workspace(tmp_path):
    for file in ('pyproject.toml', 'uv.lock', '.python-version', 'common/pyproject.toml', 'frontend/pyproject.toml', 'backend/pyproject.toml'):
        target = tmp_path/file
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(ROOT/file, target)
    return tmp_path


def snapshot(root):
    return {name: (root/name).read_bytes() for name in ('pyproject.toml', 'uv.lock', 'common/pyproject.toml', 'frontend/pyproject.toml', 'backend/pyproject.toml')}


def successful_lock(command, *, cwd, **kwargs):
    assert command[:2] == ['uv', 'lock']
    text = 'version = 1\n'
    for project in release.projects(cwd).values():
        text += f'\n[[package]]\nname = "{project["name"]}"\nversion = "{project["version"]}"\n'
    (cwd/'uv.lock').write_text(text)
    return SimpleNamespace(returncode=0)


@pytest.mark.parametrize('component', ['common', 'frontend', 'backend'])
def test_runtime_versions_come_from_installed_distributions(component):
    package = import_module(component)
    metadata = release.read(ROOT/component/'pyproject.toml')
    assert package.__version__ == version(metadata['project']['name'])
    assert package_version(component) == package.__version__
    assert not (ROOT/component/'src'/component/'pyproject.toml').exists()


def test_checked_in_release_metadata_and_lock_agree():
    assert release.consistency_errors(ROOT) == []


@pytest.mark.parametrize('part,expected', [('major', '1.0.0'), ('minor', '0.2.0'), ('patch', '0.1.1')])
def test_version_increment_rules(part, expected):
    assert release.bump_version('0.1.0', part) == expected
    assert release.bump_version('0.1.0.dev3', part) == expected


def test_set_and_bump_update_all_metadata_and_workspace_pins(workspace):
    release.apply_version(workspace, '0.2.0.dev1', runner=successful_lock)
    assert release.consistency_errors(workspace) == []
    common_name = release.read(workspace/'common/pyproject.toml')['project']['name']
    for component in ('frontend', 'backend'):
        metadata = release.read(workspace/component/'pyproject.toml')
        assert common_name+'==0.2.0.dev1' in metadata['project']['dependencies']
    assert not (workspace/'.version-bump.lock').exists()


def test_dry_run_does_not_change_release_files_or_run_uv(workspace):
    before = snapshot(workspace)
    def forbidden(*args, **kwargs):
        pytest.fail('dry-run must not resolve dependencies')
    release.apply_version(workspace, '1.0.0', dry_run=True, runner=forbidden)
    assert snapshot(workspace) == before
    assert not (workspace/'.version-bump.lock').exists()


@pytest.mark.parametrize('failure', ['exit', 'exception', 'inconsistent-lock', 'interrupt'])
def test_failed_resolution_restores_metadata_and_lock(workspace, failure):
    before = snapshot(workspace)
    def broken(command, *, cwd, **kwargs):
        if failure == 'inconsistent-lock':
            return SimpleNamespace(returncode=0)
        (cwd/'uv.lock').write_text('partial write')
        if failure == 'exception':
            raise OSError('resolver unavailable')
        if failure == 'interrupt':
            raise KeyboardInterrupt()
        return SimpleNamespace(returncode=1)
    with pytest.raises((ValueError, OSError, KeyboardInterrupt)):
        release.apply_version(workspace, '1.0.0', runner=broken)
    assert snapshot(workspace) == before
    assert not (workspace/'.version-bump.lock').exists()


def test_an_existing_update_lock_prevents_concurrent_writes(workspace):
    before = snapshot(workspace)
    (workspace/'.version-bump.lock').write_text('another update')
    with pytest.raises(ValueError, match='lock exists'):
        release.apply_version(workspace, '1.0.0', runner=successful_lock)
    assert snapshot(workspace) == before
    assert (workspace/'.version-bump.lock').read_text() == 'another update'


def test_unrelated_versions_and_metadata_strings_are_not_rewritten():
    text = '''[project]
name = "example"
version = "0.1.0" # release
requires-python = ">=3.12"
dependencies = [
  "local-common[settings]==0.1.0",
  "third-party==8.4.2",
]
[tool.application]
name = "local-common==0.1.0"
'''
    updated = release.updated_metadata(text, '0.2.0', {'local-common'})
    assert 'version = "0.2.0" # release' in updated
    assert 'local-common[settings]==0.2.0' in updated
    assert 'third-party==8.4.2' in updated
    assert 'name = "local-common==0.1.0"' in updated
    tomllib.loads(updated)


def test_actual_uv_lock_can_update_release_offline_in_an_isolated_copy(workspace):
    if not shutil.which('uv'):
        pytest.skip('uv is required for workspace lock validation')
    current = release.release_version(workspace)
    release.apply_version(workspace, release.bump_version(current, 'patch'), offline=True)
    assert release.consistency_errors(workspace) == []
