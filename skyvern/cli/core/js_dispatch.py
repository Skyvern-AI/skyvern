from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from typing import Any

from skyvern.exceptions import SkyvernPageAnalysisTimeout
from skyvern.forge.sdk.core import skyvern_context
from skyvern.webeye.action_deadline import (
    ACTION_DEADLINE_HEADROOM_MS,
    cancellation_pending,
    outer_cap_seconds,
    raise_if_cancelled,
    record_browser_timeout,
    under_action_deadline,
)
from skyvern.webeye.browser_health import BrowserOperation

__all__ = [
    "ACTION_DEADLINE_HEADROOM_MS",
    "CallerJsDispatchError",
    "cancel_aware",
    "cancellation_pending",
    "deadline_ended_the_call",
    "deadline_reached",
    "outer_cap_seconds",
    "raise_if_cancelled",
    "record_browser_timeout",
    "record_unreported_timeout",
    "under_action_deadline",
    "unwrap_caller_js_error",
    "without_navigation_recovery",
]


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


def deadline_ended_the_call(exc: Exception, deadline: float) -> bool:
    """Whether the action deadline is what ended this call. Pure; records nothing."""
    return isinstance(exc, SkyvernPageAnalysisTimeout) or deadline_reached(deadline)


def record_unreported_timeout(exc: Exception) -> None:
    """Tally the browser-health strike the engine records inside its own timeout handlers.

    A driver error raised as the deadline cancelled the call never reaches one, so without this the
    hang never counts toward marking the browser degraded."""
    if not isinstance(exc, SkyvernPageAnalysisTimeout):
        skyvern_context.record_browser_timeout(BrowserOperation.EVALUATE)


def cancel_aware(dispatch: Callable[[], Awaitable[Any]]) -> Callable[[], Awaitable[Any]]:
    """Check for cancellation before every dispatch, including the engine's recovery retries.

    Recovery re-invokes this closure against the new document, so a mutation would otherwise be sent
    again after its caller had already been cancelled."""

    async def _dispatch() -> Any:
        raise_if_cancelled()
        return await dispatch()

    return _dispatch
