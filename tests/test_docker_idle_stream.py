import socket
import threading
import time

import pytest

from backend.docker.client import DockerClient


@pytest.mark.parametrize('chunked', [True, False])
def test_idle_event_stream_outlives_request_timeout_and_stops(tmp_path, chunked):
    path = tmp_path / 'docker.sock'
    ready = threading.Event()
    subscribed = threading.Event()
    received = threading.Event()
    stop = threading.Event()
    errors = []

    def engine():
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as listener:
            listener.bind(str(path))
            listener.listen(1)
            ready.set()
            connection, _ = listener.accept()
            with connection:
                connection.settimeout(3)
                connection.recv(4096)
                headers = b'HTTP/1.1 200 OK\r\n'
                if chunked:
                    headers += b'Transfer-Encoding: chunked\r\n'
                connection.sendall(headers + b'\r\n')
                subscribed.set()
                time.sleep(.35)  # Deliberately exceed the .1 second request timeout.
                message = b'{"Type":"network","Action":"create"}\n'
                if chunked:
                    message = f'{len(message):x}\r\n'.encode() + message + b'\r\n'
                connection.sendall(message)
                connection.recv(4096)  # Shutdown must close the otherwise idle subscription.

    def watch():
        try:
            DockerClient(f'unix://{path}', api_version='v1.47', timeout=.1).watch_events(
                lambda event: received.set(), stop)
        except Exception as exc:
            errors.append(exc)

    engine_thread = threading.Thread(target=engine, daemon=True)
    watcher = threading.Thread(target=watch, daemon=True)
    engine_thread.start()
    assert ready.wait(2)
    watcher.start()
    try:
        assert subscribed.wait(2)
        assert received.wait(2), errors
        assert watcher.is_alive()
    finally:
        stop.set()
        watcher.join(timeout=2)
        engine_thread.join(timeout=2)
    assert not watcher.is_alive()
    assert not engine_thread.is_alive()
    assert errors == []
