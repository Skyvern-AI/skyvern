from __future__ import annotations

import asyncio
import math
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from skyvern.exceptions import ActionDeadlineExceeded
from skyvern.forge.sdk.core import skyvern_context
from skyvern.webeye.browser_health import BrowserOperation

ACTION_DEADLINE_HEADROOM_MS = 5000
DEADLINE_REARM_INTERVAL_SECONDS = 1.0


def cancellation_pending() -> bool:
    """Whether this task carries an unhandled cancellation request.

    A driver can translate an external cancellation into an ordinary transport error, and returning a
    result for that would let the caller treat a cancelled operation as one that completed."""
    task = asyncio.current_task()
    return task is not None and task.cancelling() > 0


def raise_if_cancelled() -> None:
    """Re-raise an external cancellation that never surfaced as an exception.

    Navigation recovery can settle and retry straight through a cancellation the driver translated
    or swallowed, so a successful return is not proof the caller still wants the result."""
    if cancellation_pending():
        raise asyncio.CancelledError


def record_browser_timeout(operation: BrowserOperation = BrowserOperation.EVALUATE) -> None:
    """Tally a strike for a call the caller bounded itself, so ``record_unreported_timeout`` does not
    mistake the synthesized timeout for one the engine tallied."""
    skyvern_context.record_browser_timeout(operation)


@asynccontextmanager
async def under_action_deadline(
    *, budget_ms: int | None, operation: BrowserOperation | None = None
) -> AsyncIterator[None]:
    """Bound a driver call whose own worst-case ceiling — every leg, retry and verification loop it may
    run — already fits ``budget_ms`` (``None`` leaves it unbounded), so only a driver that stopped
    answering reaches this deadline. ``operation`` tallies a browser-health strike, and belongs only to
    a verb whose budget is generous enough that an expiry means the browser is gone."""
    budget_seconds = None if budget_ms is None else (budget_ms + ACTION_DEADLINE_HEADROOM_MS) / 1000
    task = asyncio.current_task()
    assert task is not None
    loop = asyncio.get_running_loop()
    rearms = 0
    settled = False
    rearm_handle: asyncio.TimerHandle | None = None

    def expired() -> ActionDeadlineExceeded:
        if operation is not None:
            record_browser_timeout(operation)
        return ActionDeadlineExceeded(f"The browser did not answer within {budget_ms} ms")

    # asyncio.timeout cancels once. A leg that turns that cancellation into an ordinary error which the
    # body swallows leaves any later driver leg with nothing to end it, so after expiry the task is
    # cancelled again every interval until the scope exits.
    def rearm() -> None:
        nonlocal rearms, rearm_handle
        if settled:
            return
        rearms += 1
        task.cancel()
        rearm_handle = loop.call_later(DEADLINE_REARM_INTERVAL_SECONDS, rearm)

    rearm_handle = (
        None if budget_seconds is None else loop.call_later(budget_seconds + DEADLINE_REARM_INTERVAL_SECONDS, rearm)
    )

    def settle() -> None:
        # Our own re-arm cancels must never read as an external cancellation once the scope is over.
        nonlocal settled
        if settled:
            return
        settled = True
        if rearm_handle is not None:
            rearm_handle.cancel()
        for _ in range(rearms):
            task.uncancel()

    # The driver may surface the deadline's cancellation as TimeoutError, translate it into its own
    # error, or swallow it and return; every exit path consults the deadline, not the exception type.
    try:
        async with asyncio.timeout(budget_seconds) as deadline:
            yield
    except TimeoutError as exc:
        settle()
        raise_if_cancelled()
        if not deadline.expired():
            raise
        raise expired() from exc
    except asyncio.CancelledError:
        settle()
        if rearms and not cancellation_pending():
            raise expired() from None
        raise
    except Exception as exc:
        settle()
        raise_if_cancelled()
        if deadline.expired():
            raise expired() from exc
        raise
    finally:
        # A KeyboardInterrupt or SystemExit through the scope must not leave the chain cancelling the task.
        settle()
    raise_if_cancelled()
    if deadline.expired():
        raise expired()


def outer_cap_seconds(budget_ms: int) -> int:
    """A ceiling for a caller wrapping a tool that bounds itself at ``budget_ms``; it arms strictly after
    the inner deadline so the tool's structured timeout wins the race."""
    return math.ceil((budget_ms + 2 * ACTION_DEADLINE_HEADROOM_MS) / 1000)
