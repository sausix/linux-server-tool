import asyncio
import json
import socket
import struct
import threading
import time
from unittest.mock import Mock

import pytest

from backend.docker.client import DockerClient
from backend.docker.logs import LogDecoder, LiveLogs, LogPosition, history, sources
from backend.daemon import DaemonServer, build_parser
from backend.daemon_config import DaemonSettings
from common.protocol import ProtocolError, encode_message, request


def frame(stream, text):
    data = text.encode()
    return bytes([stream, 0, 0, 0]) + struct.pack('>I', len(data)) + data


def engine():
    client = Mock()
    def raw(path, **kwargs):
        if path == '/containers/json':
            return [{'Id': 'a'*64}, {'Id': 'b'*64}]
        return {'Name': '/web' if 'a'*64 in path else '/other', 'Config': {'Tty': False,
                'Env': ['SECRET=private'], 'Labels': {'com.docker.compose.project': 'demo',
                                                     'com.docker.compose.service': 'web' if 'a'*64 in path else 'other'}},
                'State': {'Running': True}}
    client.request.side_effect = raw
    return client


def test_decoder_preserves_streams_timestamps_utf8_and_multiline_frames():
    result = []
    decoder = LogDecoder({'id': 'a'*64, 'name': 'web', 'service': 'web', 'tty': False}, result.append)
    payload = frame(1, '2026-11-01T10:00:00.123456789Z héllo\n') + frame(2, '2026-11-01T10:00:01Z error\nsecond')
    for index in range(0, len(payload), 3):
        decoder.feed(payload[index:index+3])
    decoder.finish()
    assert [(line['stream'], line['text']) for line in result] == [('stdout', 'héllo'), ('stderr', 'error'), ('stderr', 'second')]
    assert result[0]['timestamp'] == '2026-11-01T10:00:00.123456789Z'
    assert result[-1]['timestamp'] is None


def test_tty_is_explicitly_combined_and_long_lines_are_bounded():
    result = []
    decoder = LogDecoder({'id': 'a'*64, 'name': 'web', 'service': '', 'tty': True}, result.append)
    decoder.feed(b'2026-11-01T10:00:00Z \x1b[31mhello\x1b[0m\n')
    for _ in range(100):
        decoder.feed(b'x'*1000)
    decoder.feed(b'\n'); decoder.finish()
    assert result[0]['stream'] == 'combined' and result[0]['text'] == 'hello'
    assert len(result[1]['text']) == 4096 and result[1]['truncated']


def test_service_scope_rechecks_labels_and_global_history_limit():
    client = engine(); settings = DaemonSettings(_env_file=None)
    params = {'scope': 'service', 'kind': 'compose', 'name': 'demo', 'service': 'web', 'limit': 2}
    selected = sources(client, params, settings)
    assert [item['name'] for item in selected] == ['web']
    assert 'SECRET' not in json.dumps(selected)
    def logs(identity, callback, stop, **kwargs):
        assert identity == 'a'*64
        for value in range(4):
            callback(frame(2 if value == 3 else 1, f'2026-11-01T10:00:0{value}Z line {value}\n'))
    client.read_logs.side_effect = logs
    data = history(client, params, settings)
    assert [line['text'] for line in data['lines']] == ['line 2', 'line 3']
    assert data['lines'][-1]['stream'] == 'stderr'
    params = {**params, 'scope': 'stack'}
    client.read_logs.side_effect = lambda identity, callback, stop, **kwargs: callback(frame(1, '2026-11-01T10:00:00Z '+identity[:1]+'\n'))
    assert len(history(client, {**params, 'limit': 1}, settings)['lines']) == 1


def test_invalid_limits_disabled_feature_and_env_cli(monkeypatch):
    server = DaemonServer(DaemonSettings(_env_file=None, docker_access='disabled'))
    with pytest.raises(ProtocolError, match='disabled'):
        server.dispatch('docker.logs.tail', {'scope': 'container', 'id': 'a'*64})
    client = engine()
    with pytest.raises(ValueError, match='Line limit'):
        history(client, {'scope': 'container', 'id': 'a'*64, 'limit': 0}, DaemonSettings(_env_file=None))
    client.request.assert_not_called()
    monkeypatch.setenv('BACKEND_DOCKER_LOG_MAX_LINES', '800')
    monkeypatch.setenv('BACKEND_DOCKER_LOG_DEFAULT_LINES', '123')
    args = build_parser().parse_args(['--docker-log-max-lines', '900', '--docker-log-max-streams', '4', '--docker-log-default-lines', '321'])
    assert DaemonSettings(_env_file=None).docker_log_max_lines == 800
    assert DaemonSettings(_env_file=None).docker_log_default_lines == 123
    assert DaemonSettings(_env_file=None, docker_log_default_lines=args.docker_log_default_lines).docker_log_default_lines == 321
    with pytest.raises(ValueError, match='must not exceed'):
        DaemonSettings(_env_file=None, docker_log_default_lines=1000)
    assert DaemonSettings(_env_file=None, docker_log_max_lines=args.docker_log_max_lines,
                          docker_log_max_streams=args.docker_log_max_streams).docker_log_max_streams == 4


def test_standalone_swarm_service_uses_its_own_service_identity():
    client = Mock()
    def read(path, **kwargs):
        if path == '/services/service123':
            return {'ID': 'service123', 'Spec': {'Name': 'worker'}}
        if path == '/containers/json':
            assert 'com.docker.swarm.service.id=service123' in kwargs['query']['filters']
            return [{'Id': 'a'*64}, {'Id': 'b'*64}]
        return {'Name': '/worker', 'Config': {'Labels': {'com.docker.swarm.service.id': 'service123' if 'a'*64 in path else 'unrelated',
                                                       'com.docker.swarm.service.name': 'worker'}}}
    client.request.side_effect = read
    result = sources(client, {'scope': 'service', 'kind': 'swarm', 'service_id': 'service123'}, DaemonSettings(_env_file=None))
    assert [item['id'] for item in result] == ['a'*64]


@pytest.mark.parametrize('chunked', [True, False])
def test_idle_log_stream_outlives_request_timeout_and_stops(tmp_path, chunked):
    path = tmp_path/'docker.sock'; ready = threading.Event(); received = threading.Event(); stop = threading.Event(); errors = []
    def remote():
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as listener:
            listener.bind(str(path)); listener.listen(1); ready.set()
            connection, _ = listener.accept()
            with connection:
                connection.settimeout(3); request_data = connection.recv(4096)
                assert b'follow=true' in request_data and b'tail=0' in request_data
                connection.sendall(b'HTTP/1.1 200 OK\r\n' + (b'Transfer-Encoding: chunked\r\n' if chunked else b'') + b'\r\n')
                time.sleep(.3)
                message = frame(2, '2026-11-01T10:00:00Z live\n')
                if chunked:
                    message = f'{len(message):x}\r\n'.encode() + message + b'\r\n'
                connection.sendall(message); connection.recv(4096)
    def follow():
        try:
            DockerClient(f'unix://{path}', api_version='v1.47', timeout=.1).read_logs('a'*64, lambda chunk: received.set(), stop, tail=0, follow=True)
        except Exception as exc:
            errors.append(exc)
    remote_thread = threading.Thread(target=remote, daemon=True); reader = threading.Thread(target=follow, daemon=True)
    remote_thread.start(); assert ready.wait(2); reader.start()
    try:
        assert received.wait(2), errors
        assert reader.is_alive()
    finally:
        stop.set(); reader.join(2); remote_thread.join(2)
    assert not reader.is_alive() and not remote_thread.is_alive() and not errors


def test_live_queue_is_bounded_and_reports_dropped_records():
    async def run():
        server = DaemonServer(DaemonSettings(_env_file=None))
        server.events.bind(asyncio.get_running_loop())
        live = LiveLogs(engine(), server.settings, {'scope': 'container', 'id': 'a'*64}, server.events)
        for index in range(1000):
            live.publish({'type': 'line', 'line': {'text': str(index)}})
        await asyncio.sleep(0)
        records = live.records()
        assert (await anext(records))['type'] == 'gap'
        assert (await anext(records))['line']['text'] == '744'
        assert len(live.queue) == 0
        await records.aclose()
        assert live.closed and not server.events.subscribers
    asyncio.run(run())


def test_log_subscription_requires_auth_and_stops_on_disconnect(tmp_path):
    async def run():
        token = 'test-auth-token-at-least-32-characters'
        server = DaemonServer(DaemonSettings(_env_file=None, docker_access='ro', access_token=token))
        server.events.bind(asyncio.get_running_loop()); server.docker = engine()
        stopped = threading.Event()
        def logs(identity, callback, stop, **kwargs):
            callback(frame(2, '2026-11-01T10:00:00Z live\n'))
            stop.wait(3)
            if stop.is_set(): stopped.set()
        server.docker.read_logs.side_effect = logs
        path = tmp_path/'daemon.sock'
        listener = await asyncio.start_unix_server(server.handle_client, path=str(path))
        try:
            reader, writer = await asyncio.open_unix_connection(str(path))
            params = {'scope': 'container', 'id': 'a'*64, 'limit': 500}
            writer.write(encode_message(request('docker.logs.subscribe', params, 'wrong'))); await writer.drain()
            assert json.loads(await reader.readline())['error']['code'] == -32001
            server.docker.read_logs.assert_not_called()
            writer.write(encode_message(request('docker.logs.subscribe', params, token))); await writer.drain()
            assert 'result' in json.loads(await asyncio.wait_for(reader.readline(), 2))
            event = json.loads(await asyncio.wait_for(reader.readline(), 2))
            assert event['method'] == 'docker.log' and event['params']['line']['stream'] == 'stderr'
            writer.close(); await writer.wait_closed()
            assert await asyncio.to_thread(stopped.wait, 2)
            for _ in range(20):
                if not server.events.subscribers: break
                await asyncio.sleep(.01)
            assert not server.events.subscribers
        finally:
            listener.close(); await listener.wait_closed()
    asyncio.run(run())


def test_replay_keeps_new_repeated_boundary_lines_without_repeating_old_ones():
    stamp = '2026-11-01T10:00:00.123456789Z'
    position = LogPosition({'timestamp': stamp, 'count': 2})
    records = [{'timestamp': '2026-11-01T10:00:00.000000001Z'}, *[{'timestamp': stamp} for _ in range(3)],
               {'timestamp': '2026-11-01T10:00:01Z'}]
    assert [position.accept(record) for record in records] == [False, False, False, True, True]


@pytest.mark.parametrize('scope', ['stack', 'service'])
def test_live_catches_new_finished_jobs_and_restarts_without_manual_fetch(scope):
    async def run():
        settings = DaemonSettings(_env_file=None, docker_log_discovery_interval_seconds=.05)
        server = DaemonServer(settings); server.events.bind(asyncio.get_running_loop())
        client = Mock()
        run1, run2 = '2026-11-01T10:00:00Z', '2026-11-01T10:00:02Z'
        containers = {'a'*64: {'started': run1, 'running': True, 'lines': [(run1, 'already shown'), ('2026-11-01T10:00:01Z', 'after snapshot')]}}
        calls = []
        def request(path, **kwargs):
            if path == '/containers/json':
                return [{'Id': key} for key in containers]
            key = path.split('/')[2]; source = containers[key]
            return {'Id': key, 'Name': '/web-' + key[:1], 'Config': {'Labels': {
                'com.docker.compose.project': 'demo', 'com.docker.compose.service': 'web'}},
                'State': {'Running': source['running'], 'StartedAt': source['started']}}
        def logs(identity, callback, stop, **kwargs):
            calls.append((identity, kwargs))
            for timestamp, text in list(containers[identity]['lines']):
                callback(frame(1, timestamp + ' ' + text + '\n'))
            if kwargs['follow']:
                stop.wait(3)
        client.request.side_effect = request; client.read_logs.side_effect = logs
        live = LiveLogs(client, settings, {'scope': scope, 'kind': 'compose', 'name': 'demo', 'service': 'web',
            'cursors': {'a'*64: {'timestamp': run1, 'count': 1, 'started_at': run1}}}, server.events)
        await live.reconcile(); records = live.records()
        async def next_line():
            while True:
                item = await asyncio.wait_for(anext(records), 2)
                if item['type'] == 'line': return item['line']
        try:
            assert (await next_line())['text'] == 'after snapshot'
            # No Docker event: the bounded fallback must also discover a job
            # which has already stopped by the time its logs are opened.
            containers['b'*64] = {'started': run2, 'running': False, 'lines': [(run2, 'short job startup')]}
            assert (await next_line())['text'] == 'short job startup'
            await live.reconcile()
            assert len([call for call in calls if call[0] == 'b'*64]) == 1
            # Same ID, new StartedAt; the previous follow connection is stale.
            containers['a'*64]['started'] = run2
            containers['a'*64]['lines'].append((run2, 'restarted startup'))
            server.events.publish('docker.container.start', {})
            assert (await next_line())['text'] == 'restarted startup'
            assert calls[0][1]['since'] is not None
        finally:
            await records.aclose()
            for _, worker in live.workers.values():
                await asyncio.to_thread(worker.join, 1)
    asyncio.run(run())
