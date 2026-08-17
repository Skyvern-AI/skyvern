from __future__ import annotations

import asyncio
import json
import os
import socket
from collections.abc import Awaitable, Callable
from contextlib import suppress
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from skyvern.browser_extension import broker_server as broker_server_module
from skyvern.browser_extension.broker_client import BrokerClient
from skyvern.browser_extension.broker_protocol import (
    CONTROL_FRAME_LIMIT,
    MAX_CLIENT_OUTPUT_BYTES,
    MAX_ENCODED_CONTROL_FRAME_BYTES,
    MAX_ENCODED_OPERATION_FRAME_BYTES,
    encode_frame,
    event_frame,
    write_frame,
)
from skyvern.browser_extension.broker_server import BrowserExtensionBrokerServer, _ClientConnection
from skyvern.browser_extension.broker_state import BrokerPaths, read_readiness, record_startup_failure
from skyvern.browser_extension.errors import (
    BrowserExtensionBrokerError,
    BrowserExtensionNotConnectedError,
    ExtensionRequestError,
)
from skyvern.browser_extension.relay import ExtensionRelayServer


class FakeExtension:
    def __init__(self) -> None:
        self.protocol_version = 2
        self.scoped_tabs: list[dict] = []
        self.attached_tabs: set[int] = set()
        self.detach_fails = False
        self.last_reset_epoch: str | None = None
        self.last_reset_generation = -1
        self.last_reset_ok: bool | None = None
        self.reset_sweep_count = 0
        self.reset_gate = asyncio.Event()
        self.reset_gate.set()
        self.reset_started = asyncio.Event()
        self.reset_lock = asyncio.Lock()

    async def receive_reset(
        self,
        frame: dict,
        send_ack: Callable[[dict], Awaitable[None]],
    ) -> None:
        async with self.reset_lock:
            epoch = frame.get("epoch")
            generation = frame.get("generation")
            if not isinstance(epoch, str) or not epoch or type(generation) is not int or generation < 0:
                return
            if (
                epoch == self.last_reset_epoch
                and generation == self.last_reset_generation
                and self.last_reset_ok is True
            ):
                await send_ack(
                    {
                        "v": 2,
                        "type": "extension.reset_ack",
                        "epoch": epoch,
                        "generation": generation,
                        "ok": True,
                        "failedTabCount": 0,
                    }
                )
                return
            if epoch == self.last_reset_epoch and generation < self.last_reset_generation:
                return
            self.reset_started.set()
            await self.reset_gate.wait()
            self.reset_sweep_count += 1
            failed_tab_count = len(self.attached_tabs) if self.detach_fails else 0
            if failed_tab_count == 0:
                self.attached_tabs.clear()
                self.scoped_tabs = []
            self.last_reset_epoch = epoch
            self.last_reset_generation = generation
            self.last_reset_ok = failed_tab_count == 0
            await send_ack(
                {
                    "v": 2,
                    "type": "extension.reset_ack",
                    "epoch": epoch,
                    "generation": generation,
                    "ok": failed_tab_count == 0,
                    "failedTabCount": failed_tab_count,
                }
            )


class FakeExtensionWebSocket:
    def __init__(self, relay: ExtensionRelayServer, extension: FakeExtension) -> None:
        self.relay = relay
        self.extension = extension
        self.closed = False
        self.frames: list[dict] = []
        self.reset_tasks: set[asyncio.Task[None]] = set()

    async def send_json(self, frame: dict) -> None:
        self.frames.append(frame)
        if frame.get("type") == "extension.reset":
            task = asyncio.create_task(self.extension.receive_reset(frame, self._send_to_relay))
            self.reset_tasks.add(task)
            task.add_done_callback(self.reset_tasks.discard)

    async def close(self, *, code: int, message: bytes) -> None:
        self.closed = True

    async def send_hello(self) -> None:
        await self.relay._handle_text_frame(
            self,  # type: ignore[arg-type]
            json.dumps(
                {
                    "v": 2,
                    "type": "event",
                    "event": "extension.hello",
                    "params": {
                        "protocolVersion": 2,
                        "extensionVersion": "test",
                        "scopedTabs": list(self.extension.scoped_tabs),
                    },
                }
            ),
        )

    async def _send_to_relay(self, frame: dict) -> None:
        if self.closed:
            return
        await self.relay._handle_text_frame(self, json.dumps(frame))  # type: ignore[arg-type]


class FakeRelay:
    def __init__(
        self,
        _token: str,
        port: int,
        on_event: Callable[[str, dict], Awaitable[None]],
        on_disconnect: Callable[[], Awaitable[None]] | None,
        *,
        extension: FakeExtension | None = None,
        auto_connect: bool = True,
    ) -> None:
        self.bound_port = port
        self._scoped_tabs: list[dict] = []
        self.connected = False
        self.on_event = on_event
        self.on_disconnect = on_disconnect
        self.extension = extension or FakeExtension()
        self.auto_connect = auto_connect
        self.started = False
        self.stopped = False
        self.nonce = "pairing-nonce-sentinel"
        self.pending_request_count = 0
        self.connection_cycles = 0
        self.extension_protocol_version: int | None = self.extension.protocol_version
        self.extension_connection_generation = 1
        self.reset_frames: list[dict] = []
        self.reset_tasks: set[asyncio.Task[None]] = set()

    @property
    def scoped_tabs(self) -> list[dict]:
        return self._scoped_tabs

    @scoped_tabs.setter
    def scoped_tabs(self, tabs: list[dict]) -> None:
        self._scoped_tabs = list(tabs)
        self.extension.scoped_tabs = list(tabs)

    @property
    def attached_tabs(self) -> set[int]:
        return self.extension.attached_tabs

    @attached_tabs.setter
    def attached_tabs(self, tab_ids: set[int]) -> None:
        self.extension.attached_tabs = set(tab_ids)

    async def start(self) -> None:
        self.started = True

    async def stop(self) -> None:
        self.stopped = True
        for task in tuple(self.reset_tasks):
            task.cancel()
        if self.reset_tasks:
            await asyncio.gather(*self.reset_tasks, return_exceptions=True)

    async def wait_connected(self, _timeout: float) -> bool:
        return self.connected

    async def cycle_connection(self, _timeout: float) -> bool:
        self.connection_cycles += 1
        self.connected = False
        self._scoped_tabs = []
        return True

    async def send_reset(self, epoch: str, generation: int) -> bool:
        frame = {"v": 2, "type": "extension.reset", "epoch": epoch, "generation": generation}
        self.reset_frames.append(frame)
        task = asyncio.create_task(self.extension.receive_reset(frame, self._receive_extension_frame))
        self.reset_tasks.add(task)
        task.add_done_callback(self.reset_tasks.discard)
        return self.connected

    async def hello(self) -> None:
        self.connected = True
        self.extension_protocol_version = self.extension.protocol_version
        self._scoped_tabs = list(self.extension.scoped_tabs)
        await self.on_event(
            "extension.hello",
            {
                "protocolVersion": self.extension_protocol_version,
                "extensionVersion": "test",
                "scopedTabs": list(self._scoped_tabs),
            },
        )

    async def emit_event(self, event: str, params: dict) -> None:
        await self.on_event(event, params)

    async def _receive_extension_frame(self, frame: dict) -> None:
        if frame.get("type") != "extension.reset_ack":
            return
        if frame.get("ok") is True:
            self._scoped_tabs = []
        await self.on_event(
            "extension.reset_ack",
            {
                "epoch": frame.get("epoch"),
                "generation": frame.get("generation"),
                "ok": frame.get("ok"),
                "failedTabCount": frame.get("failedTabCount"),
            },
        )

    async def request(
        self,
        op: str,
        args: dict,
        timeout: float = 30.0,
        *,
        retain_until_terminal: bool = False,
        on_registered: Callable[[], None] | None = None,
        on_terminal: Callable[[], None] | None = None,
    ) -> dict:
        if on_registered is not None:
            on_registered()
        if on_terminal is not None:
            on_terminal()
        return {"op": op, "args": args, "timeout": timeout}

    async def wait_pending_requests(self, _timeout: float) -> bool:
        return self.pending_request_count == 0

    def get_or_create_pairing_nonce(self) -> str:
        return self.nonce

    def cancel_pairing_nonce(self) -> None:
        self.nonce = "cancelled"


class BlockingRelay(FakeRelay):
    def __init__(
        self,
        token: str,
        port: int,
        on_event: Callable[[str, dict], Awaitable[None]],
        on_disconnect: Callable[[], Awaitable[None]] | None,
    ) -> None:
        super().__init__(token, port, on_event, on_disconnect)
        self.request_started = asyncio.Event()
        self.release_request = asyncio.Event()

    async def request(
        self,
        op: str,
        args: dict,
        timeout: float = 30.0,
        *,
        retain_until_terminal: bool = False,
        on_registered: Callable[[], None] | None = None,
        on_terminal: Callable[[], None] | None = None,
    ) -> dict:
        if on_registered is not None:
            on_registered()
        self.request_started.set()
        await self.release_request.wait()
        if on_terminal is not None:
            on_terminal()
        return {"op": op, "args": args, "timeout": timeout}


class ControlledResetRelay(FakeRelay):
    async def request(
        self,
        op: str,
        args: dict,
        timeout: float = 30.0,
        *,
        retain_until_terminal: bool = False,
        on_registered: Callable[[], None] | None = None,
        on_terminal: Callable[[], None] | None = None,
    ) -> dict:
        if op == "debugger.send" and args.get("tabId") not in self.attached_tabs:
            raise ExtensionRequestError("DEBUGGER_DETACHED", "The debugger is not attached")
        return await super().request(
            op,
            args,
            timeout,
            retain_until_terminal=retain_until_terminal,
            on_registered=on_registered,
            on_terminal=on_terminal,
        )


class BlockingResetSendRelay(ControlledResetRelay):
    def __init__(
        self,
        token: str,
        port: int,
        on_event: Callable[[str, dict], Awaitable[None]],
        on_disconnect: Callable[[], Awaitable[None]] | None,
    ) -> None:
        super().__init__(token, port, on_event, on_disconnect)
        self.reset_send_started = asyncio.Event()
        self.reset_send_cancelled = asyncio.Event()
        self.release_reset_send = asyncio.Event()

    async def send_reset(self, epoch: str, generation: int) -> bool:
        self.reset_send_started.set()
        try:
            await self.release_reset_send.wait()
        except asyncio.CancelledError:
            self.reset_send_cancelled.set()
            raise
        return self.connected


class ReplacingProtocolRelay(ControlledResetRelay):
    def __init__(
        self,
        token: str,
        port: int,
        on_event: Callable[[str, dict], Awaitable[None]],
        on_disconnect: Callable[[], Awaitable[None]] | None,
    ) -> None:
        super().__init__(token, port, on_event, on_disconnect)
        self.extension.protocol_version = 1
        self.extension_protocol_version = 1

    async def cycle_connection(self, _timeout: float) -> bool:
        self.connection_cycles += 1
        self.extension_connection_generation += 1
        self.extension.protocol_version = 2
        self.extension_protocol_version = 2
        await self.hello()
        return True


@pytest.mark.asyncio
async def test_fake_extension_reexecutes_failed_identity_and_reacks_success() -> None:
    extension = FakeExtension()
    extension.attached_tabs = {71}
    extension.detach_fails = True
    acknowledgements: list[dict] = []

    async def capture_ack(frame: dict) -> None:
        acknowledgements.append(frame)

    frame = {"v": 2, "type": "extension.reset", "epoch": "daemon-epoch", "generation": 4}
    await extension.receive_reset(frame, capture_ack)
    extension.detach_fails = False
    await extension.receive_reset(frame, capture_ack)
    await extension.receive_reset(frame, capture_ack)

    assert extension.reset_sweep_count == 2
    assert [ack["ok"] for ack in acknowledgements] == [False, True, True]


@pytest.mark.asyncio
async def test_server_allows_one_client_and_never_returns_pairing_material() -> None:
    opened: list[str] = []
    server = BrowserExtensionBrokerServer(
        19777,
        pairing_opener=lambda url: not opened.append(url),
    )
    relay = FakeRelay("extension-secret-sentinel", 19777, server._handle_extension_event, server._handle_disconnect)
    server._relay = relay
    first = BrokerClient(19777, _ignore_event, auto_spawn=False)
    second = BrokerClient(19777, _ignore_event, auto_spawn=False)
    first_server_task = await _connect_over_socketpair(server, first)
    try:
        with pytest.raises(BrowserExtensionBrokerError, match="BROKER_BUSY"):
            await _connect_over_socketpair(server, second)

        result = await first.begin_pairing()
        assert result["opened"] is True
        assert "nonce" not in repr(result).lower()
        assert "url" not in repr(result).lower()
        assert opened == ["http://127.0.0.1:19777/pair#pairing-nonce-sentinel"]

        forwarded = await first.request("tabs.activate", {"tabId": 7})
        assert forwarded == {"op": "tabs.activate", "args": {"tabId": 7}, "timeout": 30.0}
    finally:
        await second.stop()
        await first.stop()
        await asyncio.wait_for(first_server_task, 1.0)
        await server.stop()

    assert relay.stopped


@pytest.mark.asyncio
async def test_pairing_open_failure_returns_only_nonce_fragment_fallback_url(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    token = "extension-secret-must-not-cross-broker-control"
    open_extension_url = MagicMock(return_value=False)
    monkeypatch.setattr(
        "skyvern.browser_extension.runtime.BrowserExtensionRuntime.open_extension_url",
        open_extension_url,
    )
    server = BrowserExtensionBrokerServer(19777)
    relay = FakeRelay(token, 19777, server._handle_extension_event, server._handle_disconnect)
    server._relay = relay
    client = BrokerClient(19777, _ignore_event, auto_spawn=False)
    server_task = await _connect_over_socketpair(server, client)
    try:
        result = await client.begin_pairing()

        assert result["active"] is True
        assert result["opened"] is False
        assert result["pairingUrl"] == "http://127.0.0.1:19777/pair#pairing-nonce-sentinel"
        assert token not in repr(result)
        open_extension_url.assert_called_once_with("http://127.0.0.1:19777/pair#pairing-nonce-sentinel")
    finally:
        await client.stop()
        await asyncio.wait_for(server_task, 1.0)
        await server.stop()


@pytest.mark.asyncio
async def test_cached_client_reenrolls_after_broker_restart(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    original_server = BrowserExtensionBrokerServer(19777)
    original_server._relay = FakeRelay(
        "extension-secret",
        19777,
        original_server._handle_extension_event,
        original_server._handle_disconnect,
    )
    client = BrokerClient(19777, _ignore_event, auto_spawn=False)
    original_server_task = await _connect_over_socketpair(original_server, client)
    original_client_id = client._client_id
    await original_server.stop()
    await asyncio.wait_for(original_server_task, 1.0)
    await _eventually(lambda: not client.broker_connected)

    restarted_server = BrowserExtensionBrokerServer(19777)
    restarted_relay = FakeRelay(
        "extension-secret",
        19777,
        restarted_server._handle_extension_event,
        restarted_server._handle_disconnect,
    )
    restarted_server._relay = restarted_relay
    restarted_tasks: list[asyncio.Task[None]] = []

    async def open_restarted_connection(_paths: object) -> tuple[asyncio.StreamReader, asyncio.StreamWriter]:
        server_socket, client_socket = socket.socketpair()
        server_reader, server_writer = await asyncio.open_connection(sock=server_socket)
        client_reader, client_writer = await asyncio.open_connection(sock=client_socket)
        restarted_tasks.append(asyncio.create_task(restarted_server._handle_connection(server_reader, server_writer)))
        return client_reader, client_writer

    monkeypatch.setattr(
        "skyvern.browser_extension.broker_client.read_broker_state",
        lambda _paths: SimpleNamespace(
            lifecycle="ready",
            externalPort=19777,
            controlEndpoint=str(client.paths.control_socket),
            protocolMin=1,
            protocolMax=1,
            brokerGeneration=1,
            pid=123,
            processStart="marker",
        ),
    )
    monkeypatch.setattr("skyvern.browser_extension.broker_client.process_identity_matches", lambda _pid, _marker: True)
    monkeypatch.setattr("skyvern.browser_extension.broker_client._open_control_connection", open_restarted_connection)

    try:
        await restarted_relay.hello()
        await _eventually(lambda: not restarted_server._extension_reset_quarantined)
        result = await client.request("tabs.activate", {"tabId": 7})

        assert result == {"op": "tabs.activate", "args": {"tabId": 7}, "timeout": 30.0}
        assert len(restarted_tasks) == 2
        assert client._client_id is not None
        assert client._client_id != original_client_id
    finally:
        await client.stop()
        for task in restarted_tasks:
            await asyncio.wait_for(task, 1.0)
        await restarted_server.stop()


@pytest.mark.asyncio
async def test_cached_client_surfaces_structured_error_when_reenrollment_is_denied(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    server = BrowserExtensionBrokerServer(19777)
    server._relay = FakeRelay("extension-secret", 19777, server._handle_extension_event, server._handle_disconnect)
    active_client = BrokerClient(19777, _ignore_event, auto_spawn=False)
    active_server_task = await _connect_over_socketpair(server, active_client)
    stale_client = BrokerClient(19777, _ignore_event, auto_spawn=False)
    stale_client._client_id = "a" * 32
    stale_client._recovery_secret = "stale-recovery-secret"
    attempted_connections: list[asyncio.Task[None]] = []

    async def open_connection(_paths: object) -> tuple[asyncio.StreamReader, asyncio.StreamWriter]:
        server_socket, client_socket = socket.socketpair()
        server_reader, server_writer = await asyncio.open_connection(sock=server_socket)
        client_reader, client_writer = await asyncio.open_connection(sock=client_socket)
        attempted_connections.append(asyncio.create_task(server._handle_connection(server_reader, server_writer)))
        return client_reader, client_writer

    monkeypatch.setattr(
        "skyvern.browser_extension.broker_client.read_broker_state",
        lambda _paths: SimpleNamespace(
            lifecycle="ready",
            externalPort=19777,
            controlEndpoint=str(stale_client.paths.control_socket),
            protocolMin=1,
            protocolMax=1,
            brokerGeneration=1,
            pid=123,
            processStart="marker",
        ),
    )
    monkeypatch.setattr("skyvern.browser_extension.broker_client.process_identity_matches", lambda _pid, _marker: True)
    monkeypatch.setattr("skyvern.browser_extension.broker_client._open_control_connection", open_connection)

    try:
        with pytest.raises(BrowserExtensionBrokerError) as error_info:
            await stale_client.start()

        assert error_info.value.code == "BROKER_BUSY"
        assert len(attempted_connections) == 2
        assert stale_client._client_id is None
        assert stale_client._recovery_secret is None
    finally:
        await stale_client.stop()
        await active_client.stop()
        for task in attempted_connections:
            await asyncio.wait_for(task, 1.0)
        await asyncio.wait_for(active_server_task, 1.0)
        await server.stop()


@pytest.mark.asyncio
async def test_known_client_with_bad_proof_remains_auth_failed() -> None:
    server = BrowserExtensionBrokerServer(19777)
    server._relay = FakeRelay("extension-secret", 19777, server._handle_extension_event, server._handle_disconnect)
    enrolled = BrokerClient(19777, _ignore_event, auto_spawn=False)
    enrolled_server_task = await _connect_over_socketpair(server, enrolled)
    client_id = enrolled._client_id
    await enrolled.stop()
    await asyncio.wait_for(enrolled_server_task, 1.0)
    assert client_id is not None

    attacker = BrokerClient(19777, _ignore_event, auto_spawn=False)
    attacker._client_id = client_id
    attacker._recovery_secret = "wrong-recovery-secret"
    server_socket, client_socket = socket.socketpair()
    server_reader, server_writer = await asyncio.open_connection(sock=server_socket)
    client_reader, client_writer = await asyncio.open_connection(sock=client_socket)
    server_task = asyncio.create_task(server._handle_connection(server_reader, server_writer))
    try:
        with pytest.raises(BrowserExtensionBrokerError) as error_info:
            await attacker._authenticate(client_reader, client_writer)

        assert error_info.value.code == "AUTH_FAILED"
        assert attacker._client_id == client_id
        assert len(server._credentials) == 1
    finally:
        client_writer.close()
        await client_writer.wait_closed()
        await asyncio.wait_for(server_task, 1.0)
        await attacker.stop()
        await server.stop()


@pytest.mark.asyncio
async def test_operator_client_can_status_pair_and_stop_while_mcp_is_active() -> None:
    server = BrowserExtensionBrokerServer(19777, pairing_opener=lambda _url: True)
    relay = FakeRelay("extension-secret", 19777, server._handle_extension_event, server._handle_disconnect)
    server._relay = relay
    mcp = BrokerClient(19777, _ignore_event, auto_spawn=False)
    operator = BrokerClient(19777, _ignore_event, auto_spawn=False, operator=True)
    mcp_server_task = await _connect_over_socketpair(server, mcp)
    operator_server_task = await _connect_over_socketpair(server, operator)
    try:
        assert (await operator.broker_status())["clientCount"] == 1
        assert (await operator.begin_pairing())["active"] is True
        assert (await operator.stop_broker())["stopping"] is True
        assert server._shutdown_event.is_set()
    finally:
        await operator.stop()
        await mcp.stop()
        await asyncio.wait_for(operator_server_task, 1.0)
        await asyncio.wait_for(mcp_server_task, 1.0)
        await server.stop()


@pytest.mark.asyncio
async def test_only_operator_connection_can_cancel_another_principals_pairing() -> None:
    server = BrowserExtensionBrokerServer(19777, pairing_opener=lambda _url: True)
    relay = FakeRelay("extension-secret", 19777, server._handle_extension_event, server._handle_disconnect)
    server._relay = relay
    mcp = BrokerClient(19777, _ignore_event, auto_spawn=False)
    operator = BrokerClient(19777, _ignore_event, auto_spawn=False, operator=True)
    mcp_server_task = await _connect_over_socketpair(server, mcp)
    operator_server_task = await _connect_over_socketpair(server, operator)
    try:
        await operator.begin_pairing()
        with pytest.raises(BrowserExtensionBrokerError, match="PAIRING_BUSY"):
            await mcp._control_request("pairing.cancel", {"operatorConfirmed": True}, 5.0)

        assert server._pairing_owner == "operator"
        assert relay.nonce == "pairing-nonce-sentinel"
        assert await operator.cancel_pairing() == {"cancelled": True}
        assert relay.nonce == "cancelled"
    finally:
        await operator.stop()
        await mcp.stop()
        await asyncio.wait_for(operator_server_task, 1.0)
        await asyncio.wait_for(mcp_server_task, 1.0)
        await server.stop()


@pytest.mark.asyncio
async def test_operator_status_does_not_expose_active_mcp_tabs() -> None:
    server = BrowserExtensionBrokerServer(19777)
    relay = FakeRelay("extension-secret", 19777, server._handle_extension_event, server._handle_disconnect)
    server._relay = relay
    mcp = BrokerClient(19777, _ignore_event, auto_spawn=False)
    operator = BrokerClient(19777, _ignore_event, auto_spawn=False, operator=True)
    mcp_server_task = await _connect_over_socketpair(server, mcp)
    relay.scoped_tabs = [{"tabId": 17}, {"tabId": 23}]
    await relay.hello()
    operator_server_task = await _connect_over_socketpair(server, operator)
    try:
        assert (await mcp.broker_status())["tabIds"] == [17, 23]
        assert (await operator.broker_status())["tabIds"] == []
    finally:
        await operator.stop()
        await mcp.stop()
        await asyncio.wait_for(operator_server_task, 1.0)
        await asyncio.wait_for(mcp_server_task, 1.0)
        await server.stop()


@pytest.mark.asyncio
async def test_fresh_operator_connection_retrieves_pending_pairing_flow() -> None:
    opened: list[str] = []
    server = BrowserExtensionBrokerServer(19777, pairing_opener=lambda url: not opened.append(url))
    relay = FakeRelay("extension-secret", 19777, server._handle_extension_event, server._handle_disconnect)
    server._relay = relay
    first = BrokerClient(19777, _ignore_event, auto_spawn=False, operator=True)
    second = BrokerClient(19777, _ignore_event, auto_spawn=False, operator=True)
    first_server_task = await _connect_over_socketpair(server, first)
    try:
        assert (await first.begin_pairing())["active"] is True
    finally:
        await first.stop()
        await asyncio.wait_for(first_server_task, 1.0)

    second_server_task = await _connect_over_socketpair(server, second)
    try:
        assert (await second.pairing_status())["owned"] is True
        assert (await second.begin_pairing())["active"] is True
        assert len(opened) == 2
    finally:
        await second.stop()
        await asyncio.wait_for(second_server_task, 1.0)
        await server.stop()


@pytest.mark.asyncio
async def test_pairing_completion_clears_flow_but_preserves_operator_rate_limit() -> None:
    server = BrowserExtensionBrokerServer(19777, pairing_opener=lambda _url: True)
    relay = FakeRelay("extension-secret", 19777, server._handle_extension_event, server._handle_disconnect)
    server._relay = relay
    first = BrokerClient(19777, _ignore_event, auto_spawn=False, operator=True)
    second = BrokerClient(19777, _ignore_event, auto_spawn=False, operator=True)
    first_server_task = await _connect_over_socketpair(server, first)
    await first.begin_pairing()
    await server._handle_pairing_complete()
    assert server._pairing_owner is None
    await first.stop()
    await asyncio.wait_for(first_server_task, 1.0)

    second_server_task = await _connect_over_socketpair(server, second)
    try:
        assert (await second.pairing_status())["active"] is False
        with pytest.raises(BrowserExtensionBrokerError, match="RATE_LIMITED"):
            await second.begin_pairing()
    finally:
        await second.stop()
        await asyncio.wait_for(second_server_task, 1.0)
        await server.stop()


@pytest.mark.asyncio
async def test_outstanding_extension_request_blocks_reenrollment_until_terminal() -> None:
    server = BrowserExtensionBrokerServer(19777)
    relay = BlockingRelay("extension-secret", 19777, server._handle_extension_event, server._handle_disconnect)
    server._relay = relay
    first = BrokerClient(19777, _ignore_event, auto_spawn=False)
    second = BrokerClient(19777, _ignore_event, auto_spawn=False)
    first_server_task = await _connect_over_socketpair(server, first)
    request_task = asyncio.create_task(first.request("tabs.activate", {"tabId": 7}))
    await asyncio.wait_for(relay.request_started.wait(), 1.0)
    await first.stop()
    await asyncio.wait_for(first_server_task, 1.0)

    with pytest.raises(BrowserExtensionBrokerError, match="BROKER_BUSY"):
        await _connect_over_socketpair(server, second)

    relay.release_request.set()
    with suppress(BrowserExtensionNotConnectedError):
        await request_task
    await _eventually(lambda: not server._forwarded_tasks)
    second_server_task = await _connect_over_socketpair(server, second)
    await second.stop()
    await asyncio.wait_for(second_server_task, 1.0)
    await server.stop()


@pytest.mark.asyncio
@pytest.mark.parametrize("release_path", ["abrupt_eof", "clean_stop", "cancelled_flow"])
async def test_client_release_resets_extension_before_successor_enrollment(release_path: str) -> None:
    server = BrowserExtensionBrokerServer(19777)
    relay = ControlledResetRelay("extension-secret", 19777, server._handle_extension_event, server._handle_disconnect)
    server._relay = relay
    first = BrokerClient(19777, _ignore_event, auto_spawn=False)
    second = BrokerClient(19777, _ignore_event, auto_spawn=False)
    first_server_task = await _connect_over_socketpair(server, first)
    second_server_task: asyncio.Task[None] | None = None
    second_connect_task: asyncio.Task[asyncio.Task[None]] | None = None
    try:
        relay.scoped_tabs = [{"tabId": 71, "url": "https://private.test", "title": "Private"}]
        relay.attached_tabs = {71}
        await relay.hello()
        await _eventually(
            lambda: first.scoped_tabs == [{"tabId": 71, "url": "https://private.test", "title": "Private"}]
        )
        relay.extension.reset_started.clear()
        relay.extension.reset_gate.clear()
        if release_path == "clean_stop":
            await first.stop()
        elif release_path == "abrupt_eof":
            assert first._writer is not None
            first._writer.transport.abort()
        else:
            first_server_task.cancel()

        await asyncio.wait_for(relay.extension.reset_started.wait(), 1.0)
        second_connect_task = asyncio.create_task(_connect_over_socketpair(server, second))
        await asyncio.sleep(0.05)
        assert not second_connect_task.done()

        await relay.hello()
        assert relay.reset_frames[-1]["generation"] == 1
        assert not second_connect_task.done()
        relay.extension.reset_gate.set()
        second_server_task = await asyncio.wait_for(second_connect_task, 1.0)
        await _eventually(lambda: second.broker_connected)
        assert second.scoped_tabs == []
        with pytest.raises(ExtensionRequestError, match="DEBUGGER_DETACHED"):
            await second.request("debugger.send", {"tabId": 71, "method": "Runtime.enable"})
        assert relay.connection_cycles == 0
    finally:
        if second_connect_task is not None and not second_connect_task.done():
            second_connect_task.cancel()
            with suppress(asyncio.CancelledError):
                await second_connect_task
        await second.stop()
        await first.stop()
        if second_server_task is not None:
            await asyncio.wait_for(second_server_task, 1.0)
        await asyncio.wait_for(first_server_task, 1.0)
        await server.stop()


@pytest.mark.asyncio
async def test_restarted_daemon_resets_surviving_extension_snapshot_before_exposure() -> None:
    extension = FakeExtension()
    original_server = BrowserExtensionBrokerServer(19777)
    original_relay = ControlledResetRelay(
        "extension-secret",
        19777,
        original_server._handle_extension_event,
        original_server._handle_disconnect,
        extension=extension,
    )
    original_server._relay = original_relay
    original_client = BrokerClient(19777, _ignore_event, auto_spawn=False)
    original_server_task = await _connect_over_socketpair(original_server, original_client)
    original_reset = original_relay.reset_frames[0]

    original_relay.scoped_tabs = [{"tabId": 71, "url": "https://private.test", "title": "Private"}]
    original_relay.attached_tabs = {71}
    await original_relay.hello()
    await _eventually(lambda: original_client.scoped_tabs != [])
    await original_server.stop()
    await asyncio.wait_for(original_server_task, 1.0)

    restarted_server = BrowserExtensionBrokerServer(19777)
    restarted_relay = ControlledResetRelay(
        "extension-secret",
        19777,
        restarted_server._handle_extension_event,
        restarted_server._handle_disconnect,
        extension=extension,
        auto_connect=False,
    )
    restarted_server._relay = restarted_relay
    restarted_client = BrokerClient(19777, _ignore_event, auto_spawn=False)
    restarted_server_task = await _connect_over_socketpair(restarted_server, restarted_client)
    ready_task = asyncio.create_task(restarted_client.wait_connected(1.0))

    await restarted_relay.hello()
    assert await ready_task
    assert restarted_client.scoped_tabs == []
    assert restarted_relay.reset_frames[0]["generation"] == original_reset["generation"]
    assert restarted_relay.reset_frames[0]["epoch"] != original_reset["epoch"]

    await restarted_client.stop()
    await asyncio.wait_for(restarted_server_task, 1.0)
    await restarted_server.stop()


@pytest.mark.asyncio
async def test_new_daemon_epoch_executes_reset_when_generation_restarts_below_prior_run() -> None:
    extension = FakeExtension()
    original_server = BrowserExtensionBrokerServer(19777)
    original_relay = ControlledResetRelay(
        "extension-secret",
        19777,
        original_server._handle_extension_event,
        original_server._handle_disconnect,
        extension=extension,
    )
    original_server._relay = original_relay
    original_client = BrokerClient(19777, _ignore_event, auto_spawn=False)
    original_server_task = await _connect_over_socketpair(original_server, original_client)
    await original_client.stop()
    await asyncio.wait_for(original_server_task, 1.0)
    await _eventually(lambda: not original_server._extension_reset_quarantined)
    prior_reset = original_relay.reset_frames[-1]
    await original_server.stop()

    extension.scoped_tabs = [{"tabId": 71, "url": "https://private.test", "title": "Private"}]
    extension.attached_tabs = {71}
    restarted_server = BrowserExtensionBrokerServer(19777)
    restarted_relay = ControlledResetRelay(
        "extension-secret",
        19777,
        restarted_server._handle_extension_event,
        restarted_server._handle_disconnect,
        extension=extension,
        auto_connect=False,
    )
    restarted_server._relay = restarted_relay
    restarted_client = BrokerClient(19777, _ignore_event, auto_spawn=False)
    restarted_server_task = await _connect_over_socketpair(restarted_server, restarted_client)

    await restarted_relay.hello()
    assert await restarted_client.wait_connected(1.0)
    assert restarted_client.scoped_tabs == []
    assert restarted_relay.reset_frames[0]["generation"] < prior_reset["generation"]
    assert restarted_relay.reset_frames[0]["epoch"] != prior_reset["epoch"]

    await restarted_client.stop()
    await asyncio.wait_for(restarted_server_task, 1.0)
    await restarted_server.stop()


@pytest.mark.asyncio
async def test_quarantine_suppresses_extension_events_until_reset_ack() -> None:
    received: list[tuple[str, dict]] = []

    async def capture_event(event: str, params: dict) -> None:
        received.append((event, params))

    server = BrowserExtensionBrokerServer(19777)
    relay = ControlledResetRelay(
        "extension-secret",
        19777,
        server._handle_extension_event,
        server._handle_disconnect,
        auto_connect=False,
    )
    server._relay = relay
    client = BrokerClient(19777, capture_event, auto_spawn=False)
    server_task = await _connect_over_socketpair(server, client)
    relay.scoped_tabs = [{"tabId": 71, "url": "https://private.test", "title": "Private"}]
    relay.attached_tabs = {71}
    relay.extension.reset_gate.clear()

    await relay.hello()
    await asyncio.wait_for(relay.extension.reset_started.wait(), 1.0)
    await relay.emit_event(
        "debugger.event",
        {"tabId": 71, "method": "Runtime.consoleAPICalled", "params": {"private": "payload"}},
    )
    await asyncio.sleep(0)
    assert received == []
    assert client.scoped_tabs == []
    with pytest.raises(BrowserExtensionBrokerError) as error_info:
        await client.request("debugger.send", {"tabId": 71, "method": "Runtime.enable"})
    assert error_info.value.code == "EXTENSION_RESET_IN_PROGRESS"
    assert error_info.value.retry_after == 0.1

    relay.extension.reset_gate.set()
    assert await client.wait_connected(1.0)
    await relay.emit_event("debugger.event", {"tabId": 72, "method": "Runtime.executionContextCreated", "params": {}})
    await _eventually(lambda: len(received) == 2)
    assert received[0] == ("extension.hello", {"scopedTabs": []})
    assert received[1][0] == "debugger.event"

    await client.stop()
    await asyncio.wait_for(server_task, 1.0)
    await server.stop()


@pytest.mark.asyncio
async def test_relay_reconnect_resets_before_active_client_sees_new_traffic() -> None:
    received: list[tuple[str, dict]] = []

    async def capture_event(event: str, params: dict) -> None:
        received.append((event, params))

    server = BrowserExtensionBrokerServer(19777)
    relay = ControlledResetRelay("extension-secret", 19777, server._handle_extension_event, server._handle_disconnect)
    server._relay = relay
    client = BrokerClient(19777, capture_event, auto_spawn=False)
    server_task = await _connect_over_socketpair(server, client)
    relay.scoped_tabs = [{"tabId": 71, "url": "https://private.test", "title": "Private"}]
    relay.attached_tabs = {71}
    await relay.hello()
    await _eventually(lambda: client.scoped_tabs != [])

    relay.connected = False
    await server._handle_disconnect()
    await _eventually(lambda: not client.connected)
    received.clear()
    relay.extension.reset_started.clear()
    relay.extension.reset_gate.clear()
    await relay.hello()
    await asyncio.wait_for(relay.extension.reset_started.wait(), 1.0)
    await relay.emit_event("debugger.event", {"tabId": 71, "method": "Runtime.consoleAPICalled", "params": {}})
    await asyncio.sleep(0)
    assert received == []
    assert client.scoped_tabs == []

    relay.extension.reset_gate.set()
    assert await client.wait_connected(1.0)
    assert client.scoped_tabs == []
    await client.stop()
    await asyncio.wait_for(server_task, 1.0)
    await server.stop()


@pytest.mark.asyncio
async def test_detach_failure_keeps_daemon_quarantined_and_fails_enrollment() -> None:
    server = BrowserExtensionBrokerServer(19777)
    extension = FakeExtension()
    extension.scoped_tabs = [{"tabId": 71, "url": "https://private.test", "title": "Private"}]
    extension.attached_tabs = {71}
    extension.detach_fails = True
    relay = ControlledResetRelay(
        "extension-secret",
        19777,
        server._handle_extension_event,
        server._handle_disconnect,
        extension=extension,
        auto_connect=False,
    )
    server._relay = relay

    await relay.hello()
    await _eventually(lambda: server._extension_reset_error == "EXTENSION_RESET_FAILED")
    client = BrokerClient(19777, _ignore_event, auto_spawn=False)
    with pytest.raises(BrowserExtensionBrokerError) as error_info:
        await _connect_over_socketpair(server, client)

    assert error_info.value.code == "EXTENSION_RESET_FAILED"
    await client.stop()
    await server.stop()


@pytest.mark.asyncio
async def test_successor_enrollment_proceeds_when_owner_releases_without_extension(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(broker_server_module, "EXTENSION_RESET_TIMEOUT_SECONDS", 0.01)
    server = BrowserExtensionBrokerServer(19777)
    relay = ControlledResetRelay(
        "extension-secret",
        19777,
        server._handle_extension_event,
        server._handle_disconnect,
        auto_connect=False,
    )
    server._relay = relay
    first = BrokerClient(19777, _ignore_event, auto_spawn=False)
    second = BrokerClient(19777, _ignore_event, auto_spawn=False)
    first_server_task = await _connect_over_socketpair(server, first)

    await first.stop()
    await asyncio.wait_for(first_server_task, 1.0)
    second_server_task = await _connect_over_socketpair(server, second)

    assert second.broker_connected
    assert not second.connected
    assert relay.reset_frames == []
    await server.stop()
    await second.stop()
    await asyncio.wait_for(second_server_task, 1.0)


@pytest.mark.asyncio
async def test_successor_enrollment_proceeds_when_extension_disconnects_during_reset() -> None:
    server = BrowserExtensionBrokerServer(19777)
    relay = ControlledResetRelay("extension-secret", 19777, server._handle_extension_event, server._handle_disconnect)
    server._relay = relay
    first = BrokerClient(19777, _ignore_event, auto_spawn=False)
    second = BrokerClient(19777, _ignore_event, auto_spawn=False)
    first_server_task = await _connect_over_socketpair(server, first)

    relay.extension.reset_started.clear()
    relay.extension.reset_gate.clear()
    await first.stop()
    await asyncio.wait_for(relay.extension.reset_started.wait(), 1.0)
    relay.connected = False
    relay.extension_protocol_version = None
    await server._handle_disconnect()
    await asyncio.wait_for(first_server_task, 1.0)
    relay.auto_connect = False
    second_server_task = await _connect_over_socketpair(server, second)

    assert second.broker_connected
    assert not second.connected
    await server.stop()
    await second.stop()
    await asyncio.wait_for(second_server_task, 1.0)


@pytest.mark.asyncio
async def test_late_extension_reconnect_is_reset_before_successor_sees_snapshot(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(broker_server_module, "EXTENSION_RESET_TIMEOUT_SECONDS", 0.01)
    server = BrowserExtensionBrokerServer(19777)
    relay = ControlledResetRelay(
        "extension-secret",
        19777,
        server._handle_extension_event,
        server._handle_disconnect,
        auto_connect=False,
    )
    server._relay = relay
    first = BrokerClient(19777, _ignore_event, auto_spawn=False)
    second = BrokerClient(19777, _ignore_event, auto_spawn=False)
    first_server_task = await _connect_over_socketpair(server, first)

    await first.stop()
    await asyncio.wait_for(first_server_task, 1.0)
    second_server_task = await _connect_over_socketpair(server, second)
    ready_task = asyncio.create_task(second.wait_connected(1.0))

    relay.scoped_tabs = [{"tabId": 71, "url": "https://private.test", "title": "Private"}]
    relay.attached_tabs = {71}
    relay.extension.reset_gate.clear()
    await relay.hello()
    await asyncio.wait_for(relay.extension.reset_started.wait(), 1.0)
    assert len(relay.reset_frames) == 1
    assert not second.connected
    assert second.scoped_tabs == []
    status = await second.broker_status()
    assert status["extensionConnected"] is False
    assert status["tabIds"] == []
    with pytest.raises(BrowserExtensionBrokerError, match="EXTENSION_RESET_IN_PROGRESS"):
        await second.request("debugger.send", {"tabId": 71, "method": "Runtime.enable"})
    assert not ready_task.done()

    relay.extension.reset_gate.set()
    assert await ready_task
    assert second.scoped_tabs == []
    await server.stop()
    await second.stop()
    await asyncio.wait_for(second_server_task, 1.0)


@pytest.mark.asyncio
async def test_successor_enrollment_fails_structurally_when_extension_reset_times_out(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(broker_server_module, "EXTENSION_RESET_TIMEOUT_SECONDS", 0.01)
    server = BrowserExtensionBrokerServer(19777)
    relay = ControlledResetRelay(
        "extension-secret",
        19777,
        server._handle_extension_event,
        server._handle_disconnect,
        auto_connect=False,
    )
    relay.scoped_tabs = [{"tabId": 71}]
    relay.extension.reset_gate.clear()
    server._relay = relay
    first = BrokerClient(19777, _ignore_event, auto_spawn=False)
    second = BrokerClient(19777, _ignore_event, auto_spawn=False)
    first_server_task = await _connect_over_socketpair(server, first)

    await first.stop()
    await relay.hello()
    await asyncio.wait_for(relay.extension.reset_started.wait(), 1.0)
    await asyncio.sleep(0.02)
    await asyncio.wait_for(first_server_task, 1.0)
    with pytest.raises(BrowserExtensionBrokerError) as error_info:
        await _connect_over_socketpair(server, second)

    assert error_info.value.code == "EXTENSION_RESET_TIMEOUT"
    await second.stop()
    await server.stop()


@pytest.mark.asyncio
async def test_v1_extension_warns_and_uses_cycle_only_fallback(monkeypatch: pytest.MonkeyPatch) -> None:
    warnings: list[tuple[str, dict]] = []
    monkeypatch.setattr(
        broker_server_module.LOG,
        "warning",
        lambda event, **fields: warnings.append((event, fields)),
    )
    server = BrowserExtensionBrokerServer(19777)
    relay = FakeRelay("extension-secret", 19777, server._handle_extension_event, server._handle_disconnect)
    relay.extension.protocol_version = 1
    relay.extension_protocol_version = 1
    server._relay = relay
    first = BrokerClient(19777, _ignore_event, auto_spawn=False)
    second = BrokerClient(19777, _ignore_event, auto_spawn=False)
    first_server_task = await _connect_over_socketpair(server, first)
    initial_connection_cycles = relay.connection_cycles
    warnings.clear()
    relay.scoped_tabs = [{"tabId": 71}]
    relay.attached_tabs = {71}
    await relay.hello()

    await first.stop()
    await asyncio.wait_for(first_server_task, 1.0)
    second_server_task = await _connect_over_socketpair(server, second)

    assert relay.connection_cycles == initial_connection_cycles + 1
    assert relay.reset_frames == []
    assert second.scoped_tabs == []
    assert warnings == [
        (
            "browser_extension_protocol_skew",
            {"extension_protocol": 1, "broker_protocol": 2, "fallback": "cycle_only"},
        )
    ]
    await second.stop()
    await asyncio.wait_for(second_server_task, 1.0)
    await server.stop()


@pytest.mark.asyncio
async def test_v2_replacement_during_v1_cycle_still_requires_reset_ack() -> None:
    server = BrowserExtensionBrokerServer(19777)
    relay = ReplacingProtocolRelay("extension-secret", 19777, server._handle_extension_event, server._handle_disconnect)
    server._relay = relay
    first = BrokerClient(19777, _ignore_event, auto_spawn=False)
    second = BrokerClient(19777, _ignore_event, auto_spawn=False)
    first_server_task = await _connect_over_socketpair(server, first)
    initial_connection_cycles = relay.connection_cycles
    relay.extension.protocol_version = 1
    relay.extension_protocol_version = 1
    relay.scoped_tabs = [{"tabId": 71}]
    relay.attached_tabs = {71}
    await relay.hello()
    relay.extension.reset_started.clear()
    relay.extension.reset_gate.clear()
    second_connect_task: asyncio.Task[asyncio.Task[None]] | None = None
    second_server_task: asyncio.Task[None] | None = None
    try:
        await first.stop()
        await asyncio.wait_for(relay.extension.reset_started.wait(), 1.0)
        second_connect_task = asyncio.create_task(_connect_over_socketpair(server, second))
        await asyncio.sleep(0.05)
        assert not second_connect_task.done()
        relay.extension.reset_gate.set()
        second_server_task = await asyncio.wait_for(second_connect_task, 1.0)
        assert second.scoped_tabs == []
        assert relay.connection_cycles == initial_connection_cycles + 1
        assert relay.reset_frames
    finally:
        if second_connect_task is not None and not second_connect_task.done():
            second_connect_task.cancel()
            with suppress(asyncio.CancelledError):
                await second_connect_task
        await second.stop()
        await first.stop()
        if second_server_task is not None:
            await asyncio.wait_for(second_server_task, 1.0)
        await asyncio.wait_for(first_server_task, 1.0)
        await server.stop()


@pytest.mark.asyncio
async def test_reset_reack_after_socket_drop_unblocks_successor_without_second_sweep() -> None:
    server = BrowserExtensionBrokerServer(19777)
    relay = ExtensionRelayServer(
        "extension-secret",
        19777,
        server._handle_extension_event,
        server._handle_disconnect,
        control_pairing_only=True,
    )
    extension = FakeExtension()
    first_websocket = FakeExtensionWebSocket(relay, extension)
    relay._websocket = first_websocket  # type: ignore[assignment]
    relay.extension_protocol_version = 2
    server._relay = relay

    await first_websocket.send_hello()
    await _eventually(lambda: not server._extension_reset_quarantined)
    initial_sweep_count = extension.reset_sweep_count
    first = BrokerClient(19777, _ignore_event, auto_spawn=False)
    second = BrokerClient(19777, _ignore_event, auto_spawn=False)
    first_server_task = await _connect_over_socketpair(server, first)

    extension.scoped_tabs = [{"tabId": 71, "url": "https://private.test", "title": "Private"}]
    extension.attached_tabs = {71}
    relay.scoped_tabs = list(extension.scoped_tabs)
    extension.reset_started.clear()
    extension.reset_gate.clear()
    await first.stop()
    await asyncio.wait_for(extension.reset_started.wait(), 1.0)
    reset_identity = (server._extension_reset_epoch, server._extension_reset_generation)

    await first_websocket.close(code=1001, message=b"transport lost")
    await relay._handle_disconnect(first_websocket)  # type: ignore[arg-type]
    extension.reset_gate.set()
    await _eventually(lambda: extension.reset_sweep_count == initial_sweep_count + 1)
    await _eventually(lambda: not first_websocket.reset_tasks)
    await asyncio.wait_for(first_server_task, 1.0)

    assert (extension.last_reset_epoch, extension.last_reset_generation) == reset_identity
    assert extension.last_reset_ok is True
    assert server._extension_reset_quarantined

    second_websocket = FakeExtensionWebSocket(relay, extension)
    await relay._activate_connection(second_websocket, 2)  # type: ignore[arg-type]
    await second_websocket.send_hello()
    await _eventually(lambda: not server._extension_reset_quarantined)

    replayed_resets = [frame for frame in second_websocket.frames if frame.get("type") == "extension.reset"]
    assert [(frame["epoch"], frame["generation"]) for frame in replayed_resets] == [reset_identity]
    assert extension.reset_sweep_count == initial_sweep_count + 1

    second_server_task = await _connect_over_socketpair(server, second)
    assert second.broker_connected
    assert second.scoped_tabs == []
    await second.stop()
    await asyncio.wait_for(second_server_task, 1.0)
    await server.stop()


@pytest.mark.asyncio
async def test_reset_send_deadline_surfaces_structured_timeout(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(broker_server_module, "EXTENSION_RESET_TIMEOUT_SECONDS", 0.01)
    server = BrowserExtensionBrokerServer(19777)
    relay = BlockingResetSendRelay(
        "extension-secret",
        19777,
        server._handle_extension_event,
        server._handle_disconnect,
    )
    relay.connected = True
    relay.extension_protocol_version = 2
    server._relay = relay
    server._extension_reset_quarantined = False
    server._extension_reset_error = None
    first = BrokerClient(19777, _ignore_event, auto_spawn=False)
    first_server_task = await _connect_over_socketpair(server, first)

    await first.stop()
    await asyncio.wait_for(relay.reset_send_started.wait(), 0.2)
    await asyncio.wait_for(first_server_task, 0.2)

    assert relay.reset_send_cancelled.is_set()
    assert server._extension_reset_error == "EXTENSION_RESET_TIMEOUT"
    second = BrokerClient(19777, _ignore_event, auto_spawn=False)
    with pytest.raises(BrowserExtensionBrokerError) as error_info:
        await _connect_over_socketpair(server, second)
    assert error_info.value.code == "EXTENSION_RESET_TIMEOUT"
    await second.stop()
    await server.stop()


@pytest.mark.asyncio
async def test_owner_release_resets_timed_out_extension_request_before_reenrollment() -> None:
    server = BrowserExtensionBrokerServer(19777)
    relay = ExtensionRelayServer(
        "extension-secret",
        19777,
        server._handle_extension_event,
        server._handle_disconnect,
        control_pairing_only=True,
    )

    extension = FakeExtension()
    websocket = FakeExtensionWebSocket(relay, extension)
    relay._websocket = websocket  # type: ignore[assignment]
    relay.extension_protocol_version = 2
    server._relay = relay
    await websocket.send_hello()
    await _eventually(lambda: not server._extension_reset_quarantined)
    first = BrokerClient(19777, _ignore_event, auto_spawn=False)
    second = BrokerClient(19777, _ignore_event, auto_spawn=False)
    first_server_task = await _connect_over_socketpair(server, first)
    with pytest.raises(ExtensionRequestError, match="timed out"):
        await first.request("tabs.activate", {"tabId": 7}, timeout=0.01)
    assert relay.pending_request_count == 1
    await first.stop()
    await asyncio.wait_for(first_server_task, 1.0)
    assert relay.pending_request_count == 0
    second_server_task = await _connect_over_socketpair(server, second)
    assert second.scoped_tabs == []
    await second.stop()
    await asyncio.wait_for(second_server_task, 1.0)
    await server.stop()


@pytest.mark.asyncio
@pytest.mark.parametrize("limit_name", ["MAX_REQUESTS_PER_CLIENT", "MAX_GLOBAL_REQUESTS"])
async def test_retained_timed_out_extension_requests_remain_bounded(
    monkeypatch: pytest.MonkeyPatch,
    limit_name: str,
) -> None:
    monkeypatch.setattr(broker_server_module, limit_name, 1)
    server = BrowserExtensionBrokerServer(19777)
    relay = ExtensionRelayServer(
        "extension-secret",
        19777,
        server._handle_extension_event,
        server._handle_disconnect,
        control_pairing_only=True,
    )

    class WebSocket:
        closed = False

        def __init__(self) -> None:
            self.requests: list[dict] = []

        async def send_json(self, frame: dict) -> None:
            self.requests.append(frame)

        async def close(self, *, code: int, message: bytes) -> None:
            self.closed = True

    websocket = WebSocket()
    relay._websocket = websocket  # type: ignore[assignment]
    relay._connected_event.set()
    server._relay = relay
    server._extension_reset_quarantined = False
    client = BrokerClient(19777, _ignore_event, auto_spawn=False)
    server_task = await _connect_over_socketpair(server, client)
    try:
        with pytest.raises(ExtensionRequestError, match="timed out"):
            await client.request("tabs.activate", {"tabId": 7}, timeout=0.01)
        assert relay.pending_request_count == 1
        active = server._active_client
        assert active is not None
        assert len(active.request_ids) == 1
        assert server._global_requests == 1

        with pytest.raises(BrowserExtensionBrokerError, match="RESOURCE_LIMIT"):
            await client.request("tabs.activate", {"tabId": 8}, timeout=0.01)
        assert len(websocket.requests) == 1

        await relay._handle_text_frame(
            relay._websocket,
            json.dumps({"v": 1, "type": "response", "id": websocket.requests[0]["id"], "ok": True, "result": {}}),
        )
        await _eventually(lambda: relay.pending_request_count == 0)
        await _eventually(lambda: not active.request_ids and server._global_requests == 0)

        with pytest.raises(ExtensionRequestError, match="timed out"):
            await client.request("tabs.activate", {"tabId": 9}, timeout=0.01)
        assert len(websocket.requests) == 2
    finally:
        await client.stop()
        await asyncio.wait_for(server_task, 1.0)
        await relay._handle_disconnect(relay._websocket)
        await server.stop()


@pytest.mark.asyncio
async def test_retained_timed_out_request_holds_inbound_bytes_until_terminal(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    server = BrowserExtensionBrokerServer(19777)
    relay = ExtensionRelayServer(
        "extension-secret",
        19777,
        server._handle_extension_event,
        server._handle_disconnect,
        control_pairing_only=True,
    )

    class WebSocket:
        closed = False

        def __init__(self) -> None:
            self.requests: list[dict] = []

        async def send_json(self, frame: dict) -> None:
            self.requests.append(frame)

        async def close(self, *, code: int, message: bytes) -> None:
            self.closed = True

    websocket = WebSocket()
    relay._websocket = websocket  # type: ignore[assignment]
    relay._connected_event.set()
    server._relay = relay
    server._extension_reset_quarantined = False
    client = BrokerClient(19777, _ignore_event, auto_spawn=False)
    server_task = await _connect_over_socketpair(server, client)
    try:
        with pytest.raises(ExtensionRequestError, match="timed out"):
            await client.request("debugger.send", {"tabId": 7, "params": {"padding": "x" * 4096}}, timeout=0.01)
        active = server._active_client
        assert active is not None
        assert active.inbound_bytes > 4096
        assert server._global_inbound_bytes == active.inbound_bytes

        monkeypatch.setattr(broker_server_module, "MAX_CLIENT_INBOUND_BYTES", active.inbound_bytes)
        with pytest.raises(BrowserExtensionNotConnectedError):
            await client.request("debugger.send", {"tabId": 7, "params": {"padding": "y" * 4096}}, timeout=0.01)
        assert len(websocket.requests) == 1
        await asyncio.wait_for(server_task, 1.0)

        await relay._handle_text_frame(
            relay._websocket,
            json.dumps({"v": 1, "type": "response", "id": websocket.requests[0]["id"], "ok": True, "result": {}}),
        )
        await _eventually(lambda: active.inbound_bytes == 0 and server._global_inbound_bytes == 0)
    finally:
        await client.stop()
        if not server_task.done():
            await asyncio.wait_for(server_task, 1.0)
        await relay._handle_disconnect(relay._websocket)
        await server.stop()


@pytest.mark.asyncio
async def test_retained_timed_out_requests_enforce_per_tab_limit_until_terminal(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(broker_server_module, "MAX_REQUESTS_PER_TAB", 1)
    server = BrowserExtensionBrokerServer(19777)
    relay = ExtensionRelayServer(
        "extension-secret",
        19777,
        server._handle_extension_event,
        server._handle_disconnect,
        control_pairing_only=True,
    )

    class WebSocket:
        closed = False

        def __init__(self) -> None:
            self.requests: list[dict] = []

        async def send_json(self, frame: dict) -> None:
            self.requests.append(frame)

        async def close(self, *, code: int, message: bytes) -> None:
            self.closed = True

    websocket = WebSocket()
    relay._websocket = websocket  # type: ignore[assignment]
    relay._connected_event.set()
    server._relay = relay
    server._extension_reset_quarantined = False
    client = BrokerClient(19777, _ignore_event, auto_spawn=False)
    server_task = await _connect_over_socketpair(server, client)
    try:
        with pytest.raises(ExtensionRequestError, match="timed out"):
            await client.request("tabs.activate", {"tabId": 7}, timeout=0.01)
        assert server._tab_request_counts == {7: 1}

        with pytest.raises(BrowserExtensionBrokerError, match="RESOURCE_LIMIT"):
            await client.request("debugger.send", {"tabId": 7}, timeout=0.01)
        assert len(websocket.requests) == 1

        with pytest.raises(ExtensionRequestError, match="timed out"):
            await client.request("tabs.activate", {"tabId": 8}, timeout=0.01)
        assert server._tab_request_counts == {7: 1, 8: 1}

        await relay._handle_text_frame(
            relay._websocket,
            json.dumps({"v": 1, "type": "response", "id": websocket.requests[0]["id"], "ok": True, "result": {}}),
        )
        await _eventually(lambda: server._tab_request_counts == {8: 1})

        with pytest.raises(ExtensionRequestError, match="timed out"):
            await client.request("debugger.send", {"tabId": 7}, timeout=0.01)
        assert server._tab_request_counts == {7: 1, 8: 1}
        assert len(websocket.requests) == 3
    finally:
        await client.stop()
        await asyncio.wait_for(server_task, 1.0)
        await relay._handle_disconnect(relay._websocket)
        await _eventually(lambda: not server._tab_request_counts)
        await server.stop()


@pytest.mark.asyncio
@pytest.mark.parametrize("tab_id", [5.0, "5"])
async def test_integer_like_tab_ids_enforce_per_tab_limit(
    monkeypatch: pytest.MonkeyPatch,
    tab_id: float | str,
) -> None:
    monkeypatch.setattr(broker_server_module, "MAX_REQUESTS_PER_TAB", 1)
    server = BrowserExtensionBrokerServer(19777)
    relay = BlockingRelay("extension-secret", 19777, server._handle_extension_event, server._handle_disconnect)
    server._relay = relay
    client = BrokerClient(19777, _ignore_event, auto_spawn=False)
    server_task = await _connect_over_socketpair(server, client)
    connection = server._active_client
    assert connection is not None
    request = {"op": "tabs.activate", "args": {"tabId": tab_id}, "timeout": 30.0}
    first = asyncio.create_task(server._dispatch(connection, "extension.request", request))
    try:
        await asyncio.wait_for(relay.request_started.wait(), 1.0)
        assert server._tab_request_counts == {5: 1}

        with pytest.raises(BrowserExtensionBrokerError) as error_info:
            await asyncio.wait_for(server._dispatch(connection, "extension.request", request), 0.1)

        assert error_info.value.code == "RESOURCE_LIMIT"
        relay.release_request.set()
        result = await first
        assert result["args"]["tabId"] == 5
        assert type(result["args"]["tabId"]) is int
    finally:
        relay.release_request.set()
        if not first.done():
            await first
        await client.stop()
        await asyncio.wait_for(server_task, 1.0)
        await server.stop()


@pytest.mark.asyncio
@pytest.mark.parametrize("tab_id", [5.5, "5.0", "5.5", {"value": 5}])
async def test_invalid_tab_ids_are_rejected_before_forwarding(tab_id: object) -> None:
    server = BrowserExtensionBrokerServer(19777)
    relay = BlockingRelay("extension-secret", 19777, server._handle_extension_event, server._handle_disconnect)
    server._relay = relay
    client = BrokerClient(19777, _ignore_event, auto_spawn=False)
    server_task = await _connect_over_socketpair(server, client)
    try:
        with pytest.raises(BrowserExtensionBrokerError) as error_info:
            await client.request("tabs.activate", {"tabId": tab_id})

        assert error_info.value.code == "INVALID_FRAME"
        assert not relay.request_started.is_set()
        assert server._tab_request_counts == {}
    finally:
        relay.release_request.set()
        await client.stop()
        await asyncio.wait_for(server_task, 1.0)
        await server.stop()


@pytest.mark.asyncio
@pytest.mark.parametrize("frame_type", ["ping", "pong"])
async def test_oversized_control_heartbeat_is_rejected(frame_type: str) -> None:
    server = BrowserExtensionBrokerServer(19777)
    server._relay = FakeRelay("extension-secret", 19777, server._handle_extension_event, server._handle_disconnect)
    client = BrokerClient(19777, _ignore_event, auto_spawn=False)
    server_socket, client_socket = socket.socketpair()
    server_reader, server_writer = await asyncio.open_connection(sock=server_socket)
    client_reader, client_writer = await asyncio.open_connection(sock=client_socket)
    server_task = asyncio.create_task(server._handle_connection(server_reader, server_writer))
    await client._authenticate(client_reader, client_writer)
    try:
        await write_frame(
            client_writer,
            {"v": 1, "type": frame_type, "padding": "x" * CONTROL_FRAME_LIMIT},
        )
        await asyncio.wait_for(server_task, 1.0)
        assert server._active_client is None
    finally:
        client_writer.close()
        with suppress(BrokenPipeError):
            await client_writer.wait_closed()
        await server.stop()


@pytest.mark.asyncio
async def test_oversized_control_prefix_is_rejected_without_waiting_for_declared_body() -> None:
    server = BrowserExtensionBrokerServer(19777)
    server._relay = FakeRelay("extension-secret", 19777, server._handle_extension_event, server._handle_disconnect)
    client = BrokerClient(19777, _ignore_event, auto_spawn=False)
    server_socket, client_socket = socket.socketpair()
    server_reader, server_writer = await asyncio.open_connection(sock=server_socket)
    client_reader, client_writer = await asyncio.open_connection(sock=client_socket)
    server_task = asyncio.create_task(server._handle_connection(server_reader, server_writer))
    await client._authenticate(client_reader, client_writer)
    try:
        client_writer.write((1024 * 1024).to_bytes(4, "big") + b'{"v":1,"type":"pong","padding":')
        await client_writer.drain()
        await asyncio.wait_for(server_task, 1.0)
        assert server._active_client is None
    finally:
        client_writer.close()
        await client_writer.wait_closed()
        await server.stop()


@pytest.mark.asyncio
async def test_failed_sender_does_not_leak_active_client_slot(monkeypatch: pytest.MonkeyPatch) -> None:
    server = BrowserExtensionBrokerServer(19777)
    server._relay = FakeRelay("extension-secret", 19777, server._handle_extension_event, server._handle_disconnect)
    client = BrokerClient(19777, _ignore_event, auto_spawn=False)
    server_task = await _connect_over_socketpair(server, client)

    async def fail_write(*_args: object) -> None:
        raise BrokenPipeError

    monkeypatch.setattr(server, "_write_encoded", fail_write)
    active = server._active_client
    assert active is not None
    await server._send_event(active, "extension.disconnected", {})
    await _eventually(lambda: active.sender_task is not None and active.sender_task.done())
    await client.stop()
    await asyncio.wait_for(server_task, 1.0)
    assert server._active_client is None
    await server.stop()


@pytest.mark.asyncio
async def test_failed_enrollment_response_never_publishes_slot_or_hangs_stop(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    server = BrowserExtensionBrokerServer(19777)
    server._relay = FakeRelay("extension-secret", 19777, server._handle_extension_event, server._handle_disconnect)
    original_write_frame = broker_server_module.write_frame
    writes = 0

    async def fail_enrollment_response(*args: object, **kwargs: object) -> int:
        nonlocal writes
        writes += 1
        if writes == 2:
            raise BrokenPipeError
        return await original_write_frame(*args, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(broker_server_module, "write_frame", fail_enrollment_response)
    client = BrokerClient(19777, _ignore_event, auto_spawn=False)
    server_socket, client_socket = socket.socketpair()
    server_reader, server_writer = await asyncio.open_connection(sock=server_socket)
    client_reader, client_writer = await asyncio.open_connection(sock=client_socket)
    server_task = asyncio.create_task(server._handle_connection(server_reader, server_writer))
    try:
        with pytest.raises(EOFError):
            await client._authenticate(client_reader, client_writer)
        await asyncio.wait_for(server_task, 1.0)
        assert server._active_client is None
        monkeypatch.setattr(broker_server_module, "write_frame", original_write_frame)
        replacement = BrokerClient(19777, _ignore_event, auto_spawn=False)
        replacement_server_task = await _connect_over_socketpair(server, replacement)
        await replacement.stop()
        await asyncio.wait_for(replacement_server_task, 1.0)
        await asyncio.wait_for(server.stop(), 1.0)
    finally:
        client_writer.close()
        await client_writer.wait_closed()


@pytest.mark.asyncio
async def test_global_request_cap_includes_operator_connections(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(broker_server_module, "MAX_GLOBAL_REQUESTS", 1)
    server = BrowserExtensionBrokerServer(19777)
    server._relay = FakeRelay("extension-secret", 19777, server._handle_extension_event, server._handle_disconnect)
    first = BrokerClient(19777, _ignore_event, auto_spawn=False, operator=True)
    second = BrokerClient(19777, _ignore_event, auto_spawn=False, operator=True)
    first_server_task = await _connect_over_socketpair(server, first)
    second_server_task = await _connect_over_socketpair(server, second)
    started = asyncio.Event()
    release = asyncio.Event()
    original_dispatch = server._dispatch

    async def blocking_dispatch(connection: _ClientConnection, op: str, args: dict) -> dict:
        if op == "broker.status" and not started.is_set():
            started.set()
            await release.wait()
        return await original_dispatch(connection, op, args)

    monkeypatch.setattr(server, "_dispatch", blocking_dispatch)
    first_request = asyncio.create_task(first.broker_status())
    await asyncio.wait_for(started.wait(), 1.0)
    try:
        with pytest.raises(BrowserExtensionBrokerError, match="RESOURCE_LIMIT"):
            await second.broker_status()
    finally:
        release.set()
        await first_request
        await first.stop()
        await second.stop()
        await asyncio.wait_for(first_server_task, 1.0)
        await asyncio.wait_for(second_server_task, 1.0)
        await server.stop()


def test_global_output_cap_includes_operator_connections(monkeypatch: pytest.MonkeyPatch) -> None:
    size = len(encode_frame(event_frame("test", {"value": "x" * 32})))
    monkeypatch.setattr(broker_server_module, "MAX_GLOBAL_OUTPUT_BYTES", size)
    server = BrowserExtensionBrokerServer(19777)
    reader = asyncio.StreamReader()

    class Writer:
        def close(self) -> None:
            return None

    first = _ClientConnection("first", 1, reader, Writer(), operator=True)  # type: ignore[arg-type]
    second = _ClientConnection("second", 1, reader, Writer(), operator=True)  # type: ignore[arg-type]

    assert server._reserve_output(first, size)
    assert not server._reserve_output(second, size)
    server._release_output(first, size)
    assert server._global_output_bytes == 0


def test_client_output_budget_admits_one_operation_frame_and_backpressures_multiples() -> None:
    server = BrowserExtensionBrokerServer(19777)
    reader = asyncio.StreamReader()

    class Writer:
        def close(self) -> None:
            return None

    connection = _ClientConnection("client", 1, reader, Writer())  # type: ignore[arg-type]
    twenty_mib_frame_size = 20 * 1024 * 1024

    assert MAX_CLIENT_OUTPUT_BYTES == MAX_ENCODED_OPERATION_FRAME_BYTES + MAX_ENCODED_CONTROL_FRAME_BYTES
    assert server._reserve_output(connection, twenty_mib_frame_size)
    assert not server._reserve_output(connection, twenty_mib_frame_size)
    assert connection.queued_output_bytes == twenty_mib_frame_size
    server._release_output(connection, twenty_mib_frame_size)
    assert connection.queued_output_bytes == 0
    assert server._global_output_bytes == 0


@pytest.mark.asyncio
async def test_completed_request_ids_are_released() -> None:
    server = BrowserExtensionBrokerServer(19777)
    server._relay = FakeRelay("extension-secret", 19777, server._handle_extension_event, server._handle_disconnect)
    client = BrokerClient(19777, _ignore_event, auto_spawn=False)
    server_task = await _connect_over_socketpair(server, client)
    try:
        for _ in range(3):
            await client.broker_status()
        active = server._active_client
        assert active is not None
        await _eventually(lambda: not active.request_ids)
    finally:
        await client.stop()
        await asyncio.wait_for(server_task, 1.0)
        await server.stop()


@pytest.mark.asyncio
async def test_extension_hello_does_not_orphan_pending_pairing_nonce() -> None:
    server = BrowserExtensionBrokerServer(19777, pairing_opener=lambda _url: True)
    relay = FakeRelay("extension-secret", 19777, server._handle_extension_event, server._handle_disconnect)
    server._relay = relay
    client = BrokerClient(19777, _ignore_event, auto_spawn=False)
    server_task = await _connect_over_socketpair(server, client)
    try:
        await client.begin_pairing()
        owner = server._pairing_owner
        await server._handle_extension_event("extension.hello", {"scopedTabs": []})
        assert server._pairing_owner == owner
        assert relay.nonce == "pairing-nonce-sentinel"
    finally:
        await client.stop()
        await asyncio.wait_for(server_task, 1.0)
        await server.stop()


@pytest.mark.asyncio
async def test_spawned_daemon_detaches_only_after_server_readiness(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    events: list[str] = []

    class ReadyServer:
        def __init__(self, _port: int, *, base_dir: object = None) -> None:
            self._shutdown_event = asyncio.Event()
            self._shutdown_event.set()
            self.running = False

        async def start(self) -> None:
            events.append("start")
            self.running = True

        async def stop(self) -> None:
            events.append("stop")
            self.running = False

    monkeypatch.setattr(broker_server_module, "BrowserExtensionBrokerServer", ReadyServer)
    monkeypatch.setattr(broker_server_module, "enable_broker_state_locked", lambda _paths: (_paths, "existing"))
    monkeypatch.setattr(broker_server_module.os, "setsid", lambda: events.append("setsid"))
    monkeypatch.setattr(
        broker_server_module,
        "write_readiness",
        lambda _fd, status, **_fields: events.append(status),
    )
    monkeypatch.setattr(broker_server_module.os, "close", lambda _fd: None)
    monkeypatch.setattr(broker_server_module, "_detach_startup_stderr", lambda: events.append("stderr-detached"))

    await broker_server_module.run_broker_daemon(19777, base_dir=tmp_path / "run", ready_fd=19)

    assert events == ["start", "READY", "setsid", "stderr-detached", "stop"]


@pytest.mark.asyncio
async def test_pre_ready_daemon_aborts_when_starter_dies_and_releases_daemon_lock(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    events: list[str] = []
    base_dir = tmp_path / "run"
    paths = broker_server_module.ensure_run_directory(19777, base_dir=base_dir)

    class HungServer:
        def __init__(self, _port: int, *, base_dir: object = None) -> None:
            self._shutdown_event = asyncio.Event()
            self.running = False
            self.lock = broker_server_module.OwnerFileLock(paths.daemon_lock)

        async def start(self) -> None:
            events.append("start")
            assert self.lock.acquire(blocking=False)
            await asyncio.Event().wait()

        async def stop(self) -> None:
            events.append("stop")
            self.lock.release()

    monkeypatch.delenv(broker_server_module.READY_FD_ENV, raising=False)
    monkeypatch.setenv(broker_server_module.STARTER_PID_ENV, "12345")
    monkeypatch.setenv(broker_server_module.STARTER_PROCESS_START_ENV, "starter-marker")
    monkeypatch.setattr(broker_server_module, "BrowserExtensionBrokerServer", HungServer)
    monkeypatch.setattr(broker_server_module, "enable_broker_state_locked", lambda _paths: (_paths, "existing"))
    monkeypatch.setattr(broker_server_module, "process_identity_matches", lambda _pid, _marker: False)

    with pytest.raises(BrowserExtensionBrokerError, match="STARTER_EXITED"):
        await broker_server_module.run_broker_daemon(19777, base_dir=base_dir)

    assert events == ["start", "stop"]
    replacement = broker_server_module.OwnerFileLock(paths.daemon_lock)
    assert replacement.acquire(blocking=False)
    replacement.release()


@pytest.mark.asyncio
async def test_daemon_auto_enables_under_spawn_lock_and_clears_cached_failure(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    home = tmp_path / "home"
    token_dir = home / ".skyvern"
    token_dir.mkdir(parents=True, mode=0o700)
    token_dir.chmod(0o700)
    legacy_path = token_dir / "browser_extension_token"
    legacy_path.write_text("legacy-secret")
    legacy_path.chmod(0o600)
    base_dir = tmp_path / "run"
    paths = broker_server_module.ensure_run_directory(19777, base_dir=base_dir)
    record_startup_failure(
        paths,
        code="BROKER_NOT_ENABLED",
        port=19777,
        observed_state_fingerprint="missing",
    )
    events: list[str] = []

    class ReadyServer:
        def __init__(self, _port: int, *, base_dir: object = None) -> None:
            self._shutdown_event = asyncio.Event()
            self._shutdown_event.set()
            self.running = False

        async def start(self) -> None:
            events.append("start")
            self.running = True

        async def stop(self) -> None:
            events.append("stop")
            self.running = False

    original_enable = broker_server_module.enable_broker_state_locked

    def enable_while_locked(locked_paths: BrokerPaths) -> tuple[BrokerPaths, str]:
        contender = broker_server_module.OwnerFileLock(paths.spawn_lock)
        assert not contender.acquire(blocking=False)
        return original_enable(locked_paths)

    monkeypatch.setenv("HOME", str(home))
    monkeypatch.delenv(broker_server_module.SPAWN_LOCK_FD_ENV, raising=False)
    monkeypatch.setattr(broker_server_module, "BrowserExtensionBrokerServer", ReadyServer)
    monkeypatch.setattr(broker_server_module, "enable_broker_state_locked", enable_while_locked)
    monkeypatch.setattr(broker_server_module.os, "setsid", lambda: None)
    monkeypatch.setattr(broker_server_module, "_detach_startup_stderr", lambda: None)
    read_fd, write_fd = os.pipe()
    try:
        await broker_server_module.run_broker_daemon(19777, base_dir=base_dir, ready_fd=write_fd)
        assert read_readiness(read_fd, timeout=0.1) == {"status": "READY", "port": 19777}
    finally:
        os.close(read_fd)

    assert events == ["start", "stop"]
    assert paths.extension_secret.read_text() == "legacy-secret"
    assert legacy_path.read_text() == "legacy-secret"
    assert paths.extension_secret.stat().st_mode & 0o777 == 0o600
    assert legacy_path.stat().st_mode & 0o777 == 0o600
    assert paths.leases.read_text() == '{"leases":[],"schemaVersion":1}'
    assert not paths.startup_failure.exists()


@pytest.mark.asyncio
async def test_daemon_auto_enable_reports_unsafe_journal_without_touching_legacy_credential(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    home = tmp_path / "home"
    token_dir = home / ".skyvern"
    token_dir.mkdir(parents=True, mode=0o700)
    token_dir.chmod(0o700)
    legacy_path = token_dir / "browser_extension_token"
    legacy_path.write_text("legacy-secret")
    legacy_path.chmod(0o600)
    base_dir = tmp_path / "run"
    paths = broker_server_module.ensure_run_directory(19777, base_dir=base_dir)
    paths.leases.write_text("not-json")
    paths.leases.chmod(0o600)

    class UnstartedServer:
        def __init__(self, _port: int, *, base_dir: object = None) -> None:
            self._shutdown_event = asyncio.Event()
            self.running = False

        async def start(self) -> None:
            raise AssertionError("unsafe state must fail before server startup")

        async def stop(self) -> None:
            return None

    monkeypatch.setenv("HOME", str(home))
    monkeypatch.delenv(broker_server_module.SPAWN_LOCK_FD_ENV, raising=False)
    monkeypatch.setattr(broker_server_module, "BrowserExtensionBrokerServer", UnstartedServer)
    read_fd, write_fd = os.pipe()
    try:
        with pytest.raises(BrowserExtensionBrokerError) as error_info:
            await broker_server_module.run_broker_daemon(19777, base_dir=base_dir, ready_fd=write_fd)
        assert error_info.value.code == "UNSAFE_STATE"
        assert read_readiness(read_fd, timeout=0.1) == {
            "status": "ERROR",
            "code": "UNSAFE_STATE",
        }
    finally:
        os.close(read_fd)

    assert legacy_path.read_text() == "legacy-secret"
    assert not paths.extension_secret.exists()


async def _ignore_event(_event: str, _params: dict) -> None:
    return None


async def _eventually(predicate: Callable[[], bool]) -> None:
    for _ in range(100):
        if predicate():
            return
        await asyncio.sleep(0.01)
    raise AssertionError("condition did not become true")


async def _connect_over_socketpair(
    server: BrowserExtensionBrokerServer,
    client: BrokerClient,
) -> asyncio.Task[None]:
    relay = server.relay
    if isinstance(relay, FakeRelay) and relay.auto_connect and not relay.connected:
        await relay.hello()
        await _eventually(
            lambda: (
                not server._extension_reset_quarantined
                or server._extension_reset_error not in {None, "EXTENSION_RESET_IN_PROGRESS"}
            )
        )
    server_socket, client_socket = socket.socketpair()
    server_reader, server_writer = await asyncio.open_connection(sock=server_socket)
    client_reader, client_writer = await asyncio.open_connection(sock=client_socket)
    server_task = asyncio.create_task(server._handle_connection(server_reader, server_writer))
    try:
        connection_generation = await client._authenticate(client_reader, client_writer)
    except BaseException:
        client_writer.close()
        await client_writer.wait_closed()
        await asyncio.wait_for(server_task, 1.0)
        raise
    client._reader = client_reader
    client._writer = client_writer
    client._connection_generation = connection_generation
    client._transport_generation += 1
    client._reader_task = asyncio.create_task(
        client._read_loop(client_reader, client_writer, client._transport_generation)
    )
    return server_task
