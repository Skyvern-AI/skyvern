from __future__ import annotations

import functools
import time
from collections.abc import Awaitable, Callable
from contextvars import ContextVar
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, ParamSpec, TypeVar

from skyvern.cli.core.guards import STALE_FRAME_HINT, stale_frame_selection_in

# Module-level flag: when True, make_result() strips fields that waste AI context
# tokens (echoed inputs, sdk_equivalent, browser_context, timing, empty collections).
# Set once at MCP server startup; CLI paths leave it False.
_concise_responses: bool = False

# Fields inside data{} that are debug/scripting aids, not decision-relevant for AI.
_DATA_STRIP_KEYS = frozenset(
    {
        "sdk_equivalent",
        "ai_mode",
        "selector",
        "intent",
    }
)

# Keys whose None value is meaningful (e.g. JS eval returning null).
# These survive the concise filter even when None.
_DATA_KEEP_NONE_KEYS = frozenset(
    {
        "result",
        "extracted",
    }
)

# (action, key) pairs whose None value is meaningful only for that specific
# action, not globally — e.g. a session has no recordings yet. Scoped rather
# than added to _DATA_KEEP_NONE_KEYS so it doesn't change unrelated tools
# (skyvern_run_task/skyvern_login also have a recording_url field).
_ACTION_DATA_KEEP_NONE_KEYS = frozenset(
    {
        ("skyvern_browser_session_get", "recording_url"),
        ("skyvern_browser_session_close", "recording_url"),
        # None is the terminal page marker; the client needs it to stop paginating.
        ("skyvern_page", "cursor_next"),
    }
)


def set_concise_responses(enabled: bool) -> None:
    global _concise_responses  # noqa: PLW0603
    _concise_responses = enabled


class ErrorCode:
    NO_ACTIVE_BROWSER = "NO_ACTIVE_BROWSER"
    SESSION_EXPIRED = "SESSION_EXPIRED"
    BROWSER_NOT_FOUND = "BROWSER_NOT_FOUND"
    SELECTOR_NOT_FOUND = "SELECTOR_NOT_FOUND"
    ACTION_FAILED = "ACTION_FAILED"
    AI_FALLBACK_FAILED = "AI_FALLBACK_FAILED"
    SDK_ERROR = "SDK_ERROR"
    TIMEOUT = "TIMEOUT"
    INVALID_INPUT = "INVALID_INPUT"
    WORKFLOW_NOT_FOUND = "WORKFLOW_NOT_FOUND"
    RUN_NOT_FOUND = "RUN_NOT_FOUND"
    API_ERROR = "API_ERROR"
    STALE_FRAME_SELECTION = "STALE_FRAME_SELECTION"


@dataclass
class Artifact:
    kind: str
    path: str
    mime: str
    bytes: int
    created_at: str = field(default_factory=lambda: datetime.now(timezone.utc).isoformat())

    def to_dict(self) -> dict[str, Any]:
        return {
            "kind": self.kind,
            "path": self.path,
            "mime": self.mime,
            "bytes": self.bytes,
            "created_at": self.created_at,
        }


@dataclass
class BrowserContext:
    mode: str
    session_id: str | None = None
    cdp_url: str | None = None
    can_access_localhost: bool | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "mode": self.mode,
            "session_id": self.session_id,
            "cdp_url": self.cdp_url,
        }


def make_result(
    action: str,
    *,
    ok: bool = True,
    browser_context: BrowserContext | None = None,
    data: dict[str, Any] | None = None,
    artifacts: list[Artifact] | None = None,
    timing_ms: dict[str, int] | None = None,
    warnings: list[str] | None = None,
    error: dict[str, Any] | None = None,
) -> dict[str, Any]:
    if _concise_responses:
        result: dict[str, Any] = {"ok": ok}
        if error:
            result["error"] = error
        if warnings:
            result["warnings"] = warnings
        if data:
            concise_data = {
                k: v
                for k, v in data.items()
                if k not in _DATA_STRIP_KEYS
                and (v is not None or k in _DATA_KEEP_NONE_KEYS or (action, k) in _ACTION_DATA_KEEP_NONE_KEYS)
            }
            if concise_data:
                result["data"] = concise_data
        if artifacts:
            result["artifacts"] = [a.to_dict() for a in artifacts]
        return result

    return {
        "ok": ok,
        "action": action,
        "browser_context": (browser_context or BrowserContext(mode="none")).to_dict(),
        "data": data,
        "artifacts": [a.to_dict() for a in (artifacts or [])],
        "timing_ms": timing_ms or {},
        "warnings": warnings or [],
        "error": error,
    }


def make_error(
    code: str,
    message: str,
    hint: str,
    details: dict[str, Any] | None = None,
    exc: BaseException | None = None,
) -> dict[str, Any]:
    if stale_frame_selection_in(exc):
        code = ErrorCode.STALE_FRAME_SELECTION
        hint = STALE_FRAME_HINT
    return {
        "code": code,
        "message": message,
        "hint": hint,
        "details": details or {},
    }


_P = ParamSpec("_P")
_R = TypeVar("_R")

_pending_attach: ContextVar[int | None] = ContextVar("mcp_pending_attach", default=None)


def _record_attach(started: float) -> None:
    elapsed_ms = int((time.perf_counter() - started) * 1000)
    _pending_attach.set((_pending_attach.get() or 0) + elapsed_ms)


def _take_attach() -> int | None:
    pending = _pending_attach.get()
    _pending_attach.set(None)
    return pending


def drop_pending_attach() -> None:
    """Called at MCP tool dispatch so an attach whose tool never opened a Timer cannot reach the next call."""
    _pending_attach.set(None)


def restore_pending_attach(attach_ms: int | None) -> None:
    """Hand an attach a finished Timer already claimed to the next Timer of the same tool call."""
    if attach_ms:
        _pending_attach.set((_pending_attach.get() or 0) + attach_ms)


def count_browser_attach(fn: Callable[_P, Awaitable[_R]]) -> Callable[_P, Awaitable[_R]]:
    """Report the browser attach a tool does before opening its Timer as that tool's own attach mark."""

    @functools.wraps(fn)
    async def counted(*args: _P.args, **kwargs: _P.kwargs) -> _R:
        started = time.perf_counter()
        try:
            return await fn(*args, **kwargs)
        finally:
            _record_attach(started)

    return counted


class Timer:
    def __init__(self) -> None:
        self._start: float = 0
        self._attach_ms: int | None = None
        self._marks: dict[str, int] = {}

    def __enter__(self) -> Timer:
        self._start = time.perf_counter()
        self._attach_ms = _take_attach()
        return self

    def __exit__(self, *args: Any) -> None:
        elapsed_ms = int((time.perf_counter() - self._start) * 1000)
        if self._attach_ms is not None:
            self._marks["attach"] = self._attach_ms
        self._marks["total"] = elapsed_ms
        _pending_attach.set(None)

    def mark(self, name: str) -> None:
        self._marks[name] = int((time.perf_counter() - self._start) * 1000)

    @property
    def timing_ms(self) -> dict[str, int]:
        return self._marks.copy()
