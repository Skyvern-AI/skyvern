from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from typing import Any

from skyvern.exceptions import SkyvernPageAnalysisTimeout
from skyvern.forge.sdk.core import skyvern_context
from skyvern.webeye.browser_health import BrowserOperation


class CallerJsDispatchError(Exception):
    """Carries the driver error from caller-authored JS past the engine's navigation recovery.

    The engine only recovers driver-family errors, so a plain wrapper is re-raised unchanged instead of
    re-running an expression that is not known to be idempotent."""

    def __init__(self, original: Exception) -> None:
        super().__init__(str(original))
        self.original = original


def without_navigation_recovery(dispatch: Callable[[], Awaitable[Any]]) -> Callable[[], Awaitable[Any]]:
    async def _dispatch() -> Any:
        try:
            return await dispatch()
        except Exception as exc:
            raise CallerJsDispatchError(exc) from exc

    return _dispatch


def unwrap_caller_js_error(exc: Exception) -> Exception:
    return exc.original if isinstance(exc, CallerJsDispatchError) else exc


def deadline_reached(deadline: float) -> bool:
    """Whether the action deadline has passed, so a driver error raised under it is the deadline's doing.

    Cancelling an in-flight evaluate can surface the driver's own transport error ahead of the engine's
    timeout, which would otherwise be reported to the caller as a script failure."""
    return asyncio.get_running_loop().time() >= deadline


def cancellation_pending() -> bool:
    """Whether this task carries an unhandled cancellation request.

    A driver can translate an external cancellation into an ordinary transport error, and returning a
    result for that would let the caller treat a cancelled operation as one that completed."""
    task = asyncio.current_task()
    return task is not None and task.cancelling() > 0


def deadline_ended_the_call(exc: Exception, deadline: float) -> bool:
    """Whether the action deadline is what ended this call. Pure; records nothing."""
    return isinstance(exc, SkyvernPageAnalysisTimeout) or deadline_reached(deadline)


def record_unreported_timeout(exc: Exception) -> None:
    """Tally the browser-health strike the engine records inside its own timeout handlers.

    A driver error raised as the deadline cancelled the call never reaches one, so without this the
    hang never counts toward marking the browser degraded."""
    if not isinstance(exc, SkyvernPageAnalysisTimeout):
        skyvern_context.record_browser_timeout(BrowserOperation.EVALUATE)


def record_browser_timeout() -> None:
    """Tally a strike for a browser-protocol call that never answered within the action deadline.

    For the callers that detect this themselves rather than letting the engine raise, so that a
    synthesized timeout is not mistaken by ``record_unreported_timeout`` for one the engine tallied."""
    skyvern_context.record_browser_timeout(BrowserOperation.EVALUATE)


def raise_if_cancelled() -> None:
    """Re-raise an external cancellation that never surfaced as an exception.

    Navigation recovery can settle and retry straight through a cancellation the driver translated
    or swallowed, so a successful return is not proof the caller still wants the result."""
    if cancellation_pending():
        raise asyncio.CancelledError


def cancel_aware(dispatch: Callable[[], Awaitable[Any]]) -> Callable[[], Awaitable[Any]]:
    """Check for cancellation before every dispatch, including the engine's recovery retries.

    Recovery re-invokes this closure against the new document, so a mutation would otherwise be sent
    again after its caller had already been cancelled."""

    async def _dispatch() -> Any:
        raise_if_cancelled()
        return await dispatch()

    return _dispatch
