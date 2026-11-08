import json
from unittest.mock import Mock

import pytest

from backend.daemon import DaemonServer
from backend.daemon_config import DaemonSettings
from backend.docker.stacks import snapshot
from backend.docker.compose.definitions import StackStore
from backend.docker.compose.metadata import parse_document, validate_metadata, load_definition
from common.protocol import ProtocolError


def settings(tmp_path, **kwargs):
    return DaemonSettings(_env_file=None, compose_root=tmp_path / 'managed', stack_database=tmp_path / 'stacks.sqlite3',
                          compose_allowed_roots=[tmp_path], **kwargs)


def client(containers=(), states=None):
    engine = Mock()
    def request(path, **kwargs):
        if path == '/containers/json':
            return list(containers)
        if path == '/volumes':
            return {'Volumes': []}
        identity = path.split('/')[2]
        return (states or {})[identity]
    engine.request.side_effect = request
    engine.networks.return_value = []
    engine.info.return_value = {'Swarm': {}}
    return engine


@pytest.mark.parametrize('requires', ['db', ['db', 'cache'], ['db', {'stack': 'job', 'state': 'completed'}]])
def test_dependency_forms_default_to_healthy(requires):
    meta = validate_metadata({'x-stackmeta': {'autostart': True, 'dependencies': {'requires': requires}}, 'services': {'web': {'restart': 'no'}}})
    assert meta['eligible']
    assert meta['requires'][0] == {'stack': 'db', 'state': 'healthy'}


def test_yaml_no_stays_string_and_restart_policy_excludes_metadata():
    document = parse_document('services:\n  web:\n    restart: no\nx-stackmeta:\n  autostart: true\n', '.yaml')
    assert document['services']['web']['restart'] == 'no'
    assert validate_metadata(document)['eligible']
    document['services']['web']['restart'] = 'unless-stopped'
    meta = validate_metadata(document)
    assert not meta['eligible']
    assert {item['field'] for item in meta['invalid_fields']} >= {'autostart', 'dependencies'}
    assert meta['restart_conflicts'][0]['service'] == 'web'


@pytest.mark.parametrize('bad', [False, {'autostart': 'yes'}, {'restart': True}, {'dependencies': {'requires': [{'stack': 'db', 'state': []}]}}, {'dependencies': {'requires': ['db', 'db']}}])
def test_invalid_metadata_is_reported_without_crashing(bad):
    assert not validate_metadata({'x-stackmeta': bad})['eligible']


def test_compose_sources_keep_override_order_and_reject_unsafe_paths(tmp_path):
    base, override = tmp_path / 'z-base.json', tmp_path / 'a-override.yaml'
    base.write_text(json.dumps({'services': {'web': {'restart': 'always'}}, 'x-stackmeta': {'description': 'base'}}))
    override.write_text('services:\n  web:\n    restart: no\nx-stackmeta:\n  description: override\n')
    stack = {'name': 'demo', 'config_files': [str(base), str(override)]}
    document, sources, warnings = load_definition(stack, settings(tmp_path))
    assert sources == [str(base), str(override)] and not warnings
    assert validate_metadata(document)['eligible']
    assert document['x-stackmeta']['description'] == 'override'
    outside = tmp_path.parent / 'outside-lsdfm-compose.yaml'
    outside.write_text('x-stackmeta: {description: secret-do-not-expose}')
    try:
        link = tmp_path / 'escape.yaml'; link.symlink_to(outside)
        document, _, warnings = load_definition({'config_files': [str(link)]}, settings(tmp_path))
        assert document is None and warnings
        assert 'secret-do-not-expose' not in str(warnings)
    finally:
        outside.unlink()


def test_discovery_uses_working_directory_and_json(tmp_path):
    (tmp_path / 'compose.json').write_text('{"x-stackmeta":{},"services":{}}')
    document, sources, warnings = load_definition({'name': 'demo', 'working_directory': str(tmp_path)}, settings(tmp_path))
    assert document == {'x-stackmeta': {}, 'services': {}}
    assert not warnings and sources == [str(tmp_path / 'compose.json')]


def test_state_assessment_health_fallback_completed_and_missing(tmp_path):
    path = tmp_path / 'compose.yaml'
    path.write_text('''services:
  app:
    restart: no
x-stackmeta:
  autostart: true
  description: Example
  dependencies:
    requires:
      - database
      - stack: migration
        state: completed
      - stack: missing
''')
    containers = [
        {'Id': 'a' * 64, 'Labels': {'com.docker.compose.project': 'app', 'com.docker.compose.service': 'app',
                                  'com.docker.compose.project.config_files': str(path)}, 'State': 'running'},
        {'Id': 'b' * 64, 'Labels': {'com.docker.compose.project': 'database', 'com.docker.compose.service': 'db'}, 'State': 'running'},
        {'Id': 'c' * 64, 'Labels': {'com.docker.compose.project': 'migration', 'com.docker.compose.service': 'job'}, 'State': 'exited'},
    ]
    engine = client(containers, {'b' * 64: {'State': {'Running': True}},
                                 'c' * 64: {'State': {'Status': 'exited', 'ExitCode': 0}}})
    virtual = [{'name': name, 'revision': 1, 'definition': {'services': {service: {}}}}
               for name, service in [('database', 'db'), ('migration', 'job')]]
    result = snapshot(engine, settings=settings(tmp_path), virtual_definitions=virtual)
    app = next(item for item in result['stacks'] if item['name'] == 'app')
    assert app['kind'] == 'compose-meta' and app['base_kind'] == 'compose'
    dependencies = app['metadata']['requires']
    assert dependencies[0]['satisfied'] and dependencies[1]['satisfied']
    assert dependencies[2]['assessment'] == 'error' and 'missing' in dependencies[2]['reason']


def test_virtual_storage_survives_new_instance_and_checks_revisions(tmp_path):
    store = StackStore(tmp_path / 'stacks.sqlite3')
    definition = {'services': {'web': {'image': 'web:1'}}, 'x-stackmeta': {'description': 'Virtual'}}
    assert store.list() == []
    assert store.save('demo', definition)['revision'] == 1
    second = StackStore(store.path)
    assert second.get('demo')['definition'] == definition
    with pytest.raises(ValueError, match='changed'):
        second.save('demo', definition)
    assert second.save('demo', definition, 1)['revision'] == 2
    result = snapshot(client(), settings=settings(tmp_path), virtual_definitions=second.list())
    stack = result['stacks'][0]
    assert stack['kind'] == 'compose-meta' and stack['definition_origin'] == 'virtual'
    assert stack['state'] == 'empty' and stack['services'] == 1


def test_cycles_and_restart_rules_are_excluded(tmp_path):
    virtual = [{'name': name, 'revision': 1, 'definition': {'services': {},
                'x-stackmeta': {'dependencies': {'requires': other}}}} for name, other in [('a', 'b'), ('b', 'a')]]
    result = snapshot(client(), settings=settings(tmp_path), virtual_definitions=virtual)
    for stack in result['stacks']:
        assert not stack['metadata']['eligible']
        assert stack['metadata']['requires'][0]['assessment'] == 'error'


def test_definition_rpc_preview_permissions_and_no_deployment(tmp_path):
    server = DaemonServer(settings(tmp_path, docker_access='rw'))
    server.docker = client()
    params = {'name': 'demo', 'definition': {'services': {}, 'x-stackmeta': {}}}
    assert not server.dispatch('docker.stack.definition.save', params)['applied']
    assert not server.settings.stack_database.exists()
    assert server.dispatch('docker.stack.definition.save', {**params, 'apply': True})['revision'] == 1
    assert server.dispatch('docker.stack.definition.get', {'name': 'demo'})['name'] == 'demo'
    server.settings.docker_access = 'readonly'
    with pytest.raises(ProtocolError, match='read-only'):
        server.dispatch('docker.stack.definition.save', params)
    assert not any(call.kwargs.get('method') in {'POST', 'DELETE'} for call in server.docker.request.call_args_list)


def test_ready_is_no_longer_a_supported_dependency_state():
    result = validate_metadata({'x-stackmeta': {'dependencies': {'requires': [{'stack': 'db', 'state': 'ready'}]}}})
    assert not result['eligible']


def test_safe_yaml_rejects_duplicate_keys_and_recursive_aliases():
    import yaml
    with pytest.raises(yaml.YAMLError):
        parse_document('services: {}\nx-stackmeta: {autostart: true, autostart: false}', '.yaml')
    with pytest.raises(ValueError):
        parse_document('services: {}\nx-stackmeta: &loop {description: *loop}', '.yaml')
    document = parse_document('defaults: &defaults {restart: no}\nservices: {web: {<<: *defaults}}\nx-stackmeta: {}', '.yaml')
    assert validate_metadata(document)['eligible']


def test_compose_config_env_and_cli_overrides(tmp_path, monkeypatch):
    from backend.daemon import build_parser
    monkeypatch.setenv('BACKEND_COMPOSE_ALLOWED_ROOTS', json.dumps([str(tmp_path)]))
    monkeypatch.setenv('BACKEND_STACK_DATABASE', str(tmp_path / 'env.sqlite3'))
    assert DaemonSettings(_env_file=None).compose_allowed_roots == [tmp_path]
    args = build_parser().parse_args(['--stack-database', str(tmp_path / 'cli.sqlite3'), '--compose-search-directories', str(tmp_path)])
    configured = DaemonSettings(_env_file=None, stack_database=args.stack_database, compose_search_directories=args.compose_search_directories)
    assert configured.stack_database.name == 'cli.sqlite3'
    assert configured.compose_search_directories == [tmp_path]


@pytest.mark.parametrize(('desired', 'state', 'healthcheck', 'expected'), [
    ('healthy', {'Running': True}, {}, 'satisfied'),
    ('healthy', {'Running': True, 'Health': {'Status': 'starting'}}, {'Test': ['CMD', 'true']}, 'pending'),
    ('healthy', {'Running': True, 'Health': {'Status': 'unhealthy'}}, {'Test': ['CMD', 'true']}, 'error'),
    ('started', {'Running': True, 'Paused': True}, {}, 'pending'),
    ('started', {'Running': True, 'Health': {'Status': 'unhealthy'}}, {'Test': ['CMD', 'true']}, 'satisfied'),
    ('completed', {'Status': 'exited', 'ExitCode': 0}, {}, 'satisfied'),
    ('completed', {'Status': 'exited', 'ExitCode': 1}, {}, 'error'),
    ('completed', {'Status': 'exited'}, {}, 'unknown'),
    ('completed', {'Status': 'running', 'ExitCode': 0}, {}, 'pending'),
])
def test_dependency_runtime_conditions(tmp_path, desired, state, healthcheck, expected):
    identity = 'e' * 64
    engine = client([{'Id': identity, 'State': state.get('Status', 'running'), 'Status': 'Up (' + state.get('Health', {}).get('Status', '') + ')',
                      'Labels': {'com.docker.compose.project': 'db', 'com.docker.compose.service': 'db'}}],
                    {identity: {'State': state, 'Config': {'Healthcheck': healthcheck}}})
    virtual = [{'name': 'app', 'revision': 1, 'definition': {'services': {}, 'x-stackmeta': {
        'dependencies': {'requires': [{'stack': 'db', 'state': desired}]}}}},
        {'name': 'db', 'revision': 1, 'definition': {'services': {'db': {}}}}]
    result = snapshot(engine, settings=settings(tmp_path), virtual_definitions=virtual)
    requirement = next(item for item in result['stacks'] if item['name'] == 'app')['metadata']['requires'][0]
    assert requirement['assessment'] == expected
    assert requirement['satisfied'] is (expected == 'satisfied')


def test_host_root_mapping_and_inventory_does_not_expose_compose_secrets(tmp_path):
    host = tmp_path / 'host'
    directory = host / 'srv' / 'demo'; directory.mkdir(parents=True)
    path = directory / 'compose.json'
    path.write_text(json.dumps({'services': {'web': {'environment': {'SECRET': 'hidden-config-value'}, 'command': 'hidden-config-command'}}, 'x-stackmeta': {'description': 'Public metadata'}}))
    config = DaemonSettings(_env_file=None, compose_host_root=host, compose_allowed_roots=['/srv'], stack_database=tmp_path / 'stacks.sqlite3')
    engine = client([{'Id': 'f' * 64, 'State': 'running', 'Labels': {
        'com.docker.compose.project': 'demo', 'com.docker.compose.service': 'web',
        'com.docker.compose.project.config_files': '/srv/demo/compose.json'}}])
    result = snapshot(engine, settings=config)
    assert result['stacks'][0]['kind'] == 'compose-meta'
    assert result['stacks'][0]['metadata']['sources'] == ['/srv/demo/compose.json']
    assert 'hidden-config-value' not in str(result) and 'hidden-config-command' not in str(result)


def test_compose_sources_reuse_system_host_root_without_search_directories(tmp_path):
    from backend.docker.compose.files import read_source
    from backend.docker.compose.metadata import local_compose_path
    host = tmp_path / 'host'
    directory = host / 'root/docker/bind9'
    directory.mkdir(parents=True)
    source = directory / 'docker-compose.yml'
    source.write_text('services: {dns: {image: bind9}}\nx-stackmeta: {description: Host definition}\n')
    config = DaemonSettings(_env_file=None, system_host_root=host, compose_allowed_roots=['/root/docker'],
                            compose_root=tmp_path / 'managed', stack_database=tmp_path / 'state.sqlite3')
    stack = {'name': 'bind9', 'kind': 'compose', 'config_files': ['/root/docker/bind9/docker-compose.yml'],
             'working_directory': '/root/docker/bind9'}
    assert not config.compose_search_directories
    document, paths, warnings = load_definition(stack, config)
    assert not warnings and document['services']['dns']['image'] == 'bind9'
    assert local_compose_path(paths[0], config) == source
    assert not read_source(stack, paths[0], config)['writable']
    stack['config_files'] = []
    assert load_definition(stack, config)[0] == document
    with pytest.raises(ValueError, match='ALLOWED_ROOTS'):
        local_compose_path('/etc/compose.yml', config)
    outside = tmp_path / 'outside'; outside.mkdir()
    (directory / 'escape.yml').symlink_to(outside / 'compose.yml')
    with pytest.raises(ValueError):
        local_compose_path('/root/docker/bind9/escape.yml', config)
    dedicated = tmp_path / 'compose-host'
    config.compose_host_root = dedicated
    assert config.compose_source_root == dedicated
    assert local_compose_path(paths[0], config) == dedicated / paths[0].lstrip('/')
    managed = config.compose_root / 'local/compose.yaml'
    assert local_compose_path(managed, config) == managed


def test_compose_file_watches_use_docker_subscription_and_deduplicate(tmp_path, monkeypatch):
    from backend.events import EventRuntime, EventBroker, InotifyEventSource
    monkeypatch.setattr(InotifyEventSource, 'start', lambda self: None)
    runtime = EventRuntime(EventBroker(), client(), settings(tmp_path, network_access='disabled'))
    runtime.add_compose_paths([tmp_path / 'compose.yaml'])
    runtime.add_compose_paths([tmp_path / 'compose.json'])
    assert len(runtime.sources) == 1
    assert runtime.sources[0].topic == 'docker.compose.changed'
    runtime.stop.set()
    runtime.add_compose_paths([tmp_path / 'other' / 'compose.json'])
    assert len(runtime.sources) == 1


def test_compose_path_diagnostics_find_missing_or_inaccessible_ancestor(tmp_path, monkeypatch):
    from pathlib import Path
    from backend.docker.compose.metadata import compose_path_diagnostic
    host = tmp_path / 'host'; host.mkdir()
    config = DaemonSettings(_env_file=None, system_host_root=host, compose_allowed_roots=['/root/docker'],
                            stack_database=tmp_path / 'state.sqlite3')
    stack = {'name': 'bind9', 'config_files': ['/root/docker/bind9/docker-compose.yml']}
    document, _, warnings = load_definition(stack, config)
    assert document is None and 'Path does not exist: /root.' in warnings[0]
    assert str(host) not in warnings[0]
    (host / 'root').mkdir()
    assert compose_path_diagnostic(stack['config_files'][0], config) == 'Path does not exist: /root/docker'
    original_stat = Path.stat
    def denied(path, *args, **kwargs):
        if path.is_relative_to(host / 'root/docker'):
            raise PermissionError('private container path')
        return original_stat(path, *args, **kwargs)
    monkeypatch.setattr(Path, 'stat', denied)
    import os
    original_access = os.access
    monkeypatch.setattr(os, 'access', lambda path, mode, **kwargs: False if path == host / 'root' else original_access(path, mode, **kwargs))
    assert compose_path_diagnostic(stack['config_files'][0], config) == 'No access to /root'
    assert compose_path_diagnostic('/etc/compose.yaml', config) is None
