"""Minimal Docker Engine API client using only the Python standard library."""

from __future__ import annotations

import http.client
import json
import socket
import ssl
import threading
from pathlib import Path
from urllib.parse import urlencode, urlparse
from typing import Callable


from common.errors import BackendError


class DockerError(BackendError):
    def __init__(self, message, status_code=None):
        super().__init__(message)
        self.status_code = status_code


class UnixHTTPConnection(http.client.HTTPConnection):
    def __init__(self, socket_path: str, timeout: float):
        super().__init__("localhost", timeout=timeout)
        self.socket_path = socket_path

    def connect(self) -> None:
        self.sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.sock.settimeout(self.timeout)
        self.sock.connect(self.socket_path)


class DockerClient:
    def __init__(
        self,
        endpoint: str,
        api_version: str = "auto",
        timeout: float = 5.0,
        tls_verify: bool = True,
        tls_ca: Path | None = None,
        tls_cert: Path | None = None,
        tls_key: Path | None = None,
    ):
        self.endpoint = endpoint
        self.api_version = api_version
        self.timeout = timeout
        self.tls_verify = tls_verify
        self.tls_ca = tls_ca
        self.tls_cert = tls_cert
        self.tls_key = tls_key
        self._negotiated_api_version: str | None = None

    def _version_prefix(self) -> str:
        if self.api_version != "auto":
            return self.api_version
        if self._negotiated_api_version is None:
            version = self.request("/version", versioned=False)
            api_version = version.get("ApiVersion") if isinstance(version, dict) else None
            if not isinstance(api_version, str) or not api_version.replace(".", "").isdigit():
                raise DockerError("Docker API version negotiation failed")
            self._negotiated_api_version = f"v{api_version}"
        return self._negotiated_api_version

    def _connection(self):
        parsed = urlparse(self.endpoint)
        if parsed.scheme == "unix":
            return UnixHTTPConnection(parsed.path, self.timeout)
        host = parsed.hostname or "localhost"
        use_tls = parsed.scheme == "https" or (
            parsed.scheme == "tcp" and parsed.port == 2376
        )
        port = parsed.port or (2376 if use_tls else 2375)
        if not use_tls:
            return http.client.HTTPConnection(host, port, timeout=self.timeout)
        if self.tls_verify:
            context = ssl.create_default_context(cafile=str(self.tls_ca) if self.tls_ca else None)
        else:
            context = ssl._create_unverified_context()
        if self.tls_cert and self.tls_key:
            context.load_cert_chain(str(self.tls_cert), str(self.tls_key))
        return http.client.HTTPSConnection(host, port, timeout=self.timeout, context=context)

    def request(
        self,
        path: str,
        *,
        versioned: bool = True,
        query: dict | None = None,
        expect_json: bool = True,
        method: str = 'GET',
        timeout: float | None = None,
        data: dict | None = None,
    ):
        request_path = f"/{self._version_prefix()}{path}" if versioned else path
        if query:
            request_path = f"{request_path}?{urlencode(query)}"
        connection = self._connection()
        if timeout is not None:
            connection.timeout = timeout
        try:
            headers = {'Accept': 'application/json'}
            body_data = None
            if data is not None:
                body_data = json.dumps(data, allow_nan=False).encode()
                headers['Content-Type'] = 'application/json'
            connection.request(method, request_path, body=body_data, headers=headers)
            response = connection.getresponse()
            body = response.read()
        except (OSError, http.client.HTTPException, ssl.SSLError) as exc:
            raise DockerError(str(exc)) from exc
        finally:
            connection.close()
        if response.status >= 400:
            detail = body.decode("utf-8", errors="replace").strip()
            try:
                payload = json.loads(detail)
                if isinstance(payload, dict) and isinstance(payload.get('message'), str):
                    detail = payload['message']
            except json.JSONDecodeError:
                pass
            raise DockerError(detail or f'Docker API returned HTTP {response.status}', status_code=response.status)
        if not body:
            return None
        if not expect_json:
            return body.decode("utf-8", errors="replace")
        try:
            return json.loads(body)
        except json.JSONDecodeError as exc:
            raise DockerError("Docker API returned invalid JSON") from exc

    def ping(self) -> dict:
        result = self.request("/_ping", versioned=False, expect_json=False)
        return {"reachable": result == "OK"}

    def version(self) -> dict:
        return self.request("/version", versioned=False)

    def info(self) -> dict:
        return self.request("/info")

    def networks(self) -> list[dict]:
        return self.request("/networks")

    def read_logs(self, container_id, callback, stop, *, tail=200, follow=False, since=None):
        """Read Engine log bytes without decoding or losing multiplex headers."""
        import re
        if not isinstance(container_id, str) or not re.fullmatch(r'[a-fA-F0-9]{12,64}', container_id):
            raise ValueError('Invalid container ID')
        connection = self._connection()
        response, interrupter = None, None
        finished = threading.Event()
        try:
            path = f'/{self._version_prefix()}/containers/{container_id}/logs?'
            query = {'stdout': 'true', 'stderr': 'true', 'timestamps': 'true',
                     'tail': str(tail), 'follow': str(follow).lower()}
            if since is not None:
                query['since'] = str(since)
            path += urlencode(query)
            connection.request('GET', path)
            stream_socket = connection.sock
            def interrupt():
                while not finished.wait(.1):
                    if stop.is_set():
                        try:
                            stream_socket.shutdown(socket.SHUT_RDWR)
                        except OSError:
                            pass
                        return
            interrupter = threading.Thread(target=interrupt, name='backend-log-stop', daemon=True)
            interrupter.start()
            response = connection.getresponse()
            if response.status >= 400:
                raw = response.read(8192).decode('utf-8', errors='replace')
                try:
                    detail = json.loads(raw).get('message', '')
                except (ValueError, AttributeError):
                    detail = ''
                raise DockerError(detail or f'Docker logs returned HTTP {response.status}', response.status)
            if follow:
                stream_socket.settimeout(None)
            total = 0
            while not stop.is_set():
                chunk = response.read1(65536)
                if not chunk:
                    break
                total += len(chunk)
                callback(chunk)
                if not follow and total > 16 * 1024 * 1024:
                    raise DockerError('Log history exceeds the byte limit; reduce the requested line count')
        except (OSError, http.client.HTTPException, ssl.SSLError) as exc:
            if not stop.is_set():
                raise DockerError(str(exc)) from exc
        finally:
            finished.set()
            if interrupter:
                interrupter.join(timeout=.5)
            if response:
                response.close()
            connection.close()

    def watch_events(
        self, callback: Callable[[dict], None], stop: threading.Event
    ) -> None:
        connection = self._connection()
        response = None
        finished = threading.Event()
        interrupter = None
        try:
            connection.request(
                "GET",
                f"/{self._version_prefix()}/events",
                headers={"Accept": "application/json"},
            )
            stream_socket = connection.sock
            response = connection.getresponse()
            if response.status >= 400:
                raise DockerError(f"Docker events returned HTTP {response.status}")
            # Request setup remains bounded. Once subscribed, an idle stream is
            # healthy and must not inherit the ordinary request read timeout.
            stream_socket.settimeout(None)

            def interrupt_on_stop():
                while not finished.wait(0.1):
                    if stop.is_set():
                        try:
                            stream_socket.shutdown(socket.SHUT_RDWR)
                        except OSError:
                            pass
                        return

            interrupter = threading.Thread(target=interrupt_on_stop,
                                           name='backend-docker-stream-stop', daemon=True)
            interrupter.start()
            while not stop.is_set():
                try:
                    line = response.readline()
                except (OSError, TimeoutError) as exc:
                    if stop.is_set():
                        return
                    raise DockerError(str(exc)) from exc
                if not line:
                    if stop.is_set():
                        return
                    raise DockerError("Docker event stream closed")
                try:
                    event = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if isinstance(event, dict):
                    callback(event)
        except (OSError, http.client.HTTPException, ssl.SSLError) as exc:
            if stop.is_set():
                return
            raise DockerError(str(exc)) from exc
        finally:
            finished.set()
            if interrupter:
                interrupter.join(timeout=1)
            if response:
                response.close()
            connection.close()
