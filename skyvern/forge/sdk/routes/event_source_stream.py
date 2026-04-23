import asyncio
from typing import Any, Awaitable, Callable, Protocol

import structlog
from fastapi import Request
from pydantic import BaseModel
from sse_starlette import EventSourceResponse, JSONServerSentEvent, ServerSentEvent

LOG = structlog.get_logger()

DEFAULT_KEEPALIVE_INTERVAL_SECONDS = 10

# Strong references to handler tasks that outlive their SSE response. When a
# client disconnects mid-stream, we let the handler keep running so any
# in-flight work (agent loops, DB persistence) finishes cleanly. Without a
# strong reference, the event loop could garbage-collect the task while it
# is still running.
_BACKGROUND_HANDLER_TASKS: set["asyncio.Task[None]"] = set()


class EventSourceStream(Protocol):
    """Protocol for Server-Sent Events (SSE) streams."""

    async def send(self, data: Any) -> bool:
        """
        Send data as an SSE event.

        Returns:
            True if the event was accepted (queued for delivery or dropped
            because the client is gone). False only if the stream has been
            explicitly closed. Callers should treat a False return as a
            terminal state and stop emitting; they should NOT treat a
            client disconnect as a reason to abort in-flight work.
        """
        ...

    async def is_disconnected(self) -> bool:
        """Check if the client has disconnected."""
        ...

    async def close(self) -> None:
        """Signal that the stream is complete."""
        ...


class FastAPIEventSourceStream:
    """
    FastAPI implementation of EventSourceStream.

    Sending is decoupled from client presence. When the client disconnects,
    the handler task keeps running (so agent loops and DB persistence
    complete) and subsequent send() calls silently drop their payload
    instead of growing the queue. The generator stops yielding and
    sse-starlette closes the HTTP response; the handler runs out in the
    background.

    Usage:
        @app.post("/stream")
        async def my_endpoint(request: Request) -> EventSourceResponse:
            async def handler(stream: EventSourceStream) -> None:
                await stream.send(MyUpdateModel(status="Processing..."))
                result = await do_work()
                await stream.send({"status": "Done", "result": result})

            return FastAPIEventSourceStream.create(request, handler)
    """

    def __init__(self, request: Request) -> None:
        self._request = request
        self._queue: asyncio.Queue[Any] = asyncio.Queue()
        self._closed = False
        # Latches once the client goes away so repeated is_disconnected()
        # calls don't hit the ASGI receive channel after the response has
        # been torn down.
        self._client_gone = False

    async def send(self, data: Any) -> bool:
        """Send data as an SSE event. Accepts Pydantic models or dicts.

        When the client is still connected, the event is queued for
        delivery. When the client has disconnected, the event is dropped
        silently so the handler can continue to completion without
        growing memory. Returns False only if the stream has been
        explicitly closed.
        """
        if self._closed:
            return False
        if await self.is_disconnected():
            return True
        await self._queue.put(data)
        return True

    async def is_disconnected(self) -> bool:
        """Check if the client has disconnected.

        Caches the first positive result so callers made after the ASGI
        response has been torn down don't try to pull from a receive
        channel that may no longer be live.
        """
        if self._client_gone:
            return True
        try:
            disconnected = await self._request.is_disconnected()
        except Exception as exc:
            # Starlette's is_disconnected can raise various errors once
            # the ASGI receive channel is gone (RuntimeError on closed
            # queues, anyio/asyncio cancellation oddities, etc.). Treat
            # any such failure as "client gone" -- we're polling to
            # decide whether to skip emitting events, and if we can't
            # tell we'd rather stop emitting than spin. Log so a
            # genuinely new failure mode shows up in telemetry instead
            # of hiding behind the disconnect path.
            LOG.debug("is_disconnected raised; treating as disconnect", error=str(exc))
            disconnected = True
        if disconnected:
            self._client_gone = True
        return disconnected

    async def close(self) -> None:
        """Signal that the stream is complete."""
        self._closed = True
        await self._queue.put(None)

    def _serialize(self, data: Any) -> Any:
        """Serialize data to JSON-compatible format."""
        if isinstance(data, BaseModel):
            return data.model_dump(mode="json")
        return data

    async def _generate(self) -> Any:
        """Internal generator that yields SSE events from the queue."""
        while True:
            try:
                data = await self._queue.get()
                if data is None:
                    break
                if await self.is_disconnected():
                    break
                yield JSONServerSentEvent(data=self._serialize(data))
            except Exception:
                break

    @classmethod
    def create(
        cls,
        request: Request,
        handler: Callable[[EventSourceStream], Awaitable[None]],
        ping_interval: int = DEFAULT_KEEPALIVE_INTERVAL_SECONDS,
    ) -> EventSourceResponse:
        """
        Create an EventSourceResponse that runs the handler with an EventSourceStream.

        Args:
            request: The FastAPI request object
            handler: An async function that receives the stream and sends events
            ping_interval: Interval in seconds for keep-alive pings (default: 10)

        Returns:
            An EventSourceResponse that can be returned from a FastAPI endpoint
        """
        stream = cls(request)
        task = asyncio.create_task(cls._run_handler(stream, handler))
        # Hold a strong reference so the event loop can't GC the task if the
        # generator is torn down first (SSE client disconnect). The set is
        # bounded in practice by the handler's own timeout — every copilot
        # handler must eventually return (see TOTAL_TIMEOUT_SECONDS in
        # skyvern/forge/sdk/copilot/enforcement.py) which removes the task
        # from the set via the done_callback below. If you wire a new
        # EventSource endpoint through here, give its handler a similar
        # hard cap or this set becomes an unbounded leak.
        _BACKGROUND_HANDLER_TASKS.add(task)
        task.add_done_callback(_BACKGROUND_HANDLER_TASKS.discard)

        async def event_generator() -> Any:
            async for event in stream._generate():
                yield event
            # Intentionally do NOT cancel the handler task here. SSE
            # disconnect must not kill in-flight work (the copilot agent
            # often needs tens of seconds to finish and then persist its
            # reply to the chat history). The handler keeps running in
            # the background; stream.send() silently drops events once
            # the client is gone so the queue cannot grow unbounded.

        def ping_message_factory() -> ServerSentEvent:
            return ServerSentEvent(comment="keep-alive")

        return EventSourceResponse(
            event_generator(),
            ping=ping_interval,
            ping_message_factory=ping_message_factory,
        )

    @staticmethod
    async def _run_handler(
        stream: EventSourceStream,
        handler: Callable[[EventSourceStream], Awaitable[None]],
    ) -> None:
        """Run the handler and ensure the stream is closed when done."""
        try:
            await handler(stream)
        except asyncio.CancelledError:
            # Process/server shutdown — propagate. Client disconnect alone
            # no longer cancels this task (see create()).
            raise
        except Exception:
            LOG.exception("SSE handler failed")
        finally:
            await stream.close()
