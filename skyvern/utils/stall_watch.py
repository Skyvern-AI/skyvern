import asyncio
import contextlib
from collections.abc import Iterator
from pathlib import PurePath
from types import AsyncGeneratorType, CoroutineType, FrameType, GeneratorType
from typing import Any, TypeVar

import structlog

LOG = structlog.get_logger()

T = TypeVar("T")

# Keep the innermost frames: the call that is actually waiting sits at the bottom of a deep chain.
_MAX_REPORTED_FRAMES = 40
_MAX_WALKED_FRAMES = 500

ABANDONED_TASK_RECANCEL_SECONDS = 1.0
ABANDONED_TASK_WARN_AFTER_RECANCELS = 60


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


class TaskWaitExpired(TimeoutError):
    """``wait_for_task`` stopped waiting; it has already logged where the task was waiting."""


def _retrieve_outcome(task: asyncio.Task[Any]) -> None:
    if not task.cancelled():
        task.exception()


def _cancel_until_done(task: asyncio.Task[Any], recancels: int) -> None:
    if task.done():
        return
    task.cancel()
    if recancels == ABANDONED_TASK_WARN_AFTER_RECANCELS:
        LOG.warning(
            "Abandoned task is still running after repeated cancellation",
            task_name=task.get_name(),
            await_chain=describe_await_chain(task),
        )
    # The first repeat runs on the next loop pass, right after the task handles the first cancellation, so a task
    # that swallowed it is cancelled again at its next await rather than a full interval later.
    delay = 0.0 if recancels == 0 else ABANDONED_TASK_RECANCEL_SECONDS
    asyncio.get_running_loop().call_later(delay, _cancel_until_done, task, recancels + 1)


def abandon_task(task: asyncio.Task[Any]) -> None:
    """Cancel ``task`` until it ends, for a caller that will not wait for it."""
    # One cancel() is a single CancelledError at the task's current await. A call that swallows it would let the
    # task carry on against a browser the caller has moved on from.
    task.add_done_callback(_retrieve_outcome)
    _cancel_until_done(task, 0)


async def _wait_until(
    task: asyncio.Task[Any], deadline: float | None, message: str, log_fields: dict[str, Any]
) -> bool:
    # Unlike asyncio.wait_for, expiry does not wait for the task to unwind: a call that holds its cancellation
    # would otherwise hold the caller too. asyncio.wait never raises the task's own outcome, so a CancelledError
    # here is always the caller's.
    loop = asyncio.get_running_loop()
    started = loop.time()
    try:
        done, _ = await asyncio.wait({task}, timeout=None if deadline is None else max(0.0, deadline - started))
    except asyncio.CancelledError:
        # Plain ``await task`` would have cancelled the task along with the caller.
        abandon_task(task)
        raise
    if done:
        return True
    LOG.warning(
        message,
        waited_seconds=round(loop.time() - started),
        await_chain=describe_await_chain(task),
        **log_fields,
    )
    abandon_task(task)
    return False


async def wait_for_task(task: asyncio.Task[T], deadline: float | None, message: str, **log_fields: Any) -> T:
    """Await ``task`` until the event-loop time ``deadline`` (``None`` waits unbounded); on expiry, log where it
    waits, cancel it until it ends, and raise TaskWaitExpired."""
    if not await _wait_until(task, deadline, message, log_fields):
        raise TaskWaitExpired(f"{task.get_name()} did not finish in time")
    return task.result()


async def cancel_and_wait(task: asyncio.Task[Any], deadline: float | None, message: str, **log_fields: Any) -> bool:
    """Cancel ``task`` and wait until ``deadline`` for it to end, discarding how it ended. Returns whether it ended;
    one that did not is logged and cancelled until it does."""
    task.cancel()
    if not await _wait_until(task, deadline, message, log_fields):
        return False
    _retrieve_outcome(task)
    return True


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
