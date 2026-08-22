from __future__ import annotations

import asyncio
import json
import secrets
from collections.abc import Coroutine
from functools import partial
from typing import Any, Protocol

import structlog
from aiohttp import WSMsgType, web

from skyvern.browser_extension.errors import (
    BrowserExtensionBrokerError,
    BrowserExtensionError,
    ExtensionRequestError,
)
from skyvern.browser_extension.protocol import is_cdp_method_allowed
from skyvern.browser_extension.relay import _MAX_WS_MESSAGE_BYTES
from skyvern.browser_extension.target_registry import VirtualTargetRegistry

LOG = structlog.get_logger()

_VERSION_RESULT = {
    "protocolVersion": "1.3",
    "product": "Chrome/999.0.0.0",
    "revision": "",
    "userAgent": "Skyvern-Extension-Bridge",
    "jsVersion": "",
}
_WINDOW_BOUNDS = {"left": 0, "top": 0, "width": 1280, "height": 720, "windowState": "normal"}
_BROWSER_TARGET_INFO = {
    "targetId": "skyvern-browser",
    "type": "browser",
    "title": "",
    "url": "",
    "attached": True,
    "canAccessOpener": False,
}
_CHILD_AUTO_ATTACH_PARAMS = {
    "flatten": True,
    "autoAttach": True,
    "waitForDebuggerOnStart": False,
    "filter": [{"type": "iframe", "exclude": False}],
}
_UNSUPPORTED_CHILD_TARGET_TYPES = {"service_worker", "shared_worker", "worker"}
_CHILD_AUTO_ATTACH_TIMEOUT_SECONDS = 3.0
_CHILD_DETACH_TIMEOUT_SECONDS = 2.0
_ROOT_TARGET_GATE_METHODS = {
    "Browser.close",
    "Target.activateTarget",
    "Target.attachToTarget",
    "Target.closeTarget",
    "Target.createTarget",
    "Target.getTargetInfo",
    "Target.getTargets",
    "Target.setDiscoverTargets",
}


class _ExtensionRelay(Protocol):
    scoped_tabs: list[dict[str, Any]]

    async def request(self, op: str, args: dict[str, Any], timeout: float = 30.0) -> dict[str, Any]: ...

    async def ensure_root_lease(self) -> dict[str, Any] | None: ...

    async def release_tab(self, tab_id: int) -> None: ...


class ExtensionCdpAdapter:
    def __init__(self, registry: VirtualTargetRegistry, relay: _ExtensionRelay) -> None:
        self._registry = registry
        self._relay = relay
        self._capability = secrets.token_urlsafe(32)
        self._runner: web.AppRunner | None = None
        self._site: web.TCPSite | None = None
        self._port: int | None = None
        self._client_ws: web.WebSocketResponse | None = None
        self._client_guard = asyncio.Lock()
        self._send_lock = asyncio.Lock()
        self._root_target_setup_lock = asyncio.Lock()
        self._auto_attach = False
        self._discover_targets = False
        self._attached_tabs: set[int] = set()
        self._attach_locks: dict[int, asyncio.Lock] = {}
        self._opener_ids: dict[int, str] = {}
        self._scope_generations: dict[int, int] = {}
        self._scope_tombstones: set[int] = set()
        self._background_tasks: set[asyncio.Task[None]] = set()
        self._client_tasks: dict[web.WebSocketResponse, set[asyncio.Task[None]]] = {}
        self._closing_client_websockets: set[web.WebSocketResponse] = set()
        self._pending_child_sessions: set[str] = set()
        self._pending_child_events: dict[str, list[dict]] = {}

    async def start(self) -> None:
        if self._runner is not None:
            return
        app = web.Application()
        app.router.add_get(f"/cdp/{self._capability}", self._handle_websocket)
        runner = web.AppRunner(app)
        await runner.setup()
        site = web.TCPSite(runner, "127.0.0.1", 0)
        await site.start()
        addresses = runner.addresses
        if not addresses:
            await runner.cleanup()
            raise RuntimeError("CDP adapter failed to bind")
        self._runner = runner
        self._site = site
        self._port = int(addresses[0][1])

    async def stop(self) -> None:
        async with self._client_guard:
            ws = self._client_ws
            self._client_ws = None
        self._reset_connection_state()
        if ws is not None and not ws.closed:
            await ws.close(code=1001, message=b"adapter stopped")
        if self._runner is not None:
            await self._runner.cleanup()
        self._runner = None
        self._site = None
        self._port = None

    @property
    def cdp_ws_url(self) -> str:
        if self._port is None:
            raise RuntimeError("CDP adapter is not started")
        return f"ws://127.0.0.1:{self._port}/cdp/{self._capability}"

    async def handle_extension_event(self, event: str, params: dict) -> None:
        if event == "debugger.event":
            await self._handle_debugger_event(params)
        elif event == "debugger.detached":
            await self._remove_tab_with_events(params.get("tabId"))
        elif event == "scope.tabRemoved":
            await self._remove_tab_with_events(params.get("tabId"))
        elif event in {"scope.tabAdded", "tabs.created"}:
            tab_id = params.get("tabId")
            if type(tab_id) is not int:
                return
            if event == "scope.tabAdded":
                try:
                    self._registry.target_id_for_tab(tab_id)
                except KeyError:
                    pass
                else:
                    return
            generation = self._begin_tab_scope(tab_id)
            if self._auto_attach:
                self._spawn(
                    self._handle_tab_added(
                        params,
                        include_opener=event == "tabs.created",
                        generation=generation,
                    )
                )
                await asyncio.sleep(0)
            else:
                await self._handle_tab_added(
                    params,
                    include_opener=event == "tabs.created",
                    generation=generation,
                )
        elif event == "extension.hello":
            scoped_tabs = params.get("scopedTabs")
            if isinstance(scoped_tabs, list):
                tabs = [
                    (tab, self._begin_tab_scope(tab["tabId"]))
                    for tab in scoped_tabs
                    if isinstance(tab, dict) and type(tab.get("tabId")) is int
                ]
                if self._auto_attach:
                    self._spawn(self._handle_hello_tabs(tabs))
                    await asyncio.sleep(0)
                else:
                    await self._handle_hello_tabs(tabs)

    async def on_extension_disconnect(self) -> None:
        async with self._client_guard:
            ws = self._client_ws
            self._client_ws = None
        self._reset_connection_state()
        if ws is not None and not ws.closed:
            await ws.close(code=1001, message=b"extension disconnected")

    async def _handle_websocket(self, request: web.Request) -> web.WebSocketResponse:
        ws = web.WebSocketResponse(max_msg_size=_MAX_WS_MESSAGE_BYTES)
        await ws.prepare(request)
        async with self._client_guard:
            if self._client_ws is not None:
                rejected = True
            else:
                rejected = False
                self._client_ws = ws
        if rejected:
            await ws.close(code=4409, message=b"CDP client already connected")
            return ws

        try:
            async for message in ws:
                if message.type == WSMsgType.TEXT:
                    self._spawn_client_task(ws, self._handle_client_text(ws, message.data))
                elif message.type == WSMsgType.ERROR:
                    break
                elif message.type == WSMsgType.BINARY:
                    await ws.close(code=1003, message=b"text frames required")
                    break
        finally:
            await self._cancel_client_tasks(ws)
            async with self._client_guard:
                if self._client_ws is ws and ws not in self._closing_client_websockets:
                    self._client_ws = None
                    self._reset_connection_state()
        return ws

    async def _handle_client_text(self, ws: web.WebSocketResponse, raw: str) -> None:
        try:
            payload = json.loads(raw)
        except json.JSONDecodeError:
            await self._send(ws, {"error": {"code": -32700, "message": "Parse error"}})
            return
        if not isinstance(payload, dict):
            await self._send(ws, {"error": {"code": -32600, "message": "Invalid Request"}})
            return
        request_id = payload.get("id")
        method = payload.get("method")
        params = payload.get("params", {})
        if not isinstance(method, str) or not isinstance(params, dict):
            await self._send(ws, {"id": request_id, "error": {"code": -32600, "message": "Invalid Request"}})
            return
        LOG.debug("browser_extension_cdp_message", method=method, size_bytes=len(raw.encode()))
        session_id = payload.get("sessionId")
        try:
            if isinstance(session_id, str):
                if self._registry.is_browser_session_alias(session_id):
                    await self._handle_root_command(ws, request_id, method, params, session_id)
                else:
                    await self._handle_session_command(ws, request_id, session_id, method, params)
            else:
                await self._handle_root_command(ws, request_id, method, params)
        except (ExtensionRequestError, BrowserExtensionBrokerError) as exc:
            error = {"code": -32000, "message": f"{exc.code}: {exc.message}"}
            response = {"id": request_id, "error": error}
            if isinstance(session_id, str):
                response["sessionId"] = session_id
            await self._send(ws, response)

    async def _handle_session_command(
        self, ws: web.WebSocketResponse, request_id: object, session_id: str, method: str, params: dict
    ) -> None:
        try:
            tab_id, chrome_session_id = self._registry.resolve_session(session_id)
        except KeyError:
            await self._send(
                ws,
                {
                    "id": request_id,
                    "sessionId": session_id,
                    "error": {"code": -32001, "message": "session not found"},
                },
            )
            return
        if not is_cdp_method_allowed(method, params):
            raise ExtensionRequestError("CDP_METHOD_NOT_ALLOWED", "The requested CDP method is not allowed.")
        relay_params = params
        if method == "Target.setAutoAttach" and params.get("autoAttach") is True:
            relay_params = {**params, "filter": [{"type": "iframe", "exclude": False}]}
        args = {"tabId": tab_id, "method": method, "params": relay_params}
        if chrome_session_id is not None:
            args["sessionId"] = chrome_session_id
        relay_result = await self._relay.request("debugger.send", args)
        await self._send(
            ws,
            {"id": request_id, "sessionId": session_id, "result": relay_result.get("result", {})},
        )

    async def _handle_root_command(
        self,
        ws: web.WebSocketResponse,
        request_id: object,
        method: str,
        params: dict,
        response_session_id: str | None = None,
    ) -> None:
        if method == "Target.setAutoAttach":
            async with self._root_target_setup_lock:
                await self._set_auto_attach(ws, request_id, params, response_session_id)
            return
        if method in _ROOT_TARGET_GATE_METHODS:
            # Auto-attach replaces temporary tab-<id> targets with main-frame ids.
            # Wait for that setup without serializing these commands afterward.
            async with self._root_target_setup_lock:
                pass

        if method == "Browser.getVersion":
            await self._reply(ws, request_id, dict(_VERSION_RESULT), response_session_id)
        elif method == "Browser.setDownloadBehavior":
            await self._reply(ws, request_id, {}, response_session_id)
        elif method == "Browser.close":
            await self._reply(ws, request_id, {}, response_session_id)
            self._closing_client_websockets.add(ws)
            self._spawn(self._shutdown_client(ws))
        elif method == "Browser.getWindowForTarget":
            await self._reply(
                ws,
                request_id,
                {"windowId": 1, "bounds": dict(_WINDOW_BOUNDS)},
                response_session_id,
            )
        elif method == "Browser.setWindowBounds":
            await self._reply(ws, request_id, {}, response_session_id)
        elif method == "Browser.getWindowBounds":
            await self._reply(ws, request_id, {"bounds": dict(_WINDOW_BOUNDS)}, response_session_id)
        elif method == "Target.setDiscoverTargets":
            await self._set_discover_targets(ws, request_id, params, response_session_id)
        elif method == "Target.getTargets":
            self._register_scoped_tabs()
            await self._reply(
                ws,
                request_id,
                {"targetInfos": self._page_target_infos()},
                response_session_id,
            )
        elif method == "Target.getTargetInfo":
            await self._get_target_info(ws, request_id, params, response_session_id)
        elif method == "Target.createTarget":
            await self._create_target(ws, request_id, params, response_session_id)
        elif method == "Target.closeTarget":
            await self._close_target(ws, request_id, params, response_session_id)
        elif method == "Target.activateTarget":
            await self._activate_target(ws, request_id, params, response_session_id)
        elif method == "Target.attachToTarget":
            await self._attach_to_target(ws, request_id, params, response_session_id)
        elif method == "Target.attachToBrowserTarget":
            await self._attach_to_browser_target(ws, request_id, response_session_id)
        elif method == "Target.detachFromTarget":
            await self._detach_from_target(ws, request_id, params, response_session_id)
        else:
            await self._send_error(
                ws,
                request_id,
                -32601,
                f"'{method}' wasn't found",
                response_session_id,
            )

    async def _set_auto_attach(
        self,
        ws: web.WebSocketResponse,
        request_id: object,
        params: dict,
        response_session_id: str | None,
    ) -> None:
        previous_auto_attach = self._auto_attach
        self._auto_attach = params.get("autoAttach") is True
        if not self._auto_attach:
            await self._reply(ws, request_id, {}, response_session_id)
            return
        tabs = [tab for tab in self._relay.scoped_tabs if isinstance(tab, dict)]
        if not tabs:
            # Acquire a root tab through the transport: a multi-client broker grants a
            # lease (free user-shared tab or a new scoped tab); the embedded relay
            # returns its first scoped tab or None.
            try:
                root = await self._relay.ensure_root_lease()
            except Exception as exc:
                LOG.debug(
                    "browser_extension_root_lease_failed",
                    error_type=type(exc).__name__,
                )
                root = None
            if root is not None and type(root.get("tabId")) is int:
                tabs = [root]
        if not tabs:
            try:
                created = await self._relay.request("tabs.create", {"url": "about:blank"})
                tab_id = created["tabId"]
                if type(tab_id) is not int:
                    raise BrowserExtensionError("Created tab id is invalid")
            except BaseException:
                self._auto_attach = previous_auto_attach
                raise
            tabs = [{"tabId": tab_id, "url": "about:blank", "title": ""}]

        newly_attached: list[tuple[int, str, int]] = []
        try:
            for tab in tabs:
                tab_id = tab.get("tabId")
                if type(tab_id) is not int:
                    continue
                generation = self._active_scope_generation(tab_id)
                if generation is None:
                    continue
                attached = await self._ensure_attached(tab, generation=generation)
                if attached is not None:
                    target_id, is_new = attached
                    if is_new:
                        newly_attached.append((tab_id, target_id, generation))
        except BaseException:
            self._auto_attach = previous_auto_attach
            for attached_tab_id, _, _ in reversed(newly_attached):
                await self._discard_failed_attachment_safely(
                    attached_tab_id,
                    suppress_interrupts=True,
                )
            raise

        await self._reply(ws, request_id, {}, response_session_id)
        for tab_id, target_id, generation in newly_attached:
            await self._emit_attached(tab_id, target_id, generation)

    async def _set_discover_targets(
        self,
        ws: web.WebSocketResponse,
        request_id: object,
        params: dict,
        response_session_id: str | None,
    ) -> None:
        self._discover_targets = params.get("discover") is True
        self._register_scoped_tabs()
        await self._reply(ws, request_id, {}, response_session_id)
        if self._discover_targets:
            for target_info in self._page_target_infos():
                await self._emit(
                    "Target.targetCreated",
                    {"targetInfo": target_info},
                    response_session_id,
                )
            await self._emit(
                "Target.targetCreated",
                {"targetInfo": dict(_BROWSER_TARGET_INFO)},
                response_session_id,
            )

    async def _get_target_info(
        self,
        ws: web.WebSocketResponse,
        request_id: object,
        params: dict,
        response_session_id: str | None,
    ) -> None:
        target_id = params.get("targetId")
        if target_id is None:
            target_info = dict(_BROWSER_TARGET_INFO)
        else:
            self._register_scoped_tabs()
            try:
                if not isinstance(target_id, str):
                    raise KeyError(target_id)
                tab_id = self._registry.tab_for_target(target_id)
                target_info = self._registry.target_info(target_id)
                if target_id == self._registry.target_id_for_tab(tab_id):
                    target_info = self._target_info(tab_id)
            except KeyError:
                await self._send_error(ws, request_id, -32000, "target not found", response_session_id)
                return
        await self._reply(ws, request_id, {"targetInfo": target_info}, response_session_id)

    async def _create_target(
        self,
        ws: web.WebSocketResponse,
        request_id: object,
        params: dict,
        response_session_id: str | None,
    ) -> None:
        url = params.get("url", "about:blank")
        if not isinstance(url, str):
            url = "about:blank"
        created = await self._relay.request("tabs.create", {"url": url})
        tab = {"tabId": created["tabId"], "url": url, "title": ""}
        tab_id = tab["tabId"]
        if type(tab_id) is not int:
            await self._send_error(ws, request_id, -32000, "created target is invalid", response_session_id)
            return
        generation = self._begin_tab_scope(tab_id)
        attached = await self._ensure_attached(tab, generation=generation)
        if attached is None:
            await self._send_error(ws, request_id, -32000, "target was revoked", response_session_id)
            return
        target_id, _ = attached
        await self._emit_attached(tab_id, target_id, generation)
        await self._reply(ws, request_id, {"targetId": target_id}, response_session_id)

    async def _close_target(
        self,
        ws: web.WebSocketResponse,
        request_id: object,
        params: dict,
        response_session_id: str | None,
    ) -> None:
        target_id = params.get("targetId")
        if not isinstance(target_id, str):
            await self._send_error(ws, request_id, -32000, "target not found", response_session_id)
            return
        try:
            tab_id = self._registry.tab_for_target(target_id)
        except KeyError:
            await self._send_error(ws, request_id, -32000, "target not found", response_session_id)
            return
        await self._relay.request("tabs.remove", {"tabId": tab_id})
        await self._reply(ws, request_id, {"success": True}, response_session_id)

    async def _activate_target(
        self,
        ws: web.WebSocketResponse,
        request_id: object,
        params: dict,
        response_session_id: str | None,
    ) -> None:
        target_id = params.get("targetId")
        if not isinstance(target_id, str):
            await self._send_error(ws, request_id, -32000, "target not found", response_session_id)
            return
        try:
            tab_id = self._registry.tab_for_target(target_id)
        except KeyError:
            await self._send_error(ws, request_id, -32000, "target not found", response_session_id)
            return
        await self._relay.request("tabs.activate", {"tabId": tab_id})
        await self._reply(ws, request_id, {}, response_session_id)

    async def _attach_to_target(
        self,
        ws: web.WebSocketResponse,
        request_id: object,
        params: dict,
        response_session_id: str | None,
    ) -> None:
        self._register_scoped_tabs()
        requested_target_id = params.get("targetId")
        if not isinstance(requested_target_id, str):
            await self._send_error(ws, request_id, -32000, "target not found", response_session_id)
            return
        try:
            tab_id = self._registry.tab_for_target(requested_target_id)
            info = self._target_info(tab_id)
        except KeyError:
            await self._send_error(ws, request_id, -32000, "target not found", response_session_id)
            return
        tab = {"tabId": tab_id, "url": info["url"], "title": info["title"]}
        generation = self._active_scope_generation(tab_id)
        if generation is None:
            await self._send_error(ws, request_id, -32000, "target was revoked", response_session_id)
            return
        attached = await self._ensure_attached(tab, generation=generation)
        if attached is None:
            await self._send_error(ws, request_id, -32000, "target was revoked", response_session_id)
            return
        target_id, is_new = attached
        session_id = self._registry.create_root_session_alias(tab_id)
        await self._reply(ws, request_id, {"sessionId": session_id}, response_session_id)
        if is_new:
            await self._emit_attached(tab_id, target_id, generation)

    async def _attach_to_browser_target(
        self,
        ws: web.WebSocketResponse,
        request_id: object,
        response_session_id: str | None,
    ) -> None:
        session_id = self._registry.create_browser_session_alias()
        await self._reply(ws, request_id, {"sessionId": session_id}, response_session_id)

    async def _detach_from_target(
        self,
        ws: web.WebSocketResponse,
        request_id: object,
        params: dict,
        response_session_id: str | None,
    ) -> None:
        session_id = params.get("sessionId")
        if isinstance(session_id, str):
            if self._registry.remove_browser_session_alias(session_id):
                await self._reply(ws, request_id, {}, response_session_id)
                return
            if self._registry.remove_root_session_alias(session_id):
                await self._reply(ws, request_id, {}, response_session_id)
                return
            try:
                tab_id, chrome_session_id = self._registry.resolve_session(session_id)
            except KeyError:
                tab_id = None
                chrome_session_id = None
            if tab_id is not None and chrome_session_id is None:
                await self._relay.request("debugger.detach", {"tabId": tab_id})
                self._forget_tab(tab_id)
        await self._reply(ws, request_id, {}, response_session_id)

    async def _handle_debugger_event(self, payload: dict, replaying_pending_event: bool = False) -> None:
        tab_id = payload.get("tabId")
        method = payload.get("method")
        event_params = payload.get("params")
        if type(tab_id) is not int or not isinstance(method, str) or not isinstance(event_params, dict):
            return
        payload_session_id = payload.get("sessionId")
        if (
            isinstance(payload_session_id, str)
            and payload_session_id in self._pending_child_sessions
            and not replaying_pending_event
        ):
            self._pending_child_events.setdefault(payload_session_id, []).append(payload)
            return
        try:
            if isinstance(payload_session_id, str):
                outer_session_ids = [payload_session_id]
                self._registry.resolve_session(outer_session_ids[0])
            else:
                outer_session_ids = self._registry.root_session_ids(tab_id)
        except KeyError:
            return

        if method == "Page.frameNavigated":
            self._update_main_frame(tab_id, event_params)
        if method == "Target.attachedToTarget":
            child_session_id = event_params.get("sessionId")
            target_info = event_params.get("targetInfo")
            if isinstance(child_session_id, str) and isinstance(target_info, dict):
                # The tab's debugger session reports its own top-level document as a
                # "page" child on every cross-process swap; a real browser endpoint
                # never surfaces that, and forwarding it duplicates the tab target.
                if target_info.get("type") == "page":
                    return
                if target_info.get("type") in _UNSUPPORTED_CHILD_TARGET_TYPES:
                    if child_session_id in self._pending_child_sessions:
                        return
                    self._pending_child_sessions.add(child_session_id)
                    self._spawn(self._discard_unsupported_child(tab_id, child_session_id, target_info))
                    await asyncio.sleep(0)
                    return
                if self._auto_attach:
                    if child_session_id in self._pending_child_sessions:
                        return
                    self._pending_child_sessions.add(child_session_id)
                    self._spawn(
                        self._initialize_child_target(
                            tab_id,
                            child_session_id,
                            target_info,
                            event_params,
                            outer_session_ids,
                        )
                    )
                    await asyncio.sleep(0)
                else:
                    self._registry.register_child_session(tab_id, child_session_id, target_info)
                    await self._emit_to_sessions(method, event_params, outer_session_ids)
                return
        elif method == "Target.detachedFromTarget":
            child_session_id = event_params.get("sessionId")
            if not isinstance(child_session_id, str):
                return
            if child_session_id in self._pending_child_sessions:
                self._pending_child_sessions.discard(child_session_id)
                self._spawn(self._discard_buffered_child_events(tab_id, child_session_id))
            try:
                self._registry.resolve_session(child_session_id)
            except KeyError:
                return
            await self._emit_to_sessions(method, event_params, outer_session_ids)
            self._registry.remove_child_session(child_session_id)
            return
        await self._emit_to_sessions(method, event_params, outer_session_ids)

    async def _initialize_child_target(
        self,
        tab_id: int,
        child_session_id: str,
        target_info: dict,
        event_params: dict,
        outer_session_ids: list[str],
    ) -> None:
        try:
            await self._relay.request(
                "debugger.send",
                {
                    "tabId": tab_id,
                    "sessionId": child_session_id,
                    "method": "Target.setAutoAttach",
                    "params": dict(_CHILD_AUTO_ATTACH_PARAMS),
                },
                timeout=_CHILD_AUTO_ATTACH_TIMEOUT_SECONDS,
            )
        except Exception as exc:
            LOG.debug(
                "browser_extension_child_auto_attach_failed",
                method="Target.setAutoAttach",
                error_code=exc.code if isinstance(exc, ExtensionRequestError) else "INTERNAL",
                target_type=target_info.get("type"),
            )
            try:
                await self._discard_buffered_child_events(tab_id, child_session_id)
                await self._resume_and_detach_unserviceable_child(tab_id, child_session_id)
            finally:
                self._pending_child_sessions.discard(child_session_id)
            return

        if child_session_id not in self._pending_child_sessions or not self._auto_attach:
            await self._discard_buffered_child_events(tab_id, child_session_id)
            self._pending_child_sessions.discard(child_session_id)
            return
        live_outer_session_ids = []
        for outer_session_id in outer_session_ids:
            try:
                self._registry.resolve_session(outer_session_id)
            except KeyError:
                continue
            live_outer_session_ids.append(outer_session_id)
        if not live_outer_session_ids:
            try:
                await self._discard_buffered_child_events(tab_id, child_session_id)
                await self._resume_and_detach_unserviceable_child(tab_id, child_session_id)
            finally:
                self._pending_child_sessions.discard(child_session_id)
            return
        self._registry.register_child_session(tab_id, child_session_id, target_info)
        await self._emit_to_sessions("Target.attachedToTarget", event_params, live_outer_session_ids)
        await self._replay_buffered_child_events(child_session_id)
        self._pending_child_sessions.discard(child_session_id)

    async def _replay_buffered_child_events(self, child_session_id: str) -> None:
        events = self._pending_child_events.get(child_session_id, [])
        event_index = 0
        while event_index < len(events):
            payload = events[event_index]
            event_index += 1
            await self._handle_debugger_event(payload, replaying_pending_event=True)
        self._pending_child_events.pop(child_session_id, None)

    async def _discard_buffered_child_events(self, tab_id: int, child_session_id: str) -> None:
        events = self._pending_child_events.pop(child_session_id, [])
        buffered_child_session_ids: set[str] = set()
        for payload in events:
            if payload.get("method") != "Target.attachedToTarget":
                continue
            event_params = payload.get("params")
            if not isinstance(event_params, dict):
                continue
            buffered_child_session_id = event_params.get("sessionId")
            if not isinstance(buffered_child_session_id, str):
                continue
            buffered_child_session_ids.add(buffered_child_session_id)
        for buffered_child_session_id in buffered_child_session_ids:
            await self._discard_buffered_child_events(tab_id, buffered_child_session_id)
            self._pending_child_sessions.discard(buffered_child_session_id)
            await self._resume_and_detach_unserviceable_child(tab_id, buffered_child_session_id)

    async def _discard_unsupported_child(self, tab_id: int, child_session_id: str, target_info: dict) -> None:
        LOG.debug(
            "browser_extension_child_target_skipped",
            target_type=target_info.get("type"),
        )
        try:
            await self._discard_buffered_child_events(tab_id, child_session_id)
            await self._resume_and_detach_unserviceable_child(tab_id, child_session_id)
        finally:
            self._pending_child_sessions.discard(child_session_id)

    async def _resume_and_detach_unserviceable_child(self, tab_id: int, child_session_id: str) -> None:
        try:
            await self._relay.request(
                "debugger.send",
                {
                    "tabId": tab_id,
                    "sessionId": child_session_id,
                    "method": "Runtime.runIfWaitingForDebugger",
                    "params": {},
                },
                timeout=_CHILD_DETACH_TIMEOUT_SECONDS,
            )
        except Exception as exc:
            LOG.debug(
                "browser_extension_child_resume_failed",
                method="Runtime.runIfWaitingForDebugger",
                error_type=type(exc).__name__,
            )
        try:
            await self._relay.request(
                "debugger.send",
                {
                    "tabId": tab_id,
                    "method": "Target.detachFromTarget",
                    "params": {"sessionId": child_session_id},
                },
                timeout=_CHILD_DETACH_TIMEOUT_SECONDS,
            )
        except Exception as exc:
            LOG.debug(
                "browser_extension_child_detach_failed",
                method="Target.detachFromTarget",
                error_type=type(exc).__name__,
            )

    async def _handle_hello_tabs(self, tabs: list[tuple[dict, int]]) -> None:
        for tab, generation in tabs:
            await self._handle_tab_added(tab, include_opener=False, generation=generation)

    async def _handle_tab_added(self, params: dict, include_opener: bool, generation: int) -> None:
        if self._auto_attach:
            async with self._root_target_setup_lock:
                await self._handle_tab_added_locked(params, include_opener, generation)
            return
        await self._handle_tab_added_locked(params, include_opener, generation)

    async def _handle_tab_added_locked(self, params: dict, include_opener: bool, generation: int) -> None:
        tab_id = params.get("tabId")
        if type(tab_id) is not int:
            return
        opener_id = None
        opener_tab_id = params.get("openerTabId")
        if include_opener and self._auto_attach and type(opener_tab_id) is int:
            try:
                opener_id = self._registry.target_id_for_tab(opener_tab_id)
            except KeyError:
                opener_id = None
        tab = {
            "tabId": tab_id,
            "url": params.get("url") if isinstance(params.get("url"), str) else "about:blank",
            "title": params.get("title") if isinstance(params.get("title"), str) else "",
        }
        if self._auto_attach:
            attached = await self._ensure_attached(tab, opener_id, generation)
            if attached is None:
                return
            target_id, is_new = attached
            if is_new:
                await self._emit_attached(tab_id, target_id, generation)
        else:
            if not self._scope_is_current(tab_id, generation):
                return
            target_id = self._register_tab(tab, opener_id)
        if self._discover_targets and self._scope_is_current(tab_id, generation):
            await self._emit(
                "Target.targetCreated",
                {"targetInfo": self._target_info(tab_id)},
                scope_guard=(tab_id, generation),
            )

    async def _remove_tab_with_events(self, tab_id: object) -> None:
        if type(tab_id) is not int:
            return
        self._revoke_tab_scope(tab_id)
        lock = self._attach_locks.setdefault(tab_id, asyncio.Lock())
        async with lock:
            try:
                session_ids = self._registry.root_session_ids(tab_id)
                target_id = self._registry.target_id_for_tab(tab_id)
            except KeyError:
                return
            self._forget_tab(tab_id)
            for session_id in session_ids:
                await self._emit(
                    "Target.detachedFromTarget",
                    {"sessionId": session_id, "targetId": target_id},
                )
            await self._emit("Target.targetDestroyed", {"targetId": target_id})

    async def _ensure_attached(
        self,
        tab: dict,
        opener_id: str | None = None,
        generation: int | None = None,
    ) -> tuple[str, bool] | None:
        tab_id = tab.get("tabId")
        if type(tab_id) is not int:
            raise BrowserExtensionError("Scoped tab id is invalid")
        if generation is None:
            generation = self._active_scope_generation(tab_id)
        if generation is None:
            return None
        lock = self._attach_locks.setdefault(tab_id, asyncio.Lock())
        async with lock:
            if not self._scope_is_current(tab_id, generation):
                return None
            is_new = tab_id not in self._attached_tabs
            if is_new:
                debugger_attached = False
                attachment_committed = False
                cleanup_suppress_interrupts = True
                try:
                    try:
                        await self._relay.request("debugger.attach", {"tabId": tab_id})
                        debugger_attached = True
                    except ExtensionRequestError as exc:
                        if exc.code != "CDP_ERROR" or "already attached" not in exc.message.lower():
                            raise
                        debugger_attached = True
                    real_target_id = await self._fetch_main_frame_id(tab_id)
                    if not self._scope_is_current(tab_id, generation):
                        cleanup_suppress_interrupts = False
                        return None
                    target_id = self._register_tab(tab, opener_id, real_target_id)
                    self._attached_tabs.add(tab_id)
                    attachment_committed = True
                finally:
                    if debugger_attached and not attachment_committed:
                        await self._discard_failed_attachment_safely(
                            tab_id,
                            suppress_interrupts=cleanup_suppress_interrupts,
                        )
                return target_id, is_new
            target_id = self._register_tab(tab, opener_id)
            self._attached_tabs.add(tab_id)
        return target_id, is_new

    async def _discard_failed_attachment_safely(
        self,
        tab_id: int,
        *,
        suppress_interrupts: bool,
    ) -> None:
        try:
            await asyncio.shield(self._discard_failed_attachment(tab_id))
        except Exception:
            pass
        except BaseException:
            if suppress_interrupts:
                pass
            else:
                raise

    async def _fetch_main_frame_id(self, tab_id: int) -> str | None:
        # Playwright resolves the main frame's session by targetId, so the exposed
        # page targetId must equal Chrome's real main-frame id for this tab.
        tree = await self._relay.request(
            "debugger.send",
            {"tabId": tab_id, "method": "Page.getFrameTree", "params": {}},
        )
        frame = tree.get("result", {}).get("frameTree", {}).get("frame", {})
        frame_id = frame.get("id") if isinstance(frame, dict) else None
        return frame_id if isinstance(frame_id, str) and frame_id else None

    async def _discard_failed_attachment(self, tab_id: int) -> None:
        try:
            await self._relay.request("debugger.detach", {"tabId": tab_id}, timeout=2.0)
        except BrowserExtensionError:
            pass
        self._forget_tab(tab_id)

    def _register_tab(self, tab: dict, opener_id: str | None = None, target_id_override: str | None = None) -> str:
        tab_id = tab["tabId"]
        raw_url = tab.get("url")
        url = raw_url if isinstance(raw_url, str) else "about:blank"
        raw_title = tab.get("title")
        title = raw_title if isinstance(raw_title, str) else ""
        try:
            target_id = self._registry.target_id_for_tab(tab_id)
            if target_id_override is not None and target_id != target_id_override:
                target_id = self._registry.register_tab(tab_id, url, title, target_id_override)
            else:
                self._registry.update_tab(tab_id, url, title)
        except KeyError:
            target_id = self._registry.register_tab(tab_id, url, title, target_id_override)
        if opener_id is not None:
            self._opener_ids[tab_id] = opener_id
        return target_id

    def _register_scoped_tabs(self) -> None:
        for tab in self._relay.scoped_tabs:
            if isinstance(tab, dict) and type(tab.get("tabId")) is int:
                tab_id = tab["tabId"]
                if self._active_scope_generation(tab_id) is not None:
                    self._register_tab(tab)

    def _target_info(self, tab_id: int) -> dict:
        target_info = self._registry.target_info_for_tab(tab_id)
        opener_id = self._opener_ids.get(tab_id)
        if opener_id is not None:
            target_info["openerId"] = opener_id
        return target_info

    def _page_target_infos(self) -> list[dict]:
        return [
            self._target_info(self._registry.tab_for_target(info["targetId"]))
            for info in self._registry.list_page_targets()
        ]

    async def _emit_attached(self, tab_id: int, target_id: str, generation: int) -> None:
        if not self._scope_is_current(tab_id, generation):
            return
        await self._emit(
            "Target.attachedToTarget",
            {
                "sessionId": self._registry.root_session_id(tab_id),
                "targetInfo": self._target_info(tab_id),
                "waitingForDebugger": False,
            },
            scope_guard=(tab_id, generation),
        )

    def _update_main_frame(self, tab_id: int, params: dict) -> None:
        frame = params.get("frame")
        if not isinstance(frame, dict) or frame.get("parentId") is not None:
            return
        url = frame.get("url")
        if not isinstance(url, str):
            return
        try:
            current = self._target_info(tab_id)
        except KeyError:
            return
        raw_title = frame.get("title")
        title = raw_title if isinstance(raw_title, str) else str(current["title"])
        self._registry.update_tab(tab_id, url, title)

    async def _detach_all_tabs(self) -> None:
        tab_ids = {self._registry.tab_for_target(info["targetId"]) for info in self._registry.list_page_targets()}
        tab_ids.update(tab["tabId"] for tab in self._relay.scoped_tabs if type(tab.get("tabId")) is int)
        for tab_id in sorted(tab_ids):
            try:
                await self._relay.release_tab(tab_id)
            except Exception as exc:
                LOG.debug("browser_extension_tab_release_failed", method="lease.release", error_type=type(exc).__name__)

    async def _shutdown_client(self, ws: web.WebSocketResponse) -> None:
        await self._detach_all_tabs()
        async with self._client_guard:
            if self._client_ws is ws:
                self._client_ws = None
            self._closing_client_websockets.discard(ws)
        self._reset_connection_state()
        await ws.close()

    def _forget_tab(self, tab_id: int) -> None:
        self._attached_tabs.discard(tab_id)
        self._opener_ids.pop(tab_id, None)
        self._registry.remove_tab(tab_id)

    def _begin_tab_scope(self, tab_id: int) -> int:
        generation = self._scope_generations.get(tab_id, 0) + 1
        self._scope_generations[tab_id] = generation
        self._scope_tombstones.discard(tab_id)
        return generation

    def _revoke_tab_scope(self, tab_id: int) -> int:
        generation = self._scope_generations.get(tab_id, 0) + 1
        self._scope_generations[tab_id] = generation
        self._scope_tombstones.add(tab_id)
        return generation

    def _active_scope_generation(self, tab_id: int) -> int | None:
        if tab_id in self._scope_tombstones:
            return None
        generation = self._scope_generations.get(tab_id)
        if generation is None:
            generation = self._begin_tab_scope(tab_id)
        return generation

    def _scope_is_current(self, tab_id: int, generation: int) -> bool:
        return self._scope_generations.get(tab_id) == generation and tab_id not in self._scope_tombstones

    def _reset_connection_state(self) -> None:
        current_task = asyncio.current_task()
        for task in self._background_tasks:
            if task is not current_task:
                task.cancel()
        self._auto_attach = False
        self._discover_targets = False
        self._attached_tabs.clear()
        self._attach_locks.clear()
        self._opener_ids.clear()
        self._scope_generations.clear()
        self._scope_tombstones.clear()
        self._closing_client_websockets.clear()
        self._pending_child_sessions.clear()
        self._pending_child_events.clear()
        self._registry.clear()

    def _spawn(self, coroutine: Coroutine[object, object, None]) -> None:
        task = asyncio.create_task(coroutine)
        self._background_tasks.add(task)
        task.add_done_callback(self._background_task_done)

    def _spawn_client_task(self, ws: web.WebSocketResponse, coroutine: Coroutine[object, object, None]) -> None:
        task = asyncio.create_task(coroutine)
        self._client_tasks.setdefault(ws, set()).add(task)
        task.add_done_callback(partial(self._client_task_done, ws))

    async def _cancel_client_tasks(self, ws: web.WebSocketResponse) -> None:
        tasks = list(self._client_tasks.pop(ws, set()))
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)

    def _client_task_done(self, ws: web.WebSocketResponse, task: asyncio.Task[None]) -> None:
        tasks = self._client_tasks.get(ws)
        if tasks is not None:
            tasks.discard(task)
            if not tasks:
                self._client_tasks.pop(ws, None)
        if task.cancelled():
            return
        error = task.exception()
        if error is not None:
            LOG.debug("browser_extension_client_task_failed", error_type=type(error).__name__)

    def _background_task_done(self, task: asyncio.Task[None]) -> None:
        self._background_tasks.discard(task)
        if task.cancelled():
            return
        error = task.exception()
        if error is not None:
            LOG.debug("browser_extension_event_task_failed", error_type=type(error).__name__)

    async def _reply(
        self,
        ws: web.WebSocketResponse,
        request_id: object,
        result: dict,
        session_id: str | None = None,
    ) -> None:
        payload = {"id": request_id, "result": result}
        if session_id is not None:
            payload["sessionId"] = session_id
        await self._send(ws, payload)

    async def _send_error(
        self,
        ws: web.WebSocketResponse,
        request_id: object,
        code: int,
        message: str,
        session_id: str | None = None,
    ) -> None:
        payload = {"id": request_id, "error": {"code": code, "message": message}}
        if session_id is not None:
            payload["sessionId"] = session_id
        await self._send(ws, payload)

    async def _emit_to_sessions(self, method: str, params: dict, session_ids: list[str]) -> None:
        for session_id in session_ids:
            await self._emit(method, params, session_id)

    async def _emit(
        self,
        method: str,
        params: dict,
        session_id: str | None = None,
        scope_guard: tuple[int, int] | None = None,
    ) -> None:
        ws = self._client_ws
        if ws is None or ws.closed:
            return
        payload = {"method": method, "params": params}
        if session_id is not None:
            payload["sessionId"] = session_id
        await self._send(ws, payload, scope_guard)

    async def _send(
        self,
        ws: web.WebSocketResponse,
        payload: dict,
        scope_guard: tuple[int, int] | None = None,
    ) -> None:
        async with self._send_lock:
            if scope_guard is not None and not self._scope_is_current(*scope_guard):
                return
            if not ws.closed:
                await ws.send_json(payload)
