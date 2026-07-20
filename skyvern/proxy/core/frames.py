from __future__ import annotations

import json
import math
from dataclasses import dataclass, field
from typing import Any, Union


class FrameDecodeError(ValueError):
    pass


class RemapperFullError(RuntimeError):
    """The pending-request table is full and nothing this caller owns can be evicted
    to make room. Admitting the command would mean discarding a mapping already
    promised to another client (or to the proxy), so the caller is refused instead."""


@dataclass(frozen=True)
class CdpCommand:
    id: int
    method: str
    params: dict[str, Any] | None = None
    session_id: str | None = None


@dataclass(frozen=True)
class CdpResponse:
    id: int
    result: dict[str, Any] | None = None
    error: dict[str, Any] | None = None
    session_id: str | None = None


@dataclass(frozen=True)
class CdpEvent:
    method: str
    params: dict[str, Any] | None = None
    session_id: str | None = None


CdpFrame = Union[CdpCommand, CdpResponse, CdpEvent]

# Flat-session lifecycle events. Both carry the affected session in params.sessionId
# (NOT the frame's own sessionId, which is the parent for a nested attach), so they are
# routed by ownership of that session rather than by their own scope.
TARGET_ATTACHED_EVENT = "Target.attachedToTarget"
TARGET_DETACHED_EVENT = "Target.detachedFromTarget"
# A client builds and retires its session objects from these two, and the proxy learns
# session ownership from them, so unlike ordinary events they are never droppable:
# losing one leaves the client silently desynced from the browser (a session it can
# never use, or one it thinks is still live) and strands that session's owner. A client
# too slow to take one is closed instead. Delivery, routing, and the event policy all
# key off this one set — two copies could drift, and a drift is invisible until a
# session is already stranded.
LIFECYCLE_EVENTS = frozenset({TARGET_ATTACHED_EVENT, TARGET_DETACHED_EVENT})

# The CDP domains this proxy recognizes. Used to bucket a method to bounded metric
# labels and to bound what counts as real, declared client interest — an unknown
# domain is neither. Core-owned so the metric bucketing and the event policy agree.
KNOWN_CDP_DOMAINS = frozenset(
    {
        "Accessibility",
        "Animation",
        "Audits",
        "BackgroundService",
        "Browser",
        "CacheStorage",
        "Cast",
        "Console",
        "CSS",
        "Database",
        "Debugger",
        "DeviceOrientation",
        "DOM",
        "DOMDebugger",
        "DOMSnapshot",
        "DOMStorage",
        "Emulation",
        "Fetch",
        "HeadlessExperimental",
        "HeapProfiler",
        "IndexedDB",
        "Input",
        "Inspector",
        "IO",
        "LayerTree",
        "Log",
        "Media",
        "Memory",
        "Network",
        "Overlay",
        "Page",
        "Performance",
        "Profiler",
        "Runtime",
        "Schema",
        "Security",
        "ServiceWorker",
        "Storage",
        "SystemInfo",
        "Target",
        "Tethering",
        "Tracing",
        "WebAudio",
        "WebAuthn",
    }
)

_FRAME_KEYS = frozenset({"error", "id", "method", "params", "result", "sessionId"})
_ERROR_KEYS = frozenset({"code", "data", "message"})

# The lane proxy-issued commands ride in the remapper. Client keys are allocated by
# the adapter (c1, c2, ...), so this can never be a real client; the reserved-key
# guards below keep it that way even if that scheme changes.
PROXY_CLIENT_KEY = "__proxy__"

# An in-flight command whose response never arrives would otherwise pin its mapping
# forever. Mirrors the adapter's own pending-command bound.
_MAX_PENDING_REQUESTS = 4096


def _require_id(value: object, field_name: str = "id") -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise FrameDecodeError(f"CDP {field_name} must be an integer")
    return value


def _require_text(value: object, field_name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise FrameDecodeError(f"CDP {field_name} must be a non-empty string")
    try:
        value.encode("utf-8")
    except UnicodeEncodeError as exc:
        raise FrameDecodeError(f"CDP {field_name} must be valid UTF-8 text") from exc
    return value


def _optional_object(payload: dict[str, Any], field_name: str) -> dict[str, Any] | None:
    if field_name not in payload:
        return None
    value = payload[field_name]
    if not isinstance(value, dict):
        raise FrameDecodeError(f"CDP {field_name} must be an object")
    return value


def _optional_session_id(payload: dict[str, Any]) -> str | None:
    if "sessionId" not in payload:
        return None
    return _require_text(payload["sessionId"], "sessionId")


def _validate_error(value: object) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise FrameDecodeError("CDP error must be an object")
    if set(value) - _ERROR_KEYS or "code" not in value or "message" not in value:
        raise FrameDecodeError("CDP error must contain only code, message, and optional data")
    _require_id(value["code"], "error code")
    _require_text(value["message"], "error message")
    return value


def _reject_json_constant(value: str) -> Any:
    raise ValueError(f"invalid JSON constant: {value}")


def _parse_float(value: str) -> float:
    parsed = float(value)
    if not math.isfinite(parsed):
        raise ValueError(f"non-finite JSON number: {value}")
    return parsed


def _object_pairs_hook(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    payload: dict[str, Any] = {}
    for key, value in pairs:
        if key in payload:
            raise FrameDecodeError(f"duplicate JSON field: {key}")
        payload[key] = value
    return payload


def _parse_json(raw: str | bytes) -> object:
    if isinstance(raw, bytes):
        try:
            text = raw.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise FrameDecodeError(f"invalid UTF-8 frame: {exc}") from exc
    elif isinstance(raw, str):
        text = raw
    else:
        raise FrameDecodeError("CDP frame must be text or UTF-8 bytes")

    try:
        return json.loads(
            text,
            object_pairs_hook=_object_pairs_hook,
            parse_constant=_reject_json_constant,
            parse_float=_parse_float,
        )
    except FrameDecodeError:
        raise
    except (json.JSONDecodeError, RecursionError, TypeError, ValueError) as exc:
        raise FrameDecodeError(f"invalid JSON frame: {exc}") from exc


def _decode_payload(payload: object) -> CdpFrame:
    if not isinstance(payload, dict):
        raise FrameDecodeError("CDP frame must be a JSON object")
    unexpected_keys = set(payload) - _FRAME_KEYS
    if unexpected_keys:
        raise FrameDecodeError(f"CDP frame has unexpected fields: {sorted(unexpected_keys)!r}")

    has_id = "id" in payload
    has_method = "method" in payload
    if not has_id and not has_method:
        raise FrameDecodeError("CDP frame must contain id or method")

    session_id = _optional_session_id(payload)
    if has_id:
        frame_id = _require_id(payload["id"])
        if has_method:
            if "result" in payload or "error" in payload:
                raise FrameDecodeError("CDP command cannot contain result or error")
            return CdpCommand(
                id=frame_id,
                method=_require_text(payload["method"], "method"),
                params=_optional_object(payload, "params"),
                session_id=session_id,
            )

        if "method" in payload or "params" in payload:
            raise FrameDecodeError("CDP response cannot contain method or params")
        has_result = "result" in payload
        has_error = "error" in payload
        if has_result == has_error:
            raise FrameDecodeError("CDP response must contain exactly one of result or error")
        result = _optional_object(payload, "result") if has_result else None
        error = _validate_error(payload["error"]) if has_error else None
        return CdpResponse(id=frame_id, result=result, error=error, session_id=session_id)

    if "result" in payload or "error" in payload:
        raise FrameDecodeError("CDP event cannot contain result or error")
    return CdpEvent(
        method=_require_text(payload["method"], "method"),
        params=_optional_object(payload, "params"),
        session_id=session_id,
    )


def params_session_id(frame: CdpFrame) -> str | None:
    """The session a lifecycle frame is ABOUT (params.sessionId), as opposed to the
    session it is scoped to (the frame's own sessionId)."""
    params = frame.params if isinstance(frame, (CdpCommand, CdpEvent)) else None
    value = params.get("sessionId") if params else None
    return value if isinstance(value, str) and value else None


def decode_frame(raw: str | bytes) -> CdpFrame:
    return _decode_payload(_parse_json(raw))


def _frame_payload(frame: CdpFrame) -> dict[str, Any]:
    if isinstance(frame, CdpCommand):
        payload: dict[str, Any] = {
            "id": _require_id(frame.id),
            "method": _require_text(frame.method, "method"),
        }
        if frame.params is not None:
            if not isinstance(frame.params, dict):
                raise FrameDecodeError("CDP params must be an object")
            payload["params"] = frame.params
    elif isinstance(frame, CdpResponse):
        payload = {"id": _require_id(frame.id)}
        if frame.result is not None and frame.error is not None:
            raise FrameDecodeError("CDP response cannot contain both result and error")
        if frame.error is not None:
            payload["error"] = _validate_error(frame.error)
        else:
            result = frame.result if frame.result is not None else {}
            if not isinstance(result, dict):
                raise FrameDecodeError("CDP result must be an object")
            payload["result"] = result
    elif isinstance(frame, CdpEvent):
        payload = {"method": _require_text(frame.method, "method")}
        if frame.params is not None:
            if not isinstance(frame.params, dict):
                raise FrameDecodeError("CDP params must be an object")
            payload["params"] = frame.params
    else:
        raise FrameDecodeError("unsupported CDP frame type")

    if frame.session_id is not None:
        payload["sessionId"] = _require_text(frame.session_id, "sessionId")
    return payload


def encode_frame(frame: CdpFrame) -> str:
    payload = _frame_payload(frame)
    try:
        return json.dumps(payload, ensure_ascii=True, separators=(",", ":"), allow_nan=False)
    except (TypeError, ValueError, OverflowError, RecursionError) as exc:
        raise FrameDecodeError(f"CDP frame is not JSON serializable: {exc}") from exc


@dataclass
class RequestIdRemapper:
    """Remaps client request IDs onto one upstream ID space.

    Proxy-issued commands share that ID space under `PROXY_CLIENT_KEY`, so a proxy
    command can never collide with a client's in-flight ID (which would hand one
    party the other's response).
    """

    max_pending: int = _MAX_PENDING_REQUESTS
    _next_id: int = 1
    _upstream_to_client: dict[int, tuple[str, int]] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if self.max_pending < 1:
            raise ValueError("max_pending must be positive")

    @property
    def pending_count(self) -> int:
        return len(self._upstream_to_client)

    def __len__(self) -> int:
        return self.pending_count

    def _allocate_upstream_id(self) -> int:
        upstream_id = self._next_id
        while upstream_id in self._upstream_to_client:
            upstream_id += 1
        self._next_id = upstream_id + 1
        return upstream_id

    def _evict_one_for(self, client_key: str) -> None:
        """Reclaim one slot on behalf of `client_key`, at its OWN expense.

        A client at the cap may only drop its own oldest mapping — never a co-tenant's
        and never the proxy's, since those responses were already promised and losing
        one silently desyncs that party. A caller with nothing of its own to reclaim is
        refused rather than served at someone else's cost.
        """
        for upstream_id, (owner, _) in self._upstream_to_client.items():
            if owner == client_key and owner != PROXY_CLIENT_KEY:
                del self._upstream_to_client[upstream_id]
                return
        raise RemapperFullError("no pending request of this caller's own can be evicted")

    def _remap(self, client_key: str, command: CdpCommand) -> CdpCommand:
        if not isinstance(command, CdpCommand):
            raise FrameDecodeError("only CDP commands can be remapped upstream")
        _frame_payload(command)
        # A response for an evicted ID arrives unmatched (dropped) rather than
        # mis-routed, since the ID is only recycled once the table wraps.
        while len(self._upstream_to_client) >= self.max_pending:
            self._evict_one_for(client_key)
        upstream_id = self._allocate_upstream_id()
        self._upstream_to_client[upstream_id] = (client_key, command.id)
        return CdpCommand(id=upstream_id, method=command.method, params=command.params, session_id=command.session_id)

    def to_upstream(self, client_key: str, command: CdpCommand) -> CdpCommand:
        if not isinstance(client_key, str) or not client_key.strip():
            raise ValueError("client_key must be a non-empty string")
        if client_key == PROXY_CLIENT_KEY:
            raise ValueError("client_key is reserved for proxy-issued commands")
        return self._remap(client_key, command)

    def to_upstream_as_proxy(self, command: CdpCommand) -> CdpCommand:
        """Remap a command the proxy itself issues; its response routes to
        `PROXY_CLIENT_KEY` and is consumed by the proxy, never sent to a client."""
        return self._remap(PROXY_CLIENT_KEY, command)

    def to_client(self, response: CdpResponse) -> tuple[str, CdpResponse] | None:
        if not isinstance(response, CdpResponse):
            raise FrameDecodeError("only CDP responses can be remapped to a client")
        _frame_payload(response)
        mapping = self._upstream_to_client.pop(response.id, None)
        if mapping is None:
            return None
        client_key, client_id = mapping
        return client_key, CdpResponse(
            id=client_id, result=response.result, error=response.error, session_id=response.session_id
        )

    def discard(self, upstream_id: int) -> None:
        """Forget a pending mapping whose response will never be delivered. Every
        terminal path for a response must clear this table, not only `to_client`."""
        self._upstream_to_client.pop(upstream_id, None)

    def clear_client(self, client_key: str) -> int:
        if not isinstance(client_key, str) or not client_key.strip():
            raise ValueError("client_key must be a non-empty string")
        if client_key == PROXY_CLIENT_KEY:
            # A departing client must never drop the proxy's own in-flight commands.
            raise ValueError("client_key is reserved for proxy-issued commands")
        pending_ids = [
            upstream_id for upstream_id, mapping in self._upstream_to_client.items() if mapping[0] == client_key
        ]
        for upstream_id in pending_ids:
            del self._upstream_to_client[upstream_id]
        return len(pending_ids)

    def clear(self) -> int:
        pending_count = len(self._upstream_to_client)
        self._upstream_to_client.clear()
        return pending_count
