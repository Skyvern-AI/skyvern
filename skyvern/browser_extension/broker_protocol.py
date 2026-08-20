from __future__ import annotations

import asyncio
import base64
import binascii
import json
import os
import secrets
import socket
import struct
from collections.abc import Callable, Mapping, Set
from typing import Any

from skyvern.browser_extension.errors import BrowserExtensionBrokerError, ExtensionRequestError

_HEADER = struct.Struct("!I")

BROKER_PROTOCOL_VERSION = 1
BROKER_GENERATION = 1
PREAUTH_FRAME_LIMIT = 8 * 1024
CONTROL_FRAME_LIMIT = 64 * 1024
# MAX_CLIENT_OUTPUT_BYTES is derived from this payload limit so a valid encoded operation frame always fits.
OPERATION_FRAME_LIMIT = 32 * 1024 * 1024
MAX_ENCODED_CONTROL_FRAME_BYTES = _HEADER.size + CONTROL_FRAME_LIMIT
MAX_ENCODED_OPERATION_FRAME_BYTES = _HEADER.size + OPERATION_FRAME_LIMIT
READ_TIMEOUT_SECONDS = 10.0
MAX_PENDING_CONNECTIONS = 16
MAX_AUTHENTICATED_CLIENTS = 32
MAX_REQUESTS_PER_CLIENT = 32
MAX_REQUESTS_PER_TAB = 16
MAX_QUEUED_FRAMES_PER_CLIENT = 256
MAX_CLIENT_INBOUND_BYTES = 32 * 1024 * 1024
# One maximum operation frame plus one control-frame headroom must fit; queued large frames still backpressure.
MAX_CLIENT_OUTPUT_BYTES = MAX_ENCODED_OPERATION_FRAME_BYTES + MAX_ENCODED_CONTROL_FRAME_BYTES
MAX_GLOBAL_INBOUND_BYTES = 128 * 1024 * 1024
MAX_GLOBAL_OUTPUT_BYTES = 64 * 1024 * 1024
MAX_GLOBAL_REQUESTS = 256

_SENSITIVE_KEYS = frozenset(
    {
        "token",
        "secret",
        "recoverySecret",
        "proof",
        "serverProof",
        "serverNonce",
        "clientNonce",
        "nonce",
        "pairingUrl",
        "cdpUrl",
        "url",
        "params",
        "raw",
    }
)


def new_nonce() -> str:
    return base64.urlsafe_b64encode(secrets.token_bytes(32)).rstrip(b"=").decode("ascii")


def is_valid_nonce(value: object) -> bool:
    if not isinstance(value, str) or len(value) != 43:
        return False
    try:
        decoded = base64.urlsafe_b64decode(value + "=")
    except (binascii.Error, ValueError, TypeError):
        return False
    canonical = base64.urlsafe_b64encode(decoded).rstrip(b"=").decode("ascii")
    return len(decoded) == 32 and canonical == value


def encode_frame(frame: Mapping[str, Any], *, max_size: int = OPERATION_FRAME_LIMIT) -> bytes:
    try:
        payload = json.dumps(frame, separators=(",", ":"), ensure_ascii=False, allow_nan=False).encode("utf-8")
    except (TypeError, ValueError) as exc:
        raise BrowserExtensionBrokerError("INVALID_FRAME", "Broker frame is not valid JSON") from exc
    if not payload or len(payload) > max_size:
        raise BrowserExtensionBrokerError("FRAME_TOO_LARGE", "Broker frame exceeds the allowed size")
    return _HEADER.pack(len(payload)) + payload


def decode_frame(payload: bytes, *, max_size: int = OPERATION_FRAME_LIMIT) -> dict[str, Any]:
    if not payload or len(payload) > max_size:
        raise BrowserExtensionBrokerError("FRAME_TOO_LARGE", "Broker frame exceeds the allowed size")
    try:
        value = json.loads(payload.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise BrowserExtensionBrokerError("INVALID_FRAME", "Broker frame is not valid JSON") from exc
    if not isinstance(value, dict):
        raise BrowserExtensionBrokerError("INVALID_FRAME", "Broker frame must be an object")
    if type(value.get("v")) is not int or value["v"] != BROKER_PROTOCOL_VERSION:
        raise BrowserExtensionBrokerError("INCOMPATIBLE_PROTOCOL", "Unsupported broker protocol version")
    return value


async def read_frame(
    reader: asyncio.StreamReader,
    *,
    max_size: int = OPERATION_FRAME_LIMIT,
    timeout: float | None = None,
    reserve: Callable[[int], bool] | None = None,
    control_size: int | None = None,
    large_request_op: str | None = None,
    large_response_ids: Set[str] | None = None,
    large_event: str | None = None,
) -> tuple[dict[str, Any], int]:
    async def _read() -> tuple[dict[str, Any], int]:
        try:
            header = await reader.readexactly(_HEADER.size)
        except asyncio.IncompleteReadError as exc:
            raise EOFError from exc
        (declared_size,) = _HEADER.unpack(header)
        if declared_size == 0 or declared_size > max_size:
            raise BrowserExtensionBrokerError("FRAME_TOO_LARGE", "Broker frame exceeds the allowed size")
        if reserve is not None and not reserve(declared_size):
            raise BrowserExtensionBrokerError("RESOURCE_LIMIT", "Broker inbound byte budget exceeded")
        try:
            prefix = b""
            if control_size is not None and declared_size > control_size:
                if large_request_op is not None:
                    prefix = await _read_large_request_prefix(reader, large_request_op)
                elif large_response_ids is not None and large_event is not None:
                    prefix = await _read_large_client_frame_prefix(reader, large_response_ids, large_event)
                else:
                    raise BrowserExtensionBrokerError("FRAME_TOO_LARGE", "Control frame exceeds the allowed size")
                if len(prefix) >= declared_size:
                    raise BrowserExtensionBrokerError("INVALID_FRAME", "Broker operation frame is invalid")
            payload = prefix + await reader.readexactly(declared_size - len(prefix))
        except asyncio.IncompleteReadError as exc:
            raise EOFError from exc
        frame = decode_frame(payload, max_size=max_size)
        if prefix and (frame.get("type") != "request" or frame.get("op") != large_request_op):
            if not (
                frame.get("type") == "response"
                and isinstance(frame.get("id"), str)
                and large_response_ids is not None
                and frame["id"] in large_response_ids
            ) and not (frame.get("type") == "event" and frame.get("event") == large_event):
                raise BrowserExtensionBrokerError("FRAME_TOO_LARGE", "Control frame exceeds the allowed size")
        return frame, declared_size

    try:
        if timeout is None:
            return await _read()
        return await asyncio.wait_for(_read(), timeout)
    except TimeoutError as exc:
        raise BrowserExtensionBrokerError("AUTH_TIMEOUT", "Broker authentication timed out") from exc


async def _read_large_request_prefix(reader: asyncio.StreamReader, operation: str) -> bytes:
    fixed = b'{"v":1,"type":"request","id":'
    try:
        observed = await reader.readexactly(len(fixed))
    except asyncio.IncompleteReadError as exc:
        raise EOFError from exc
    if observed != fixed:
        raise BrowserExtensionBrokerError("FRAME_TOO_LARGE", "Control frame exceeds the allowed size")

    request_id, decoded_request_id = await _read_json_string(reader)
    if not isinstance(decoded_request_id, str) or not decoded_request_id or len(decoded_request_id) > 128:
        raise BrowserExtensionBrokerError("INVALID_REQUEST", "Broker request id is invalid")

    suffix = f',"op":{json.dumps(operation, separators=(",", ":"))},"args":'.encode()
    try:
        observed_suffix = await reader.readexactly(len(suffix))
    except asyncio.IncompleteReadError as exc:
        raise EOFError from exc
    if observed_suffix != suffix:
        raise BrowserExtensionBrokerError("FRAME_TOO_LARGE", "Control frame exceeds the allowed size")
    return fixed + bytes(request_id) + observed_suffix


async def _read_large_client_frame_prefix(
    reader: asyncio.StreamReader,
    response_ids: Set[str],
    event: str,
) -> bytes:
    fixed = b'{"v":1,"type":'
    try:
        observed = await reader.readexactly(len(fixed))
    except asyncio.IncompleteReadError as exc:
        raise EOFError from exc
    if observed != fixed:
        raise BrowserExtensionBrokerError("FRAME_TOO_LARGE", "Control frame exceeds the allowed size")
    frame_type_raw, frame_type = await _read_json_string(reader)
    if frame_type == "response":
        separator = b',"id":'
        try:
            observed_separator = await reader.readexactly(len(separator))
        except asyncio.IncompleteReadError as exc:
            raise EOFError from exc
        if observed_separator != separator:
            raise BrowserExtensionBrokerError("FRAME_TOO_LARGE", "Control frame exceeds the allowed size")
        request_id_raw, request_id = await _read_json_string(reader)
        if request_id not in response_ids:
            raise BrowserExtensionBrokerError("FRAME_TOO_LARGE", "Control frame exceeds the allowed size")
        return fixed + frame_type_raw + separator + request_id_raw
    if frame_type == "event":
        suffix = f',"event":{json.dumps(event, separators=(",", ":"))},"params":'.encode()
        try:
            observed_suffix = await reader.readexactly(len(suffix))
        except asyncio.IncompleteReadError as exc:
            raise EOFError from exc
        if observed_suffix != suffix:
            raise BrowserExtensionBrokerError("FRAME_TOO_LARGE", "Control frame exceeds the allowed size")
        return fixed + frame_type_raw + observed_suffix
    raise BrowserExtensionBrokerError("FRAME_TOO_LARGE", "Control frame exceeds the allowed size")


async def _read_json_string(reader: asyncio.StreamReader) -> tuple[bytes, str]:
    try:
        opening_quote = await reader.readexactly(1)
    except asyncio.IncompleteReadError as exc:
        raise EOFError from exc
    if opening_quote != b'"':
        raise BrowserExtensionBrokerError("INVALID_FRAME", "Broker frame is invalid")
    raw = bytearray(opening_quote)
    escaped = False
    while len(raw) <= 1024:
        try:
            byte = await reader.readexactly(1)
        except asyncio.IncompleteReadError as exc:
            raise EOFError from exc
        raw.extend(byte)
        if escaped:
            escaped = False
            continue
        if byte == b"\\":
            escaped = True
            continue
        if byte == b'"':
            break
    else:
        raise BrowserExtensionBrokerError("INVALID_FRAME", "Broker frame string is invalid")
    try:
        decoded = json.loads(raw)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise BrowserExtensionBrokerError("INVALID_FRAME", "Broker frame string is invalid") from exc
    if not isinstance(decoded, str):
        raise BrowserExtensionBrokerError("INVALID_FRAME", "Broker frame string is invalid")
    return bytes(raw), decoded


async def write_frame(
    writer: asyncio.StreamWriter,
    frame: Mapping[str, Any],
    *,
    max_size: int = OPERATION_FRAME_LIMIT,
) -> int:
    encoded = encode_frame(frame, max_size=max_size)
    writer.write(encoded)
    await writer.drain()
    return len(encoded) - _HEADER.size


def request_frame(request_id: str, op: str, args: Mapping[str, Any] | None = None) -> dict[str, Any]:
    if not request_id or not isinstance(request_id, str) or not isinstance(op, str) or not op:
        raise BrowserExtensionBrokerError("INVALID_REQUEST", "Broker request id and operation are required")
    return {
        "v": BROKER_PROTOCOL_VERSION,
        "type": "request",
        "id": request_id,
        "op": op,
        "args": dict(args or {}),
    }


def response_frame(request_id: str, result: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "v": BROKER_PROTOCOL_VERSION,
        "type": "response",
        "id": request_id,
        "ok": True,
        "result": dict(result),
    }


def error_frame(request_id: str, error: BrowserExtensionBrokerError | ExtensionRequestError) -> dict[str, Any]:
    payload: dict[str, Any] = {"code": error.code, "message": error.message}
    if isinstance(error, ExtensionRequestError):
        payload["errorType"] = "extension"
    elif error.retry_after is not None:
        payload["retryAfter"] = max(0.0, error.retry_after)
    return {
        "v": BROKER_PROTOCOL_VERSION,
        "type": "response",
        "id": request_id,
        "ok": False,
        "error": payload,
    }


def peer_uid_from_transport(transport_socket: Any) -> int | None:
    try:
        if hasattr(transport_socket, "getpeereid"):
            peer_uid, _peer_gid = transport_socket.getpeereid()
            return int(peer_uid)

        duplicate = socket.socket(fileno=os.dup(transport_socket.fileno()))
        try:
            if hasattr(duplicate, "getpeereid"):
                peer_uid, _peer_gid = duplicate.getpeereid()
                return int(peer_uid)
        finally:
            duplicate.close()

        if hasattr(socket, "SO_PEERCRED"):
            credentials = transport_socket.getsockopt(socket.SOL_SOCKET, socket.SO_PEERCRED, struct.calcsize("3i"))
            _pid, peer_uid, _peer_gid = struct.unpack("3i", credentials)
            return int(peer_uid)
        if hasattr(socket, "LOCAL_PEERCRED"):
            credentials = transport_socket.getsockopt(0, socket.LOCAL_PEERCRED, 12)
            _version, peer_uid = struct.unpack_from("=II", credentials)
            return int(peer_uid)
    except (OSError, TypeError, ValueError, struct.error):
        return None
    return None


def event_frame(event: str, params: Mapping[str, Any] | None = None) -> dict[str, Any]:
    return {
        "v": BROKER_PROTOCOL_VERSION,
        "type": "event",
        "event": event,
        "params": dict(params or {}),
    }


def parse_request(frame: Mapping[str, Any]) -> tuple[str, str, dict[str, Any]]:
    if frame.get("type") != "request":
        raise BrowserExtensionBrokerError("INVALID_FRAME", "Expected a broker request")
    request_id = frame.get("id")
    op = frame.get("op")
    args = frame.get("args")
    if not isinstance(request_id, str) or not request_id or len(request_id) > 128:
        raise BrowserExtensionBrokerError("INVALID_REQUEST", "Broker request id is invalid")
    if not isinstance(op, str) or not op or len(op) > 128:
        raise BrowserExtensionBrokerError("INVALID_REQUEST", "Broker operation is invalid")
    if not isinstance(args, dict):
        raise BrowserExtensionBrokerError("INVALID_REQUEST", "Broker request args must be an object")
    return request_id, op, args


def redact(value: object) -> object:
    """Redact material prohibited from logs by spec-v3.md lines 199-205 and 213-238."""
    if isinstance(value, Mapping):
        return {str(key): "[REDACTED]" if key in _SENSITIVE_KEYS else redact(item) for key, item in value.items()}
    if isinstance(value, list):
        return [redact(item) for item in value]
    if isinstance(value, tuple):
        return tuple(redact(item) for item in value)
    return value
