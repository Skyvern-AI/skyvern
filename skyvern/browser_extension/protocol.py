from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Literal
from urllib.parse import urlsplit

from skyvern.browser_extension.errors import BrowserExtensionError

PROTOCOL_VERSION = 1
EXTENSION_ID = "dhommdmblflboaledbbfkdaapkadphlp"

ALLOWED_OPS = frozenset(
    {
        "debugger.attach",
        "debugger.detach",
        "debugger.send",
        "tabs.create",
        "tabs.remove",
        "tabs.activate",
        "tabs.list",
    }
)

ALLOWED_EVENTS = frozenset(
    {
        "extension.hello",
        "debugger.event",
        "debugger.detached",
        "scope.tabAdded",
        "scope.tabRemoved",
        "tabs.created",
    }
)

ERROR_CODES = frozenset(
    {
        "AUTH_FAILED",
        "OP_NOT_ALLOWED",
        "TAB_NOT_FOUND",
        "TAB_NOT_SCOPED",
        "RESTRICTED_URL",
        "DEBUGGER_DETACHED",
        "CDP_METHOD_NOT_ALLOWED",
        "CDP_ERROR",
        "INTERNAL",
    }
)

ALLOWED_CDP_METHOD_PREFIXES = (
    "Accessibility.",
    "Animation.",
    "CSS.",
    "Console.",
    "DOM.",
    "DOMDebugger.",
    "DOMSnapshot.",
    "DOMStorage.",
    "Debugger.",
    "Emulation.",
    "Fetch.",
    "IO.",
    "Input.",
    "Inspector.",
    "Log.",
    "Network.",
    "Overlay.",
    "Page.",
    "Performance.",
    "Profiler.",
    "Runtime.",
    "Security.",
    "Storage.",
    "Target.",
)

DENIED_CDP_METHODS = frozenset(
    {
        "Network.getAllCookies",
        "Network.clearBrowserCookies",
        "Network.clearBrowserCache",
        "Storage.getCookies",
        "Storage.setCookies",
        "Storage.clearCookies",
    }
)

RESTRICTED_URL_PREFIXES = (
    "chrome://",
    "chrome-untrusted://",
    "chrome-extension://",
    "devtools://",
    "edge://",
    "about:",
    "file://",
)

MessageKind = Literal["response", "event", "pong", "ping", "auth.proof"]


@dataclass(frozen=True, slots=True)
class ParsedMessage:
    kind: MessageKind
    request_id: str | None = None
    ok: bool | None = None
    result: dict | None = None
    error: dict | None = None
    error_code: str | None = None
    error_message: str | None = None
    event: str | None = None
    params: dict | None = None
    client_nonce: str | None = None
    proof: str | None = None


def build_request(request_id: str, op: str, args: dict) -> dict:
    if not isinstance(request_id, str) or not request_id:
        raise BrowserExtensionError("Request id must be a non-empty string")
    if not isinstance(op, str) or op not in ALLOWED_OPS:
        raise BrowserExtensionError(f"Operation is not allowed: {op}")
    if not isinstance(args, dict):
        raise BrowserExtensionError("Request args must be an object")
    return {"v": PROTOCOL_VERSION, "type": "request", "id": request_id, "op": op, "args": args}


def parse_extension_message(raw: str) -> ParsedMessage:
    try:
        payload = json.loads(raw)
    except (json.JSONDecodeError, TypeError) as exc:
        raise BrowserExtensionError("Extension frame is not valid JSON") from exc

    if not isinstance(payload, dict):
        raise BrowserExtensionError("Extension frame must be an object")
    if type(payload.get("v")) is not int or payload["v"] != PROTOCOL_VERSION:
        raise BrowserExtensionError("Unsupported extension protocol version")

    frame_type = payload.get("type")
    if frame_type == "response":
        return _parse_response(payload)
    if frame_type == "event":
        return _parse_event(payload)
    if frame_type == "ping":
        return ParsedMessage(kind="ping")
    if frame_type == "pong":
        return ParsedMessage(kind="pong")
    if frame_type == "auth.proof":
        return ParsedMessage(
            kind="auth.proof",
            client_nonce=_required_string(payload, "clientNonce"),
            proof=_required_string(payload, "proof"),
        )
    raise BrowserExtensionError("Unknown extension frame type")


def is_cdp_method_allowed(method: str, params: dict | None = None) -> bool:
    if method in DENIED_CDP_METHODS:
        return False
    if method == "Network.getCookies" and isinstance(params, dict) and "urls" in params:
        return False
    return any(method.startswith(prefix) and len(method) > len(prefix) for prefix in ALLOWED_CDP_METHOD_PREFIXES)


def is_restricted_url(url: str) -> bool:
    normalized_url = url.strip().lower()
    if normalized_url == "about:blank":
        return False
    if normalized_url.startswith(RESTRICTED_URL_PREFIXES):
        return True
    try:
        hostname = urlsplit(normalized_url).hostname
        return hostname is not None and hostname.removesuffix(".") == "chromewebstore.google.com"
    except ValueError:
        return False


def _parse_response(payload: dict) -> ParsedMessage:
    request_id = _required_string(payload, "id")
    ok = payload.get("ok")
    if type(ok) is not bool:
        raise BrowserExtensionError("Response ok must be a boolean")

    if ok:
        result = _required_dict(payload, "result")
        return ParsedMessage(kind="response", request_id=request_id, ok=True, result=result)

    error = _required_dict(payload, "error")
    error_code = _required_string(error, "code")
    error_message = _required_string(error, "message")
    if error_code not in ERROR_CODES:
        raise BrowserExtensionError("Response contains an unknown error code")
    return ParsedMessage(
        kind="response",
        request_id=request_id,
        ok=False,
        error=error,
        error_code=error_code,
        error_message=error_message,
    )


def _parse_event(payload: dict) -> ParsedMessage:
    event = _required_string(payload, "event")
    if event not in ALLOWED_EVENTS:
        raise BrowserExtensionError("Unknown extension event")
    return ParsedMessage(kind="event", event=event, params=_required_dict(payload, "params"))


def _required_string(payload: dict, field: str) -> str:
    value = payload.get(field)
    if not isinstance(value, str) or not value:
        raise BrowserExtensionError(f"Extension frame field {field} must be a non-empty string")
    return value


def _required_dict(payload: dict, field: str) -> dict:
    value = payload.get(field)
    if not isinstance(value, dict):
        raise BrowserExtensionError(f"Extension frame field {field} must be an object")
    return value
