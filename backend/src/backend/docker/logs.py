"""Bounded Docker log history and cancellable per-viewer live subscriptions."""

import asyncio
import codecs
import heapq
import json
import re
import struct
import threading
import time
from collections import deque
from datetime import datetime
from concurrent.futures import ThreadPoolExecutor

from backend.docker.client import DockerError

MAX_LINE = 4096
MAX_HISTORY_BYTES = 4 * 1024 * 1024
TIMESTAMP = re.compile(r'^(\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d(?:\.\d+)?Z) (.*)$', re.S)
ANSI = re.compile(r'\x1b\][^\x07]*(?:\x07|\x1b\\)|\x1b\[[0-?]*[ -/]*[@-~]')


def validate_target(params, settings):
    scope = params.get('scope')
    if scope not in {'stack', 'service', 'container'}:
        raise ValueError('Log scope must be stack, service or container')
    limit = params.get('limit', settings.docker_log_default_lines)
    if type(limit) is not int or not 1 <= limit <= settings.docker_log_max_lines:
        raise ValueError(f'Line limit must be between 1 and {settings.docker_log_max_lines}')
    if scope == 'container':
        if not isinstance(params.get('id'), str) or not re.fullmatch(r'[a-fA-F0-9]{12,64}', params['id']):
            raise ValueError('Invalid container ID')
    else:
        if params.get('kind') not in {'compose', 'compose-meta', 'swarm'}:
            raise ValueError('Invalid stack kind')
        if scope == 'service' and params['kind'] == 'swarm' and params.get('service_id'):
            if not isinstance(params['service_id'], str) or not re.fullmatch(r'[a-zA-Z0-9]{1,128}', params['service_id']):
                raise ValueError('Invalid Swarm service ID')
            return limit
        if not isinstance(params.get('name'), str) or not re.fullmatch(r'[a-zA-Z0-9][a-zA-Z0-9_.-]{0,127}', params['name']):
            raise ValueError('Invalid stack name')
        if scope == 'service' and (not isinstance(params.get('service'), str) or not params['service'] or len(params['service']) > 256):
            raise ValueError('A service name is required')
    return limit


def sources(client, params, settings):
    validate_target(params, settings)
    service_identity = None
    if params['scope'] == 'container':
        ids = [params['id']]
    else:
        swarm = params['kind'] == 'swarm'
        label = 'com.docker.stack.namespace' if swarm else 'com.docker.compose.project'
        filters = {'label': [f"{label}={params.get('name', '')}"]}
        if params['scope'] == 'service' and swarm and params.get('service_id'):
            service = client.request('/services/' + params['service_id'])
            service_identity = service.get('ID')
            if not isinstance(service_identity, str) or not re.fullmatch(r'[a-zA-Z0-9]{1,128}', service_identity):
                raise DockerError('Docker returned an invalid service identity')
            filters = {'label': [f'com.docker.swarm.service.id={service_identity}']}
        elif params['scope'] == 'service':
            key = 'com.docker.swarm.service.name' if swarm else 'com.docker.compose.service'
            filters['label'].append(f"{key}={params['service']}")
        raw = client.request('/containers/json', query={'all': 'true', 'filters': json.dumps(filters)})
        ids = [item['Id'] for item in raw]
    if len(ids) > settings.docker_log_max_streams:
        raise ValueError(f'Target exceeds the {settings.docker_log_max_streams}-container log limit; select a service or container')
    result = []
    for identity in sorted(set(ids)):
        if not isinstance(identity, str) or not re.fullmatch(r'[a-fA-F0-9]{12,64}', identity):
            raise ValueError('Docker returned an invalid container ID')
        try:
            raw = client.request(f'/containers/{identity}/json')
        except DockerError as exc:
            if params['scope'] != 'container' and exc.status_code == 404:
                continue
            raise
        config = raw.get('Config') or {}
        labels = config.get('Labels') or {}
        service = labels.get('com.docker.compose.service') or labels.get('com.docker.swarm.service.name', '')
        # Recheck labels after discovery, rather than trusting filtering alone.
        if params['scope'] != 'container':
            key = 'com.docker.stack.namespace' if params['kind'] == 'swarm' else 'com.docker.compose.project'
            if service_identity:
                if labels.get('com.docker.swarm.service.id') != service_identity:
                    continue
            elif labels.get(key) != params['name'] or (params['scope'] == 'service' and service != params['service']):
                continue
        canonical = raw.get('Id', identity)
        if not isinstance(canonical, str) or not re.fullmatch(r'[a-fA-F0-9]{12,64}', canonical) or not canonical.lower().startswith(identity.lower()):
            raise DockerError('Docker inspect returned a different container identity')
        result.append({'id': canonical, 'name': raw.get('Name', '').lstrip('/') or canonical[:12],
                       'service': service, 'tty': bool(config.get('Tty')),
                       'started_at': (raw.get('State') or {}).get('StartedAt') or '',
                       'running': (raw.get('State') or {}).get('Running', False)})
    return result


def timestamp_key(stamp):
    if not stamp:
        return ''
    return stamp[:19] + '.' + (stamp[20:-1] if '.' in stamp else '').ljust(9, '0')


class LogPosition:
    """Skip an inclusive Docker replay without dropping repeated boundary lines."""
    def __init__(self, checkpoint=None):
        checkpoint = checkpoint or {}
        self.timestamp, self.count = checkpoint.get('timestamp'), checkpoint.get('count', 0)
        self.boundary, self.remaining = timestamp_key(self.timestamp), self.count
        self.replaying = bool(self.boundary)

    def accept(self, record):
        stamp = record['timestamp']
        if stamp and self.replaying:
            key = timestamp_key(stamp)
            if key < self.boundary:
                return False
            if key == self.boundary and self.remaining:
                self.remaining -= 1
                if not self.remaining:
                    self.replaying = False
                return False
            self.replaying = False
        self.observe(record)
        return True

    def observe(self, record):
        stamp = record['timestamp']
        if stamp:
            if timestamp_key(stamp) > timestamp_key(self.timestamp):
                self.timestamp, self.count = stamp, 1
            elif timestamp_key(stamp) == timestamp_key(self.timestamp):
                self.count += 1

    def snapshot(self, generation):
        return {'timestamp': self.timestamp, 'count': self.count, 'started_at': generation}

    def since(self):
        if self.timestamp:
            return max(0, int(datetime.fromisoformat(self.timestamp.replace('Z', '+00:00')).timestamp()))
        return None


class LogDecoder:
    def __init__(self, source, callback):
        self.source, self.callback = source, callback
        self.header, self.remaining, self.stream = bytearray(), 0, 'stdout'
        self.decoders, self.lines, self.truncated = {}, {}, {}

    def text(self, stream, data, final=False):
        decoder = self.decoders.setdefault(stream, codecs.getincrementaldecoder('utf-8')('replace'))
        text = decoder.decode(data, final=final)
        parts = text.split('\n')
        for index, part in enumerate(parts):
            current = self.lines.get(stream, '') + part
            if len(current) > MAX_LINE + 64:
                self.truncated[stream] = True
            self.lines[stream] = current[:MAX_LINE + 64]
            if index < len(parts) - 1:
                self.emit(stream)

    def emit(self, stream):
        value = ANSI.sub('', self.lines.pop(stream, '')).rstrip('\r')
        value = ''.join(char for char in value if char >= ' ' or char == '\t')
        match = TIMESTAMP.match(value)
        timestamp, text = (match.group(1), match.group(2)) if match else (None, value)
        self.callback({'container_id': self.source['id'], 'name': self.source['name'], 'service': self.source['service'],
                       'stream': stream, 'timestamp': timestamp, 'text': text[:MAX_LINE],
                       'truncated': self.truncated.pop(stream, False) or len(text) > MAX_LINE})

    def feed(self, chunk):
        if self.source['tty']:
            self.text('combined', chunk); return
        view = memoryview(chunk)
        while view:
            if not self.remaining:
                count = min(8 - len(self.header), len(view))
                self.header.extend(view[:count]); view = view[count:]
                if len(self.header) < 8:
                    return
                kind, length = self.header[0], struct.unpack('>I', self.header[4:])[0]
                if kind not in {1, 2} or self.header[1:4] != b'\0\0\0':
                    raise DockerError('Docker returned invalid multiplexed log frames')
                self.header.clear(); self.stream = 'stderr' if kind == 2 else 'stdout'; self.remaining = length
                if not length:
                    continue
            count = min(self.remaining, len(view))
            self.text(self.stream, view[:count].tobytes()); view = view[count:]; self.remaining -= count

    def finish(self):
        for stream in list(self.decoders):
            self.text(stream, b'', final=True)
            if self.lines.get(stream):
                self.emit(stream)
        if self.header or self.remaining:
            raise DockerError('Docker log stream ended inside a multiplexed frame')


def history(client, params, settings):
    limit = validate_target(params, settings)
    selected = sources(client, params, settings)
    cursors = {}
    heap, errors, size, sequence = [], [], 0, 0
    lock = threading.Lock()
    def collect(record):
        nonlocal size, sequence
        length = len(json.dumps(record).encode())
        with lock:
            sequence += 1
            # Docker timestamps are UTC. Pad fractional seconds for comparison.
            key = timestamp_key(record['timestamp'])
            heapq.heappush(heap, (key, sequence, record, length)); size += length
            while len(heap) > limit or size > MAX_HISTORY_BYTES:
                size -= heapq.heappop(heap)[3]
    def read(source):
        position = LogPosition()
        def collect_source(record):
            position.observe(record); collect(record)
        decoder = LogDecoder(source, collect_source)
        try:
            client.read_logs(source['id'], decoder.feed, threading.Event(), tail=limit)
            decoder.finish()
            with lock:
                cursors[source['id']] = position.snapshot(source['started_at'])
        except (DockerError, OSError, ValueError) as exc:
            with lock:
                errors.append(f"{source['name']}: {exc}")
    with ThreadPoolExecutor(max_workers=min(4, len(selected)) or 1) as executor:
        list(executor.map(read, selected))
    return {'lines': [item[2] for item in sorted(heap)], 'sources': selected, 'errors': sorted(errors), 'cursors': cursors,
            'note': 'Logs cover containers visible to this Docker endpoint. TTY streams combine stdout and stderr. Long lines and history bytes are bounded.'}


class LiveLogs:
    def __init__(self, client, settings, params, broker):
        validate_target(params, settings)
        self.client, self.settings, self.params, self.broker = client, settings, params, broker
        self.queue = deque(maxlen=256)
        self.queue_lock = threading.Lock()
        self.ready, self.notified = asyncio.Event(), False
        self.workers, self.closed, self.dropped = {}, False, 0
        self.positions, self.completed, self.worker_generations = {}, {}, {}
        self.position_lock = threading.Lock()
        cursors = params.get('cursors', {})
        if not isinstance(cursors, dict) or len(cursors) > settings.docker_log_max_streams:
            raise ValueError('Invalid log checkpoints')
        for identity, cursor in cursors.items():
            if (not isinstance(identity, str) or not re.fullmatch(r'[a-fA-F0-9]{12,64}', identity)
                    or not isinstance(cursor, dict) or type(cursor.get('count')) is not int
                    or not 0 <= cursor['count'] <= 1000000000
                    or not isinstance(cursor.get('started_at'), str) or len(cursor['started_at']) > 64):
                raise ValueError('Invalid log checkpoint')
            stamp = cursor.get('timestamp')
            if stamp is not None and (not isinstance(stamp, str) or len(stamp) > 64 or not TIMESTAMP.match(stamp + ' ')):
                raise ValueError('Invalid log timestamp checkpoint')
            try:
                if stamp:
                    datetime.fromisoformat(stamp.replace('Z', '+00:00'))
            except ValueError:
                raise ValueError('Invalid log timestamp checkpoint') from None
            self.positions[identity] = dict(cursor)
        self.loop = asyncio.get_running_loop()

    def publish(self, record):
        with self.queue_lock:
            if self.closed:
                return
            if len(self.queue) == self.queue.maxlen:
                self.dropped += 1
            self.queue.append(record)
            if not self.notified:
                self.notified = True
                try:
                    self.loop.call_soon_threadsafe(self.ready.set)
                except RuntimeError:
                    pass

    def worker(self, source, stop):
        identity, generation = source['id'], source['started_at']
        with self.position_lock:
            checkpoint = dict(self.positions.get(identity, {}))
        position = LogPosition(checkpoint)
        def append(line):
            if stop.is_set() or not position.accept(line):
                return
            with self.position_lock:
                self.positions[identity] = position.snapshot(generation)
            self.publish({'type': 'line', 'line': line})
        decoder = LogDecoder(source, append)
        complete = False
        try:
            self.client.read_logs(identity, decoder.feed, stop,
                                  tail='all' if checkpoint.get('timestamp') else self.params.get('limit', self.settings.docker_log_default_lines),
                                  since=position.since(), follow=source['running'])
            if not stop.is_set():
                decoder.finish()
                complete = True
        except (DockerError, OSError, ValueError) as exc:
            if not stop.is_set():
                self.publish({'type': 'error', 'message': f"{source['name']}: {exc}"})
        finally:
            if complete:
                with self.position_lock:
                    self.completed[identity] = generation

    async def reconcile(self):
        selected = await asyncio.to_thread(sources, self.client, self.params, self.settings)
        if self.closed:
            return selected
        current = {source['id']: source for source in selected}
        for identity in list(self.workers):
            if identity not in current:
                stop, worker = self.workers[identity]
                stop.set()
                if not worker.is_alive():
                    self.workers.pop(identity)
        for identity, source in current.items():
            existing = self.workers.get(identity)
            if existing and existing[1].is_alive():
                if self.worker_generations.get(identity) != source['started_at'] or not source['running']:
                    existing[0].set()
                continue
            with self.position_lock:
                completed = self.completed.get(identity)
                previous = self.positions.get(identity)
            if not source['running'] and completed == source['started_at']:
                continue
            if not existing and len(self.workers) >= self.settings.docker_log_max_streams:
                self.publish({'type': 'gap', 'message': 'Waiting for previous log streams to close; fetch recent lines to recover history'})
                break
            with self.position_lock:
                if previous and previous.get('started_at') != source['started_at']:
                    stamp = source['started_at']
                    if TIMESTAMP.match(stamp + ' ') and timestamp_key(previous.get('timestamp')) >= timestamp_key(stamp):
                        self.positions[identity] = {**previous, 'started_at': stamp}
                    else:
                        self.positions[identity] = {'timestamp': stamp if TIMESTAMP.match(stamp + ' ') else None,
                                                   'count': 0, 'started_at': stamp}
            stop = threading.Event()
            worker = threading.Thread(target=self.worker, args=(source, stop), name='backend-log-stream', daemon=True)
            self.worker_generations[identity] = source['started_at']
            self.workers[identity] = (stop, worker); worker.start()
        with self.position_lock:
            for identity in set(self.positions) | set(self.completed) | set(self.worker_generations):
                if identity not in current and identity not in self.workers:
                    self.positions.pop(identity, None); self.completed.pop(identity, None); self.worker_generations.pop(identity, None)
        return selected

    async def records(self):
        subscription, changes = self.broker.subscribe(['docker'])
        async def watch():
            while True:
                try:
                    await asyncio.wait_for(changes.get(), timeout=self.settings.docker_log_discovery_interval_seconds)
                    await asyncio.sleep(.2)
                    while not changes.empty():
                        changes.get_nowait()
                except TimeoutError:
                    pass
                try:
                    await self.reconcile()
                except (DockerError, OSError, ValueError) as exc:
                    self.publish({'type': 'error', 'message': str(exc)})
        watcher = asyncio.create_task(watch())
        try:
            while True:
                await self.ready.wait()
                with self.queue_lock:
                    records = list(self.queue); self.queue.clear()
                    count, self.dropped = self.dropped, 0
                    self.ready.clear(); self.notified = False
                if count:
                    yield {'type': 'gap', 'message': f'{count} log notifications were dropped; fetch recent lines again'}
                for record in records:
                    yield record
        finally:
            self.broker.unsubscribe(subscription)
            watcher.cancel(); await asyncio.gather(watcher, return_exceptions=True)
            self.close()

    def close(self):
        self.closed = True
        for stop, _ in self.workers.values():
            stop.set()
