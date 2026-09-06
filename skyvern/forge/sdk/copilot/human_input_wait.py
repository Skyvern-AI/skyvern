"""Execution-time accounting shared by concurrently waiting human-input tools."""

from __future__ import annotations

import time
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from typing import TYPE_CHECKING, Literal

if TYPE_CHECKING:
    from skyvern.forge.sdk.copilot.context import CopilotContext


@dataclass
class HumanInputWait:
    count: int = 0
    started_at: float = 0.0
    deadline_when: float | None = None
    owner: Literal["question", "credential"] = "question"


@contextmanager
def pause_human_input(ctx: CopilotContext, kind: Literal["question", "credential"]) -> Iterator[None]:
    """Pause the deadline until the last overlapping wait ends; credit their union once."""
    wait = ctx.human_input_wait
    if wait.count == 0:
        wait.started_at = time.monotonic()
        wait.owner = kind
        deadline = ctx.model_stream_deadline
        wait.deadline_when = deadline.when() if deadline is not None else None
        if deadline is not None and wait.deadline_when is not None:
            deadline.reschedule(None)
    wait.count += 1
    try:
        yield
    finally:
        wait.count -= 1
        if wait.count == 0:
            elapsed = time.monotonic() - wait.started_at
            if wait.owner == "question":
                ctx.copilot_question_pause_seconds += elapsed
            else:
                ctx.copilot_credential_pause_seconds += elapsed
            deadline = ctx.model_stream_deadline
            if deadline is not None and wait.deadline_when is not None and not deadline.expired():
                try:
                    deadline.reschedule(wait.deadline_when + elapsed)
                except RuntimeError:
                    # The owning timeout may already have exited during cancellation.
                    pass
            wait.deadline_when = None
