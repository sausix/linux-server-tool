"""Live Docker, rtnetlink, and inotify event sources for backend."""

from __future__ import annotations

import asyncio
import ctypes
import json
import logging
import os
import select
import shutil
import socket
import struct
import subprocess
import threading
import time
import uuid
from pathlib import Path
from typing import Callable

from backend.network.management import detect
from backend.system.sysctl import snapshot as routing_snapshot
from backend.network.firewall import snapshot as firewall_snapshot

NETLINK_HEADER = struct.Struct("=IHHII")
INOTIFY_EVENT = struct.Struct("=iIII")
RTM_TYPES = {
    16: "network.link.changed",
    17: "network.link.deleted",
    20: "network.address.changed",
    21: "network.address.deleted",
    24: "network.route.changed",
    25: "network.route.deleted",
}
RTM_GROUPS = 0x1 | 0x10 | 0x40 | 0x100 | 0x400
logger = logging.getLogger('backend.events')
INOTIFY_MASK = 0x00000004 | 0x00000008 | 0x00000080 | 0x00000100 | 0x00000200 | 0x00000400 | 0x00000800


class EventBroker:
    def __init__(self):
        self.loop: asyncio.AbstractEventLoop | None = None
        self.subscribers: dict[str, tuple[set[str], asyncio.Queue]] = {}
        self.sequence = 0
        self.observers = []

    def observe(self, callback):
        self.observers.append(callback)

    def _observe(self, topic, data):
        for callback in self.observers:
            try:
                callback(topic, data)
            except Exception:
                logger.exception('Unable to refresh a subscribed internal model')

    def bind(self, loop: asyncio.AbstractEventLoop) -> None:
        self.loop = loop

    def subscribe(self, topics: list[str]) -> tuple[str, asyncio.Queue]:
        subscription_id = uuid.uuid4().hex
        queue: asyncio.Queue = asyncio.Queue(maxsize=256)
        self.subscribers[subscription_id] = (set(topics or ["*"]), queue)
        return subscription_id, queue

    def unsubscribe(self, subscription_id: str) -> None:
        self.subscribers.pop(subscription_id, None)

    def publish_from_thread(self, topic: str, data: dict) -> None:
        self._observe(topic, data)
        if self.loop and not self.loop.is_closed():
            self.loop.call_soon_threadsafe(self.publish, topic, data, False)

    def publish(self, topic: str, data: dict, notify=True) -> None:
        if notify:
            self._observe(topic, data)
        self.sequence += 1
        event = {
            "topic": topic,
            "sequence": self.sequence,
            "timestamp": time.time(),
            "data": data,
        }
        for topics, queue in list(self.subscribers.values()):
            if "*" not in topics and not any(
                topic == selected or topic.startswith(f"{selected}.") for selected in topics
            ):
                continue
            if queue.full():
                try:
                    queue.get_nowait()
                except asyncio.QueueEmpty:
                    pass
            queue.put_nowait(event)


class NetworkEventSource(threading.Thread):
    def __init__(self, publish: Callable[[str, dict], None], stop: threading.Event):
        super().__init__(name="backend-rtnetlink", daemon=True)
        self.publish = publish
        self.stop = stop
        self.allowed_topics = None

    def run(self) -> None:
        try:
            with socket.socket(socket.AF_NETLINK, socket.SOCK_RAW, socket.NETLINK_ROUTE) as netlink:
                netlink.bind((0, RTM_GROUPS))
                netlink.settimeout(1.0)
                while not self.stop.is_set():
                    try:
                        packet = netlink.recv(1024 * 1024)
                    except TimeoutError:
                        continue
                    except OSError as exc:
                        logger.warning('network.events: rtnetlink receive failed: %s', exc)
                        return
                    offset = 0
                    while offset + NETLINK_HEADER.size <= len(packet):
                        length, message_type, _flags, _sequence, _pid = NETLINK_HEADER.unpack_from(packet, offset)
                        if length < NETLINK_HEADER.size:
                            break
                        offset += (length + 3) & ~3
                        if topic := RTM_TYPES.get(message_type):
                            if self.allowed_topics is None or any(topic.startswith(prefix) for prefix in self.allowed_topics):
                                self.publish(topic, {"message_type": message_type})
        except OSError as exc:
            logger.warning('network.events: rtnetlink subscription unavailable: %s', exc)
            return


class InotifyEventSource(threading.Thread):
    def __init__(
        self, paths: list[Path], publish: Callable[[str, dict], None], stop: threading.Event,
        topic: str = 'network.config.changed',
    ):
        super().__init__(name="backend-inotify", daemon=True)
        self.paths = paths
        self.topic = topic
        self.publish = publish
        self.stop = stop

    def run(self) -> None:
        libc = ctypes.CDLL(None, use_errno=True)
        descriptor = libc.inotify_init1(os.O_NONBLOCK | os.O_CLOEXEC)
        if descriptor < 0:
            logger.warning('%s: inotify initialization failed: %s', self.topic, os.strerror(ctypes.get_errno()))
            return
        watches: dict[int, str] = {}
        try:
            for path in self.paths:
                watch_path = path if path.is_dir() else path.parent
                if not watch_path.exists():
                    logger.warning('%s: watch directory unavailable: %s', self.topic, watch_path)
                    continue
                watch = libc.inotify_add_watch(
                    descriptor, os.fsencode(watch_path), ctypes.c_uint32(INOTIFY_MASK)
                )
                if watch >= 0:
                    watches[watch] = str(watch_path)
                else:
                    logger.warning('%s: cannot watch %s: %s', self.topic, watch_path, os.strerror(ctypes.get_errno()))
            while not self.stop.wait(0.25):
                try:
                    payload = os.read(descriptor, 64 * 1024)
                except BlockingIOError:
                    continue
                except OSError:
                    return
                offset = 0
                while offset + INOTIFY_EVENT.size <= len(payload):
                    watch, mask, _cookie, name_length = INOTIFY_EVENT.unpack_from(payload, offset)
                    offset += INOTIFY_EVENT.size
                    raw_name = payload[offset : offset + name_length]
                    offset += name_length
                    name = raw_name.rstrip(b"\0").decode("utf-8", errors="replace")
                    self.publish(
                        self.topic,
                        {"directory": watches.get(watch), "name": name, "mask": mask},
                    )
        finally:
            os.close(descriptor)


class NftEventSource(threading.Thread):
    def __init__(self, publish: Callable[[str, dict], None], stop: threading.Event):
        super().__init__(name="backend-nft-events", daemon=True)
        self.publish = publish
        self.stop = stop

    def run(self) -> None:
        nft = shutil.which("nft")
        if not nft:
            logger.warning('network.firewall.events: nft tool not found; live firewall monitoring unavailable')
            return
        while not self.stop.is_set():
            try:
                process = subprocess.Popen(
                    [nft, "--json", "monitor"],
                    stdout=subprocess.PIPE,
                    stderr=None,
                    text=True,
                    env={"PATH": "/usr/sbin:/usr/bin:/sbin:/bin"},
                )
            except OSError as exc:
                logger.warning('network.firewall.events: cannot launch nft monitor: %s', exc)
                return
            try:
                while not self.stop.is_set() and process.poll() is None:
                    readable, _, _ = select.select([process.stdout], [], [], 1.0)
                    if not readable or process.stdout is None:
                        continue
                    line = process.stdout.readline()
                    if not line:
                        continue
                    try:
                        event = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    self.publish("network.firewall.changed", event)
            finally:
                if process.poll() is None:
                    process.terminate()
                    try:
                        process.wait(timeout=2)
                    except subprocess.TimeoutExpired:
                        process.kill()
                if not self.stop.is_set():
                    logger.warning('network.firewall.events: nft monitor exited with status %s; see tool stderr above; retrying in 60 seconds', process.returncode)
                    self.stop.wait(60.0)


class RoutingOptionsEventSource(threading.Thread):
    """procfs sysctl writes have no reliable inotify or rtnetlink notification."""

    def __init__(self, publish, stop, sources=None):
        super().__init__(name='backend-routing-options', daemon=True)
        self.publish = publish
        self.stop = stop
        self.sources = sources or {}

    def run(self):
        previous = None
        while not self.stop.is_set():
            try:
                current = routing_snapshot(include_routes=False, **self.sources)
                if previous is not None and current != previous:
                    self.publish('network.routing.changed', {})
                previous = current
            except OSError as exc:
                logger.warning('network.routing.events: sysctl scan failed: %s', exc)
            self.stop.wait(3)


class FirewallPollingEventSource(threading.Thread):
    """Legacy iptables has no equivalent to the nftables monitor stream."""

    def __init__(self, publish, stop, settings):
        super().__init__(name='backend-iptables-events', daemon=True)
        self.publish, self.stop, self.settings = publish, stop, settings

    def run(self):
        previous = None
        while not self.stop.is_set():
            current = firewall_snapshot(self.settings.command_timeout_seconds, self.settings.firewall_backend)
            # Save-format timestamps change on every read; they are not rule changes.
            for field in ('ipv4_rules', 'ipv6_rules'):
                if current.get(field):
                    current[field] = '\n'.join(line for line in current[field].splitlines() if not line.startswith('#'))
            if previous is not None and current != previous:
                self.publish('network.firewall.changed', {'backend': current['backend']})
            previous = current
            self.stop.wait(5)


class EventRuntime:
    def __init__(self, broker: EventBroker, docker, settings, docker_source=None):
        self.broker = broker
        self.docker = docker
        self.docker_source = docker_source
        self.settings = settings
        self.stop = threading.Event()
        self.sources: list[threading.Thread] = []
        self.compose_directories = set()
        self.source_lock = threading.Lock()

    def add_compose_paths(self, paths):
        if not self.settings.events_enabled or self.settings.docker_access == 'disabled':
            return
        with self.source_lock:
            if self.stop.is_set():
                return
            directories = {Path(path).parent for path in paths if Path(path).parent.is_dir()} - self.compose_directories
            if directories:
                source = InotifyEventSource(list(directories), self.broker.publish_from_thread, self.stop,
                                            topic='docker.compose.changed')
                self.compose_directories.update(directories)
                self.sources.append(source)
                source.start()

    def start(self) -> None:
        publish = self.broker.publish_from_thread
        if self.settings.network_access != "disabled":
            features = self.settings.features
            topics = []
            if features['interfaces'] != 'disabled':
                topics += ['network.link.', 'network.address.']
            if features['routing'] != 'disabled':
                topics += ['network.route.']
            if topics:
                source = NetworkEventSource(publish, self.stop)
                source.allowed_topics = topics
                self.sources.append(source)
            if features['firewall'] != 'disabled':
                if self.settings.firewall_backend in {'auto', 'nftables'} and shutil.which('nft'):
                    self.sources.append(NftEventSource(publish, self.stop))
                if self.settings.firewall_backend != 'nftables':
                    self.sources.append(FirewallPollingEventSource(publish, self.stop, self.settings))
            managers = detect(
                self.settings.networkd_directory, self.settings.interfaces_file
            )['detected'] if features['interfaces'] != 'disabled' else []
            paths = []
            for manager in managers:
                if manager['name'] == 'systemd-networkd':
                    paths.append(self.settings.networkd_directory)
                elif manager['name'] == 'ifupdown':
                    paths.append(self.settings.interfaces_file)
                    snippets = self.settings.interfaces_file.parent / 'interfaces.d'
                    if snippets.is_dir():
                        paths.append(snippets)
            if paths:
                self.sources.append(InotifyEventSource(paths, publish, self.stop))
        if self.settings.feature_access('sysctl') != 'disabled':
            self.sources.append(RoutingOptionsEventSource(publish, self.stop, self.settings.sysctl_sources))
        if self.settings.docker_access != "disabled":
            if self.docker_source is not None:
                self.sources.append(self.docker_source(self.docker, publish, self.stop))
        for source in self.sources:
            source.start()

    def close(self) -> None:
        with self.source_lock:
            self.stop.set()
            sources = list(self.sources)
        for source in sources:
            source.join(timeout=2.0)
