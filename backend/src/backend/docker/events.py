from __future__ import annotations
import threading
import time
import logging
from typing import Callable
from backend.docker.client import DockerClient, DockerError
logger = logging.getLogger("backend.docker.events")

class DockerEventSource(threading.Thread):
    def __init__(
        self,
        client: DockerClient,
        publish: Callable[[str, dict], None],
        stop: threading.Event,
    ):
        super().__init__(name="backend-docker-events", daemon=True)
        self.client = client
        self.publish = publish
        self.stop = stop

    def run(self) -> None:
        last_warning = float('-inf')
        while not self.stop.is_set():
            try:
                self.client.watch_events(
                    lambda event: self.publish(
                        f"docker.{event.get('Type', 'event')}", event
                    ),
                    self.stop,
                )
            except DockerError as exc:
                if self.stop.is_set():
                    return
                if time.monotonic() - last_warning >= 60:
                    logger.warning('docker.events: stream unavailable, retrying: %s', exc)
                    last_warning = time.monotonic()
                self.stop.wait(2.0)


