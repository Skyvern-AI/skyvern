from __future__ import annotations

import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

from skyvern import analytics

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


def set_concise_responses(enabled: bool) -> None:
    global _concise_responses  # noqa: PLW0603
    _concise_responses = enabled


class ErrorCode:
    NO_ACTIVE_BROWSER = "NO_ACTIVE_BROWSER"
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
    analytics.capture(
        "mcp_tool_call",
        data={
            **analytics.analytics_metadata(),
            "tool": action,
            "ok": ok,
            # "total" is set by Timer.__exit__; None for early-return paths before Timer starts
            "timing_ms": (timing_ms or {}).get("total"),
            "error_code": error.get("code") if error else None,
            "browser_mode": browser_context.mode if browser_context else None,
            "session_id": browser_context.session_id if browser_context else None,
        },
    )

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
                if k not in _DATA_STRIP_KEYS and (v is not None or k in _DATA_KEEP_NONE_KEYS)
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
) -> dict[str, Any]:
    return {
        "code": code,
        "message": message,
        "hint": hint,
        "details": details or {},
    }


class Timer:
    def __init__(self) -> None:
        self._start: float = 0
        self._marks: dict[str, int] = {}

    def __enter__(self) -> Timer:
        self._start = time.perf_counter()
        return self

    def __exit__(self, *args: Any) -> None:
        self._marks["total"] = int((time.perf_counter() - self._start) * 1000)

    def mark(self, name: str) -> None:
        self._marks[name] = int((time.perf_counter() - self._start) * 1000)

    @property
    def timing_ms(self) -> dict[str, int]:
        return self._marks.copy()
