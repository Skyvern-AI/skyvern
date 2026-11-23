import asyncio
from collections.abc import Awaitable, Callable

import structlog

LOG = structlog.get_logger()


async def finish_cleanup_deferring_cancellation(cleanup: Callable[[], Awaitable[object]], **log_context: str) -> None:
    """Run the cleanup to the end even if the caller is cancelled meanwhile. A cleanup failure is logged, not raised, so
    the caller re-raises its own error."""
    cleanup_task = asyncio.ensure_future(cleanup())
    while not cleanup_task.done():
        try:
            await asyncio.shield(cleanup_task)
        except (Exception, asyncio.CancelledError):
            continue
    if cleanup_task.cancelled():
        LOG.error("Cleanup was cancelled before it finished", **log_context)
    elif (error := cleanup_task.exception()) is not None:
        LOG.error("Cleanup failed", exc_info=error, **log_context)
