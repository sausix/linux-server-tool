import json
import socket
import threading
import time

import pytest

from common.client import DaemonClient, DaemonUnavailable


def test_inventory_timeout_does_not_extend_ping_timeout(tmp_path):
    path = tmp_path / 'rpc.sock'
    ready = threading.Event()

    def serve():
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as server:
            server.bind(str(path))
            server.listen(2)
            ready.set()
            for _ in range(2):
                connection, _ = server.accept()
                with connection:
                    message = json.loads(connection.recv(4096))
                    time.sleep(.2)
                    response = {'jsonrpc': '2.0', 'id': message['id'], 'result': {'options': []}}
                    try:
                        connection.sendall(json.dumps(response).encode() + b'\n')
                    except BrokenPipeError:
                        pass

    thread = threading.Thread(target=serve, daemon=True)
    thread.start()
    assert ready.wait(2)
    client = DaemonClient(f'unix://{path}', timeout=.05, method_timeouts={'network.sysctl': 1})
    assert client.call('network.sysctl') == {'options': []}
    with pytest.raises(DaemonUnavailable, match='timed out'):
        client.ping()
    thread.join(timeout=2)
    assert not thread.is_alive()
