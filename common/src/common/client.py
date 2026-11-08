from __future__ import annotations

import socket
from urllib.parse import urlparse

from .protocol import (
    JSONRPC_VERSION,
    MAX_MESSAGE_BYTES,
    ProtocolError,
    RemoteError,
    decode_message,
    encode_message,
    request,
)


class DaemonUnavailable(Exception):
    pass


class DaemonClient:
    def __init__(self, endpoint: str, token: str | None = None, timeout: float = 1.0,
                 method_timeouts: dict[str, float] | None = None):
        self.endpoint = endpoint
        self.token = token
        self.timeout = timeout
        self.method_timeouts = method_timeouts or {}

    def _connect(self, timeout: float | None = None) -> socket.socket:
        timeout = self.timeout if timeout is None else timeout
        parsed = urlparse(self.endpoint)
        try:
            if parsed.scheme == "unix":
                connection = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
                connection.settimeout(timeout)
                try:
                    connection.connect(parsed.path)
                except OSError:
                    connection.close()
                    raise
                return connection
            if parsed.scheme == "tcp" and parsed.hostname and parsed.port:
                return socket.create_connection((parsed.hostname, parsed.port), timeout)
        except OSError as exc:
            raise DaemonUnavailable(str(exc)) from exc
        raise ValueError("Daemon endpoint must use unix:///path or tcp://host:port")

    def call(self, method: str, params: dict | None = None):
        outgoing = request(method, params, self.token)
        try:
            with self._connect(self.method_timeouts.get(method)) as connection:
                connection.sendall(encode_message(outgoing))
                chunks = bytearray()
                while not chunks.endswith(b"\n"):
                    chunk = connection.recv(4096)
                    if not chunk:
                        raise DaemonUnavailable("backend closed the connection")
                    chunks.extend(chunk)
                    if len(chunks) > MAX_MESSAGE_BYTES + 1:
                        raise ProtocolError(-32600, "Response is too large")
        except (OSError, TimeoutError) as exc:
            raise DaemonUnavailable(str(exc)) from exc
        incoming = decode_message(bytes(chunks).rstrip(b"\r\n"))
        if incoming.get("jsonrpc") != JSONRPC_VERSION or incoming.get("id") != outgoing["id"]:
            raise ProtocolError(-32600, "Invalid response")
        if "error" in incoming:
            error = incoming["error"]
            raise RemoteError(
                error.get("code", -32603),
                error.get("message", "Remote error"),
                error.get("data"),
            )
        if "result" not in incoming:
            raise ProtocolError(-32600, "Invalid response")
        return incoming["result"]

    def subscribe(self, topics: list[str] | None = None, timeout: float | None = None):
        """Yield server-pushed JSON-RPC event notifications on one connection."""
        outgoing = request("events.subscribe", {"topics": topics or ["*"]}, self.token)
        connection = self._connect()
        try:
            connection.settimeout(timeout)
            connection.sendall(encode_message(outgoing))
            acknowledged = False
            buffer = bytearray()
            while True:
                while b"\n" not in buffer:
                    try:
                        chunk = connection.recv(4096)
                    except (OSError, TimeoutError) as exc:
                        raise DaemonUnavailable(str(exc)) from exc
                    if not chunk:
                        raise DaemonUnavailable("backend closed the subscription")
                    buffer.extend(chunk)
                    if len(buffer) > MAX_MESSAGE_BYTES + 1:
                        raise ProtocolError(-32600, "Subscription message is too large")
                raw_message, _, remainder = buffer.partition(b"\n")
                buffer = bytearray(remainder)
                incoming = decode_message(raw_message.rstrip(b"\r"))
                if not acknowledged and incoming.get("id") == outgoing["id"]:
                    if "error" in incoming:
                        error = incoming["error"]
                        raise RemoteError(
                            error.get("code", -32603),
                            error.get("message", "Remote error"),
                            error.get("data"),
                        )
                    acknowledged = True
                    continue
                if acknowledged and incoming.get("method") == "event":
                    yield incoming.get("params", {})
        finally:
            connection.close()

    def ping(self) -> dict:
        return self.call("ping")

