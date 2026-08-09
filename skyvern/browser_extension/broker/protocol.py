from __future__ import annotations

import base64
import hashlib
import hmac
import json
import secrets
from dataclasses import dataclass
from typing import Literal, cast

from skyvern.browser_extension.errors import BrowserExtensionError

BROKER_WS_PATH = "/broker/v1"
BROKER_HEALTH_PATH = "/broker/health"

# Envelope shape ({"v": ..., "type": ...}). Never bumped: a client and a daemon must be able to
# complete the handshake before they can negotiate anything else.
BROKER_FRAME_VERSION = 1

# Broker semantics. Bumped whenever client and daemon behaviour stops being interchangeable;
# exchanged only after authentication so version skew can never be probed anonymously.
BROKER_PROTOCOL_VERSION = 1

_CLIENT_PROOF_CONTEXT = "skyvern-broker-v1"
_SERVER_PROOF_CONTEXT = "skyvern-broker-srv-v1"

# Broker-only operations. They never reach the extension, so they sit outside ALLOWED_OPS.
PAIRING_NONCE_OP = "broker.pairingNonce"
STATUS_OP = "broker.status"

# Reported when the daemon is healthy but no extension is attached to it.
EXTENSION_NOT_CONNECTED_CODE = "EXTENSION_NOT_CONNECTED"

BrokerFrameKind = Literal[
    "auth.challenge",
    "auth.proof",
    "auth.ok",
    "client.hello",
    "broker.state",
    "broker.stepDown",
    "request",
    "response",
    "event",
    "ping",
    "pong",
]

_CLIENT_FRAME_KINDS = frozenset({"auth.proof", "client.hello", "broker.stepDown", "request", "ping", "pong"})
_SERVER_FRAME_KINDS = frozenset({"auth.challenge", "auth.ok", "broker.state", "response", "event", "ping", "pong"})


@dataclass(frozen=True, slots=True)
class BrokerFrame:
    kind: BrokerFrameKind
    request_id: str | None = None
    op: str | None = None
    args: dict | None = None
    timeout_seconds: float | None = None
    ok: bool | None = None
    result: dict | None = None
    error_code: str | None = None
    error_message: str | None = None
    event: str | None = None
    params: dict | None = None
    client_nonce: str | None = None
    server_nonce: str | None = None
    proof: str | None = None
    protocol: int | None = None
    pid: int | None = None
    root: str | None = None
    extension_connected: bool | None = None
    scoped_tabs: list[dict] | None = None


def build_broker_nonce() -> str:
    return _b64url(secrets.token_bytes(32))


def build_broker_challenge() -> tuple[str, dict]:
    server_nonce = build_broker_nonce()
    return server_nonce, {"v": BROKER_FRAME_VERSION, "type": "auth.challenge", "serverNonce": server_nonce}


def compute_broker_client_proof(token: str, server_nonce: str, client_nonce: str) -> str:
    return _compute_proof(token, f"{_CLIENT_PROOF_CONTEXT}|{server_nonce}|{client_nonce}")


def compute_broker_server_proof(token: str, client_nonce: str, server_nonce: str) -> str:
    return _compute_proof(token, f"{_SERVER_PROOF_CONTEXT}|{client_nonce}|{server_nonce}")


def verify_broker_client_proof(token: str, server_nonce: str, client_nonce: str, proof: str) -> bool:
    expected = compute_broker_client_proof(token, server_nonce, client_nonce)
    try:
        return hmac.compare_digest(expected, proof)
    except TypeError:
        return False


def is_valid_broker_nonce(nonce: str) -> bool:
    if not nonce or "=" in nonce:
        return False
    padding = "=" * (-len(nonce) % 4)
    try:
        decoded = base64.b64decode(nonce + padding, altchars=b"-_", validate=True)
    except (ValueError, TypeError):
        return False
    return len(decoded) == 32 and _b64url(decoded) == nonce


def build_request_frame(request_id: str, op: str, args: dict, timeout_seconds: float) -> dict:
    return {
        "v": BROKER_FRAME_VERSION,
        "type": "request",
        "id": request_id,
        "op": op,
        "args": args,
        "timeoutMs": int(max(timeout_seconds, 0.0) * 1000),
    }


def build_response_frame(request_id: str, result: dict) -> dict:
    return {"v": BROKER_FRAME_VERSION, "type": "response", "id": request_id, "ok": True, "result": result}


def build_error_frame(request_id: str, code: str, message: str) -> dict:
    return {
        "v": BROKER_FRAME_VERSION,
        "type": "response",
        "id": request_id,
        "ok": False,
        "error": {"code": code, "message": message},
    }


def build_event_frame(event: str, params: dict) -> dict:
    return {"v": BROKER_FRAME_VERSION, "type": "event", "event": event, "params": params}


def build_state_frame(extension_connected: bool, scoped_tabs: list[dict]) -> dict:
    return {
        "v": BROKER_FRAME_VERSION,
        "type": "broker.state",
        "extensionConnected": extension_connected,
        "scopedTabs": scoped_tabs,
    }


def parse_broker_frame(raw: str, *, from_client: bool) -> BrokerFrame:
    try:
        payload = json.loads(raw)
    except (json.JSONDecodeError, TypeError, UnicodeDecodeError) as exc:
        raise BrowserExtensionError("Broker frame is not valid JSON") from exc
    if not isinstance(payload, dict):
        raise BrowserExtensionError("Broker frame must be an object")
    if type(payload.get("v")) is not int or payload["v"] != BROKER_FRAME_VERSION:
        raise BrowserExtensionError("Unsupported broker frame version")

    raw_kind = payload.get("type")
    allowed = _CLIENT_FRAME_KINDS if from_client else _SERVER_FRAME_KINDS
    if not isinstance(raw_kind, str) or raw_kind not in allowed:
        raise BrowserExtensionError("Unknown broker frame type")
    kind = cast(BrokerFrameKind, raw_kind)

    if kind in {"ping", "pong"}:
        return BrokerFrame(kind=kind)
    if kind == "auth.challenge":
        return BrokerFrame(kind=kind, server_nonce=_required_string(payload, "serverNonce"))
    if kind == "auth.proof":
        return BrokerFrame(
            kind=kind,
            client_nonce=_required_string(payload, "clientNonce"),
            proof=_required_string(payload, "proof"),
        )
    if kind == "auth.ok":
        return BrokerFrame(
            kind=kind,
            proof=_required_string(payload, "serverProof"),
            protocol=_required_int(payload, "protocol"),
            pid=_required_int(payload, "pid"),
            root=_required_string(payload, "root"),
        )
    if kind == "client.hello":
        return BrokerFrame(kind=kind, protocol=_required_int(payload, "protocol"), pid=_required_int(payload, "pid"))
    if kind == "broker.stepDown":
        return BrokerFrame(kind=kind, protocol=_required_int(payload, "protocol"))
    if kind == "broker.state":
        return BrokerFrame(
            kind=kind,
            extension_connected=_required_bool(payload, "extensionConnected"),
            scoped_tabs=_tab_list(payload.get("scopedTabs")),
        )
    if kind == "request":
        return _parse_request(payload)
    if kind == "response":
        return _parse_response(payload)
    return _parse_event(payload)


def _parse_request(payload: dict) -> BrokerFrame:
    timeout_ms = payload.get("timeoutMs")
    if type(timeout_ms) is not int or timeout_ms <= 0:
        raise BrowserExtensionError("Broker request timeout must be a positive integer")
    return BrokerFrame(
        kind="request",
        request_id=_required_string(payload, "id"),
        op=_required_string(payload, "op"),
        args=_required_dict(payload, "args"),
        timeout_seconds=timeout_ms / 1000,
    )


def _parse_response(payload: dict) -> BrokerFrame:
    request_id = _required_string(payload, "id")
    ok = payload.get("ok")
    if type(ok) is not bool:
        raise BrowserExtensionError("Broker response ok must be a boolean")
    if ok:
        return BrokerFrame(kind="response", request_id=request_id, ok=True, result=_required_dict(payload, "result"))
    error = _required_dict(payload, "error")
    return BrokerFrame(
        kind="response",
        request_id=request_id,
        ok=False,
        error_code=_required_string(error, "code"),
        error_message=_required_string(error, "message"),
    )


def _parse_event(payload: dict) -> BrokerFrame:
    return BrokerFrame(
        kind="event",
        event=_required_string(payload, "event"),
        params=_required_dict(payload, "params"),
    )


def _tab_list(value: object) -> list[dict]:
    if not isinstance(value, list):
        raise BrowserExtensionError("Broker frame field scopedTabs must be an array")
    return [tab for tab in value if isinstance(tab, dict) and type(tab.get("tabId")) is int]


def _required_string(payload: dict, field: str) -> str:
    value = payload.get(field)
    if not isinstance(value, str) or not value:
        raise BrowserExtensionError(f"Broker frame field {field} must be a non-empty string")
    return value


def _required_dict(payload: dict, field: str) -> dict:
    value = payload.get(field)
    if not isinstance(value, dict):
        raise BrowserExtensionError(f"Broker frame field {field} must be an object")
    return value


def _required_int(payload: dict, field: str) -> int:
    value = payload.get(field)
    if type(value) is not int:
        raise BrowserExtensionError(f"Broker frame field {field} must be an integer")
    return value


def _required_bool(payload: dict, field: str) -> bool:
    value = payload.get(field)
    if type(value) is not bool:
        raise BrowserExtensionError(f"Broker frame field {field} must be a boolean")
    return value


def _compute_proof(token: str, message: str) -> str:
    digest = hmac.new(token.encode("utf-8"), message.encode("utf-8"), hashlib.sha256).digest()
    return _b64url(digest)


def _b64url(value: bytes) -> str:
    return base64.urlsafe_b64encode(value).rstrip(b"=").decode("ascii")
