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
from opentelemetry import metrics
from playwright.async_api import CDPSession
from websockets.exceptions import ConnectionClosedError, ConnectionClosedOK

from skyvern.exceptions import BlockedNavigationDestination, InvalidUrl
from skyvern.forge import app
from skyvern.forge.sdk.routes.routers import base_router, legacy_base_router
from skyvern.forge.sdk.routes.streaming.auth import auth, require_client_id
from skyvern.forge.sdk.routes.streaming.payload_limits import MAX_CLIPBOARD_PASTE_BYTES
from skyvern.forge.sdk.routes.streaming.screencast import (
    _resolve_working_page,
    release_browser_state,
    wait_for_browser_state,
)
from skyvern.forge.sdk.schemas.persistent_browser_sessions import is_final_status
from skyvern.forge.sdk.streaming.registries import (
    add_cdp_input_channel,
    del_cdp_input_channel,
    stream_ref_dec,
    try_stream_ref_inc,
)
from skyvern.forge.sdk.workflow.models.workflow import WorkflowRunStatus
from skyvern.utils.url_validators import prepend_scheme_and_validate_url
from skyvern.webeye.browser_errors import is_target_closed_message
from skyvern.webeye.browser_state import BrowserState
from skyvern.webeye.navigation import revalidate_redirect_chain, validate_navigation_destination

LOG = structlog.get_logger()

_INPUT_KIND_LABELS = frozenset(
    {
        "mouseEvent",
        "keyEvent",
        "wheelEvent",
        "insertText",
        "copySelectedText",
        "navigateEvent",
        "goBackEvent",
        "goForwardEvent",
        "reloadEvent",
    }
)
_LATENCY_BUCKETS_SECONDS = [0.001, 0.002, 0.005, 0.01, 0.02, 0.03, 0.045, 0.06, 0.1, 0.25, 0.5, 1.0, 2.5, 5.0]
_meter = metrics.get_meter("skyvern.live_view")
_input_wait_seconds = _meter.create_histogram(
    "skyvern.live_view.input_wait_seconds",
    unit="s",
    description="Input event: receive_text returned -> dispatch started, including active-page resolution",
    explicit_bucket_boundaries_advisory=_LATENCY_BUCKETS_SECONDS,
)
_input_dispatch_seconds = _meter.create_histogram(
    "skyvern.live_view.input_dispatch_seconds",
    unit="s",
    description="Input event: dispatch handling after an active CDP session is acquired",
    explicit_bucket_boundaries_advisory=_LATENCY_BUCKETS_SECONDS,
)

_VALID_MOUSE_TYPES = {"mousePressed", "mouseReleased", "mouseMoved"}
_VALID_MOUSE_BUTTONS = {"left", "middle", "right", "none"}
_VALID_KEY_TYPES = {"keyDown", "keyUp", "rawKeyDown"}
_MAX_COORD = 10000
_MAX_DELTA = 10000
_MAX_KEY_LEN = 32
_MAX_CODE_LEN = 32
_MODIFIER_MASK = 0xF
_MAX_VK_CODE = 0xFE
_MAX_MOUSE_BUTTONS_MASK = 0x7
# Matches the length Skyvern's own InvalidUrl exception documents as its supported max.
_MAX_URL_LEN = 2083
ACTIVE_PAGE_INPUT_REFRESH_INTERVAL = 0.5
_NAVIGATION_RESET_TIMEOUT_MS = 5_000
_TARGET_CLOSED_ERROR_TYPES = frozenset({"TargetClosedError", "CdpTargetClosedError"})
_PIPELINED_INPUT_KINDS = frozenset({"mouseEvent", "wheelEvent", "keyEvent", "insertText"})
_MAX_IN_FLIGHT_INPUT_DISPATCHES = 32


def _input_kind_label(kind: object) -> str:
    return kind if isinstance(kind, str) and kind in _INPUT_KIND_LABELS else "other"


_VALID_EDITING_COMMANDS = {
    "deleteBackward",
    "deleteForward",
    "insertNewline",
    "moveWordLeft",
    "moveWordLeftAndModifySelection",
    "moveWordRight",
    "moveWordRightAndModifySelection",
    "moveToLeftEndOfLine",
    "moveToLeftEndOfLineAndModifySelection",
    "moveToRightEndOfLine",
    "moveToRightEndOfLineAndModifySelection",
    "selectAll",
}

_COPY_SELECTED_TEXT_EXPRESSION = """
(() => {
  const active = document.activeElement;
  if (active) {
    const tag = active.tagName?.toLowerCase();
    const type = active.type?.toLowerCase();
    const selectableField =
      tag === "textarea" ||
      (tag === "input" && type !== "password");
    if (
      selectableField &&
      typeof active.selectionStart === "number" &&
      typeof active.selectionEnd === "number"
    ) {
      const start = Math.min(active.selectionStart, active.selectionEnd);
      const end = Math.max(active.selectionStart, active.selectionEnd);
      if (start !== end) return String(active.value ?? "").slice(start, end);
    }
  }
  return window.getSelection()?.toString() ?? "";
})()
"""


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
        entity_id: str,
        entity_type: str,
        workflow_run_id: str | None = None,
        organization_id: str | None = None,
        refresh_interval: float = ACTIVE_PAGE_INPUT_REFRESH_INTERVAL,
    ) -> None:
        self.browser_state = browser_state
        self.entity_id = entity_id
        self.entity_type = entity_type
        self.workflow_run_id = workflow_run_id
        self.organization_id = organization_id
        self.log_id_key = f"{entity_type}_id"
        self.log_id_value = entity_id
        self.refresh_interval = refresh_interval
        self.cdp_session: CDPSession | None = None
        self.page: object | None = None
        self.next_refresh_at = 0.0
        self.page_resolution_failed = False

    async def get_session(self, *, force_refresh: bool = False) -> CDPSession | None:
        now = time.monotonic()
        if not force_refresh and now < self.next_refresh_at:
            return None if self.page_resolution_failed else self.cdp_session

        page = await _resolve_working_page(
            self.browser_state,
            self.entity_id,
            self.entity_type,
            self.workflow_run_id,
            self.organization_id,
            fall_back_to_captured=self.cdp_session is None,
        )
        if page is None:
            self.next_refresh_at = now + self.refresh_interval
            if self.cdp_session is not None:
                # _resolve_working_page returns None on a transient failure precisely so the caller
                # keeps the live page; dropping the bound session here strands user input instead.
                return self.cdp_session
            self.page_resolution_failed = True
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


async def _close_input_session_and_release_browser_state(
    input_session: ActivePageCdpInputSession | None,
    browser_state: BrowserState | None,
    entity_type: str,
    entity_id: str,
) -> None:
    """Detach the child CDP session before closing its adopted browser driver.

    The observer and input sessions share the proxy's upstream connection. Releasing the
    observer first closes Playwright while leaving the input session's target attachment
    behind on that shared connection, so a reconnect can receive a live but stalled stream.
    The ``finally`` keeps browser-state ownership release cancellation-safe.
    """
    try:
        if input_session is not None:
            await input_session.close()
    finally:
        await release_browser_state(browser_state, entity_type, entity_id)


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

    buttons = msg.get("buttons", 0)
    if not isinstance(buttons, int):
        buttons = 0
    buttons = max(0, min(buttons, _MAX_MOUSE_BUTTONS_MASK))

    return {
        "type": event_type,
        "x": x,
        "y": y,
        "button": button,
        "buttons": buttons,
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

    commands = msg.get("commands")
    if isinstance(commands, list):
        validated_commands = [
            command for command in commands if isinstance(command, str) and command in _VALID_EDITING_COMMANDS
        ]
        if validated_commands:
            result["commands"] = validated_commands

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


def _validate_insert_text(msg: dict) -> dict | None:
    text = msg.get("text")
    if not isinstance(text, str):
        return None
    if len(text.encode("utf-8")) > MAX_CLIPBOARD_PASTE_BYTES:
        return None
    return {"text": text}


async def _close_ws_safely(websocket: WebSocket, code: int, reason: str = "") -> None:
    try:
        await websocket.close(code=code, reason=reason)
    except Exception:
        pass


_EVENT_DISPATCH_MAP: dict[str, tuple[t.Callable[[dict], dict | None], str]] = {
    "mouseEvent": (_validate_mouse_event, "Input.dispatchMouseEvent"),
    "keyEvent": (_validate_key_event, "Input.dispatchKeyEvent"),
    "wheelEvent": (_validate_wheel_event, "Input.dispatchMouseEvent"),
    "insertText": (_validate_insert_text, "Input.insertText"),
}


def _validate_cdp_dispatch(
    kind: str,
    msg: dict,
    log_id_key: str,
    log_id_value: str,
) -> tuple[str, dict] | None:
    entry = _EVENT_DISPATCH_MAP.get(kind)
    if entry is None:
        return None
    validator, cdp_method = entry
    validated = validator(msg)
    if validated is not None:
        return cdp_method, validated
    LOG.warning(
        "CDP input: validation failed",
        **{log_id_key: log_id_value},
        kind=kind,
        raw_event_type=msg.get("eventType"),
    )
    return None


async def _reset_page_after_navigation_failure(
    page: object,
    log_id_key: str,
    log_id_value: str,
) -> None:
    try:
        await page.goto("about:blank", timeout=_NAVIGATION_RESET_TIMEOUT_MS)  # type: ignore[attr-defined]
    except Exception:
        LOG.exception("CDP input: failed to reset page after navigation failure", **{log_id_key: log_id_value})
        raise


def _is_navigation_target_loss(error: BaseException) -> bool:
    return type(error).__name__ in _TARGET_CLOSED_ERROR_TYPES or is_target_closed_message(str(error))


async def _copy_selected_text(websocket: WebSocket, cdp_session: CDPSession) -> None:
    result = await cdp_session.send(
        "Runtime.evaluate",
        {
            "expression": _COPY_SELECTED_TEXT_EXPRESSION,
            "returnByValue": True,
        },
    )
    if not isinstance(result, dict) or result.get("exceptionDetails"):
        await websocket.send_json({"kind": "copied-text", "text": ""})
        return

    remote_result = result.get("result")
    copied_text = remote_result.get("value", "") if isinstance(remote_result, dict) else ""
    if not isinstance(copied_text, str):
        copied_text = ""
    encoded = copied_text.encode("utf-8")
    if len(encoded) > MAX_CLIPBOARD_PASTE_BYTES:
        copied_text = encoded[:MAX_CLIPBOARD_PASTE_BYTES].decode("utf-8", errors="ignore")
    await websocket.send_json({"kind": "copied-text", "text": copied_text})


async def _dispatch_navigate_event(
    page: object,
    msg: dict,
    log_id_key: str,
    log_id_value: str,
    websocket: WebSocket,
) -> None:
    raw_url = msg.get("url")
    if not isinstance(raw_url, str) or not raw_url.strip() or len(raw_url) > _MAX_URL_LEN:
        LOG.warning("CDP input: navigate validation failed", **{log_id_key: log_id_value}, reason="malformed_url")
        await websocket.send_json({"kind": "navigate-error", "reason": "invalid_url"})
        return

    try:
        # Normalize (bare host -> https://, well-formedness) before validating or navigating so
        # the browser is never asked to load exactly what the user typed unvalidated.
        url = await asyncio.to_thread(prepend_scheme_and_validate_url, raw_url)
    except InvalidUrl:
        LOG.warning("CDP input: navigate validation failed", **{log_id_key: log_id_value}, reason="invalid_url")
        await websocket.send_json({"kind": "navigate-error", "reason": "invalid_url"})
        return

    try:
        # Fail closed before any request reaches the remote browser -- the same choke point
        # every real page.goto in the codebase funnels through (see navigate_with_retry).
        await asyncio.to_thread(validate_navigation_destination, url)
    except BlockedNavigationDestination as error:
        LOG.info("CDP input: navigate blocked", **{log_id_key: log_id_value}, reason=error.reason)
        await websocket.send_json({"kind": "navigate-error", "reason": "blocked"})
        return

    try:
        response = await page.goto(url)  # type: ignore[attr-defined]
    except Exception as error:
        if _is_navigation_target_loss(error):
            raise
        LOG.warning(
            "CDP input: navigation failed",
            **{log_id_key: log_id_value},
            error_type=type(error).__name__,
        )
        # A timeout does not cancel Chrome's in-flight navigation. Supersede it before preserving
        # input so a late redirect cannot commit after a one-time page.url validation.
        await _reset_page_after_navigation_failure(page, log_id_key, log_id_value)
        await websocket.send_json({"kind": "navigate-error", "reason": "failed"})
        return

    try:
        # page.goto follows redirects at the network layer, so a validated public entry point
        # can still land on an internal host -- re-check the followed chain (SKY-13112 pattern).
        await revalidate_redirect_chain(response, validate_navigation_destination)
    except BlockedNavigationDestination as error:
        LOG.info("CDP input: navigate blocked via redirect", **{log_id_key: log_id_value}, reason=error.reason)
        await _reset_page_after_navigation_failure(page, log_id_key, log_id_value)
        await websocket.send_json({"kind": "navigate-error", "reason": "blocked"})


_HISTORY_EVENT_OFFSETS = {"goBackEvent": -1, "goForwardEvent": 1, "reloadEvent": 0}


async def _dispatch_history_event(
    cdp_session: CDPSession,
    kind: str,
    log_id_key: str,
    log_id_value: str,
    websocket: WebSocket,
) -> None:
    history = await cdp_session.send("Page.getNavigationHistory", {})
    entries = history.get("entries") or [] if isinstance(history, dict) else []
    current_index = history.get("currentIndex") if isinstance(history, dict) else None
    if not isinstance(current_index, int):
        return

    target_index = current_index + _HISTORY_EVENT_OFFSETS[kind]
    if not 0 <= target_index < len(entries):
        # Nothing in that direction. A real browser greys the button out rather than erroring,
        # and the frontend mirrors that from canGoBack/canGoForward.
        return

    entry = entries[target_index]
    url = entry.get("url")
    entry_id = entry.get("id")
    if not isinstance(url, str) or not isinstance(entry_id, int):
        return

    try:
        # A destination blocked mid-redirect leaves its entry in the back stack -- the navigate
        # guard resets the page, not the history -- so replaying an entry unvalidated would
        # reopen exactly the SSRF that _dispatch_navigate_event closes.
        await asyncio.to_thread(validate_navigation_destination, url)
    except BlockedNavigationDestination as error:
        LOG.info(
            "CDP input: history navigation blocked",
            **{log_id_key: log_id_value},
            kind=kind,
            reason=error.reason,
        )
        await websocket.send_json({"kind": "navigate-error", "reason": "blocked"})
        return

    if kind == "reloadEvent":
        await cdp_session.send("Page.reload", {})
    else:
        await cdp_session.send("Page.navigateToHistoryEntry", {"entryId": entry_id})


async def _dispatch_event(
    cdp_session: CDPSession,
    page: object,
    kind: str,
    msg: dict,
    log_id_key: str,
    log_id_value: str,
    websocket: WebSocket,
) -> None:
    if kind == "navigateEvent":
        await _dispatch_navigate_event(page, msg, log_id_key, log_id_value, websocket)
        return

    if kind in _HISTORY_EVENT_OFFSETS:
        await _dispatch_history_event(cdp_session, kind, log_id_key, log_id_value, websocket)
        return

    if kind == "copySelectedText":
        await _copy_selected_text(websocket, cdp_session)
        return

    dispatch = _validate_cdp_dispatch(kind, msg, log_id_key, log_id_value)
    if dispatch is None:
        return
    cdp_method, validated = dispatch
    await cdp_session.send(cdp_method, validated)


async def _run_input_loop(
    websocket: WebSocket,
    channel: CdpInputChannel,
    input_session: ActivePageCdpInputSession,
    log_id_key: str,
    log_id_value: str,
) -> None:
    dropped_log_count = 0
    no_active_page_log_count = 0
    dispatch_semaphore = asyncio.Semaphore(_MAX_IN_FLIGHT_INPUT_DISPATCHES)
    dispatch_tasks: set[asyncio.Task[None]] = set()
    dispatch_failure_close_started = False

    async def dispatch_pipelined_event(
        cdp_session: CDPSession,
        kind: str,
        cdp_method: str,
        validated: dict,
        received_at: float,
    ) -> None:
        nonlocal dispatch_failure_close_started
        attributes = {"event_kind": _input_kind_label(kind)}
        dispatch_started = time.monotonic()
        _input_wait_seconds.record(dispatch_started - received_at, attributes)
        try:
            try:
                await cdp_session.send(cdp_method, validated)
            finally:
                _input_dispatch_seconds.record(time.monotonic() - dispatch_started, attributes)
        except Exception as error:
            if _is_navigation_target_loss(error):
                LOG.debug(
                    "CDP input: dropped event for detached pipelined session",
                    **{log_id_key: log_id_value},
                    kind=kind,
                    error_type=type(error).__name__,
                )
                return
            LOG.warning(
                "CDP input: failed to dispatch event; closing input channel",
                **{log_id_key: log_id_value},
                kind=kind,
                exc_info=True,
            )
            if not dispatch_failure_close_started:
                dispatch_failure_close_started = True
                await _close_ws_safely(websocket, code=4411, reason="dispatch_failed")
        finally:
            dispatch_semaphore.release()

    try:
        while True:
            try:
                raw = await websocket.receive_text()
            except WebSocketDisconnect:
                break
            received_at = time.monotonic()

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

            if kind in _PIPELINED_INPUT_KINDS:
                dispatch = _validate_cdp_dispatch(kind, msg, log_id_key, log_id_value)
                if dispatch is None:
                    continue
                cdp_method, validated = dispatch
                await dispatch_semaphore.acquire()
                if dispatch_failure_close_started:
                    dispatch_semaphore.release()
                    break
                task = asyncio.create_task(
                    dispatch_pipelined_event(cdp_session, kind, cdp_method, validated, received_at)
                )
                dispatch_tasks.add(task)
                task.add_done_callback(dispatch_tasks.discard)
                # CDPSession.send writes to one transport before awaiting a response, preserving receive order here.
                await asyncio.sleep(0)
                if dispatch_failure_close_started:
                    break
                continue

            attributes = {"event_kind": _input_kind_label(kind)}
            dispatch_started = time.monotonic()
            _input_wait_seconds.record(dispatch_started - received_at, attributes)
            try:
                await _dispatch_event(cdp_session, input_session.page, kind, msg, log_id_key, log_id_value, websocket)
            except Exception:
                LOG.warning(
                    "CDP input: failed to dispatch event; closing input channel",
                    **{log_id_key: log_id_value},
                    kind=kind,
                    exc_info=True,
                )
                if not dispatch_failure_close_started:
                    dispatch_failure_close_started = True
                    await websocket.close(code=4411, reason="dispatch_failed")
                break
            _input_dispatch_seconds.record(time.monotonic() - dispatch_started, attributes)
    finally:
        pending_tasks = tuple(dispatch_tasks)
        for task in pending_tasks:
            task.cancel()
        if pending_tasks:
            await asyncio.gather(*pending_tasks, return_exceptions=True)


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
    browser_state: BrowserState | None = None
    stream_registered = False
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
            if workflow_run.status == WorkflowRunStatus.running:
                break
            if workflow_run.status.is_final():
                LOG.info("CDP input: workflow run already finalized", workflow_run_id=workflow_run_id)
                await websocket.close(code=4409, reason="workflow_run_closing")
                return
            if workflow_run.status == WorkflowRunStatus.paused:
                break
            if time.monotonic() >= deadline:
                LOG.warning("CDP input: timed out waiting for running status", workflow_run_id=workflow_run_id)
                await websocket.close(code=4408, reason="wait_timeout")
                return
            await asyncio.sleep(1)

        if not try_stream_ref_inc(workflow_run_id):
            LOG.info("CDP input: workflow run cleanup already started", workflow_run_id=workflow_run_id)
            await websocket.close(code=4409, reason="workflow_run_closing")
            return
        stream_registered = True

        browser_state = await wait_for_browser_state(workflow_run_id, "workflow_run")
        if browser_state is None:
            LOG.warning("CDP input: timed out waiting for browser state", workflow_run_id=workflow_run_id)
            await websocket.close(code=4408, reason="browser_state_timeout")
            return

        input_session = ActivePageCdpInputSession(browser_state, workflow_run_id, "workflow_run")
        cdp_session = await input_session.get_session(force_refresh=True)
        if cdp_session is None:
            LOG.warning("CDP input: no working page", workflow_run_id=workflow_run_id)
            await websocket.close(code=4410, reason="no_working_page")
            return
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
        try:
            await _close_input_session_and_release_browser_state(
                input_session, browser_state, "workflow_run", workflow_run_id
            )
        finally:
            if stream_registered:
                await stream_ref_dec(workflow_run_id)
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
    browser_state: BrowserState | None = None
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

        input_session = ActivePageCdpInputSession(
            browser_state, browser_session_id, "browser_session", organization_id=organization_id
        )
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
        await _close_input_session_and_release_browser_state(
            input_session, browser_state, "browser_session", browser_session_id
        )
        await channel.close()
        LOG.info("CDP input channel closed", browser_session_id=browser_session_id, client_id=client_id)
