from __future__ import annotations

import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any


class ErrorCode:
    NO_ACTIVE_BROWSER = "NO_ACTIVE_BROWSER"
    BROWSER_NOT_FOUND = "BROWSER_NOT_FOUND"
    SELECTOR_NOT_FOUND = "SELECTOR_NOT_FOUND"
    ACTION_FAILED = "ACTION_FAILED"
    AI_FALLBACK_FAILED = "AI_FALLBACK_FAILED"
    SDK_ERROR = "SDK_ERROR"
    TIMEOUT = "TIMEOUT"
    INVALID_INPUT = "INVALID_INPUT"


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
