from __future__ import annotations

import asyncio
import itertools
import os
import secrets
import time
from collections.abc import Awaitable, Callable
from contextlib import suppress

import aiohttp
import structlog

from skyvern.browser_extension.broker.daemon import spawn_daemon
from skyvern.browser_extension.broker.protocol import (
    BROKER_FRAME_VERSION,
    BROKER_PROTOCOL_VERSION,
    BROKER_WS_PATH,
    EXTENSION_NOT_CONNECTED_CODE,
    PAIRING_NONCE_OP,
    STATUS_OP,
    BrokerFrame,
    build_broker_nonce,
    build_request_frame,
    compute_broker_client_proof,
    compute_broker_server_proof,
    parse_broker_frame,
)
from skyvern.browser_extension.errors import (
    BrowserExtensionError,
    BrowserExtensionNotConnectedError,
    ExtensionRequestError,
)
from skyvern.browser_extension.relay import _MAX_WS_MESSAGE_BYTES

LOG = structlog.get_logger(__name__)

_CONNECT_TIMEOUT_SECONDS = 5.0
_HANDSHAKE_TIMEOUT_SECONDS = 10.0
_SPAWN_ATTEMPTS = 40
_SPAWN_POLL_SECONDS = 0.25
_SPAWN_EVERY_N_ATTEMPTS = 4
_RECONNECT_INITIAL_SECONDS = 0.5
_RECONNECT_MAX_SECONDS = 10.0
_REQUEST_GRACE_SECONDS = 5.0
_STEP_DOWN_COOLDOWN_SECONDS = 60.0


class LegacyBridgeOwnerError(BrowserExtensionError):
    """Raised when the bridge port is held by a pre-broker single-owner MCP instance."""


class BrokerTransport:
    """Client side of the broker daemon, shaped like ExtensionRelayServer.

    ExtensionCdpAdapter and BrowserExtensionRuntime only ever touch a handful of relay members, so
    an MCP process can swap in this transport and drive a browser it does not own.
    """

    def __init__(
        self,
        token: str,
        port: int,
        on_event: Callable[[str, dict], Awaitable[None]],
        on_disconnect: Callable[[], Awaitable[None]] | None = None,
    ) -> None:
        self._token = token
        self._port = port
        self._on_event = on_event
        self._on_disconnect = on_disconnect
        self._session: aiohttp.ClientSession | None = None
        self._websocket: aiohttp.ClientWebSocketResponse | None = None
        self._reader_task: asyncio.Task[None] | None = None
        self._request_ids = itertools.count(1)
        self._pending: dict[str, asyncio.Future[dict]] = {}
        self._connected_event = asyncio.Event()
        self._extension_connected = False
        self._stopped = False
        self._last_step_down_at: float | None = None
        self.bound_port = port
        self.scoped_tabs: list[dict] = []
        self.daemon_pid: int | None = None
        self.daemon_protocol: int | None = None

    @property
    def connected(self) -> bool:
        websocket = self._websocket
        return self._extension_connected and websocket is not None and not websocket.closed

    async def start(self) -> None:
        await self._connect_with_spawn()
        self._reader_task = asyncio.create_task(self._run_reader())

    async def stop(self) -> None:
        self._stopped = True
        task = self._reader_task
        self._reader_task = None
        if task is not None:
            task.cancel()
            with suppress(asyncio.CancelledError):
                await task
        await self._teardown_connection()

    async def wait_connected(self, timeout: float) -> bool:
        if self.connected:
            return True
        try:
            await asyncio.wait_for(self._connected_event.wait(), timeout)
        except TimeoutError:
            return False
        return self.connected

    async def request(self, op: str, args: dict, timeout: float = 30.0) -> dict:
        websocket = self._websocket
        if websocket is None or websocket.closed:
            raise BrowserExtensionNotConnectedError("Skyvern browser extension is not connected")

        request_id = f"b-{next(self._request_ids)}"
        future: asyncio.Future[dict] = asyncio.get_running_loop().create_future()
        self._pending[request_id] = future
        try:
            await websocket.send_json(build_request_frame(request_id, op, args, timeout))
        except (ConnectionError, RuntimeError, ValueError):
            self._discard_pending(request_id)
            raise BrowserExtensionNotConnectedError("Skyvern browser extension is not connected") from None

        try:
            return await asyncio.wait_for(asyncio.shield(future), timeout + _REQUEST_GRACE_SECONDS)
        except TimeoutError:
            self._discard_pending(request_id)
            raise ExtensionRequestError("INTERNAL", f"extension request timed out: {op}") from None
        except asyncio.CancelledError:
            self._discard_pending(request_id)
            raise

    async def acquire_pairing_nonce(self) -> str:
        result = await self.request(PAIRING_NONCE_OP, {}, timeout=10.0)
        nonce = result.get("nonce")
        if not isinstance(nonce, str) or not nonce:
            raise BrowserExtensionError("The browser extension broker returned an invalid pairing response")
        port = result.get("port")
        if type(port) is int:
            self.bound_port = port
        return nonce

    async def broker_status(self) -> dict:
        return await self.request(STATUS_OP, {}, timeout=10.0)

    async def _connect_with_spawn(self) -> None:
        last_error: BaseException | None = None
        for attempt in range(_SPAWN_ATTEMPTS):
            try:
                await self._connect()
                return
            except LegacyBridgeOwnerError:
                raise
            except (OSError, BrowserExtensionError) as exc:
                last_error = exc
            # Re-spawn periodically rather than once: the first daemon may have lost the bind race
            # to an outgoing instance that has since exited.
            if attempt % _SPAWN_EVERY_N_ATTEMPTS == 0:
                spawn_daemon(self._port)
            await asyncio.sleep(_SPAWN_POLL_SECONDS)
        raise BrowserExtensionError(
            f"Could not reach or start the Skyvern browser-extension broker on port {self._port}"
        ) from last_error

    async def _connect(self) -> None:
        session = aiohttp.ClientSession(
            timeout=aiohttp.ClientTimeout(
                total=None, connect=_CONNECT_TIMEOUT_SECONDS, sock_connect=_CONNECT_TIMEOUT_SECONDS
            )
        )
        try:
            websocket = await session.ws_connect(
                f"ws://127.0.0.1:{self._port}{BROKER_WS_PATH}",
                max_msg_size=_MAX_WS_MESSAGE_BYTES,
                heartbeat=20.0,
                autoping=True,
            )
        except aiohttp.WSServerHandshakeError as exc:
            await session.close()
            # A live listener that does not speak the broker protocol is a pre-broker MCP instance
            # holding the port; spawning another daemon would only lose the bind.
            raise LegacyBridgeOwnerError(
                f"Port {self._port} is held by an older Skyvern MCP session that does not share the "
                "browser-extension bridge"
            ) from exc
        except (aiohttp.ClientConnectionError, OSError) as exc:
            await session.close()
            raise OSError(f"broker connect failed: {type(exc).__name__}") from exc

        try:
            await asyncio.wait_for(self._handshake(websocket), _HANDSHAKE_TIMEOUT_SECONDS)
        except BaseException:
            await websocket.close()
            await session.close()
            raise

        self._session = session
        self._websocket = websocket

    async def _handshake(self, websocket: aiohttp.ClientWebSocketResponse) -> None:
        challenge = await self._receive_frame(websocket)
        if challenge is None or challenge.kind != "auth.challenge" or challenge.server_nonce is None:
            raise BrowserExtensionError("The browser-extension broker sent an invalid challenge")

        client_nonce = build_broker_nonce()
        await websocket.send_json(
            {
                "v": BROKER_FRAME_VERSION,
                "type": "auth.proof",
                "clientNonce": client_nonce,
                "proof": compute_broker_client_proof(self._token, challenge.server_nonce, client_nonce),
            }
        )

        auth_ok = await self._receive_frame(websocket)
        if auth_ok is None or auth_ok.kind != "auth.ok" or auth_ok.proof is None or auth_ok.protocol is None:
            raise BrowserExtensionError("The browser-extension broker rejected this session")
        expected = compute_broker_server_proof(self._token, client_nonce, challenge.server_nonce)
        if not secrets.compare_digest(expected, auth_ok.proof):
            raise BrowserExtensionError("The browser-extension broker failed authentication")

        if auth_ok.protocol < BROKER_PROTOCOL_VERSION and self._may_request_step_down():
            LOG.info(
                "browser_extension_broker_step_down_requested",
                daemon_protocol=auth_ok.protocol,
                protocol=BROKER_PROTOCOL_VERSION,
                daemon_root=auth_ok.root,
            )
            self._last_step_down_at = time.monotonic()
            await websocket.send_json(
                {"v": BROKER_FRAME_VERSION, "type": "broker.stepDown", "protocol": BROKER_PROTOCOL_VERSION}
            )
            raise OSError("broker is stepping down for a newer client")

        self.daemon_pid = auth_ok.pid
        self.daemon_protocol = auth_ok.protocol
        await websocket.send_json(
            {
                "v": BROKER_FRAME_VERSION,
                "type": "client.hello",
                "protocol": BROKER_PROTOCOL_VERSION,
                "pid": os.getpid(),
            }
        )

        state = await self._receive_frame(websocket)
        if state is None or state.kind != "broker.state" or state.extension_connected is None:
            raise BrowserExtensionError("The browser-extension broker sent an invalid state frame")
        self._apply_state(state.extension_connected, state.scoped_tabs or [])

    def _may_request_step_down(self) -> bool:
        last = self._last_step_down_at
        return last is None or time.monotonic() - last >= _STEP_DOWN_COOLDOWN_SECONDS

    async def _receive_frame(self, websocket: aiohttp.ClientWebSocketResponse) -> BrokerFrame | None:
        message = await websocket.receive()
        if message.type is not aiohttp.WSMsgType.TEXT:
            return None
        try:
            return parse_broker_frame(message.data, from_client=False)
        except BrowserExtensionError:
            return None

    async def _run_reader(self) -> None:
        delay = _RECONNECT_INITIAL_SECONDS
        while not self._stopped:
            websocket = self._websocket
            if websocket is not None:
                try:
                    await self._read_until_closed(websocket)
                except asyncio.CancelledError:
                    raise
                except Exception:
                    LOG.exception("browser_extension_broker_reader_failed")
                await self._teardown_connection()
            if self._stopped:
                return
            try:
                await self._connect()
            except LegacyBridgeOwnerError:
                LOG.warning("browser_extension_broker_reconnect_blocked_by_legacy_owner", port=self._port)
                await asyncio.sleep(delay)
                delay = min(delay * 2, _RECONNECT_MAX_SECONDS)
                continue
            except (OSError, BrowserExtensionError):
                spawn_daemon(self._port)
                await asyncio.sleep(delay)
                delay = min(delay * 2, _RECONNECT_MAX_SECONDS)
                continue
            delay = _RECONNECT_INITIAL_SECONDS
            LOG.info("browser_extension_broker_reconnected", port=self._port, daemon_pid=self.daemon_pid)

    async def _read_until_closed(self, websocket: aiohttp.ClientWebSocketResponse) -> None:
        async for message in websocket:
            if message.type is not aiohttp.WSMsgType.TEXT:
                if message.type in {aiohttp.WSMsgType.CLOSE, aiohttp.WSMsgType.CLOSED, aiohttp.WSMsgType.ERROR}:
                    return
                continue
            try:
                frame = parse_broker_frame(message.data, from_client=False)
            except BrowserExtensionError:
                LOG.warning("browser_extension_broker_invalid_frame")
                continue
            await self._handle_frame(websocket, frame)

    async def _handle_frame(self, websocket: aiohttp.ClientWebSocketResponse, frame: BrokerFrame) -> None:
        if frame.kind == "ping":
            with suppress(ConnectionError, RuntimeError, ValueError):
                await websocket.send_json({"v": BROKER_FRAME_VERSION, "type": "pong"})
            return
        if frame.kind == "pong":
            return
        if frame.kind == "response":
            self._complete_request(frame)
            return
        if frame.kind == "broker.state" and frame.extension_connected is not None:
            was_connected = self._extension_connected
            self._apply_state(frame.extension_connected, frame.scoped_tabs or [])
            if was_connected and not frame.extension_connected:
                self._fail_pending()
                await self._call_on_disconnect()
            return
        if frame.kind == "event" and frame.event is not None and frame.params is not None:
            self._update_scoped_tabs(frame.event, frame.params)
            if frame.event == "extension.hello":
                self._extension_connected = True
                self._connected_event.set()
            try:
                await self._on_event(frame.event, frame.params)
            except Exception:
                LOG.exception("browser extension event callback failed", event=frame.event)

    def _apply_state(self, extension_connected: bool, scoped_tabs: list[dict]) -> None:
        self._extension_connected = extension_connected
        if not extension_connected:
            self._connected_event.clear()
            self.scoped_tabs = []
            return
        self.scoped_tabs = [tab for tab in scoped_tabs if type(tab.get("tabId")) is int]
        self._connected_event.set()

    def _update_scoped_tabs(self, event: str, params: dict) -> None:
        if event == "extension.hello":
            tabs = params.get("scopedTabs")
            self.scoped_tabs = (
                [tab for tab in tabs if isinstance(tab, dict) and type(tab.get("tabId")) is int]
                if isinstance(tabs, list)
                else []
            )
            return
        if event in {"scope.tabAdded", "tabs.created"}:
            tab_id = params.get("tabId")
            if type(tab_id) is not int:
                return
            url = params.get("url")
            title = params.get("title")
            snapshot = {
                "tabId": tab_id,
                "url": url if isinstance(url, str) else "",
                "title": title if isinstance(title, str) else "",
            }
            self.scoped_tabs = [tab for tab in self.scoped_tabs if tab.get("tabId") != tab_id]
            self.scoped_tabs.append(snapshot)
            return
        if event == "scope.tabRemoved":
            tab_id = params.get("tabId")
            if type(tab_id) is int:
                self.scoped_tabs = [tab for tab in self.scoped_tabs if tab.get("tabId") != tab_id]

    def _complete_request(self, frame: BrokerFrame) -> None:
        if frame.request_id is None:
            return
        future = self._pending.pop(frame.request_id, None)
        if future is None or future.done():
            return
        if frame.ok:
            future.set_result(frame.result or {})
            return
        code = frame.error_code or "INTERNAL"
        message = frame.error_message or "broker request failed"
        if code == EXTENSION_NOT_CONNECTED_CODE:
            future.set_exception(BrowserExtensionNotConnectedError(message))
            return
        future.set_exception(ExtensionRequestError(code, message))

    def _discard_pending(self, request_id: str) -> None:
        pending = self._pending.pop(request_id, None)
        if pending is not None and not pending.done():
            pending.cancel()

    def _fail_pending(self) -> None:
        pending = list(self._pending.values())
        self._pending.clear()
        for future in pending:
            if not future.done():
                future.set_exception(BrowserExtensionNotConnectedError("Skyvern browser extension is not connected"))

    async def _teardown_connection(self) -> None:
        websocket = self._websocket
        session = self._session
        self._websocket = None
        self._session = None
        was_connected = self._extension_connected
        self._extension_connected = False
        self._connected_event.clear()
        self.scoped_tabs = []
        self._fail_pending()
        if websocket is not None and not websocket.closed:
            with suppress(Exception):
                await websocket.close()
        if session is not None:
            with suppress(Exception):
                await session.close()
        if was_connected:
            await self._call_on_disconnect()

    async def _call_on_disconnect(self) -> None:
        if self._on_disconnect is None:
            return
        try:
            await self._on_disconnect()
        except Exception:
            LOG.exception("browser extension disconnect callback failed")
