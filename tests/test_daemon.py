from __future__ import annotations

import subprocess
import sys
import threading
import time
from pathlib import Path

import pytest

from common.client import DaemonClient, DaemonUnavailable
from common.protocol import (
    ProtocolError,
    RemoteError,
    decode_message,
    encode_message,
)
from backend.daemon import DaemonServer
from backend.daemon_config import DaemonSettings


def test_protocol_round_trip():
    message = {"jsonrpc": "2.0", "id": "example", "method": "ping", "params": {}}
    assert decode_message(encode_message(message).rstrip()) == message


def test_component_access_modes_are_enforced():
    disabled = DaemonServer(
        DaemonSettings(docker_access="disabled", network_access="disabled")
    )
    with pytest.raises(ProtocolError, match="Docker access is disabled"):
        disabled.dispatch("docker.info", {})
    with pytest.raises(ProtocolError, match="Network access is disabled"):
        disabled.dispatch("network.snapshot", {})

    readonly = DaemonServer(DaemonSettings(network_access="readonly"))
    assert readonly.dispatch(
        "network.link.set", {"interface": "lo", "up": True, "apply": False}
    )["applied"] is False
    with pytest.raises(ProtocolError, match="read-only"):
        readonly.dispatch(
            "network.link.set", {"interface": "lo", "up": True, "apply": True}
        )


def test_separate_daemon_process_over_unix_socket(tmp_path: Path):
    socket_path = tmp_path / "backend.sock"
    networkd_path = tmp_path / "networkd"
    networkd_path.mkdir()
    token = "test-daemon-token-that-is-long-enough"
    client = DaemonClient(f"unix://{socket_path}", token=token, timeout=0.2)
    process = subprocess.Popen(
        [
            str(Path(sys.executable).parent / "backend"),
            "--unix-socket",
            str(socket_path),
            "--access-token",
            token,
            "--networkd-directory",
            str(networkd_path),
            '--docker-access', 'ro',
            '--network-access', 'ro',
        ],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    try:
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            try:
                status = client.ping()
                break
            except DaemonUnavailable:
                if process.poll() is not None:
                    stdout, stderr = process.communicate()
                    pytest.fail(f"Backend exited early:\n{stdout}\n{stderr}")
                time.sleep(0.05)
        else:
            pytest.fail("Backend did not create its Unix socket")

        assert status["service"] == "backend"
        from backend import __version__
        assert status["version"] == __version__
        assert 'protocol' not in status
        assert status["docker_access"] == "readonly"
        assert status["network_access"] == "readonly"
        assert client.call("system.info")["platform"]
        network = client.call("network.snapshot")
        assert {"interfaces", "addresses", "routes", "configuration"} <= set(network)
        assert isinstance(network.get('docker_subnets', []), list)
        assert any(link["name"] == "lo" for link in network["interfaces"])
        manager = client.call("network.manager")
        assert manager["temporary_backend"] == "rtnetlink"
        plan = client.call(
            "network.address.change",
            {"action": "add", "interface": "lo", "address": "192.0.2.1/32"},
        )
        assert plan["applied"] is False
        with pytest.raises(RemoteError, match="read-only"):
            client.call(
                "network.address.change",
                {
                    "action": "add",
                    "interface": "lo",
                    "address": "192.0.2.1/32",
                    "apply": True,
                },
            )

        def change_configuration():
            time.sleep(0.2)
            (networkd_path / "10-test.network").write_text("[Match]\nName=lo\n")

        producer = threading.Thread(target=change_configuration)
        producer.start()
        event = next(client.subscribe(["network.config"], timeout=3))
        producer.join()
        assert event["topic"] == "network.config.changed"
        with pytest.raises(RemoteError, match="Unauthorized"):
            DaemonClient(f"unix://{socket_path}").ping()
        with pytest.raises(RemoteError, match="Method not found"):
            client.call("does.not.exist")
    finally:
        process.terminate()
        process.wait(timeout=5)

