from unittest.mock import Mock

import pytest

from backend import events
from backend.daemon_config import DaemonSettings


@pytest.mark.parametrize('backend', [None, 'systemd-networkd', 'ifupdown'])
def test_only_detected_backend_configuration_is_watched(tmp_path, monkeypatch, backend):
    networkd = tmp_path / 'networkd'
    interfaces = tmp_path / 'network' / 'interfaces'
    interfaces.parent.mkdir()
    snippets = interfaces.parent / 'interfaces.d'
    snippets.mkdir()
    detected = [] if backend is None else [{'name': backend}]
    monkeypatch.setattr(events, 'detect', lambda *args: {'detected': detected})
    monkeypatch.setattr(events.NetworkEventSource, 'start', lambda self: None)
    monkeypatch.setattr(events.NftEventSource, 'start', lambda self: None)
    monkeypatch.setattr(events.FirewallPollingEventSource, 'start', lambda self: None)
    monkeypatch.setattr(events.RoutingOptionsEventSource, 'start', lambda self: None)
    monkeypatch.setattr(events.InotifyEventSource, 'start', lambda self: None)
    runtime = events.EventRuntime(events.EventBroker(), Mock(), DaemonSettings(
        docker_access='disabled', networkd_directory=networkd, interfaces_file=interfaces,
    ))
    runtime.start()
    watchers = [source for source in runtime.sources if isinstance(source, events.InotifyEventSource)]
    if backend is None:
        assert not watchers
    else:
        assert watchers[0].paths == ([networkd] if backend == 'systemd-networkd' else [interfaces, snippets])
    assert any(isinstance(source, events.NetworkEventSource) for source in runtime.sources)
