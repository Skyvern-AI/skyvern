"""
CDP input channel for interactive browser control via Chrome DevTools Protocol.

Accepts mouse, keyboard, and wheel events from the frontend and dispatches
them to the browser page via CDP's Input domain. Ownership gating ensures
only the "user" interactor can send input events; the "agent" interactor
(default) blocks all input.
"""

import asyncio
import dataclasses
import json
import typing as t

import structlog
from fastapi import WebSocket, WebSocketDisconnect
from playwright.async_api import CDPSession
from websockets.exceptions import ConnectionClosedError, ConnectionClosedOK

from skyvern.forge import app
from skyvern.forge.sdk.routes.routers import base_router, legacy_base_router
from skyvern.forge.sdk.routes.streaming.registries import (
    add_cdp_input_channel,
    del_cdp_input_channel,
    stream_ref_dec,
    stream_ref_inc,
)
from skyvern.forge.sdk.routes.streaming.screencast import wait_for_browser_state
from skyvern.forge.sdk.schemas.persistent_browser_sessions import is_final_status
from skyvern.forge.sdk.services.org_auth_service import get_current_org
from skyvern.forge.sdk.workflow.models.workflow import WorkflowRunStatus

LOG = structlog.get_logger()

_VALID_MOUSE_TYPES = {"mousePressed", "mouseReleased", "mouseMoved"}
_VALID_MOUSE_BUTTONS = {"left", "middle", "right", "none"}
_VALID_KEY_TYPES = {"keyDown", "keyUp"}
_MAX_COORD = 10000
_MAX_DELTA = 10000
_MAX_KEY_LEN = 32
_MAX_CODE_LEN = 32
_MODIFIER_MASK = 0xF


@dataclasses.dataclass
class CdpInputChannel:
    client_id: str
    organization_id: str
    websocket: WebSocket
    _interactor: t.Literal["agent", "user"] = "agent"

    def __post_init__(self) -> None:
        add_cdp_input_channel(self)

    @property
    def interactor(self) -> str:
        return self._interactor

    @interactor.setter
    def interactor(self, value: str) -> None:
        self._interactor = value

    async def close(self) -> None:
        del_cdp_input_channel(self.client_id)


def _validated_modifiers(msg: dict) -> int:
    modifiers = msg.get("modifiers", 0)
    if not isinstance(modifiers, int):
        return 0
    return modifiers & _MODIFIER_MASK


def _validate_mouse_event(msg: dict) -> dict | None:
    event_type = msg.get("eventType")
    if event_type not in _VALID_MOUSE_TYPES:
        return None

    x = msg.get("x")
    y = msg.get("y")
    if not isinstance(x, (int, float)) or not isinstance(y, (int, float)):
        return None

    x = max(0, min(int(x), _MAX_COORD))
    y = max(0, min(int(y), _MAX_COORD))

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

    return result


def _validate_wheel_event(msg: dict) -> dict | None:
    x = msg.get("x")
    y = msg.get("y")
    if not isinstance(x, (int, float)) or not isinstance(y, (int, float)):
        return None

    x = max(0, min(int(x), _MAX_COORD))
    y = max(0, min(int(y), _MAX_COORD))

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


# ---------------------------------------------------------------------------
# Shared helpers for both WebSocket endpoints
# ---------------------------------------------------------------------------


async def _accept_and_authenticate(
    websocket: WebSocket,
    apikey: str | None,
    token: str | None,
    log_id_key: str,
    log_id_value: str,
) -> str | None:
    """Accept the WebSocket and authenticate. Returns organization_id on success, None on failure."""
    try:
        await websocket.accept()
        if not token and not apikey:
            LOG.warning("CDP input: no credential provided", **{log_id_key: log_id_value})
            await websocket.close(code=1002)
            return None
    except ConnectionClosedOK:
        return None

    try:
        organization = await get_current_org(x_api_key=apikey, authorization=token)
        return organization.organization_id
    except Exception:
        LOG.warning("CDP input: auth failed", **{log_id_key: log_id_value}, exc_info=True)
        try:
            await websocket.close(code=1002)
        except Exception:
            pass
        return None


def _require_client_id(
    websocket: WebSocket,
    client_id: str | None,
    log_id_key: str,
    log_id_value: str,
) -> bool:
    """Log and close if client_id is missing. Returns True if client_id is valid."""
    if client_id:
        return True
    LOG.error("CDP input: no client_id provided", **{log_id_key: log_id_value})
    return False


async def _close_ws_safely(websocket: WebSocket, code: int, reason: str = "") -> None:
    try:
        await websocket.close(code=code, reason=reason)
    except Exception:
        pass


async def _verify_cdp_session(
    cdp_session: CDPSession,
    log_id_key: str,
    log_id_value: str,
    client_id: str,
) -> None:
    """Run a lightweight health check on a CDP session and log the result."""
    try:
        test_result = await cdp_session.send(
            "Runtime.evaluate",
            {"expression": "document.title + ' | ' + location.href"},
        )
        page_info = test_result.get("result", {}).get("value", "unknown")
        LOG.info(
            "CDP input: session verified",
            **{log_id_key: log_id_value},
            client_id=client_id,
            page_info=page_info,
        )
    except Exception:
        LOG.warning(
            "CDP input: session health check failed",
            **{log_id_key: log_id_value},
            exc_info=True,
        )


async def _dispatch_event(
    cdp_session: CDPSession,
    kind: str,
    msg: dict,
    log_id_key: str,
    log_id_value: str,
) -> None:
    """Validate and dispatch a single input event via CDP."""
    if kind == "mouseEvent":
        validated = _validate_mouse_event(msg)
        if validated:
            await cdp_session.send("Input.dispatchMouseEvent", validated)
        else:
            LOG.warning(
                "CDP input: mouse validation failed",
                **{log_id_key: log_id_value},
                raw_event_type=msg.get("eventType"),
            )
    elif kind == "keyEvent":
        validated = _validate_key_event(msg)
        if validated:
            await cdp_session.send("Input.dispatchKeyEvent", validated)
        else:
            LOG.warning(
                "CDP input: key validation failed",
                **{log_id_key: log_id_value},
                raw_event_type=msg.get("eventType"),
            )
    elif kind == "wheelEvent":
        validated = _validate_wheel_event(msg)
        if validated:
            await cdp_session.send("Input.dispatchMouseEvent", validated)
        else:
            LOG.warning(
                "CDP input: wheel validation failed",
                **{log_id_key: log_id_value},
            )


async def _run_input_loop(
    websocket: WebSocket,
    channel: CdpInputChannel,
    cdp_session: CDPSession,
    log_id_key: str,
    log_id_value: str,
) -> None:
    """Read messages from the WebSocket and dispatch input events via CDP."""
    dropped_log_count = 0
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

        # Control messages (always processed)
        if kind == "take-control":
            channel.interactor = "user"
            LOG.info("CDP input: take-control received", **{log_id_key: log_id_value}, client_id=channel.client_id)
            continue
        if kind == "cede-control":
            channel.interactor = "agent"
            LOG.info("CDP input: cede-control received", **{log_id_key: log_id_value}, client_id=channel.client_id)
            continue

        # Input events (gated behind ownership)
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


# ---------------------------------------------------------------------------
# WebSocket endpoints
# ---------------------------------------------------------------------------


@legacy_base_router.websocket("/stream/cdp_input/workflow_run/{workflow_run_id}")
async def cdp_input_stream(
    websocket: WebSocket,
    workflow_run_id: str,
    client_id: str | None = None,
    apikey: str | None = None,
    token: str | None = None,
) -> None:
    organization_id = await _accept_and_authenticate(
        websocket,
        apikey,
        token,
        "workflow_run_id",
        workflow_run_id,
    )
    if organization_id is None:
        return

    if not _require_client_id(websocket, client_id, "workflow_run_id", workflow_run_id):
        await _close_ws_safely(websocket, 1002)
        return

    channel = CdpInputChannel(
        client_id=client_id,
        organization_id=organization_id,
        websocket=websocket,
    )

    cdp_session: CDPSession | None = None
    try:
        # Wait for the workflow run to exist and be authorized
        while True:
            workflow_run = await app.DATABASE.get_workflow_run(
                workflow_run_id=workflow_run_id,
                organization_id=organization_id,
            )
            if not workflow_run or workflow_run.organization_id != organization_id:
                LOG.info("CDP input: workflow run not found", workflow_run_id=workflow_run_id)
                await websocket.close(code=4404, reason="workflow_run_not_found")
                return
            if workflow_run.status == WorkflowRunStatus.running or workflow_run.status.is_final():
                break
            await asyncio.sleep(1)

        # Wait for browser state
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

        cdp_session = await page.context.new_cdp_session(page)
        stream_ref_inc(workflow_run_id)

        await _verify_cdp_session(cdp_session, "workflow_run_id", workflow_run_id, client_id)

        LOG.info("CDP input channel ready", workflow_run_id=workflow_run_id, client_id=client_id)
        await websocket.send_json({"kind": "ready"})

        await _run_input_loop(websocket, channel, cdp_session, "workflow_run_id", workflow_run_id)

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
            stream_ref_dec(workflow_run_id)
            try:
                await cdp_session.detach()
            except Exception:
                pass
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
    organization_id = await _accept_and_authenticate(
        websocket,
        apikey,
        token,
        "browser_session_id",
        browser_session_id,
    )
    if organization_id is None:
        return

    if not _require_client_id(websocket, client_id, "browser_session_id", browser_session_id):
        await _close_ws_safely(websocket, 1002)
        return

    channel = CdpInputChannel(
        client_id=client_id,
        organization_id=organization_id,
        websocket=websocket,
    )

    cdp_session: CDPSession | None = None
    try:
        # Verify the browser session exists and is accessible
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

        # Wait for browser state
        browser_state = await wait_for_browser_state(browser_session_id, "browser_session")
        if browser_state is None:
            LOG.warning("CDP input: timed out waiting for browser state", browser_session_id=browser_session_id)
            await websocket.close(code=4408, reason="browser_state_timeout")
            return

        page = await browser_state.get_working_page()
        if page is None:
            LOG.warning("CDP input: no working page", browser_session_id=browser_session_id)
            await websocket.close(code=4410, reason="no_working_page")
            return

        cdp_session = await page.context.new_cdp_session(page)

        await _verify_cdp_session(cdp_session, "browser_session_id", browser_session_id, client_id)

        LOG.info("CDP input channel ready", browser_session_id=browser_session_id, client_id=client_id)
        await websocket.send_json({"kind": "ready"})

        await _run_input_loop(websocket, channel, cdp_session, "browser_session_id", browser_session_id)

    except ConnectionClosedOK:
        LOG.info("CDP input: WS closed cleanly", browser_session_id=browser_session_id)
    except ConnectionClosedError:
        LOG.warning("CDP input: WS connection error", browser_session_id=browser_session_id)
    except WebSocketDisconnect:
        LOG.info("CDP input: WS disconnected", browser_session_id=browser_session_id)
    except Exception:
        LOG.warning("CDP input: unexpected error", browser_session_id=browser_session_id, exc_info=True)
    finally:
        if cdp_session is not None:
            try:
                await cdp_session.detach()
            except Exception:
                pass
        await channel.close()
        LOG.info("CDP input channel closed", browser_session_id=browser_session_id, client_id=client_id)
