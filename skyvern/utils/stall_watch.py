import asyncio
import contextlib
from collections.abc import Iterator
from pathlib import PurePath
from types import AsyncGeneratorType, CoroutineType, FrameType, GeneratorType
from typing import Any

import structlog

LOG = structlog.get_logger()

# Keep the innermost frames: the call that is actually waiting sits at the bottom of a deep chain.
_MAX_REPORTED_FRAMES = 40
_MAX_WALKED_FRAMES = 500


def _frame_and_awaited(awaitable: object) -> tuple[FrameType | None, object]:
    if isinstance(awaitable, CoroutineType):
        return awaitable.cr_frame, awaitable.cr_await
    if isinstance(awaitable, GeneratorType):
        return awaitable.gi_frame, awaitable.gi_yieldfrom
    if isinstance(awaitable, AsyncGeneratorType):
        return awaitable.ag_frame, awaitable.ag_await
    return None, None


def describe_await_chain(task: asyncio.Task[Any]) -> list[str]:
    """Where a suspended task is waiting, outermost first, as ``dir/file.py:line function`` entries."""
    frames: list[str] = []
    awaitable: object = task.get_coro()
    while awaitable is not None and len(frames) < _MAX_WALKED_FRAMES:
        frame, awaited = _frame_and_awaited(awaitable)
        if frame is None:
            # A future or another awaitable with no frame of its own ends the chain.
            frames.append(type(awaitable).__name__)
            break
        path = PurePath(frame.f_code.co_filename)
        frames.append(f"{path.parent.name}/{path.name}:{frame.f_lineno} {frame.f_code.co_name}")
        awaitable = awaited
    return frames[-_MAX_REPORTED_FRAMES:]


async def _log_while_running(
    target: asyncio.Task[Any],
    message: str,
    first_after_seconds: float,
    repeat_every_seconds: float,
    log_fields: dict[str, Any],
) -> None:
    loop = asyncio.get_running_loop()
    started = loop.time()
    delay = first_after_seconds
    while True:
        await asyncio.sleep(delay)
        if target.done():
            return
        try:
            LOG.warning(
                message,
                elapsed_seconds=round(loop.time() - started),
                await_chain=describe_await_chain(target),
                **log_fields,
            )
        except Exception:
            with contextlib.suppress(Exception):
                LOG.exception("Stall watcher stopped after failing to log", stall_message=message, **log_fields)
            return
        delay = repeat_every_seconds


@contextlib.contextmanager
def log_if_stalled(
    message: str,
    *,
    first_after_seconds: float,
    repeat_every_seconds: float,
    **log_fields: Any,
) -> Iterator[None]:
    """While the enclosed block runs longer than ``first_after_seconds``, log where the current task is waiting.

    Diagnostics only: the block runs exactly as without it, and the watcher stops when the block exits.
    """
    target = asyncio.current_task()
    if target is None:
        yield
        return
    watcher = asyncio.create_task(
        _log_while_running(target, message, first_after_seconds, repeat_every_seconds, log_fields)
    )
    try:
        yield
    finally:
        watcher.cancel()
