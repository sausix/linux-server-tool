"""Shared JSON-RPC 2.0 protocol primitives for application and backend."""

from __future__ import annotations

import json
import uuid
from typing import Any

JSONRPC_VERSION = "2.0"
# Inventory responses, especially nftables rulesets and Docker info, can be
# substantially larger than control requests while still needing a hard bound.
MAX_MESSAGE_BYTES = 8 * 1024 * 1024


class ProtocolError(Exception):
    def __init__(self, code: int, message: str, data: Any = None):
        super().__init__(message)
        self.code = code
        self.message = message
        self.data = data


class RemoteError(ProtocolError):
    pass


def encode_message(message: dict[str, Any]) -> bytes:
    encoded = json.dumps(message, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
    if len(encoded) > MAX_MESSAGE_BYTES:
        raise ProtocolError(-32600, "Message is too large")
    return encoded + b"\n"


def decode_message(payload: bytes) -> dict[str, Any]:
    if len(payload) > MAX_MESSAGE_BYTES:
        raise ProtocolError(-32600, "Message is too large")
    try:
        message = json.loads(payload)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ProtocolError(-32700, "Parse error") from exc
    if not isinstance(message, dict):
        raise ProtocolError(-32600, "Invalid Request")
    return message


def request(method: str, params: dict[str, Any] | None = None, token: str | None = None):
    message: dict[str, Any] = {
        "jsonrpc": JSONRPC_VERSION,
        "id": uuid.uuid4().hex,
        "method": method,
        "params": params or {},
    }
    if token:
        message["auth"] = token
    return message


def success(request_id: str | int | None, result: Any) -> dict[str, Any]:
    return {"jsonrpc": JSONRPC_VERSION, "id": request_id, "result": result}


def failure(
    request_id: str | int | None, code: int, message: str, data: Any = None
) -> dict[str, Any]:
    error: dict[str, Any] = {"code": code, "message": message}
    if data is not None:
        error["data"] = data
    return {"jsonrpc": JSONRPC_VERSION, "id": request_id, "error": error}


def validate_request(message: dict[str, Any]) -> tuple[str | int | None, str, dict[str, Any]]:
    request_id = message.get("id")
    method = message.get("method")
    params = message.get("params", {})
    if message.get("jsonrpc") != JSONRPC_VERSION or not isinstance(method, str):
        raise ProtocolError(-32600, "Invalid Request")
    if not isinstance(params, dict):
        raise ProtocolError(-32602, "Invalid params")
    return request_id, method, params

