from __future__ import annotations

import asyncio
from collections.abc import Awaitable
from typing import TypeVar

T = TypeVar("T")


async def await_to_terminal_state(awaitable: Awaitable[T]) -> T:
    """Finish an essential async operation before propagating caller cancellation.

    ``asyncio.shield`` alone protects the child task from one cancellation but the
    caller can still be cancelled again while waiting. This helper retains a strong
    task reference and defers every caller cancellation until the child reaches a
    terminal state.
    """

    task = asyncio.ensure_future(awaitable)
    pending_cancellation: asyncio.CancelledError | None = None
    while not task.done():
        try:
            await asyncio.shield(task)
        except asyncio.CancelledError as cancellation:
            if pending_cancellation is None:
                pending_cancellation = cancellation
        except BaseException:
            # ``shield`` forwards a terminal child failure. Inspect task.result()
            # below exactly once so cancellation/error arbitration is consistent.
            break

    try:
        result = task.result()
    except BaseException as operation_error:
        if pending_cancellation is not None:
            raise pending_cancellation from operation_error
        raise
    if pending_cancellation is not None:
        raise pending_cancellation
    return result
