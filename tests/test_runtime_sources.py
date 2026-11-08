from unittest.mock import Mock

from backend.daemon import DaemonServer
from backend.daemon_config import DaemonSettings
from backend.docker.runtime_sources import annotate_docker_source
from backend.system.sysctl import snapshot
from frontend.inventory import filter_inventory


def items():
    return [{'key': key, 'value': '1', 'sources': []} for key in ('net.ipv4.ip_forward', 'net.ipv6.conf.all.forwarding')]


def test_docker_forwarding_source_is_explicitly_inferred_and_preserves_declarations(tmp_path):
    data = items()
    annotate_docker_source(data)
    assert all(item['configuration_sources'][0]['name'] == 'Docker daemon' for item in data)
    assert all(item['configuration_sources'][0]['confidence'] == 'inferred' for item in data)
    assert len(filter_inventory('sysctl', {'options': data}, [('sysctl.source', 'Docker daemon')])['options']) == 2
    data = items()
    data[0]['sources'] = [{'file': '/etc/sysctl.conf'}]
    annotate_docker_source(data)
    assert 'configuration_sources' not in data[0]
    assert data[1]['configuration_sources']
    data = items()
    data[1]['value'] = '0'
    annotate_docker_source(data)
    assert all('configuration_sources' not in item for item in data)


def test_runtime_source_requires_reachable_local_docker_and_respects_disabled_forwarding(tmp_path):
    etc = tmp_path / 'etc'
    (etc / 'docker').mkdir(parents=True)
    server = DaemonServer(DaemonSettings(_env_file=None, host_etc_directory=etc))
    server.docker = Mock()
    server.docker.ping.return_value = {'reachable': True}
    data = items()
    server.annotate_configuration_sources(data)
    assert data[0]['configuration_sources']
    for config in ['{"ip-forward": false}', '{"firewall-backend": "nftables"}']:
        (etc / 'docker/daemon.json').write_text(config)
        data = items()
        server.annotate_configuration_sources(data)
        assert all('configuration_sources' not in item for item in data)
    server = DaemonServer(DaemonSettings(_env_file=None, docker_access='disabled'))
    server.docker = Mock()
    server.annotate_configuration_sources(items())
    server.docker.ping.assert_not_called()
    remote = DaemonServer(DaemonSettings(_env_file=None, docker_endpoint='tcp://example:2376', access_token='test-token-for-a-test-only'))
    remote.docker = Mock()
    remote.annotate_configuration_sources(items())
    remote.docker.ping.assert_not_called()


def test_socket_buffers_and_core_declarations_are_in_sysctl_inventory(tmp_path):
    proc, config = tmp_path / 'proc', tmp_path / 'etc'
    config.mkdir()
    for key, value in {'net/core/rmem_max': '8388608', 'net/core/rmem_default': '212992', 'net/core/wmem_max': '8388608',
                       'net/core/optmem_max': '65536', 'net/ipv4/tcp_rmem': '4096 131072 8388608', 'net/ipv4/udp_mem': '100 200 300'}.items():
        path = proc / key
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(value)
    (config / '90-buffers.conf').write_text('net.core.rmem_max=8388608\n')
    data = snapshot(proc, [config], config / 'sysctl.conf', include_routes=False, kernel_defaults={})
    options = {item['key']: item for item in data['options']}
    assert options['net.core.rmem_max']['systemd_candidate']['value'] == '8388608'
    assert options['net.core.rmem_max']['unit'] == 'bytes'
    assert 'socket-buffer' in options['net.core.wmem_max']['classes']
    assert options['net.ipv4.udp_mem']['unit'] == 'pages'
