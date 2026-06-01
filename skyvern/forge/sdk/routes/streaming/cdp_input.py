"""
CDP input channel for interactive browser control via Chrome DevTools Protocol.
"""

import asyncio
import dataclasses
import json
import time
import typing as t

import structlog
from fastapi import WebSocket, WebSocketDisconnect
from playwright.async_api import CDPSession
from websockets.exceptions import ConnectionClosedError, ConnectionClosedOK

from skyvern.forge import app
from skyvern.forge.sdk.routes.routers import base_router, legacy_base_router
from skyvern.forge.sdk.routes.streaming.auth import auth, require_client_id
from skyvern.forge.sdk.routes.streaming.registries import (
    add_cdp_input_channel,
    del_cdp_input_channel,
    stream_ref_dec,
    stream_ref_inc,
)
from skyvern.forge.sdk.routes.streaming.screencast import wait_for_browser_state
from skyvern.forge.sdk.schemas.persistent_browser_sessions import is_final_status
from skyvern.forge.sdk.workflow.models.workflow import WorkflowRunStatus
from skyvern.webeye.browser_state import BrowserState

LOG = structlog.get_logger()

_VALID_MOUSE_TYPES = {"mousePressed", "mouseReleased", "mouseMoved"}
_VALID_MOUSE_BUTTONS = {"left", "middle", "right", "none"}
_VALID_KEY_TYPES = {"keyDown", "keyUp", "rawKeyDown"}
_MAX_COORD = 10000
_MAX_DELTA = 10000
_MAX_KEY_LEN = 32
_MAX_CODE_LEN = 32
_MODIFIER_MASK = 0xF
_MAX_VK_CODE = 0xFE
ACTIVE_PAGE_INPUT_REFRESH_INTERVAL = 0.5


@dataclasses.dataclass
class CdpInputChannel:
    client_id: str
    organization_id: str
    websocket: WebSocket
    interactor: t.Literal["agent", "user"] = "agent"

    def __post_init__(self) -> None:
        add_cdp_input_channel(self)

    async def close(self) -> None:
        del_cdp_input_channel(self.client_id)


class ActivePageCdpInputSession:
    def __init__(
        self,
        browser_state: BrowserState,
        log_id_key: str,
        log_id_value: str,
        refresh_interval: float = ACTIVE_PAGE_INPUT_REFRESH_INTERVAL,
    ) -> None:
        self.browser_state = browser_state
        self.log_id_key = log_id_key
        self.log_id_value = log_id_value
        self.refresh_interval = refresh_interval
        self.cdp_session: CDPSession | None = None
        self.page: object | None = None
        self.next_refresh_at = 0.0
        self.page_resolution_failed = False

    async def get_session(self, *, force_refresh: bool = False) -> CDPSession | None:
        now = time.monotonic()
        if not force_refresh and now < self.next_refresh_at:
            return None if self.page_resolution_failed else self.cdp_session

        page = await self.browser_state.get_working_page()
        if page is None:
            self.page_resolution_failed = True
            self.next_refresh_at = now + self.refresh_interval
            return None

        self.page_resolution_failed = False
        self.next_refresh_at = now + self.refresh_interval
        if self.cdp_session is not None and page is self.page:
            return self.cdp_session

        await self.close()
        session = await page.context.new_cdp_session(page)  # type: ignore[attr-defined]
        self.cdp_session = session
        self.page = page
        LOG.info(
            "CDP input rebound to active page",
            **{self.log_id_key: self.log_id_value},
            url=getattr(page, "url", ""),
        )
        return session

    async def close(self) -> None:
        if self.cdp_session is None:
            self.page = None
            return
        session = self.cdp_session
        self.cdp_session = None
        self.page = None
        try:
            await session.detach()
        except Exception:
            pass


def _validated_modifiers(msg: dict) -> int:
    modifiers = msg.get("modifiers", 0)
    if not isinstance(modifiers, int):
        return 0
    return modifiers & _MODIFIER_MASK


def _validated_coords(msg: dict) -> tuple[int, int] | None:
    x = msg.get("x")
    y = msg.get("y")
    if not isinstance(x, (int, float)) or not isinstance(y, (int, float)):
        return None
    return (
        max(0, min(int(x), _MAX_COORD)),
        max(0, min(int(y), _MAX_COORD)),
    )


def _validate_mouse_event(msg: dict) -> dict | None:
    event_type = msg.get("eventType")
    if event_type not in _VALID_MOUSE_TYPES:
        return None

    coords = _validated_coords(msg)
    if coords is None:
        return None
    x, y = coords

    button = msg.get("button", "none")
    if button not in _VALID_MOUSE_BUTTONS:
        button = "none"

    click_count = msg.get("clickCount", 0)
    if not isinstance(click_count, int):
        click_count = 0
    click_count = max(0, min(click_count, 3))

    return {
        "type": event_type,
        "x": x,
        "y": y,
        "button": button,
        "clickCount": click_count,
        "modifiers": _validated_modifiers(msg),
    }


def _validate_key_event(msg: dict) -> dict | None:
    event_type = msg.get("eventType")
    if event_type not in _VALID_KEY_TYPES:
        return None

    key = msg.get("key", "")
    if not isinstance(key, str) or len(key) > _MAX_KEY_LEN:
        return None

    code = msg.get("code", "")
    if not isinstance(code, str) or len(code) > _MAX_CODE_LEN:
        return None

    result: dict[str, t.Any] = {
        "type": event_type,
        "key": key,
        "code": code,
        "modifiers": _validated_modifiers(msg),
    }

    # Only include text for printable single characters on keyDown
    text = msg.get("text", "")
    if isinstance(text, str) and len(text) == 1 and text.isprintable() and event_type == "keyDown":
        result["text"] = text

    # Forward `windowsVirtualKeyCode` so CDP can resolve non-printable keys
    # (Backspace, Enter, Arrow*, etc.) to actual editing actions.
    vk = msg.get("windowsVirtualKeyCode")
    if isinstance(vk, int) and 0 <= vk <= _MAX_VK_CODE:
        result["windowsVirtualKeyCode"] = vk

    return result


def _validate_wheel_event(msg: dict) -> dict | None:
    coords = _validated_coords(msg)
    if coords is None:
        return None
    x, y = coords

    delta_x = msg.get("deltaX", 0)
    delta_y = msg.get("deltaY", 0)
    if not isinstance(delta_x, (int, float)):
        delta_x = 0
    if not isinstance(delta_y, (int, float)):
        delta_y = 0
    delta_x = max(-_MAX_DELTA, min(int(delta_x), _MAX_DELTA))
    delta_y = max(-_MAX_DELTA, min(int(delta_y), _MAX_DELTA))

    return {
        "type": "mouseWheel",
        "x": x,
        "y": y,
        "deltaX": delta_x,
        "deltaY": delta_y,
        "modifiers": _validated_modifiers(msg),
    }


async def _close_ws_safely(websocket: WebSocket, code: int, reason: str = "") -> None:
    try:
        await websocket.close(code=code, reason=reason)
    except Exception:
        pass


_EVENT_DISPATCH_MAP: dict[str, tuple[t.Callable[[dict], dict | None], str]] = {
    "mouseEvent": (_validate_mouse_event, "Input.dispatchMouseEvent"),
    "keyEvent": (_validate_key_event, "Input.dispatchKeyEvent"),
    "wheelEvent": (_validate_wheel_event, "Input.dispatchMouseEvent"),
}


async def _dispatch_event(
    cdp_session: CDPSession,
    kind: str,
    msg: dict,
    log_id_key: str,
    log_id_value: str,
) -> None:
    entry = _EVENT_DISPATCH_MAP.get(kind)
    if entry is None:
        return
    validator, cdp_method = entry
    validated = validator(msg)
    if validated:
        await cdp_session.send(cdp_method, validated)
    else:
        LOG.warning(
            "CDP input: validation failed",
            **{log_id_key: log_id_value},
            kind=kind,
            raw_event_type=msg.get("eventType"),
        )


async def _run_input_loop(
    websocket: WebSocket,
    channel: CdpInputChannel,
    input_session: ActivePageCdpInputSession,
    log_id_key: str,
    log_id_value: str,
) -> None:
    dropped_log_count = 0
    no_active_page_log_count = 0
    while True:
        try:
            raw = await websocket.receive_text()
        except WebSocketDisconnect:
            break

        try:
            msg = json.loads(raw)
        except json.JSONDecodeError:
            LOG.warning("CDP input: malformed JSON", **{log_id_key: log_id_value})
            continue

        kind = msg.get("kind") or msg.get("type")

        if kind == "take-control":
            channel.interactor = "user"
            LOG.info("CDP input: take-control received", **{log_id_key: log_id_value}, client_id=channel.client_id)
            continue
        if kind == "cede-control":
            channel.interactor = "agent"
            LOG.info("CDP input: cede-control received", **{log_id_key: log_id_value}, client_id=channel.client_id)
            continue

        if channel.interactor != "user":
            if dropped_log_count < 5:
                LOG.info(
                    "CDP input: event dropped",
                    interactor=channel.interactor,
                    **{log_id_key: log_id_value},
                    kind=kind,
                )
                dropped_log_count += 1
            continue

        try:
            cdp_session = await input_session.get_session()
        except Exception:
            LOG.warning(
                "CDP input: failed to resolve active page; closing input channel",
                **{log_id_key: log_id_value},
                kind=kind,
                exc_info=True,
            )
            await websocket.close(code=4411, reason="active_page_resolution_failed")
            break

        if cdp_session is None:
            if no_active_page_log_count < 5:
                LOG.info("CDP input: no active page; event skipped", **{log_id_key: log_id_value}, kind=kind)
                no_active_page_log_count += 1
            continue

        try:
            await _dispatch_event(cdp_session, kind, msg, log_id_key, log_id_value)
        except Exception:
            LOG.warning(
                "CDP input: failed to dispatch event; closing input channel",
                **{log_id_key: log_id_value},
                kind=kind,
                exc_info=True,
            )
            await websocket.close(code=4411, reason="dispatch_failed")
            break


@legacy_base_router.websocket("/stream/cdp_input/workflow_run/{workflow_run_id}")
async def cdp_input_stream(
    websocket: WebSocket,
    workflow_run_id: str,
    client_id: str | None = None,
    apikey: str | None = None,
    token: str | None = None,
) -> None:
    organization_id = await auth(apikey=apikey, token=token, websocket=websocket, workflow_run_id=workflow_run_id)
    if organization_id is None:
        return

    if not require_client_id(client_id, workflow_run_id=workflow_run_id):
        await _close_ws_safely(websocket, 1002)
        return
    assert client_id is not None

    channel = CdpInputChannel(
        client_id=client_id,
        organization_id=organization_id,
        websocket=websocket,
    )

    cdp_session: CDPSession | None = None
    input_session: ActivePageCdpInputSession | None = None
    try:
        deadline = time.monotonic() + 120
        while True:
            workflow_run = await app.DATABASE.workflow_runs.get_workflow_run(
                workflow_run_id=workflow_run_id,
                organization_id=organization_id,
            )
            if not workflow_run or workflow_run.organization_id != organization_id:
                LOG.info("CDP input: workflow run not found", workflow_run_id=workflow_run_id)
                await websocket.close(code=4404, reason="workflow_run_not_found")
                return
            if workflow_run.status == WorkflowRunStatus.running or workflow_run.status.is_final():
                break
            if workflow_run.status == WorkflowRunStatus.paused:
                break
            if time.monotonic() >= deadline:
                LOG.warning("CDP input: timed out waiting for running status", workflow_run_id=workflow_run_id)
                await websocket.close(code=4408, reason="wait_timeout")
                return
            await asyncio.sleep(1)

        browser_state = await wait_for_browser_state(workflow_run_id, "workflow_run")
        if browser_state is None:
            LOG.warning("CDP input: timed out waiting for browser state", workflow_run_id=workflow_run_id)
            await websocket.close(code=4408, reason="browser_state_timeout")
            return

        page = await browser_state.get_working_page()
        if page is None:
            LOG.warning("CDP input: no working page", workflow_run_id=workflow_run_id)
            await websocket.close(code=4410, reason="no_working_page")
            return

        input_session = ActivePageCdpInputSession(browser_state, "workflow_run_id", workflow_run_id)
        cdp_session = await input_session.get_session(force_refresh=True)
        if cdp_session is None:
            LOG.warning("CDP input: no working page", workflow_run_id=workflow_run_id)
            await websocket.close(code=4410, reason="no_working_page")
            return
        stream_ref_inc(workflow_run_id)

        LOG.info("CDP input channel ready", workflow_run_id=workflow_run_id, client_id=client_id)
        await websocket.send_json({"kind": "ready"})

        await _run_input_loop(websocket, channel, input_session, "workflow_run_id", workflow_run_id)

    except ConnectionClosedOK:
        LOG.info("CDP input: WS closed cleanly", workflow_run_id=workflow_run_id)
    except ConnectionClosedError:
        LOG.warning("CDP input: WS connection error", workflow_run_id=workflow_run_id)
    except WebSocketDisconnect:
        LOG.info("CDP input: WS disconnected", workflow_run_id=workflow_run_id)
    except Exception:
        LOG.warning("CDP input: unexpected error", workflow_run_id=workflow_run_id, exc_info=True)
    finally:
        if cdp_session is not None:
            await stream_ref_dec(workflow_run_id)
        if input_session is not None:
            await input_session.close()
        await channel.close()
        LOG.info("CDP input channel closed", workflow_run_id=workflow_run_id, client_id=client_id)


@base_router.websocket("/stream/cdp_input/browser_session/{browser_session_id}")
async def cdp_input_browser_session_stream(
    websocket: WebSocket,
    browser_session_id: str,
    client_id: str | None = None,
    apikey: str | None = None,
    token: str | None = None,
) -> None:
    organization_id = await auth(apikey=apikey, token=token, websocket=websocket, browser_session_id=browser_session_id)
    if organization_id is None:
        return

    if not require_client_id(client_id, browser_session_id=browser_session_id):
        await _close_ws_safely(websocket, 1002)
        return
    assert client_id is not None

    channel = CdpInputChannel(
        client_id=client_id,
        organization_id=organization_id,
        websocket=websocket,
    )

    input_session: ActivePageCdpInputSession | None = None
    try:
        session = await app.PERSISTENT_SESSIONS_MANAGER.get_session(
            session_id=browser_session_id,
            organization_id=organization_id,
        )
        if not session:
            LOG.info("CDP input: browser session not found", browser_session_id=browser_session_id)
            await websocket.close(code=4404, reason="browser_session_not_found")
            return
        if is_final_status(session.status):
            LOG.info("CDP input: browser session already finalized", browser_session_id=browser_session_id)
            await websocket.close(code=4404, reason="browser_session_finalized")
            return

        browser_state = await wait_for_browser_state(
            browser_session_id,
            "browser_session",
            organization_id=organization_id,
        )
        if browser_state is None:
            LOG.warning("CDP input: timed out waiting for browser state", browser_session_id=browser_session_id)
            await websocket.close(code=4408, reason="browser_state_timeout")
            return

        page = await browser_state.get_working_page()
        if page is None:
            LOG.warning("CDP input: no working page", browser_session_id=browser_session_id)
            await websocket.close(code=4410, reason="no_working_page")
            return

        input_session = ActivePageCdpInputSession(browser_state, "browser_session_id", browser_session_id)
        if await input_session.get_session(force_refresh=True) is None:
            LOG.warning("CDP input: no working page", browser_session_id=browser_session_id)
            await websocket.close(code=4410, reason="no_working_page")
            return

        LOG.info("CDP input channel ready", browser_session_id=browser_session_id, client_id=client_id)
        await websocket.send_json({"kind": "ready"})

        await _run_input_loop(websocket, channel, input_session, "browser_session_id", browser_session_id)

    except ConnectionClosedOK:
        LOG.info("CDP input: WS closed cleanly", browser_session_id=browser_session_id)
    except ConnectionClosedError:
        LOG.warning("CDP input: WS connection error", browser_session_id=browser_session_id)
    except WebSocketDisconnect:
        LOG.info("CDP input: WS disconnected", browser_session_id=browser_session_id)
    except Exception:
        LOG.warning("CDP input: unexpected error", browser_session_id=browser_session_id, exc_info=True)
    finally:
        if input_session is not None:
            await input_session.close()
        await channel.close()
        LOG.info("CDP input channel closed", browser_session_id=browser_session_id, client_id=client_id)
