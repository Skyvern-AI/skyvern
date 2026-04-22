"""Regression tests for SKY-8986: SSE disconnect must not kill the handler.

The SSE stream is a view of work the backend is doing on the client's behalf
(e.g., the workflow copilot agent). Closing the browser tab or losing the TCP
connection mid-stream used to cancel the handler task, which in turn
cancelled the agent run and lost the unpersisted chat reply. The fix in
SKY-8986 decouples the handler from the SSE response lifecycle: the handler
runs to completion even after the client goes away, and subsequent send()
calls drop their payload silently instead of backing up the in-memory queue.
"""

from __future__ import annotations

import asyncio
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

from skyvern.forge.sdk.routes.event_source_stream import (
    _BACKGROUND_HANDLER_TASKS,
    FastAPIEventSourceStream,
)


def _make_request(is_disconnected_values: list[bool]) -> MagicMock:
    """Build a fake Starlette Request whose is_disconnected() replays a script."""
    request = MagicMock()
    request.is_disconnected = AsyncMock(side_effect=is_disconnected_values)
    return request


@pytest.mark.asyncio
async def test_send_drops_events_silently_after_disconnect() -> None:
    """Once the client is gone, send() returns True but does not queue events.

    If send() queued instead, a long-running agent would grow the queue
    unbounded (no one is reading it) and leak memory until the process
    was restarted.
    """
    request = _make_request([False, True, True, True])
    stream = FastAPIEventSourceStream(request)

    first = await stream.send({"n": 1})
    second = await stream.send({"n": 2})
    third = await stream.send({"n": 3})

    assert first is True
    assert second is True
    assert third is True
    # Only the first send (when connected) queued anything.
    assert stream._queue.qsize() == 1


@pytest.mark.asyncio
async def test_is_disconnected_latches_after_first_positive() -> None:
    """Avoid hammering the ASGI receive channel after the response is torn down.

    The underlying Request.is_disconnected reads from the receive channel,
    which may not be live after the ASGI task group has exited. Once we
    observe disconnect once, cache it so later calls don't hit the channel.
    """
    request = MagicMock()
    # If we didn't cache, the second call would raise.
    request.is_disconnected = AsyncMock(side_effect=[True, RuntimeError("channel closed")])
    stream = FastAPIEventSourceStream(request)

    assert await stream.is_disconnected() is True
    assert await stream.is_disconnected() is True
    # Only one underlying call thanks to caching.
    assert request.is_disconnected.await_count == 1


@pytest.mark.asyncio
async def test_is_disconnected_treats_exception_as_disconnect() -> None:
    """If checking the ASGI receive channel fails, assume the client is gone.

    This protects handlers that keep running after the response has been
    torn down: they still call is_disconnected periodically and must not
    crash on a stale receive channel.
    """
    request = MagicMock()
    request.is_disconnected = AsyncMock(side_effect=RuntimeError("closed"))
    stream = FastAPIEventSourceStream(request)

    assert await stream.is_disconnected() is True


@pytest.mark.asyncio
async def test_handler_runs_to_completion_after_sse_generator_exits() -> None:
    """SKY-8986 regression: handler must NOT be cancelled on client disconnect.

    Simulates a client that disconnects immediately (generator receives no
    events before exiting). The handler should keep running in the
    background and finish its work. This is the bug fix: the previous
    implementation cancelled the handler task in the generator's finally
    block, killing an in-flight copilot agent.
    """
    request = MagicMock()
    request.is_disconnected = AsyncMock(return_value=True)

    handler_finished = asyncio.Event()

    async def handler(stream: Any) -> None:
        # Simulate agent work that takes some time and runs past the
        # moment the SSE generator decides the client is gone.
        await asyncio.sleep(0.01)
        await stream.send({"progress": "halfway"})
        await asyncio.sleep(0.01)
        await stream.send({"progress": "done"})
        handler_finished.set()

    response = FastAPIEventSourceStream.create(request, handler)

    # The EventSourceResponse body iterator should close immediately since
    # the client is already disconnected, but the handler keeps running in
    # the background. We only care that the handler eventually finishes.
    async for _ in response.body_iterator:  # drain (should be empty)
        pass

    await asyncio.wait_for(handler_finished.wait(), timeout=2.0)
    # One task can remain in the registry briefly while its done callback
    # fires; yield once so the callback runs.
    await asyncio.sleep(0)
    # The registry is cleaned up once the task is fully done.
    assert not any(not t.done() for t in _BACKGROUND_HANDLER_TASKS)


@pytest.mark.asyncio
async def test_handler_exception_does_not_break_other_streams() -> None:
    """An error inside a handler after disconnect must not crash the process.

    The handler runs as a background task after disconnect; without the
    catch-and-log inside _run_handler an unhandled exception would surface
    only as an asyncio warning at GC time.
    """
    request = MagicMock()
    request.is_disconnected = AsyncMock(return_value=True)

    async def handler(stream: Any) -> None:
        raise RuntimeError("boom")

    response = FastAPIEventSourceStream.create(request, handler)
    async for _ in response.body_iterator:
        pass

    # Allow the handler task to complete and its done-callback to fire.
    await asyncio.sleep(0.05)
    # No exception propagated out of the ASGI response iteration.
    # Background tasks are cleaned from the registry on completion.
    assert not any(not t.done() for t in _BACKGROUND_HANDLER_TASKS)
