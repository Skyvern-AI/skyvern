"""
CDP screencast loop for local-mode browser streaming.

Uses Chrome's Page.startScreencast() to stream JPEG frames from the browser
over a WebSocket connection.
"""

import asyncio
from collections.abc import Awaitable, Callable

import structlog
from fastapi import WebSocket
from playwright.async_api import CDPSession

from skyvern.forge import app
from skyvern.webeye.browser_state import BrowserState

LOG = structlog.get_logger()

DEFAULT_WIDTH = 1280
DEFAULT_HEIGHT = 720


async def wait_for_browser_state(
    entity_id: str,
    entity_type: str,
    workflow_run_id: str | None = None,
    timeout: float = 120,
    poll_interval: float = 1.0,
) -> BrowserState | None:
    """Poll BROWSER_MANAGER until a BrowserState with a working page is available."""
    elapsed = 0.0
    while elapsed < timeout:
        browser_state = await _resolve_browser_state(entity_id, entity_type, workflow_run_id)

        if browser_state is not None:
            page = await browser_state.get_working_page()
            if page is not None:
                return browser_state

        await asyncio.sleep(poll_interval)
        elapsed += poll_interval

    return None


async def _resolve_browser_state(
    entity_id: str,
    entity_type: str,
    workflow_run_id: str | None = None,
) -> BrowserState | None:
    if entity_type == "workflow_run":
        return app.BROWSER_MANAGER.get_for_workflow_run(entity_id)
    if entity_type == "task":
        return app.BROWSER_MANAGER.get_for_task(entity_id, workflow_run_id)
    if entity_type == "browser_session":
        return await app.PERSISTENT_SESSIONS_MANAGER.get_browser_state(entity_id)
    return None


async def start_screencast_loop(
    websocket: WebSocket,
    browser_state: BrowserState,
    entity_id: str,
    entity_type: str,
    check_finalized: Callable[[], Awaitable[bool]],
) -> None:
    """Stream CDP screencast frames over a WebSocket."""
    id_key = f"{entity_type}_id"
    cdp_session: CDPSession | None = None
    frame_queue: asyncio.Queue[str] = asyncio.Queue(maxsize=2)
    viewport_info: dict[str, int] = {"width": DEFAULT_WIDTH, "height": DEFAULT_HEIGHT}

    async def _ack_frame(session_id: int) -> None:
        if cdp_session is None:
            return
        try:
            await cdp_session.send("Page.screencastFrameAck", {"sessionId": session_id})
        except Exception:
            pass

    def _update_viewport_from_metadata(metadata: dict) -> None:
        device_width = metadata.get("deviceWidth")
        device_height = metadata.get("deviceHeight")
        if isinstance(device_width, (int, float)) and device_width > 0:
            viewport_info["width"] = int(device_width)
        if isinstance(device_height, (int, float)) and device_height > 0:
            viewport_info["height"] = int(device_height)

    async def _on_frame(params: dict) -> None:
        data = params.get("data", "")
        session_id = params.get("sessionId", 0)
        metadata = params.get("metadata", {})
        if metadata:
            _update_viewport_from_metadata(metadata)
        asyncio.create_task(_ack_frame(session_id))
        if not data:
            return
        # Drop oldest frame if queue is full to keep latency low
        if frame_queue.full():
            try:
                frame_queue.get_nowait()
            except asyncio.QueueEmpty:
                pass
        try:
            frame_queue.put_nowait(data)
        except asyncio.QueueFull:
            pass

    async def _frame_forwarding_loop() -> None:
        while True:
            data = await frame_queue.get()
            current_url = ""
            try:
                page = await browser_state.get_working_page()
                if page is not None:
                    current_url = page.url
            except Exception:
                pass
            await websocket.send_json(
                {
                    id_key: entity_id,
                    "status": "running",
                    "screenshot": data,
                    "format": "jpeg",
                    "viewport_width": viewport_info["width"],
                    "viewport_height": viewport_info["height"],
                    "url": current_url,
                }
            )

    async def _completion_polling_loop() -> None:
        while True:
            await asyncio.sleep(2)
            try:
                if await check_finalized():
                    return
            except Exception:
                LOG.warning(
                    "Error checking finalization status",
                    entity_id=entity_id,
                    entity_type=entity_type,
                    exc_info=True,
                )

    try:
        page = await browser_state.get_working_page()
        if page is None:
            raise RuntimeError("No working page available for screencast")

        cdp_session = await page.context.new_cdp_session(page)
        cdp_session.on("Page.screencastFrame", _on_frame)
        await cdp_session.send(
            "Page.startScreencast",
            {
                "format": "jpeg",
                "quality": 60,
                "maxWidth": DEFAULT_WIDTH,
                "maxHeight": DEFAULT_HEIGHT,
            },
        )
        LOG.info("CDP screencast started", entity_id=entity_id, entity_type=entity_type)

        forward_task = asyncio.create_task(_frame_forwarding_loop())
        poll_task = asyncio.create_task(_completion_polling_loop())

        done, pending = await asyncio.wait(
            [forward_task, poll_task],
            return_when=asyncio.FIRST_COMPLETED,
        )
        for task in pending:
            task.cancel()
            try:
                await task
            except (asyncio.CancelledError, Exception):
                pass

        # Re-raise forwarding errors (e.g. WebSocket disconnect)
        for task in done:
            exc = task.exception()
            if task is forward_task and exc is not None:
                raise exc

    except Exception:
        LOG.info(
            "Screencast loop ended",
            entity_id=entity_id,
            entity_type=entity_type,
            exc_info=True,
        )
    finally:
        if cdp_session is not None:
            try:
                await cdp_session.send("Page.stopScreencast", {})
            except Exception:
                pass
            try:
                await cdp_session.detach()
            except Exception:
                pass
        LOG.info("CDP screencast cleaned up", entity_id=entity_id, entity_type=entity_type)
