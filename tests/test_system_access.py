from __future__ import annotations
from common.identity import SESSION_COOKIE, SOURCE_KEY, MANIFEST_NAME, DEFINITIONS_DIRECTORY, MANAGED_HEADER

import socket
import threading
from pathlib import Path

from backend.docker.client import DockerClient
from backend.network.management import configure


def test_docker_ping_over_unix_socket(tmp_path: Path):
    socket_path = tmp_path / "docker.sock"
    ready = threading.Event()

    def fake_docker():
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as server:
            server.bind(str(socket_path))
            server.listen(1)
            ready.set()
            connection, _ = server.accept()
            with connection:
                request = connection.recv(4096)
                assert b"GET /_ping HTTP/1.1" in request
                connection.sendall(
                    b"HTTP/1.1 200 OK\r\nContent-Length: 2\r\nContent-Type: text/plain\r\n\r\nOK"
                )

    thread = threading.Thread(target=fake_docker)
    thread.start()
    assert ready.wait(2)
    assert DockerClient(f"unix://{socket_path}").ping() == {"reachable": True}
    thread.join(timeout=2)
    assert not thread.is_alive()


def test_docker_api_version_is_negotiated(tmp_path: Path):
    socket_path = tmp_path / "docker-version.sock"
    ready = threading.Event()

    def fake_docker():
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as server:
            server.bind(str(socket_path))
            server.listen(2)
            ready.set()
            connection, _ = server.accept()
            with connection:
                assert b"GET /version " in connection.recv(4096)
                body = b'{"ApiVersion":"1.47"}'
                connection.sendall(
                    b"HTTP/1.1 200 OK\r\nContent-Type: application/json\r\nContent-Length: "
                    + str(len(body)).encode()
                    + b"\r\n\r\n"
                    + body
                )
            connection, _ = server.accept()
            with connection:
                assert b"GET /v1.47/info " in connection.recv(4096)
                body = b'{"ID":"test-engine"}'
                connection.sendall(
                    b"HTTP/1.1 200 OK\r\nContent-Type: application/json\r\nContent-Length: "
                    + str(len(body)).encode()
                    + b"\r\n\r\n"
                    + body
                )

    thread = threading.Thread(target=fake_docker)
    thread.start()
    assert ready.wait(2)
    assert DockerClient(f"unix://{socket_path}").info() == {"ID": "test-engine"}
    thread.join(timeout=2)
    assert not thread.is_alive()


def test_docker_event_stream(tmp_path: Path):
    socket_path = tmp_path / "docker-events.sock"
    ready = threading.Event()
    stop = threading.Event()
    received = []

    def fake_docker():
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as server:
            server.bind(str(socket_path))
            server.listen(2)
            ready.set()
            connection, _ = server.accept()
            with connection:
                connection.recv(4096)
                body = b'{"ApiVersion":"1.47"}'
                connection.sendall(
                    b"HTTP/1.1 200 OK\r\nContent-Length: "
                    + str(len(body)).encode()
                    + b"\r\n\r\n"
                    + body
                )
            connection, _ = server.accept()
            with connection:
                assert b"GET /v1.47/events " in connection.recv(4096)
                body = b'{"Type":"network","Action":"create"}\n'
                connection.sendall(b"HTTP/1.1 200 OK\r\n\r\n" + body)

    thread = threading.Thread(target=fake_docker)
    thread.start()
    assert ready.wait(2)

    def callback(event):
        received.append(event)
        stop.set()

    DockerClient(f"unix://{socket_path}").watch_events(callback, stop)
    thread.join(timeout=2)
    assert received == [{"Type": "network", "Action": "create"}]


def test_networkd_configuration_is_previewed_then_written(tmp_path: Path):
    networkd = tmp_path / "networkd"
    interfaces = tmp_path / "network" / "interfaces"
    configuration = {
        "interface": "eth0",
        "addresses": ["192.0.2.10/24", "2001:db8::10/64"],
        "gateways": ["192.0.2.1", "2001:db8::1"],
        "dns": ["192.0.2.53"],
    }
    preview = configure(
        "systemd-networkd", configuration, networkd, interfaces, commit=False
    )
    target = Path(preview["target"])
    assert not target.exists()
    assert "Address=2001:db8::10/64" in preview["content"]

    result = configure(
        "systemd-networkd", configuration, networkd, interfaces, commit=True
    )
    assert result["committed"] is True
    assert target.read_text().startswith(MANAGED_HEADER)


def test_ifupdown_configuration_preview(tmp_path: Path):
    result = configure(
        "ifupdown",
        {"interface": "eth1", "dhcp4": True, "auto": True},
        tmp_path / "networkd",
        tmp_path / "etc-network" / "interfaces",
    )
    assert "auto eth1" in result["content"]
    assert "iface eth1 inet dhcp" in result["content"]
