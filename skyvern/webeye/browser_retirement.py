import asyncio
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any


class BrowserRetirementReason(StrEnum):
    replacement = "replacement"
    session_ending = "session_ending"


@dataclass(frozen=True)
class BrowserOperationRejected:
    reason: BrowserRetirementReason


class BrowserStatePublicationRejected(RuntimeError):
    def __init__(self, session_id: str) -> None:
        self.session_id = session_id
        super().__init__(f"Browser session {session_id} is ending and cannot accept a browser state")


@dataclass
class BrowserRetirement:
    started: asyncio.Event = field(default_factory=asyncio.Event)
    reason: BrowserRetirementReason | None = None
    _cancelled_tasks: set[asyncio.Task[Any]] = field(default_factory=set)

    def begin(self, reason: BrowserRetirementReason) -> None:
        if self.started.is_set():
            return
        self.reason = reason
        self.started.set()

    def cancel(self, task: asyncio.Task[Any]) -> None:
        """Cancel an admitted operation and remember that this retirement owns one cancel."""
        self._cancelled_tasks.add(task)
        task.cancel()

    def consume_cancellation(self, task: asyncio.Task[Any]) -> bool:
        """Consume only the cancellation issued by this retirement, never a caller's cancel."""
        if task not in self._cancelled_tasks:
            return False
        self._cancelled_tasks.discard(task)
        if task.cancelling():
            task.uncancel()
        return True
