from copy import deepcopy
from unittest.mock import Mock

import pytest

from backend.docker.compose.models import ComposeModels
from backend.daemon_config import DaemonSettings
from backend.docker.compose.tree import document
from backend.docker.compose.control import StackControl
from backend.docker.compose.definitions import StackStore


def model(tmp_path):
    settings = DaemonSettings(_env_file=None, docker_access='rw', compose_root=tmp_path / 'managed',
                              compose_allowed_roots=[tmp_path], stack_database=tmp_path / 'state.sqlite3')
    registry = ComposeModels(settings, StackStore(settings.stack_database))
    source = {'services': {'web': {'image': 'web:1', 'environment': {'DROP': 'yes', 'KEEP': 'yes'}}}}
    origin = {'type': 'file', 'files': [str(tmp_path / 'compose.yaml')], 'directory': str(tmp_path), 'available': True}
    registry.update({'app': {'document': source, 'origin': origin}}, [{'name': 'app', 'kind': 'compose', 'running': 1}])
    assert registry.tree('app') is None
    registry.change_tree('app', {'action': 'initialize', 'expected_hash': registry.summary('app')['definition_hash'], 'apply': True})
    return registry, source, origin


def change(registry, action, node, **kwargs):
    return registry.change_tree('app', {'action': action, 'node': node,
                                      'expected_revision': registry.tree('app')['revision'], **kwargs})


def test_readonly_origin_layers_are_immutable_previewed_and_persistent(tmp_path):
    registry, source, origin = model(tmp_path)
    initial = deepcopy(registry.tree('app'))
    edited = deepcopy(source)
    edited['services']['web']['image'] = 'web:2'
    edited['services']['web']['environment'] = {'NULL': None}
    preview = change(registry, 'add', initial['active'], definition=edited, label='Upgrade')
    assert not preview['applied'] and 'web:2' in preview['diff']
    assert registry.tree('app') == initial
    change(registry, 'add', initial['active'], definition=edited, label='Upgrade', apply=True)
    tree = registry.tree('app')
    child = tree['nodes'][-1]['id']
    assert tree['active'] == initial['active']
    change(registry, 'activate', child, apply=True)
    tree = registry.tree('app')
    assert child != tree['running'] == initial['active']
    assert document(tree, child) == edited
    assert tree['nodes'][-1]['parent'] == initial['active']
    assert 'definition' not in tree['nodes'][-1] and tree['nodes'][-1]['patch']
    assert registry.details('app')['definition'] == edited
    # The actual file model remains the origin, not a copied/replaced file.
    assert registry.inputs['app']['document'] == source
    updated_source = deepcopy(source); updated_source['services']['web']['image'] = 'web:3'
    registry.update({'app': {'document': updated_source, 'origin': origin}}, [{'name': 'app', 'kind': 'compose', 'running': 1}])
    assert registry.details('app')['definition'] == edited
    assert registry.tree('app')['source'] != initial['source']
    assert document(registry.tree('app'), initial['source']) == source
    assert not registry.node_info('app', initial['source'])['writable']
    # Baseline changes only after a successful reviewed operation.
    registry.mark_applied('app', edited, child)
    assert registry.tree('app')['running'] == child
    restored = ComposeModels(registry.settings, registry.store)
    assert restored.names() == ['app']
    restored.update({'app': {'document': updated_source, 'origin': origin}}, [{'name': 'app', 'kind': 'compose', 'running': 1}])
    assert restored.details('app')['definition'] == edited
    assert restored.tree('app')['running'] == child
    assert restored.details('app')['baseline_evidence'] == 'applied'


def test_select_delete_protections_and_stale_revision(tmp_path):
    registry, source, _ = model(tmp_path)
    root = registry.tree('app')['active']
    change(registry, 'add', root, definition=source, label='First', apply=True)
    first = registry.tree('app')['nodes'][-1]['id']
    change(registry, 'add', first, definition=source, label='Second', apply=True)
    second = registry.tree('app')['nodes'][-1]['id']
    change(registry, 'activate', root, apply=True)
    with pytest.raises(ValueError, match='child'):
        change(registry, 'delete', first, apply=True)
    for protected in (root,):
        with pytest.raises(ValueError, match='cannot be deleted'):
            change(registry, 'delete', protected, apply=True)
    stale = registry.tree('app')['revision']
    change(registry, 'delete', second, apply=True)
    with pytest.raises(ValueError, match='changed'):
        registry.change_tree('app', {'action': 'activate', 'node': first, 'expected_revision': stale, 'apply': True})
    change(registry, 'delete', first, apply=True)
    assert len(registry.tree('app')['nodes']) == 1
    with pytest.raises(ValueError, match='nonempty'):
        change(registry, 'add', root, definition=source, label='\n', apply=True)
    assert 'definition' not in str(registry.summary('app')['tree']['nodes'][0].keys())


def test_layered_host_prefix_project_preserves_host_bind_sources(tmp_path):
    registry, source, origin = model(tmp_path)
    host = tmp_path / 'host'
    directory = host / 'root/docker/app'; directory.mkdir(parents=True)
    path = directory / 'compose.yaml'; path.write_text('services: {web: {image: "web:1"}}')
    registry.settings.system_host_root = host
    # Pydantic assignment does not convert values; use Path for runtime fixture edits.
    from pathlib import Path
    registry.settings.compose_allowed_roots = [Path('/root/docker')]
    origin = {'type': 'file', 'files': ['/root/docker/app/compose.yaml'], 'directory': '/root/docker/app', 'available': True}
    registry.update({'app': {'document': source, 'origin': origin}}, [])
    change(registry, 'add', registry.tree('app')['active'], definition={'services': {'web': {'image': 'web:2'}}}, label='Update', apply=True)
    change(registry, 'activate', registry.tree('app')['nodes'][-1]['id'], apply=True)
    controller = StackControl(Mock(), registry.settings, registry.store, Mock(), models=registry)
    project = controller.project({'name': 'app', 'kind': 'compose', 'config_files': origin['files'], 'working_directory': origin['directory']})
    assert project['directory'] == str(directory)
    assert project['definition']['services']['web']['image'] == 'web:2'
    normalized = {'services': {'web': {'volumes': [{'type': 'bind', 'source': str(directory / 'data')},
                                                   {'type': 'bind', 'source': '/etc/bind'}, {'type': 'volume', 'source': 'named'}]}}}
    mounts = controller.host_bind_paths(normalized, project)['services']['web']['volumes']
    assert [item['source'] for item in mounts] == ['/root/docker/app/data', '/etc/bind', 'named']
    assert path.read_text() == 'services: {web: {image: "web:1"}}'


def test_management_is_explicit_and_keyed_by_global_stack_name(tmp_path):
    settings = DaemonSettings(_env_file=None, compose_root=tmp_path / 'managed', stack_database=tmp_path / 'state.sqlite3')
    store = StackStore(settings.stack_database)
    registry = ComposeModels(settings, store)
    source = {'services': {'web': {'image': 'web:1'}}}
    registry.update({'app': {'document': source, 'origin': {'type': 'file', 'files': ['/root/docker/app/compose.yaml'], 'directory': '/root/docker/app'}}},
                    [{'name': 'app', 'kind': 'compose', 'running': 1}])
    assert registry.summary('app')['tree'] is None and not store.path.exists()
    params = {'action': 'initialize', 'expected_hash': registry.summary('app')['definition_hash']}
    registry.change_tree('app', params)
    assert not store.path.exists()
    registry.change_tree('app', {**params, 'apply': True})
    tree = registry.tree('app')
    assert len(tree['nodes']) == 1
    assert tree['nodes'][0]['label'] == '/root/docker/app/compose.yaml'
    assert tree['active'] == tree['running'] == tree['nodes'][0]['id']
    assert store.definition_tree('another-engine-endpoint', 'app') == tree
    with pytest.raises(ValueError, match='already initialized'):
        registry.change_tree('app', {**params, 'apply': True})


def test_duplicate_is_independent_sibling_and_edit_protects_parent_and_running(tmp_path):
    registry, source, _ = model(tmp_path)
    root = registry.tree('app')['active']
    change(registry, 'duplicate', root, apply=True)
    copy = registry.tree('app')['nodes'][-1]
    assert copy['parent'] is None and copy['id'] != root
    assert copy['definition'] == source and 'patch' not in copy
    assert registry.tree('app')['active'] == registry.tree('app')['running'] == root
    assert registry.node_info('app', copy['id'])['writable']
    updated = {'services': {'web': {'image': 'updated:1'}}}
    change(registry, 'edit', copy['id'], label='Updated copy', definition=updated, apply=True)
    assert document(registry.tree('app'), copy['id']) == updated
    assert document(registry.tree('app'), root) == source
    assert registry.details('app')['definition'] == source
    assert registry.summary('app')['tree']['nodes'][-1]['can_delete']
    change(registry, 'add', copy['id'], label='Child', definition=updated, apply=True)
    assert not registry.node_info('app', copy['id'])['writable']
    with pytest.raises(ValueError, match='read-only'):
        change(registry, 'edit', root, label='Forbidden', definition=updated, apply=True)
    with pytest.raises(ValueError, match='read-only'):
        change(registry, 'edit', copy['id'], label='Forbidden', definition=updated, apply=True)


def test_new_root_definition_import_has_owned_resources_and_no_activation(tmp_path):
    import base64
    registry, source, _ = model(tmp_path)
    root = registry.tree('app')['active']
    params = {'action': 'new', 'label': 'Imported version', 'expected_revision': registry.tree('app')['revision'],
              'identity': 'b' * 32, 'main_file': 'compose.yaml', 'files': {
                  'compose.yaml': base64.b64encode(b'services: {web: {image: "imported:1", volumes: ["./config:/config:ro"]}}').decode(),
                  'config/dns.conf': base64.b64encode(b'safe auxiliary data').decode()}}
    preview = registry.change_tree('app', params)
    assert preview['diff'] == ''
    assert preview['definition']['services']['web']['image'] == 'imported:1'
    target = registry.settings.compose_root / 'app/.lsdfm-definitions' / params['identity']
    assert not target.exists() and len(registry.tree('app')['nodes']) == 1
    registry.change_tree('app', {**params, 'expected_asset_hash': preview['assets']['hash'], 'apply': True})
    assert (target / 'config/dns.conf').read_text() == 'safe auxiliary data'
    tree = registry.tree('app')
    assert tree['active'] == tree['running'] == root
    assert tree['nodes'][-1]['parent'] is None
    assert tree['nodes'][-1]['id'] == params['identity']
    assert document(tree, params['identity'])['services']['web']['image'] == 'imported:1'
    change(registry, 'activate', params['identity'], apply=True)
    controller = StackControl(Mock(), registry.settings, registry.store, Mock(), models=registry)
    project = controller.project({'name': 'app', 'kind': 'compose', 'config_files': [str(tmp_path / 'missing.yaml')], 'working_directory': str(tmp_path)})
    assert project['directory'] == str(target)
    assert not project['source_hashes'] and 'files' not in project
    assert project['definition']['services']['web']['image'] == 'imported:1'
    # The globally named stack has no second project/workspace created by the import.
    assert not (registry.settings.compose_root / params['identity']).exists()
