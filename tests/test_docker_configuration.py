import json
from unittest.mock import Mock

from backend.docker import configuration as config


def test_binding_argument_forms():
    assert config.binding_arguments(['dockerd', '-H', 'fd://', '--host=tcp://127.0.0.1:2375',
        '-Hunix:///run/docker.sock', '--config-file=/custom/daemon.json']) == {
            'hosts': ['fd://', 'tcp://127.0.0.1:2375', 'unix:///run/docker.sock'],
            'config_file': '/custom/daemon.json'}


def test_local_file_process_and_systemd_binding_conflict(tmp_path, monkeypatch):
    path = tmp_path / 'daemon.json'
    path.write_text(json.dumps({'hosts': ['unix:///run/docker.sock'], 'debug': True,
        'proxies': {'http-proxy': 'http://user:password@proxy:8080'}, 'token': 'private'}))
    proc = tmp_path / 'proc'
    pid = proc / '123'
    pid.mkdir(parents=True)
    (pid / 'comm').write_text('dockerd\n')
    (pid / 'cmdline').write_bytes(b'/usr/bin/dockerd\0-H\0fd://\0')
    monkeypatch.setattr(config.shutil, 'which', lambda name: '/usr/bin/systemctl')
    monkeypatch.setattr(config.subprocess, 'run', Mock(side_effect=[
        Mock(stdout='MainPID=123\nFragmentPath=/usr/lib/systemd/system/docker.service\nDropInPaths=/etc/systemd/system/docker.service.d/override.conf\nExecStart={ path=/usr/bin/dockerd ; argv[]=/usr/bin/dockerd -H fd:// ; ignore_errors=no ; }\n'),
        Mock(stdout='Listen=/run/docker.sock (Stream)\n'),
    ]))
    result = config.inspect_local('unix:///run/docker.sock', path, proc)
    assert result['daemon_json']['exists'] is True
    assert result['daemon_json']['settings']['debug'] is True
    assert result['processes'][0]['hosts'] == ['fd://']
    assert result['systemd']['configured_binding']['hosts'] == ['fd://']
    assert 'override.conf' in result['systemd']['DropInPaths']
    assert 'password' not in str(result)
    assert result['daemon_json']['settings']['token'] == '[redacted]'
    assert any('both' in warning for warning in result['warnings'])


def test_missing_invalid_and_remote_configuration(tmp_path):
    path = tmp_path / 'daemon.json'
    assert config.read_config(path)['exists'] is False
    path.write_text('{invalid')
    assert config.read_config(path)['error']
    assert config.inspect_local('tcp://remote:2376', path)['daemon_json'] is None
