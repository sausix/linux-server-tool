from pathlib import Path
from unittest.mock import Mock

import pytest

from backend.system.configuration import snapshot, CATALOG
from backend.daemon_config import DaemonSettings
from backend.daemon import DaemonServer
from common.protocol import ProtocolError


def host_settings(tmp_path):
    root = tmp_path / 'host'
    (root / 'etc/sysctl.d').mkdir(parents=True)
    return root, DaemonSettings(_env_file=None, system_host_root=root)


def write(root, key, value):
    path = root / 'proc/sys' / key.replace('.', '/')
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(value)


def test_network_review_reads_live_host_values_and_persistent_sources(tmp_path):
    root, settings = host_settings(tmp_path)
    write(root, 'net.ipv4.ip_forward', '1\n')
    write(root, 'net.ipv6.conf.all.forwarding', '1\n')
    write(root, 'net.ipv6.conf.ens3.accept_ra', '1\n')
    write(root, 'net.ipv6.conf.ens3.forwarding', '1\n')
    (root / 'etc/sysctl.d/90-test.conf').write_text('net.ipv4.ip_forward=0\nnet.ipv6.conf.*.accept_ra=1\n')
    data = snapshot('network', settings)
    assert data['read_only'] is True
    ip, forwarding, ra = data['items']
    assert ip['value'] == '1' and ip['state'] == 'changed'
    assert len(ip['choices']) == 2 and ip['known_value']
    assert ip['sources'][0]['line'] == 1
    assert ip['sources'][0]['file'].startswith(str(root))
    assert forwarding['state'] == 'unconfigured'
    assert ra['state'] == 'configured'
    assert 'suppressed' in ra['assessment']
    assert (root / 'proc/sys/net/ipv4/ip_forward').read_text() == '1\n'


def test_system_review_missing_options_and_distribution_values_are_not_guessed(tmp_path):
    root, settings = host_settings(tmp_path)
    write(root, 'kernel.dmesg_restrict', '1')
    write(root, 'kernel.perf_event_paranoid', '4')
    (root / 'etc/sysctl.d/90-security.conf').write_text('kernel.dmesg_restrict=1\n')
    data = snapshot('system', settings)
    assert len(data['items']) == 5
    dmesg, pointers, bpf, perf, yama = data['items']
    assert dmesg['state'] == 'configured' and dmesg['value'] == '1'
    assert perf['value'] == '4' and perf['known_value'] is False
    assert pointers['value'] is None and pointers['state'] == 'unknown' and pointers['error']
    assert bpf['value'] is None and yama['value'] is None
    assert 'until reboot' in CATALOG['system'][2]['choices'][1]['explanation']
    with pytest.raises(ValueError):
        snapshot('../etc', settings)


def test_review_rpc_respects_category_access_and_never_exposes_a_mutation(tmp_path, monkeypatch):
    root, settings = host_settings(tmp_path)
    server = DaemonServer(settings)
    mock = Mock(return_value={'read_only': True, 'items': []})
    monkeypatch.setattr('backend.daemon.configuration_review', mock)
    assert server.dispatch('system.configuration', {'apply': True}) == {'read_only': True, 'items': []}
    assert mock.call_args.args == ('system', settings)
    assert server.dispatch('network.configuration', {}) == {'read_only': True, 'items': []}
    for category in ['system', 'network']:
        disabled = DaemonServer(DaemonSettings(_env_file=None, **{category + '_access': 'disabled'}))
        with pytest.raises(ProtocolError, match='disabled'):
            disabled.dispatch(category + '.configuration', {})
