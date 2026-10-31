"""Execution-time accounting shared by concurrently waiting human-input tools."""

from __future__ import annotations

import time
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Literal

if TYPE_CHECKING:
    from skyvern.forge.sdk.copilot.context import CopilotContext

# A charged wait can spend the turn; the backstop then waits this long so the tool returns and the turn winds down.
CHARGED_WAIT_BACKSTOP_GRACE_SECONDS = 30.0


@dataclass
class HumanInputWait:
    count: int = 0
    started_at: float = 0.0
    deadline_when: float | None = None
    owner: Literal["question", "credential"] = "question"
    # When each wait a person answered or worked on was open, in the current overlapping union.
    attended_spans: list[tuple[float, float]] = field(default_factory=list)
    # Set once the backstop needed its grace: the turn is past its budget, so later waits no longer pause it.
    grace_used: bool = False


@dataclass
class HumanInputWaiter:
    started_at: float
    # Set by a card that ended with nobody answering it or signing in on it; such a wait counts against the turn.
    unattended: bool = False


def _covered_seconds(spans: list[tuple[float, float]]) -> float:
    covered, reach = 0.0, float("-inf")
    for start, end in sorted(spans):
        if end > reach:
            covered += end - max(start, reach)
            reach = end
    return covered


@contextmanager
def pause_human_input(ctx: CopilotContext, kind: Literal["question", "credential"]) -> Iterator[HumanInputWaiter]:
    """Pause the deadline until the last overlapping wait ends, then credit only the time a person was attending."""
    wait = ctx.human_input_wait
    waiter = HumanInputWaiter(started_at=time.monotonic())
    if wait.count == 0:
        wait.started_at = waiter.started_at
        wait.owner = kind
        wait.attended_spans = []
        deadline = ctx.model_stream_deadline
        wait.deadline_when = deadline.when() if deadline is not None and not wait.grace_used else None
        if deadline is not None and wait.deadline_when is not None:
            deadline.reschedule(None)
    wait.count += 1
    try:
        yield waiter
    finally:
        now = time.monotonic()
        if not waiter.unattended:
            wait.attended_spans.append((waiter.started_at, now))
        wait.count -= 1
        if wait.count == 0:
            elapsed = _covered_seconds(wait.attended_spans)
            if wait.owner == "question":
                ctx.copilot_question_pause_seconds += elapsed
            elif wait.owner == "credential":
                ctx.copilot_credential_pause_seconds += elapsed
            deadline = ctx.model_stream_deadline
            if deadline is not None and wait.deadline_when is not None and not deadline.expired():
                resumed_at = wait.deadline_when + elapsed
                wait.grace_used = resumed_at < now + CHARGED_WAIT_BACKSTOP_GRACE_SECONDS
                try:
                    deadline.reschedule(max(resumed_at, now + CHARGED_WAIT_BACKSTOP_GRACE_SECONDS))
                except RuntimeError:
                    # The owning timeout may already have exited during cancellation.
                    pass
            wait.deadline_when = None


def turn_clock_seconds(ctx: Any, start_time: float, now: float) -> float:
    """Elapsed from ``start_time`` to ``now``, minus completed and active human-input waits.

    Getattr-coerced because tests commonly pass a bare ``MagicMock()`` as ``ctx``, whose attributes are Mocks.
    """
    pause_seconds = getattr(ctx, "copilot_credential_pause_seconds", 0.0)
    if not isinstance(pause_seconds, (int, float)):
        pause_seconds = 0.0
    question_seconds = getattr(ctx, "copilot_question_pause_seconds", 0.0)
    if not isinstance(question_seconds, (int, float)):
        question_seconds = 0.0
    wait = getattr(ctx, "human_input_wait", None)
    # Overlapping waits credit their union only when the last waiter exits.
    # Until then, exclude the active union at model/tool completion boundaries.
    active_wait_seconds = now - wait.started_at if isinstance(wait, HumanInputWait) and wait.count > 0 else 0.0
    return now - start_time - pause_seconds - question_seconds - active_wait_seconds
