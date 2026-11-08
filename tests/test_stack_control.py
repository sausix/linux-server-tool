from common.identity import SESSION_COOKIE, SOURCE_KEY, MANIFEST_NAME, DEFINITIONS_DIRECTORY, MANAGED_HEADER
import json
from unittest.mock import Mock

import pytest

from backend.daemon_config import DaemonSettings
from backend.docker.compose.control import StackControl
from backend.docker.compose.definitions import StackStore
from backend.daemon import DaemonServer
from common.protocol import ProtocolError


class ImmediateThread:
    def __init__(self, target, args, **kwargs):
        self.target, self.args = target, args
    def start(self):
        self.target(*self.args)


@pytest.fixture
def control(tmp_path, monkeypatch):
    monkeypatch.setattr('backend.docker.compose.control.threading.Thread', ImmediateThread)
    settings = DaemonSettings(_env_file=None, docker_access='rw', compose_root=tmp_path / 'managed', stack_database=tmp_path / 'stacks.sqlite3', compose_allowed_roots=[tmp_path])
    controller = StackControl(Mock(), settings, StackStore(settings.stack_database), Mock())
    return controller


def test_compose_start_uses_saved_normalized_config_after_preview(control, tmp_path):
    path = tmp_path / 'compose.yaml'; path.write_text('services: {web: {image: web:1}}')
    stack = {'name': 'demo', 'kind': 'compose', 'base_kind': 'compose', 'config_files': [str(path)], 'container_items': []}
    control.find = lambda *args: stack
    control._run = Mock(side_effect=lambda project, args: json.dumps({'name': 'demo', 'services': {'web': {'image': 'web:1'}}}) if args[0] == 'config' else '')
    preview = control.preview('demo', 'compose', 'start')
    assert control.client.request.call_count == 0
    job = control.apply('demo', 'compose', 'start', preview['fingerprint'])
    assert job['status'] == 'succeeded'
    assert control._run.call_args.args[1] == ['up', '--detach', '--no-build', '--pull', 'never']
    assert control._run.call_args.args[0]['definition']['services']['web']['image'] == 'web:1'
    path.write_text('services: {web: {image: changed}}')
    with pytest.raises(ValueError, match='changed after preview'):
        control.apply('demo', 'compose', 'start', preview['fingerprint'])


def test_pending_restart_previews_diff_executes_frozen_definition_and_updates_baseline(control):
    old = {'services': {'web': {'image': 'web:1'}}}
    new = {'services': {'web': {'image': 'web:2'}}}
    state = [{'name': 'demo', 'kind': 'compose', 'running': 1}]
    control.models.update({'demo': {'document': old, 'origin': {'type': 'virtual'}, 'warnings': []}}, state)
    control.models.update({'demo': {'document': new, 'origin': {'type': 'virtual'}, 'warnings': []}}, state)
    control.store.save('demo', new)
    control.find = lambda *args: {'name': 'demo', 'kind': 'compose', 'base_kind': 'compose', 'config_files': [], 'container_items': [{'id': 'a' * 64}]}
    control._run = Mock(side_effect=lambda project, args: json.dumps(project['definition']) if args[0] == 'config' else '')
    preview = control.preview('demo', 'compose', 'restart')
    assert 'web:1' in preview['definition_diff'] and 'web:2' in preview['definition_diff']
    assert preview['baseline_evidence'] == 'observed-unverified'
    assert control.apply('demo', 'compose', 'restart', preview['fingerprint'])['status'] == 'succeeded'
    executed, args = control._run.call_args.args
    assert args == ['up', '--detach', '--no-build', '--pull', 'never', '--force-recreate']
    assert 'files' not in executed and executed['definition'] == new
    assert control.models.details('demo')['baseline_evidence'] == 'applied'
    assert not control.models.details('demo')['pending_changes']
    assert control.store.running_definition(control.settings.docker_endpoint, 'demo') == new


def test_runtime_only_stack_controls_existing_containers(control):
    control.find = lambda *args: {'name': 'demo', 'kind': 'compose', 'base_kind': 'compose', 'config_files': [], 'container_items': [{'id': 'a' * 64}]}
    plan = control.preview('demo', 'compose', 'stop')
    assert plan['backend'] == 'containers' and plan['warnings']
    control.client.request.assert_not_called()
    job = control.apply('demo', 'compose', 'stop', plan['fingerprint'])
    assert job['status'] == 'succeeded'
    assert control.client.request.call_args.args[0] == '/containers/' + 'a' * 64 + '/stop'
    assert control.client.request.call_args.kwargs['method'] == 'POST'


def test_conversion_keeps_original_files_and_uses_revision_and_source_guards(control, tmp_path):
    path = tmp_path / 'compose.json'; original = '{"services":{"web":{"image":"web:1"}}}'; path.write_text(original)
    control.find = lambda *args: {'name': 'demo', 'kind': 'compose', 'config_files': [str(path)]}
    control._run = Mock(return_value=original)
    preview = control.convert('demo')
    assert not control.store.path.exists()
    saved = control.convert('demo', True, preview['expected'])
    assert saved['applied'] and control.store.get('demo')['definition'][SOURCE_KEY]['type'] == 'compose-files'
    assert path.read_text() == original
    preview = control.convert('demo'); path.write_text('{"services":{}}')
    with pytest.raises(ValueError, match='changed after preview'):
        control.convert('demo', True, preview['expected'])


def test_swarm_stop_start_restores_replicas_without_exposing_service_spec(control):
    service = {'ID': 'service', 'Version': {'Index': 7}, 'Spec': {'Name': 'demo_web',
        'Labels': {'com.docker.stack.namespace': 'demo'}, 'Mode': {'Replicated': {'Replicas': 3}},
        'TaskTemplate': {'ContainerSpec': {'Env': ['SECRET=hidden']}}}}
    control.find = lambda *args: {'name': 'demo', 'kind': 'swarm', 'base_kind': 'swarm'}
    control.client.request.side_effect = lambda path, **kwargs: [service] if path == '/services' else {}
    preview = control.preview('demo', 'swarm', 'stop')
    assert 'hidden' not in str(preview)
    assert control.apply('demo', 'swarm', 'stop', preview['fingerprint'])['status'] == 'succeeded'
    assert control.client.request.call_args.kwargs['data']['Mode']['Replicated']['Replicas'] == 0
    service['Spec']['Mode']['Replicated']['Replicas'] = 0; service['Version']['Index'] = 8
    preview = control.preview('demo', 'swarm', 'start')
    assert control.apply('demo', 'swarm', 'start', preview['fingerprint'])['status'] == 'succeeded'
    assert control.client.request.call_args.kwargs['data']['Mode']['Replicated']['Replicas'] == 3
    assert control.client.request.call_args.kwargs['query'] == {'version': 8}


def test_global_swarm_services_and_relative_virtual_bind_paths_are_rejected(control):
    control.find = lambda *args: {'name': 'demo', 'kind': 'swarm', 'base_kind': 'swarm'}
    control.client.request.return_value = [{'ID': 'global', 'Spec': {'Labels': {'com.docker.stack.namespace': 'demo'}, 'Mode': {'Global': {}}}}]
    with pytest.raises(ValueError, match='replicated services only'):
        control.preview('demo', 'swarm', 'stop')
    with pytest.raises(ValueError, match='absolute host paths'):
        control._check_virtual_paths({'services': {'web': {'volumes': ['./data:/data']}}})


def test_new_daemon_mutations_are_blocked_in_readonly_mode(tmp_path):
    server = DaemonServer(DaemonSettings(_env_file=None, docker_access='readonly', stack_database=tmp_path / 'stacks.sqlite3'))
    for method in ['docker.stack.file.save', 'docker.stack.convert', 'docker.stack.action']:
        with pytest.raises(ProtocolError, match='read-only'):
            server.dispatch(method, {'name': 'demo', 'apply': True})


def test_control_environment_and_cli_settings(monkeypatch):
    from backend.daemon import build_parser
    from frontend.cli import build_parser as frontend_parser
    from frontend.config import Settings
    monkeypatch.setenv('BACKEND_COMPOSE_COMMAND', '["/custom/docker","compose"]')
    monkeypatch.setenv('BACKEND_STACK_CONTROL_TIMEOUT_SECONDS', '77')
    monkeypatch.setenv('FRONTEND_DAEMON_CONTROL_TIMEOUT_SECONDS', '99')
    assert DaemonSettings(_env_file=None).compose_command == ['/custom/docker', 'compose']
    assert DaemonSettings(_env_file=None).stack_control_timeout_seconds == 77
    args = build_parser().parse_args(['--compose-command', '/usr/bin/docker', 'compose', '--stack-control-timeout-seconds', '88'])
    configured = DaemonSettings(_env_file=None, compose_command=args.compose_command, stack_control_timeout_seconds=args.stack_control_timeout_seconds)
    assert configured.compose_command == ['/usr/bin/docker', 'compose'] and configured.stack_control_timeout_seconds == 88
    assert Settings(_env_file=None).daemon_control_timeout_seconds == 99
    args = frontend_parser().parse_args(['--daemon-control-timeout-seconds', '100'])
    assert Settings(_env_file=None, daemon_control_timeout_seconds=args.daemon_control_timeout_seconds).daemon_control_timeout_seconds == 100


def test_compose_cli_uses_argument_lists_and_bounded_environment(control, tmp_path, monkeypatch):
    monkeypatch.setenv('BACKEND_ACCESS_TOKEN', 'must-not-reach-compose')
    monkeypatch.setattr('backend.docker.compose.control.shutil.which', lambda name: '/usr/bin/docker')
    captured = {}
    process = Mock(); process.returncode = 0
    def popen(command, **kwargs):
        captured.update(command=command, **kwargs)
        process.communicate.side_effect = lambda *args, **options: kwargs['stdout'].write(b'{"services":{}}')
        return process
    monkeypatch.setattr('backend.docker.compose.control.subprocess.Popen', popen)
    path = tmp_path / 'compose.yaml'; path.write_text('services: {}')
    assert json.loads(control._run({'name': 'demo', 'files': [str(path)], 'directory': str(tmp_path)}, ['config', '--format', 'json'])) == {'services': {}}
    assert captured['command'][:4] == ['/usr/bin/docker', '--host', control.settings.docker_endpoint, 'compose']
    assert captured['command'][-5:] == ['--file', str(path), 'config', '--format', 'json']
    assert captured['start_new_session'] is True and not captured.get('shell')
    assert 'BACKEND_ACCESS_TOKEN' not in captured['env']
    assert 'must-not-reach-compose' not in str(captured['env'])


def test_layer_restart_marks_reviewed_node_only_after_success(control):
    old = {'services': {'web': {'image': 'web:1'}}}
    new = {'services': {'web': {'image': 'web:2'}}}
    control.store.save('demo', old)
    control.models.update({'demo': {'document': old, 'origin': {'type': 'virtual'}}}, [{'name': 'demo', 'kind': 'compose', 'running': 1}])
    control.models.change_tree('demo', {'action': 'initialize', 'expected_hash': control.models.summary('demo')['definition_hash'], 'apply': True})
    tree = control.models.tree('demo')
    root = tree['active']
    control.models.change_tree('demo', {'action': 'add', 'node': root, 'expected_revision': tree['revision'],
                                      'label': 'Updated image', 'definition': new, 'apply': True})
    child = control.models.tree('demo')['nodes'][-1]['id']
    control.models.change_tree('demo', {'action': 'activate', 'node': child, 'expected_revision': control.models.tree('demo')['revision'], 'apply': True})
    control.find = lambda *args: {'name': 'demo', 'kind': 'compose', 'base_kind': 'compose', 'config_files': [], 'container_items': [{'id': 'a' * 64}]}
    def fail(project, args):
        if args[0] == 'config':
            return json.dumps(project['definition'])
        raise ValueError('Engine rejected update')
    control._run = Mock(side_effect=fail)
    preview = control.preview('demo', 'compose', 'restart')
    assert control.apply('demo', 'compose', 'restart', preview['fingerprint'])['status'] == 'failed'
    assert control.models.tree('demo')['running'] == root
    control._run = Mock(side_effect=lambda project, args: json.dumps(project['definition']) if args[0] == 'config' else '')
    preview = control.preview('demo', 'compose', 'restart')
    assert control.apply('demo', 'compose', 'restart', preview['fingerprint'])['status'] == 'succeeded'
    assert control.models.tree('demo')['running'] == child
    assert control.models.tree('demo')['active'] == child
    assert control._run.call_args.args[0]['definition'] == new
