from __future__ import annotations

import asyncio
import contextvars
import math
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager
from typing import Any, TypeVar

from skyvern.exceptions import ActionDeadlineExceeded
from skyvern.forge.sdk.core import skyvern_context
from skyvern.webeye.browser_health import BrowserOperation

ACTION_DEADLINE_HEADROOM_MS = 5000
DEADLINE_REARM_INTERVAL_SECONDS = 1.0

T = TypeVar("T")
_UNSET = object()


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


def _retrieve_outcome(call: asyncio.Task[Any]) -> None:
    # A call cancelled too late to stop can still end in an error, and by then nobody is awaiting it.
    if not call.cancelled():
        call.exception()


def _publish_context_writes(context: contextvars.Context) -> None:
    for var in context:
        if var.get(_UNSET) is not context[var]:
            var.set(context[var])


async def cancellable_driver_call(call: Callable[[], Awaitable[T]]) -> T:
    """Issue a driver call in a task of its own, so cancelling the caller reaches the driver's cleanup.

    playwright and patchright abandon a pending protocol reply from a done-callback on the task that
    issued the call, and only when that task ends cancelled (``_impl/_connection.py::ProtocolCallback``).
    A deadline cancels the coroutine that awaits the call inside a task which carries on, so a call
    awaited directly keeps its reply pending and the driver's late error lands on a future nobody
    retrieves -- which asyncio reports at error level as an exception that was never retrieved."""
    # A cancellation request lives on the task that received it, and the task below is a fresh one, so
    # a guard inside the call (``cancel_aware``) can no longer see one the caller already carries.
    # Issuing the call at all is what that guard is there to prevent, so answer it here instead.
    raise_if_cancelled()

    async def issue() -> T:
        return await call()

    context = contextvars.copy_context()
    call_task = asyncio.create_task(issue(), context=context)
    call_task.add_done_callback(_retrieve_outcome)
    try:
        return await call_task
    except asyncio.CancelledError:
        # A cancellation that arrives after the call has answered never reached the task.
        call_task.cancel()
        raise
    finally:
        # The task ran in a copy of this context, so a write inside the call would otherwise be lost.
        # A cancelled call may still be unwinding, and publishing a binding it is about to restore
        # would outlive the call, so only a finished one is published.
        if call_task.done() and not call_task.cancelled():
            _publish_context_writes(context)


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
