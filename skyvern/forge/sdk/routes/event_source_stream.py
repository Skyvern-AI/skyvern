import asyncio
from typing import Any, Awaitable, Callable, Protocol

from fastapi import Request
from pydantic import BaseModel
from sse_starlette import EventSourceResponse, JSONServerSentEvent, ServerSentEvent

DEFAULT_KEEPALIVE_INTERVAL_SECONDS = 10


class EventSourceStream(Protocol):
    """Protocol for Server-Sent Events (SSE) streams."""

    async def send(self, data: Any) -> bool:
        """
        Send data as an SSE event.

        Returns:
            True if the event was queued successfully, False if disconnected or closed.
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

    This class provides a cleaner interface for sending SSE updates from async functions
    instead of using yield-based generators directly.

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

    async def send(self, data: Any) -> bool:
        """
        Send data as an SSE event. Accepts Pydantic models or dicts.

        Returns:
            True if the event was queued successfully, False if disconnected or closed.
        """
        if self._closed or await self.is_disconnected():
            return False
        await self._queue.put(data)
        return True

    async def is_disconnected(self) -> bool:
        """Check if the client has disconnected."""
        return await self._request.is_disconnected()

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

        async def event_generator() -> Any:
            task = asyncio.create_task(cls._run_handler(stream, handler))
            try:
                async for event in stream._generate():
                    yield event
            finally:
                if not task.done():
                    task.cancel()
                    try:
                        await task
                    except asyncio.CancelledError:
                        pass

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
        finally:
            await stream.close()
