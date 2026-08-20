"""
CDP screencast loop for local-mode browser streaming.

Uses Chrome's Page.startScreencast() to stream JPEG frames from the browser
over a WebSocket connection.
"""

import asyncio
import time
from collections.abc import Awaitable, Callable

import structlog
from fastapi import WebSocket
from opentelemetry import metrics
from playwright.async_api import CDPSession

from skyvern.forge import app
from skyvern.webeye.browser_state import BrowserState

LOG = structlog.get_logger()

DEFAULT_WIDTH = 1280
DEFAULT_HEIGHT = 720
ACTIVE_PAGE_POLL_INTERVAL = 0.5
LATENCY_BUCKETS_SECONDS: tuple[float, ...] = (
    0.001,
    0.002,
    0.005,
    0.01,
    0.02,
    0.03,
    0.045,
    0.06,
    0.1,
    0.25,
    0.5,
    1.0,
    2.5,
    5.0,
)

_meter = metrics.get_meter("skyvern.live_view")
_frame_forward_seconds = _meter.create_histogram(
    "skyvern.live_view.frame_forward_seconds",
    unit="s",
    description="Screencast frame: CDP event received by the API -> client websocket send returned",
    explicit_bucket_boundaries_advisory=list(LATENCY_BUCKETS_SECONDS),
)
_frame_queue_seconds = _meter.create_histogram(
    "skyvern.live_view.frame_queue_seconds",
    unit="s",
    description="Screencast frame queue dwell until forwarding or eviction",
    explicit_bucket_boundaries_advisory=list(LATENCY_BUCKETS_SECONDS),
)
_frame_send_seconds = _meter.create_histogram(
    "skyvern.live_view.frame_send_seconds",
    unit="s",
    description="Screencast frame: websocket.send_json duration",
    explicit_bucket_boundaries_advisory=list(LATENCY_BUCKETS_SECONDS),
)
_frames_evicted = _meter.create_counter(
    "skyvern.live_view.frames_evicted",
    unit="{frame}",
    description="Screencast frames dropped from the forwarding queue because the client was behind",
)


async def wait_for_browser_state(
    entity_id: str,
    entity_type: str,
    workflow_run_id: str | None = None,
    organization_id: str | None = None,
    timeout: float = 120,
    poll_interval: float = 0.25,
) -> BrowserState | None:
    elapsed = 0.0
    observed: BrowserState | None = None
    try:
        while elapsed < timeout:
            browser_state = observed or await _resolve_browser_state(
                entity_id,
                entity_type,
                workflow_run_id,
                organization_id=organization_id,
            )

            if browser_state is not None:
                # An observer connection belongs to this websocket, so it is held across polls
                # rather than re-dialed while the session's first page comes up.
                if entity_type == "browser_session":
                    observed = browser_state
                page = await browser_state.get_working_page()
                if page is not None:
                    # Ownership passes to the caller, who releases it when the websocket ends.
                    observed = None
                    return browser_state

            await asyncio.sleep(poll_interval)
            elapsed += poll_interval

        return None
    finally:
        # Whatever is still held here was never handed to a caller, so nobody else will give it back.
        await release_browser_state(observed, entity_type, entity_id)


async def release_browser_state(browser_state: BrowserState | None, entity_type: str, entity_id: str) -> None:
    """Give back whatever watching adopted. Only the manager knows whether that is anything."""
    if browser_state is None or entity_type != "browser_session":
        return
    try:
        await app.PERSISTENT_SESSIONS_MANAGER.release_observer_browser_state(entity_id, browser_state)
    except Exception:
        LOG.warning("Could not release the live-view browser connection", browser_session_id=entity_id, exc_info=True)


async def _resolve_browser_state(
    entity_id: str,
    entity_type: str,
    workflow_run_id: str | None = None,
    organization_id: str | None = None,
) -> BrowserState | None:
    if entity_type == "workflow_run":
        return app.BROWSER_MANAGER.get_for_workflow_run(entity_id)
    if entity_type == "task":
        return app.BROWSER_MANAGER.get_for_task(entity_id, workflow_run_id)
    if entity_type == "browser_session":
        return await app.PERSISTENT_SESSIONS_MANAGER.get_observer_browser_state(entity_id, organization_id)
    return None


async def _resolve_working_page(
    browser_state: BrowserState,
    entity_id: str,
    entity_type: str,
    workflow_run_id: str | None = None,
    organization_id: str | None = None,
    fall_back_to_captured: bool = True,
) -> object | None:
    # The viewer's own connection outlives every page the session opens, so following the active page
    # needs no re-resolution here — and re-resolving would dial a second connection every poll.
    if entity_type == "browser_session":
        try:
            return await browser_state.get_working_page()
        except Exception:
            return None
    # Re-resolve each poll so the screencast follows a BrowserState the run swaps in post-connect (skyvern#6703).
    try:
        state = await _resolve_browser_state(entity_id, entity_type, workflow_run_id, organization_id)
    except Exception:
        state = None
    if state is not None:
        try:
            page = await state.get_working_page()
        except Exception:
            page = None
        if page is not None:
            return page
    # Captured fallback (validated by wait_for_browser_state) only before the first rebind; afterwards a transient
    # failure returns None so the caller keeps the live page instead of downgrading to the initial about:blank.
    if not fall_back_to_captured:
        return None
    try:
        return await browser_state.get_working_page()
    except Exception:
        return None


async def start_screencast_loop(
    websocket: WebSocket,
    browser_state: BrowserState,
    entity_id: str,
    entity_type: str,
    check_finalized: Callable[[], Awaitable[bool]],
    workflow_run_id: str | None = None,
    organization_id: str | None = None,
) -> None:
    id_key = f"{entity_type}_id"
    metric_attributes = {"entity_type": entity_type}
    cdp_session: CDPSession | None = None
    attached_page: object | None = None
    frame_queue: asyncio.Queue[tuple[str, float]] = asyncio.Queue(maxsize=2)
    viewport_info: dict[str, int] = {"width": DEFAULT_WIDTH, "height": DEFAULT_HEIGHT}

    async def _ack_frame(session: CDPSession, session_id: int) -> None:
        try:
            await session.send("Page.screencastFrameAck", {"sessionId": session_id})
        except Exception:
            pass

    def _update_viewport_from_page(page: object) -> None:
        viewport_size = getattr(page, "viewport_size", None)
        if not isinstance(viewport_size, dict):
            return
        width = viewport_size.get("width")
        height = viewport_size.get("height")
        if isinstance(width, (int, float)) and width > 0:
            viewport_info["width"] = int(width)
        if isinstance(height, (int, float)) and height > 0:
            viewport_info["height"] = int(height)

    def _update_viewport_from_metadata(metadata: dict) -> None:
        device_width = metadata.get("deviceWidth")
        device_height = metadata.get("deviceHeight")
        if isinstance(device_width, (int, float)) and device_width > 0:
            viewport_info["width"] = int(device_width)
        if isinstance(device_height, (int, float)) and device_height > 0:
            viewport_info["height"] = int(device_height)

    def _queue_frame(data: str, received_at: float) -> None:
        if not data:
            return
        if frame_queue.full():
            try:
                _, evicted_received_at = frame_queue.get_nowait()
                _frame_queue_seconds.record(time.monotonic() - evicted_received_at, metric_attributes)
                _frames_evicted.add(1, metric_attributes)
            except asyncio.QueueEmpty:
                pass
        try:
            frame_queue.put_nowait((data, received_at))
        except asyncio.QueueFull:
            pass

    def _drain_frame_queue() -> None:
        while True:
            try:
                frame_queue.get_nowait()
            except asyncio.QueueEmpty:
                break

    async def _on_frame(session: CDPSession, params: dict, received_at: float) -> None:
        if session is not cdp_session:
            return
        data = params.get("data", "")
        session_id = params.get("sessionId", 0)
        metadata = params.get("metadata", {})
        if metadata:
            _update_viewport_from_metadata(metadata)
        asyncio.create_task(_ack_frame(session, session_id))
        _queue_frame(data, received_at)

    async def _stop_current_screencast() -> None:
        nonlocal cdp_session, attached_page
        if cdp_session is None:
            attached_page = None
            return
        session = cdp_session
        cdp_session = None
        attached_page = None
        try:
            await session.send("Page.stopScreencast", {})
        except Exception:
            pass
        try:
            await session.detach()
        except Exception:
            pass

    async def _prime_current_frame(session: CDPSession, page: object) -> None:
        try:
            result = await session.send(
                "Page.captureScreenshot",
                {
                    "format": "jpeg",
                    "quality": 60,
                    "captureBeyondViewport": False,
                },
            )
            data = result.get("data", "") if isinstance(result, dict) else ""
            _update_viewport_from_page(page)
            _queue_frame(data, time.monotonic())
        except Exception:
            LOG.debug(
                "Could not prime CDP screencast frame",
                entity_id=entity_id,
                entity_type=entity_type,
                exc_info=True,
            )

    async def _attach_to_page(page: object) -> None:
        nonlocal cdp_session, attached_page
        if page is attached_page and cdp_session is not None:
            return

        await _stop_current_screencast()
        _drain_frame_queue()
        next_session = await page.context.new_cdp_session(page)  # type: ignore[attr-defined]
        cdp_session = next_session
        next_session.on(
            "Page.screencastFrame",
            lambda params: asyncio.create_task(_on_frame(next_session, params, time.monotonic())),
        )
        try:
            await next_session.send(
                "Page.startScreencast",
                {
                    "format": "jpeg",
                    "quality": 60,
                    "maxWidth": DEFAULT_WIDTH,
                    "maxHeight": DEFAULT_HEIGHT,
                },
            )
        except (asyncio.CancelledError, Exception):
            await _stop_current_screencast()
            raise
        attached_page = page
        await _prime_current_frame(next_session, page)
        LOG.info(
            "CDP screencast started",
            entity_id=entity_id,
            entity_type=entity_type,
            url=getattr(page, "url", ""),
        )

    async def _frame_forwarding_loop() -> None:
        while True:
            data, received_at = await frame_queue.get()
            dequeued_at = time.monotonic()
            _frame_queue_seconds.record(dequeued_at - received_at, metric_attributes)
            current_url = ""
            if attached_page is not None:
                try:
                    current_url = getattr(attached_page, "url", "") or ""
                except Exception:
                    pass
            try:
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
            except Exception:
                break
            sent_at = time.monotonic()
            _frame_send_seconds.record(sent_at - dequeued_at, metric_attributes)
            _frame_forward_seconds.record(sent_at - received_at, metric_attributes)

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

    async def _active_page_monitor_loop() -> None:
        while True:
            await asyncio.sleep(ACTIVE_PAGE_POLL_INTERVAL)
            try:
                page = await _resolve_working_page(
                    browser_state,
                    entity_id,
                    entity_type,
                    workflow_run_id,
                    organization_id,
                    fall_back_to_captured=False,
                )
                if page is not None and page is not attached_page:
                    await _attach_to_page(page)
            except Exception:
                LOG.debug(
                    "Could not refresh CDP screencast active page",
                    entity_id=entity_id,
                    entity_type=entity_type,
                    exc_info=True,
                )

    try:
        page = await _resolve_working_page(browser_state, entity_id, entity_type, workflow_run_id, organization_id)
        if page is None:
            raise RuntimeError("No working page available for screencast")

        await _attach_to_page(page)

        forward_task = asyncio.create_task(_frame_forwarding_loop())
        poll_task = asyncio.create_task(_completion_polling_loop())
        page_monitor_task = asyncio.create_task(_active_page_monitor_loop())

        done, pending = await asyncio.wait(
            [forward_task, poll_task, page_monitor_task],
            return_when=asyncio.FIRST_COMPLETED,
        )
        for task in pending:
            task.cancel()
            try:
                await task
            except (asyncio.CancelledError, Exception):
                pass

        for task in done:
            exc = task.exception()
            if task is not poll_task and exc is not None:
                raise exc

    except Exception:
        LOG.info(
            "Screencast loop ended",
            entity_id=entity_id,
            entity_type=entity_type,
            exc_info=True,
        )
    finally:
        await _stop_current_screencast()
        LOG.info("CDP screencast cleaned up", entity_id=entity_id, entity_type=entity_type)
