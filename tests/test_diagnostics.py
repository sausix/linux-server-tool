import logging
import subprocess
from unittest.mock import Mock

import pytest

from backend import diagnostics
from backend.network import firewall as firewall_inventory
from backend.daemon_config import DaemonSettings


def test_disabled_components_are_not_probed(monkeypatch):
    probe = Mock(side_effect=AssertionError('Disabled component was probed'))
    monkeypatch.setattr(diagnostics, 'network_snapshot', probe)
    docker = Mock()
    assert diagnostics.startup_checks(
        DaemonSettings(docker_access='disabled', network_access='disabled'), docker
    ) == []
    docker.ping.assert_not_called()


def test_enabled_roles_report_access_and_missing_tools(monkeypatch, caplog):
    monkeypatch.setattr(diagnostics, 'network_snapshot', Mock(side_effect=OSError(97, 'Address family not supported')))
    monkeypatch.setattr(diagnostics, 'firewall_snapshot', lambda *args: {'errors': ['Permission denied']})
    monkeypatch.setattr(diagnostics.shutil, 'which', lambda name: None)
    monkeypatch.setattr(diagnostics, 'detect', lambda *args: {'detected': []})
    with caplog.at_level(logging.WARNING):
        result = diagnostics.startup_checks(DaemonSettings(
            docker_access='disabled', network_access='readwrite', events_enabled=True, firewall_backend='nftables',
        ), Mock())
    assert any(item['role'] == 'network.read' and not item['available'] for item in result)
    assert 'AF_NETLINK' in caplog.text
    assert 'No tool found' in caplog.text
    assert 'Permission denied' in caplog.text
    assert 'No supported network manager' in caplog.text


def test_firewall_error_contains_tool_stderr(monkeypatch):
    monkeypatch.setattr(firewall_inventory.subprocess, 'run', Mock(side_effect=
        subprocess.CalledProcessError(3, ['nft'], stderr='netlink: Error: Operation not permitted')
    ))
    with pytest.raises(firewall_inventory.FirewallError, match='Operation not permitted'):
        firewall_inventory._run(['nft'], 1)
