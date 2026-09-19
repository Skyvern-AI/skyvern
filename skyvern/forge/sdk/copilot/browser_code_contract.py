"""Types shared by the copilot browser-code tool and the deployment that hosts its interpreter."""

from __future__ import annotations

import uuid
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any, Protocol

from playwright.async_api import Page

# Operations that demonstrate an interaction the scout trajectory must keep, even past the
# model-facing operation cap.
DEMONSTRATED_OPERATIONS = frozenset(
    {"click", "dblclick", "tap", "fill", "type", "press", "press_sequentially", "check", "uncheck", "set_checked"}
    | {"select_option"}
)


class BrowserCodeSessionUnavailableError(Exception):
    """The deployment could not open or keep a code session; the message is safe to show the model."""

    def __init__(
        self,
        message: str,
        *,
        error_code: str,
        retry_after_seconds: float | None = None,
        stage: str | None = None,
        session_ended: bool = False,
    ) -> None:
        super().__init__(message)
        self.error_code = error_code
        # Set when the refusal is a capacity one, so the model is told when to try again.
        self.retry_after_seconds = retry_after_seconds
        # Which step refused: dispatch, session_open or cell_admission.
        self.stage = stage
        # Set when the session this refusal came from is over, so the caller drops it now rather than
        # spending the retry it was just told to make on discovering that.
        self.session_ended = session_ended


@dataclass(frozen=True)
class BrowserCodeOperation:
    operation: str
    selector: str | None
    # None means the operation reached the browser and no reply came back, so it may have applied.
    succeeded: bool | None
    error: str | None = None
    # Set only when a trusted readback of the filled field equals the value the fill sent.
    input_value: str | None = None
    source_url: str | None = None
    result_url: str | None = None

    def to_json(self) -> dict[str, Any]:
        return {
            "operation": self.operation,
            "selector": self.selector,
            "succeeded": self.succeeded,
            "error": self.error,
            "input_value": self.input_value,
            "source_url": self.source_url,
            "result_url": self.result_url,
        }

    @classmethod
    def from_json(cls, payload: Mapping[str, Any]) -> BrowserCodeOperation:
        return cls(
            operation=str(payload["operation"]),
            selector=payload.get("selector"),
            succeeded=payload.get("succeeded"),
            error=payload.get("error"),
            input_value=payload.get("input_value"),
            source_url=payload.get("source_url"),
            result_url=payload.get("result_url"),
        )


@dataclass(frozen=True)
class BrowserCodeCellResult:
    ok: bool
    value: Any
    stdout: str | None
    stdout_truncated: bool
    error_code: str | None
    error: str | None
    failing_line: int | None
    operations: tuple[BrowserCodeOperation, ...]
    operations_omitted: int
    session_alive: bool
    current_url: str | None
    # Every demonstrated action the cell ran, uncapped, so action evidence survives the model-facing cap.
    actions: tuple[BrowserCodeOperation, ...] = ()
    # Set when this cell ran on a fresh interpreter, which the caller cannot see for itself: the host it
    # is talking to is unchanged, and only the side that reopened knows the earlier values are gone.
    interpreter_restarted: bool = False
    # Set when the interpreter was moved to another tab since the last result, which drops every page,
    # locator and frame handle the cell holds; a move the caller did not ask for is otherwise invisible.
    handles_invalidated: bool = False

    def to_json(self) -> dict[str, Any]:
        return {
            "ok": self.ok,
            "value": self.value,
            "stdout": self.stdout,
            "stdout_truncated": self.stdout_truncated,
            "error_code": self.error_code,
            "error": self.error,
            "failing_line": self.failing_line,
            "operations": [operation.to_json() for operation in self.operations],
            "operations_omitted": self.operations_omitted,
            "session_alive": self.session_alive,
            "current_url": self.current_url,
            "actions": [action.to_json() for action in self.actions],
            "interpreter_restarted": self.interpreter_restarted,
            "handles_invalidated": self.handles_invalidated,
        }

    @classmethod
    def from_json(cls, payload: Mapping[str, Any]) -> BrowserCodeCellResult:
        return cls(
            ok=bool(payload["ok"]),
            value=payload.get("value"),
            stdout=payload.get("stdout"),
            stdout_truncated=bool(payload.get("stdout_truncated", False)),
            error_code=payload.get("error_code"),
            error=payload.get("error"),
            failing_line=payload.get("failing_line"),
            operations=tuple(BrowserCodeOperation.from_json(item) for item in payload.get("operations") or ()),
            operations_omitted=int(payload.get("operations_omitted", 0)),
            session_alive=bool(payload.get("session_alive", False)),
            current_url=payload.get("current_url"),
            actions=tuple(BrowserCodeOperation.from_json(item) for item in payload.get("actions") or ()),
            interpreter_restarted=bool(payload.get("interpreter_restarted", False)),
            handles_invalidated=bool(payload.get("handles_invalidated", False)),
        )


class BrowserCodeSession(Protocol):
    @property
    def session_id(self) -> str: ...

    @property
    def page(self) -> Page: ...

    async def rebind(self, page: Page) -> None: ...

    async def run_cell(
        self, code: str, *, timeout_seconds: float, deny_pixels: bool = False
    ) -> BrowserCodeCellResult: ...

    def last_dispatched_operation(self) -> BrowserCodeOperation | None: ...

    async def close(self) -> None: ...


@dataclass
class BrowserCodeHost:
    """The turn's code session and the browser binding it was opened or last rebound for."""

    # Names this turn's interpreter to whoever hosts it. Minted here because the host is created once
    # per turn, so the identifier lives exactly as long as the session it names.
    turn_key: str = field(default_factory=lambda: uuid.uuid4().hex)
    session: BrowserCodeSession | None = None
    browser_session_id: str | None = None
    browser_session_generation: int = 0
    sessions_opened: int = 0
    interrupted: bool = False
    interrupted_operation: BrowserCodeOperation | None = None
