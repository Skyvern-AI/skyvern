import asyncio
from collections.abc import Awaitable, Callable
from functools import partial

import structlog

LOG = structlog.get_logger()

CLEANUP_TIMEOUT_SECONDS = 30.0

# A cleanup that outlives its caller's wait is kept here so the event loop's weak task reference cannot drop it.
_DETACHED_CLEANUPS: set[asyncio.Future[object]] = set()


def _log_cleanup_failure(failure_message: str, log_context: dict[str, object], cleanup: asyncio.Future[object]) -> None:
    if cleanup.cancelled():
        LOG.error(failure_message, cleanup_cancelled=True, **log_context)
    elif (error := cleanup.exception()) is not None:
        LOG.error(failure_message, exc_info=error, **log_context)


async def finish_cleanup_deferring_cancellation(
    cleanup: Callable[[], Awaitable[object]],
    *,
    failure_message: str = "Cleanup failed",
    timeout_seconds: float = CLEANUP_TIMEOUT_SECONDS,
    **log_context: object,
) -> None:
    """Wait for the cleanup even if the caller is cancelled meanwhile, for at most the timeout; a cleanup still running
    then finishes detached. A cleanup failure is logged, not raised; a cancel that arrived meanwhile is re-raised."""
    cleanup_task = asyncio.ensure_future(cleanup())
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout_seconds
    deferred_cancel: asyncio.CancelledError | None = None
    while not cleanup_task.done() and (remaining := deadline - loop.time()) > 0:
        try:
            await asyncio.wait((cleanup_task,), timeout=remaining)
        except asyncio.CancelledError as cancel:
            deferred_cancel = deferred_cancel or cancel
    if cleanup_task.done():
        _log_cleanup_failure(failure_message, log_context, cleanup_task)
    else:
        LOG.warning(
            "Cleanup outlived its wait and continues detached",
            cleanup_timeout_seconds=timeout_seconds,
            **log_context,
        )
        _DETACHED_CLEANUPS.add(cleanup_task)
        cleanup_task.add_done_callback(_DETACHED_CLEANUPS.discard)
        cleanup_task.add_done_callback(partial(_log_cleanup_failure, failure_message, log_context))
    if deferred_cancel is not None:
        raise deferred_cancel
