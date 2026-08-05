from __future__ import annotations

import asyncio
import json
import secrets
from collections.abc import Coroutine
from typing import TYPE_CHECKING

import structlog
from aiohttp import WSMsgType, web

from skyvern.browser_extension.errors import BrowserExtensionError, ExtensionRequestError
from skyvern.browser_extension.protocol import is_cdp_method_allowed
from skyvern.browser_extension.relay import _MAX_WS_MESSAGE_BYTES
from skyvern.browser_extension.target_registry import VirtualTargetRegistry

if TYPE_CHECKING:
    from skyvern.browser_extension.relay import ExtensionRelayServer


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
}


class ExtensionCdpAdapter:
    def __init__(self, registry: VirtualTargetRegistry, relay: ExtensionRelayServer) -> None:
        self._registry = registry
        self._relay = relay
        self._capability = secrets.token_urlsafe(32)
        self._runner: web.AppRunner | None = None
        self._site: web.TCPSite | None = None
        self._port: int | None = None
        self._client_ws: web.WebSocketResponse | None = None
        self._client_guard = asyncio.Lock()
        self._send_lock = asyncio.Lock()
        self._auto_attach = False
        self._discover_targets = False
        self._attached_tabs: set[int] = set()
        self._attach_locks: dict[int, asyncio.Lock] = {}
        self._opener_ids: dict[int, str] = {}
        self._scope_generations: dict[int, int] = {}
        self._scope_tombstones: set[int] = set()
        self._background_tasks: set[asyncio.Task[None]] = set()

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
                    await self._handle_client_text(ws, message.data)
                elif message.type == WSMsgType.ERROR:
                    break
                elif message.type == WSMsgType.BINARY:
                    await ws.close(code=1003, message=b"text frames required")
                    break
        finally:
            async with self._client_guard:
                if self._client_ws is ws:
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
        except ExtensionRequestError as exc:
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
        args = {"tabId": tab_id, "method": method, "params": params}
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
        if method == "Browser.getVersion":
            await self._reply(ws, request_id, dict(_VERSION_RESULT), response_session_id)
        elif method == "Browser.setDownloadBehavior":
            await self._reply(ws, request_id, {}, response_session_id)
        elif method == "Browser.close":
            await self._reply(ws, request_id, {}, response_session_id)
            await self._detach_all_tabs()
            async with self._client_guard:
                if self._client_ws is ws:
                    self._client_ws = None
            self._reset_connection_state()
            await ws.close()
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
        elif method == "Target.setAutoAttach":
            await self._set_auto_attach(ws, request_id, params, response_session_id)
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
        self._auto_attach = params.get("autoAttach") is True
        await self._reply(ws, request_id, {}, response_session_id)
        if not self._auto_attach:
            return
        tabs = [tab for tab in self._relay.scoped_tabs if isinstance(tab, dict)]
        if not tabs:
            try:
                created = await self._relay.request("tabs.create", {"url": "about:blank"})
                tab_id = created["tabId"]
                if type(tab_id) is not int:
                    raise BrowserExtensionError("Created tab id is invalid")
                tabs = [{"tabId": tab_id, "url": "about:blank", "title": ""}]
                self._begin_tab_scope(tab_id)
            except Exception as exc:
                LOG.debug(
                    "browser_extension_auto_attach_target_skipped",
                    error_type=type(exc).__name__,
                )
                return
        for tab in tabs:
            tab_id = tab.get("tabId")
            if type(tab_id) is not int:
                continue
            generation = self._active_scope_generation(tab_id)
            if generation is None:
                continue
            try:
                attached = await self._ensure_attached(tab, generation=generation)
                if attached is not None:
                    target_id, is_new = attached
                    if is_new:
                        await self._emit_attached(tab_id, target_id, generation)
            except Exception as exc:
                LOG.debug(
                    "browser_extension_auto_attach_target_skipped",
                    tab_id=tab_id,
                    error_type=type(exc).__name__,
                )

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

    async def _handle_debugger_event(self, payload: dict) -> None:
        tab_id = payload.get("tabId")
        method = payload.get("method")
        event_params = payload.get("params")
        if type(tab_id) is not int or not isinstance(method, str) or not isinstance(event_params, dict):
            return
        try:
            if isinstance(payload.get("sessionId"), str):
                outer_session_ids = [payload["sessionId"]]
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
                self._registry.register_child_session(tab_id, child_session_id, target_info)
                await self._emit_to_sessions(method, event_params, outer_session_ids)
                if self._auto_attach:
                    self._spawn(self._set_child_auto_attach(tab_id, child_session_id))
                    await asyncio.sleep(0)
                return
        elif method == "Target.detachedFromTarget":
            child_session_id = event_params.get("sessionId")
            if not isinstance(child_session_id, str):
                return
            try:
                self._registry.resolve_session(child_session_id)
            except KeyError:
                return
            await self._emit_to_sessions(method, event_params, outer_session_ids)
            self._registry.remove_child_session(child_session_id)
            return
        await self._emit_to_sessions(method, event_params, outer_session_ids)

    async def _set_child_auto_attach(self, tab_id: int, child_session_id: str) -> None:
        try:
            await self._relay.request(
                "debugger.send",
                {
                    "tabId": tab_id,
                    "sessionId": child_session_id,
                    "method": "Target.setAutoAttach",
                    "params": dict(_CHILD_AUTO_ATTACH_PARAMS),
                },
            )
        except ExtensionRequestError as exc:
            LOG.debug(
                "browser_extension_child_auto_attach_failed",
                method="Target.setAutoAttach",
                error_code=exc.code,
            )

    async def _handle_hello_tabs(self, tabs: list[tuple[dict, int]]) -> None:
        for tab, generation in tabs:
            await self._handle_tab_added(tab, include_opener=False, generation=generation)

    async def _handle_tab_added(self, params: dict, include_opener: bool, generation: int) -> None:
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
            real_target_id: str | None = None
            if is_new:
                try:
                    await self._relay.request("debugger.attach", {"tabId": tab_id})
                except ExtensionRequestError as exc:
                    if "already attached" not in exc.message.lower():
                        raise
                real_target_id = await self._fetch_main_frame_id(tab_id)
            if not self._scope_is_current(tab_id, generation):
                return None
            target_id = self._register_tab(tab, opener_id, real_target_id)
            self._attached_tabs.add(tab_id)
        return target_id, is_new

    async def _fetch_main_frame_id(self, tab_id: int) -> str | None:
        # Playwright resolves the main frame's session by targetId, so the exposed
        # page targetId must equal Chrome's real main-frame id for this tab.
        try:
            tree = await self._relay.request(
                "debugger.send",
                {"tabId": tab_id, "method": "Page.getFrameTree", "params": {}},
            )
        except ExtensionRequestError:
            return None
        frame = tree.get("result", {}).get("frameTree", {}).get("frame", {})
        frame_id = frame.get("id") if isinstance(frame, dict) else None
        return frame_id if isinstance(frame_id, str) and frame_id else None

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
        tab_ids = [self._registry.tab_for_target(info["targetId"]) for info in self._registry.list_page_targets()]
        for tab_id in tab_ids:
            try:
                await self._relay.request("debugger.detach", {"tabId": tab_id}, timeout=2.0)
            except Exception as exc:
                LOG.debug(
                    "browser_extension_debugger_detach_failed", method="debugger.detach", error_type=type(exc).__name__
                )

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
        for task in self._background_tasks:
            task.cancel()
        self._auto_attach = False
        self._discover_targets = False
        self._attached_tabs.clear()
        self._attach_locks.clear()
        self._opener_ids.clear()
        self._scope_generations.clear()
        self._scope_tombstones.clear()
        self._registry.clear()

    def _spawn(self, coroutine: Coroutine[object, object, None]) -> None:
        task = asyncio.create_task(coroutine)
        self._background_tasks.add(task)
        task.add_done_callback(self._background_task_done)

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
