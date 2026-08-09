from __future__ import annotations

import asyncio
import itertools
import os
import time
from collections.abc import Awaitable, Callable, Coroutine
from contextlib import suppress
from pathlib import Path

import structlog
from aiohttp import WSMsgType, web

from skyvern.browser_extension.broker.leases import LeaseTable
from skyvern.browser_extension.broker.protocol import (
    BROKER_FRAME_VERSION,
    BROKER_HEALTH_PATH,
    BROKER_PROTOCOL_VERSION,
    BROKER_WS_PATH,
    EXTENSION_NOT_CONNECTED_CODE,
    PAIRING_NONCE_OP,
    STATUS_OP,
    BrokerFrame,
    build_broker_challenge,
    build_error_frame,
    build_event_frame,
    build_response_frame,
    build_state_frame,
    compute_broker_server_proof,
    is_valid_broker_nonce,
    parse_broker_frame,
    verify_broker_client_proof,
)
from skyvern.browser_extension.errors import (
    BrowserExtensionError,
    BrowserExtensionNotConnectedError,
    ExtensionRequestError,
)
from skyvern.browser_extension.protocol import ALLOWED_OPS
from skyvern.browser_extension.relay import _MAX_WS_MESSAGE_BYTES, ExtensionRelayServer

LOG = structlog.get_logger(__name__)

_AUTH_TIMEOUT_SECONDS = 10.0
_HELLO_TIMEOUT_SECONDS = 10.0
_MAINTENANCE_INTERVAL_SECONDS = 5.0
_MAX_REQUEST_TIMEOUT_SECONDS = 600.0
_AUTH_CLOSE_CODE = 4403
_STEP_DOWN_CLOSE_CODE = 4001

RelayFactory = Callable[
    [str, int, Callable[[str, dict], Awaitable[None]], Callable[[], Awaitable[None]] | None],
    ExtensionRelayServer,
]


class _ClientConnection:
    def __init__(self, client_id: str, websocket: web.WebSocketResponse) -> None:
        self.client_id = client_id
        self.websocket = websocket
        self.protocol = BROKER_PROTOCOL_VERSION
        self.pid = 0
        self.tasks: set[asyncio.Task[None]] = set()
        self._send_lock = asyncio.Lock()

    def spawn(self, coroutine: Coroutine[None, None, None]) -> None:
        task = asyncio.create_task(coroutine)
        self.tasks.add(task)
        task.add_done_callback(self.tasks.discard)

    async def cancel_tasks(self) -> None:
        tasks = list(self.tasks)
        self.tasks.clear()
        for task in tasks:
            task.cancel()
        for task in tasks:
            with suppress(asyncio.CancelledError, Exception):
                await task

    async def send(self, frame: dict) -> bool:
        if self.websocket.closed:
            return False
        async with self._send_lock:
            try:
                await self.websocket.send_json(frame)
            except (ConnectionError, RuntimeError, ValueError):
                return False
        return True


class BrokerServer:
    """Singleton owner of the extension bridge port, fronting many local MCP clients.

    The Chrome extension still dials one socket; every `skyvern run mcp --browser-extension`
    process attaches as an authenticated loopback client and gets its own leased slice of the
    shared tabs.
    """

    def __init__(
        self,
        token: str,
        port: int,
        *,
        idle_timeout_seconds: float = 300.0,
        relay_factory: RelayFactory = ExtensionRelayServer,
    ) -> None:
        self._token = token
        self._idle_timeout_seconds = idle_timeout_seconds
        self._leases = LeaseTable()
        self._clients: dict[str, _ClientConnection] = {}
        self._client_ids = itertools.count(1)
        self._relay = relay_factory(token, port, self._handle_extension_event, self._handle_extension_disconnect)
        self._relay.add_route(BROKER_WS_PATH, self._handle_client_websocket)
        self._relay.add_route(BROKER_HEALTH_PATH, self._handle_health)
        self._lease_lock = asyncio.Lock()
        self._tab_creates_in_flight = 0
        self._deferred_tab_added: dict[int, dict] = {}
        self._deferred_tab_created: dict[int, dict] = {}
        self._discarded_tab_ids: set[int] = set()
        self._extension_generation = 0
        self._maintenance_task: asyncio.Task[None] | None = None
        self._shutdown_event = asyncio.Event()
        self._shutdown_reason = "stopped"
        self._idle_since: float | None = None
        self._started = False

    @property
    def bound_port(self) -> int:
        return self._relay.bound_port

    @property
    def client_count(self) -> int:
        return len(self._clients)

    @property
    def shutdown_reason(self) -> str:
        return self._shutdown_reason

    async def start(self) -> None:
        await self._relay.start()
        self._started = True
        self._idle_since = time.monotonic()
        self._maintenance_task = asyncio.create_task(self._run_maintenance())
        LOG.info(
            "browser_extension_broker_started",
            port=self._relay.bound_port,
            protocol=BROKER_PROTOCOL_VERSION,
            pid=os.getpid(),
        )

    async def stop(self) -> None:
        if not self._started:
            return
        self._started = False
        task = self._maintenance_task
        self._maintenance_task = None
        if task is not None:
            task.cancel()
            with suppress(asyncio.CancelledError):
                await task
        for client in list(self._clients.values()):
            if not client.websocket.closed:
                await client.websocket.close(code=1001, message=b"broker stopped")
        await self._relay.stop()
        LOG.info("browser_extension_broker_stopped", reason=self._shutdown_reason)

    async def wait_for_shutdown(self) -> str:
        await self._shutdown_event.wait()
        return self._shutdown_reason

    def request_shutdown(self, reason: str) -> None:
        if not self._shutdown_event.is_set():
            self._shutdown_reason = reason
            self._shutdown_event.set()

    async def _handle_health(self, _request: web.Request) -> web.Response:
        # Deliberately inert: it tells a caller a broker owns this port and nothing else. No token,
        # nonce, pid, client count, or tab data — the same posture as a bare GET /pair.
        return web.json_response(
            {"v": BROKER_FRAME_VERSION, "broker": True, "protocol": BROKER_PROTOCOL_VERSION},
            headers={"Cache-Control": "no-store", "X-Content-Type-Options": "nosniff"},
        )

    async def _handle_client_websocket(self, request: web.Request) -> web.WebSocketResponse:
        # The heartbeat is what releases a SIGKILLed agent's tab leases.
        websocket = web.WebSocketResponse(max_msg_size=_MAX_WS_MESSAGE_BYTES, heartbeat=20.0)
        # Browsers always attach an Origin to a WebSocket handshake, so requiring its absence keeps
        # a malicious page from reaching the broker even though WebSockets bypass CORS.
        if request.headers.get("Origin") is not None:
            await websocket.prepare(request)
            LOG.info("browser_extension_broker_client_rejected", reason="origin_present")
            await websocket.close(code=_AUTH_CLOSE_CODE, message=b"authentication failed")
            return websocket

        await websocket.prepare(request)
        client = await self._authenticate(websocket)
        if client is None:
            return websocket

        await self._register_client(client)
        try:
            async for message in websocket:
                if message.type is WSMsgType.TEXT:
                    if not await self._handle_client_text(client, message.data):
                        break
                elif message.type in {WSMsgType.CLOSE, WSMsgType.CLOSED, WSMsgType.ERROR}:
                    break
                else:
                    await websocket.close(code=1003, message=b"text frames required")
                    break
        finally:
            await client.cancel_tasks()
            await self._unregister_client(client)
        return websocket

    async def _authenticate(self, websocket: web.WebSocketResponse) -> _ClientConnection | None:
        server_nonce, challenge = build_broker_challenge()
        await websocket.send_json(challenge)
        proof = await self._receive_frame(websocket, _AUTH_TIMEOUT_SECONDS)
        if proof is None or proof.kind != "auth.proof" or proof.client_nonce is None or proof.proof is None:
            await self._reject(websocket, "bad_payload")
            return None
        if not is_valid_broker_nonce(proof.client_nonce):
            await self._reject(websocket, "bad_nonce")
            return None
        if not verify_broker_client_proof(self._token, server_nonce, proof.client_nonce, proof.proof):
            await self._reject(websocket, "bad_proof")
            return None

        await websocket.send_json(
            {
                "v": BROKER_FRAME_VERSION,
                "type": "auth.ok",
                "serverProof": compute_broker_server_proof(self._token, proof.client_nonce, server_nonce),
                "protocol": BROKER_PROTOCOL_VERSION,
                "pid": os.getpid(),
                "root": str(Path(__file__).resolve().parents[3]),
            }
        )

        hello = await self._receive_frame(websocket, _HELLO_TIMEOUT_SECONDS)
        if hello is None:
            await self._reject(websocket, "bad_payload")
            return None
        if hello.kind == "broker.stepDown":
            await self._handle_step_down(websocket, hello)
            return None
        if hello.kind != "client.hello" or hello.protocol is None or hello.pid is None:
            await self._reject(websocket, "bad_payload")
            return None

        client = _ClientConnection(f"c-{next(self._client_ids)}", websocket)
        client.protocol = hello.protocol
        client.pid = hello.pid
        return client

    async def _receive_frame(self, websocket: web.WebSocketResponse, timeout: float) -> BrokerFrame | None:
        try:
            message = await websocket.receive(timeout=timeout)
        except TimeoutError:
            return None
        if message.type is not WSMsgType.TEXT:
            return None
        try:
            return parse_broker_frame(message.data, from_client=True)
        except BrowserExtensionError:
            return None

    async def _reject(self, websocket: web.WebSocketResponse, reason: str) -> None:
        LOG.info("browser_extension_broker_client_rejected", reason=reason)
        await websocket.close(code=_AUTH_CLOSE_CODE, message=b"authentication failed")

    async def _handle_step_down(self, websocket: web.WebSocketResponse, frame: BrokerFrame) -> None:
        requested = frame.protocol or 0
        if requested <= BROKER_PROTOCOL_VERSION:
            LOG.info(
                "browser_extension_broker_step_down_declined",
                requested_protocol=requested,
                protocol=BROKER_PROTOCOL_VERSION,
            )
            await websocket.close(code=_STEP_DOWN_CLOSE_CODE, message=b"step down declined")
            return
        LOG.info(
            "browser_extension_broker_step_down_accepted",
            requested_protocol=requested,
            protocol=BROKER_PROTOCOL_VERSION,
        )
        await websocket.close(code=_STEP_DOWN_CLOSE_CODE, message=b"stepping down")
        self.request_shutdown("version_skew")

    async def _register_client(self, client: _ClientConnection) -> None:
        self._clients[client.client_id] = client
        self._idle_since = None
        async with self._lease_lock:
            self._leases.add_client(client.client_id)
            visible = self._leases.visible_tabs(client.client_id)
        LOG.info(
            "browser_extension_broker_client_connected",
            client_id=client.client_id,
            client_pid=client.pid,
            client_protocol=client.protocol,
            clients=len(self._clients),
        )
        await client.send(build_state_frame(self._relay.connected, visible))
        await self._rotate_offers()

    async def _unregister_client(self, client: _ClientConnection) -> None:
        if self._clients.pop(client.client_id, None) is None:
            return
        async with self._lease_lock:
            leased, offered = self._leases.remove_client(client.client_id)
        if not self._clients:
            self._idle_since = time.monotonic()
        LOG.info(
            "browser_extension_broker_client_disconnected",
            client_id=client.client_id,
            released_leases=len(leased),
            released_offers=len(offered),
            clients=len(self._clients),
        )
        await self._detach_released_tabs(leased)
        await self._rotate_offers()

    async def _detach_released_tabs(self, tab_ids: list[int]) -> None:
        for tab_id in tab_ids:
            try:
                await self._relay.request("debugger.detach", {"tabId": tab_id}, timeout=2.0)
            except Exception as exc:
                LOG.debug(
                    "browser_extension_broker_release_detach_failed",
                    tab_id=tab_id,
                    error_type=type(exc).__name__,
                )

    async def _handle_client_text(self, client: _ClientConnection, raw: str) -> bool:
        try:
            frame = parse_broker_frame(raw, from_client=True)
        except BrowserExtensionError:
            LOG.warning("browser_extension_broker_invalid_frame", client_id=client.client_id)
            return True

        if frame.kind == "ping":
            await client.send({"v": BROKER_FRAME_VERSION, "type": "pong"})
            return True
        if frame.kind == "pong":
            return True
        if frame.kind == "broker.stepDown":
            await self._handle_step_down(client.websocket, frame)
            return False
        if frame.kind == "request" and frame.request_id is not None and frame.op is not None:
            client.spawn(self._run_request(client, frame))
            return True
        return True

    async def _run_request(self, client: _ClientConnection, frame: BrokerFrame) -> None:
        request_id = frame.request_id or ""
        op = frame.op or ""
        args = frame.args or {}
        timeout = min(frame.timeout_seconds or 30.0, _MAX_REQUEST_TIMEOUT_SECONDS)
        try:
            result = await self._dispatch(client, op, args, timeout)
        except ExtensionRequestError as exc:
            await client.send(build_error_frame(request_id, exc.code, exc.message))
        except BrowserExtensionNotConnectedError:
            await client.send(
                build_error_frame(
                    request_id, EXTENSION_NOT_CONNECTED_CODE, "Skyvern browser extension is not connected"
                )
            )
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            LOG.exception("browser_extension_broker_request_failed", client_id=client.client_id, op=op)
            await client.send(build_error_frame(request_id, "INTERNAL", f"broker request failed: {type(exc).__name__}"))
        else:
            await client.send(build_response_frame(request_id, result))

    async def _dispatch(self, client: _ClientConnection, op: str, args: dict, timeout: float) -> dict:
        if op == PAIRING_NONCE_OP:
            LOG.info("browser_extension_broker_pairing_nonce_issued", client_id=client.client_id)
            return {"nonce": self._relay.create_pairing_nonce(), "port": self._relay.bound_port}
        if op == STATUS_OP:
            return {
                "extensionConnected": self._relay.connected,
                "clients": len(self._clients),
                "protocol": BROKER_PROTOCOL_VERSION,
                "pid": os.getpid(),
            }
        if op not in ALLOWED_OPS:
            raise ExtensionRequestError("OP_NOT_ALLOWED", f"Operation is not allowed: {op}")

        if op == "tabs.create":
            async with self._lease_lock:
                self._tab_creates_in_flight += 1
                extension_generation = self._extension_generation
            try:
                result = await self._relay.request(op, args, timeout)
                tab_id = result.get("tabId")
                deferred_event = None
                deferred_created_events: list[dict] = []
                granted = False
                if type(tab_id) is int:
                    url = args.get("url")
                    async with self._lease_lock:
                        deferred_event = self._deferred_tab_added.pop(tab_id, None)
                        deferred_created_events = self._pop_deferred_descendants(tab_id)
                        discarded = (
                            tab_id in self._discarded_tab_ids or extension_generation != self._extension_generation
                        )
                        self._discarded_tab_ids.discard(tab_id)
                        if not discarded:
                            self._leases.grant(
                                tab_id,
                                client.client_id,
                                _tab_snapshot(deferred_event)
                                if deferred_event is not None
                                else {
                                    "tabId": tab_id,
                                    "url": url if isinstance(url, str) else "about:blank",
                                    "title": "",
                                },
                            )
                            for event in deferred_created_events:
                                self._leases.grant(event["tabId"], client.client_id, _tab_snapshot(event))
                            granted = True
                if granted:
                    if deferred_event is not None:
                        await self._send_to(client.client_id, build_event_frame("scope.tabAdded", deferred_event))
                    for event in deferred_created_events:
                        await self._send_to(client.client_id, build_event_frame("tabs.created", event))
                return result
            finally:
                async with self._lease_lock:
                    self._tab_creates_in_flight -= 1
                    deferred_tabs = []
                    if self._tab_creates_in_flight == 0:
                        deferred_tabs = [
                            *self._deferred_tab_added.values(),
                            *self._deferred_tab_created.values(),
                        ]
                        self._deferred_tab_added.clear()
                        self._deferred_tab_created.clear()
                        self._discarded_tab_ids.clear()
                        for deferred_tab in deferred_tabs:
                            self._leases.register_tab(_tab_snapshot(deferred_tab))
                if deferred_tabs:
                    await self._rotate_offers()

        if op == "tabs.list":
            result = await self._relay.request(op, args, timeout)
            async with self._lease_lock:
                visible = {tab["tabId"] for tab in self._leases.visible_tabs(client.client_id)}
            tabs = result.get("tabs")
            if isinstance(tabs, list):
                return {
                    **result,
                    "tabs": [tab for tab in tabs if isinstance(tab, dict) and tab.get("tabId") in visible],
                }
            return result

        tab_id = args.get("tabId")
        if type(tab_id) is float and tab_id.is_integer():
            tab_id = int(tab_id)
        if type(tab_id) is not int:
            raise ExtensionRequestError("TAB_NOT_SCOPED", "Another Skyvern agent controls this tab.")
        async with self._lease_lock:
            claimed = self._leases.claim(tab_id, client.client_id)
        if not claimed:
            raise ExtensionRequestError("TAB_NOT_SCOPED", "Another Skyvern agent controls this tab.")
        return await self._relay.request(op, {**args, "tabId": tab_id}, timeout)

    async def _handle_extension_event(self, event: str, params: dict) -> None:
        if event == "extension.hello":
            await self._handle_extension_hello(params)
            return
        if event == "scope.tabAdded":
            tab_id = params.get("tabId")
            if type(tab_id) is not int:
                return
            async with self._lease_lock:
                owner = self._leases.owner(tab_id)
                if owner is None and self._tab_creates_in_flight:
                    if tab_id in self._discarded_tab_ids:
                        return
                    self._deferred_tab_added[tab_id] = dict(params)
                    return
                self._leases.register_tab(_tab_snapshot(params))
            # A tab an agent just opened is already leased to it; only a hand-shared tab is
            # unowned and needs an offer.
            if owner is not None:
                await self._send_to(owner, build_event_frame(event, params))
                return
            await self._rotate_offers()
            return
        if event == "tabs.created":
            await self._handle_tab_created(params)
            return
        if event in {"scope.tabRemoved", "debugger.detached"}:
            tab_id = params.get("tabId")
            if type(tab_id) is not int:
                return
            async with self._lease_lock:
                deferred = self._deferred_tab_added.pop(tab_id, None)
                deferred_created = self._deferred_tab_created.pop(tab_id, None)
                deferred_descendants = self._pop_deferred_descendants(tab_id)
                if deferred is not None or deferred_created is not None or deferred_descendants:
                    self._discarded_tab_ids.add(tab_id)
                owner = self._leases.forget_tab(tab_id)
            if owner is not None:
                await self._send_to(owner, build_event_frame(event, params))
            await self._rotate_offers()
            return
        if event == "debugger.event":
            tab_id = params.get("tabId")
            if type(tab_id) is not int:
                return
            async with self._lease_lock:
                owner = self._leases.lessee(tab_id)
            if owner is not None:
                await self._send_to(owner, build_event_frame(event, params))

    async def _handle_extension_hello(self, params: dict) -> None:
        tabs = params.get("scopedTabs")
        candidates = tabs if isinstance(tabs, list) else []
        snapshots = [tab for tab in candidates if isinstance(tab, dict) and type(tab.get("tabId")) is int]
        async with self._lease_lock:
            self._deferred_tab_added.clear()
            self._deferred_tab_created.clear()
            self._discarded_tab_ids.clear()
            self._leases.reset(snapshots)
            per_client = {client_id: self._leases.visible_tabs(client_id) for client_id in list(self._clients)}
        for client_id, visible in per_client.items():
            await self._send_to(client_id, build_event_frame("extension.hello", {**params, "scopedTabs": visible}))
        await self._rotate_offers()

    async def _handle_tab_created(self, params: dict) -> None:
        tab_id = params.get("tabId")
        if type(tab_id) is not int:
            return
        opener_tab_id = params.get("openerTabId")
        async with self._lease_lock:
            owner = self._leases.lessee(opener_tab_id) if type(opener_tab_id) is int else None
            if owner is not None:
                self._leases.grant(tab_id, owner, _tab_snapshot(params))
            elif type(opener_tab_id) is int and self._tab_creates_in_flight:
                self._deferred_tab_created[tab_id] = dict(params)
                return
            else:
                self._leases.register_tab(_tab_snapshot(params))
        if owner is not None:
            await self._send_to(owner, build_event_frame("tabs.created", params))
            return
        await self._rotate_offers()

    async def _handle_extension_disconnect(self) -> None:
        async with self._lease_lock:
            self._extension_generation += 1
            self._deferred_tab_added.clear()
            self._deferred_tab_created.clear()
            self._discarded_tab_ids.clear()
            self._leases.reset([])
        LOG.info("browser_extension_broker_extension_disconnected", clients=len(self._clients))
        for client_id in list(self._clients):
            await self._send_to(client_id, build_state_frame(False, []))

    def _pop_deferred_descendants(self, opener_tab_id: int) -> list[dict]:
        descendants: list[dict] = []
        pending_openers = [opener_tab_id]
        while pending_openers:
            current_opener = pending_openers.pop()
            children = [
                event for event in self._deferred_tab_created.values() if event.get("openerTabId") == current_opener
            ]
            for child in children:
                child_id = child["tabId"]
                self._deferred_tab_created.pop(child_id)
                pending_openers.append(child_id)
            descendants.extend(children)
        return descendants

    async def _rotate_offers(self) -> None:
        async with self._lease_lock:
            granted, revoked = self._leases.rotate(time.monotonic())
        for change in revoked:
            await self._send_to(
                change.client_id,
                build_event_frame("scope.tabRemoved", {"tabId": change.tab["tabId"], "reason": "unshared"}),
            )
        for change in granted:
            await self._send_to(change.client_id, build_event_frame("scope.tabAdded", change.tab))

    async def _send_to(self, client_id: str, frame: dict) -> None:
        client = self._clients.get(client_id)
        if client is None:
            return
        await client.send(frame)

    async def _run_maintenance(self) -> None:
        while True:
            await asyncio.sleep(_MAINTENANCE_INTERVAL_SECONDS)
            try:
                await self._rotate_offers()
            except Exception:
                LOG.exception("browser_extension_broker_offer_rotation_failed")
            idle_since = self._idle_since
            if idle_since is None or self._clients:
                continue
            if time.monotonic() - idle_since >= self._idle_timeout_seconds:
                LOG.info("browser_extension_broker_idle_shutdown", idle_seconds=self._idle_timeout_seconds)
                self.request_shutdown("idle")
                return


def _tab_snapshot(params: dict) -> dict:
    url = params.get("url")
    title = params.get("title")
    return {
        "tabId": params["tabId"],
        "url": url if isinstance(url, str) else "",
        "title": title if isinstance(title, str) else "",
    }
