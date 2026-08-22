"""Make a client disconnect observable to a send-only streaming websocket."""

from __future__ import annotations

import asyncio

from fastapi import WebSocket


async def _wait_for_client_disconnect(websocket: WebSocket) -> None:
    while True:
        try:
            message = await websocket.receive()
        except Exception:
            # A receive that cannot complete means the socket is unusable, which is the same signal.
            return
        if message.get("type") == "websocket.disconnect":
            return


def watch_for_client_disconnect(websocket: WebSocket) -> asyncio.Task[None]:
    """Task that completes once the client is gone. Callers must cancel it when they finish.

    A handler that only sends never reads its socket, so Starlette never processes the
    ``websocket.disconnect`` message ASGI already delivered: ``client_state`` stays CONNECTED and
    ``send`` keeps writing into a transport the peer dropped, which asyncio warns about once per
    write. Draining the receive side is what turns that into an observable event.
    """
    return asyncio.create_task(_wait_for_client_disconnect(websocket))
