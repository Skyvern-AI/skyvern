from __future__ import annotations

import asyncio
import json
import os
import shutil
import socket
from collections.abc import Awaitable, Callable
from contextlib import suppress
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from skyvern.browser_extension import broker_server as broker_server_module
from skyvern.browser_extension.broker_client import BrokerClient
from skyvern.browser_extension.broker_protocol import (
    BROKER_GENERATION,
    CLIENT_OUTPUT_RECOVERY_BYTES,
    CLIENT_OUTPUT_STALL_SECONDS,
    CONTROL_FRAME_LIMIT,
    MAX_CLIENT_OUTPUT_BYTES,
    MAX_ENCODED_CONTROL_FRAME_BYTES,
    MAX_ENCODED_OPERATION_FRAME_BYTES,
    TAB_REQUEST_QUEUE_WAIT_SECONDS,
    decode_frame,
    encode_frame,
    event_frame,
    new_nonce,
    request_frame,
    write_frame,
)
from skyvern.browser_extension.broker_server import BrowserExtensionBrokerServer, _ClientConnection
from skyvern.browser_extension.broker_state import (
    BrokerPaths,
    ensure_run_directory,
    publish_broker_state,
    read_broker_state,
    read_readiness,
    record_startup_failure,
)
from skyvern.browser_extension.errors import (
    BrowserExtensionBrokerError,
    BrowserExtensionNotConnectedError,
    ExtensionRequestError,
)
from skyvern.browser_extension.package_extension import EXTENSION_DIR, compute_extension_source_hash
from skyvern.browser_extension.relay import ExtensionRelayServer
from tests.unit.browser_extension.home_guard import _test_broker_base_dir


class FakeExtension:
    def __init__(self) -> None:
        self.protocol_version = 2
        self.build_hash: str | None = None
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
                        "scopeEventOrigins": True,
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
        self.requests: list[tuple[str, dict]] = []
        self.request_timeouts: list[float] = []
        self.connection_cycles = 0
        self.extension_protocol_version: int | None = self.extension.protocol_version
        self.extension_build_hash: str | None = self.extension.build_hash
        self.extension_connection_generation = 1
        self.reset_frames: list[dict] = []
        self.reset_tasks: set[asyncio.Task[None]] = set()
        self.sent_events: list[tuple[str, dict]] = []

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

    async def send_event(self, event: str, params: dict) -> bool:
        self.sent_events.append((event, dict(params)))
        return self.connected

    async def hello(self) -> None:
        self.connected = True
        self.extension_protocol_version = self.extension.protocol_version
        self.extension_build_hash = self.extension.build_hash
        self._scoped_tabs = list(self.extension.scoped_tabs)
        await self.on_event(
            "extension.hello",
            {
                "protocolVersion": self.extension_protocol_version,
                "extensionVersion": "test",
                "buildHash": self.extension_build_hash,
                "scopeEventOrigins": True,
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
        self.requests.append((op, dict(args)))
        if on_registered is not None:
            on_registered()
        if op == "debugger.detach":
            tab_id = args.get("tabId")
            if type(tab_id) is int:
                self.scoped_tabs = [tab for tab in self.scoped_tabs if tab.get("tabId") != tab_id]
                await self.on_event("scope.tabRemoved", {"tabId": tab_id, "reason": "detached"})
        if on_terminal is not None:
            on_terminal()
        return {"op": op, "args": args, "timeout": timeout}

    async def wait_pending_requests(self, _timeout: float) -> bool:
        return self.pending_request_count == 0

    def get_or_create_pairing_nonce(self) -> str:
        return self.nonce

    def cancel_pairing_nonce(self) -> None:
        self.nonce = "cancelled"


def _fake_relay_factory(
    secret: str,
    port: int,
    on_event: Callable[[str, dict], Awaitable[None]],
    on_disconnect: Callable[[], Awaitable[None]] | None,
    _on_pairing_complete: Callable[[], Awaitable[dict[str, str] | None]] | None,
) -> FakeRelay:
    return FakeRelay(secret, port, on_event, on_disconnect)


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
        self.requests.append((op, dict(args)))
        if on_registered is not None:
            on_registered()
        self.request_started.set()
        await self.release_request.wait()
        if on_terminal is not None:
            on_terminal()
        return {"op": op, "args": args, "timeout": timeout}


class QueueRelay(FakeRelay):
    def __init__(
        self,
        token: str,
        port: int,
        on_event: Callable[[str, dict], Awaitable[None]],
        on_disconnect: Callable[[], Awaitable[None]] | None,
    ) -> None:
        super().__init__(token, port, on_event, on_disconnect)
        self.request_gates: list[asyncio.Event] = []

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
        del retain_until_terminal
        self.requests.append((op, dict(args)))
        self.request_timeouts.append(timeout)
        if on_registered is not None:
            on_registered()
        gate = asyncio.Event()
        self.request_gates.append(gate)
        await gate.wait()
        if op == "debugger.detach":
            tab_id = args.get("tabId")
            if type(tab_id) is int:
                self.scoped_tabs = [tab for tab in self.scoped_tabs if tab.get("tabId") != tab_id]
                await self.on_event("scope.tabRemoved", {"tabId": tab_id, "reason": "detached"})
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


def test_default_server_paths_are_isolated_from_home() -> None:
    server = BrowserExtensionBrokerServer(19777, base_dir=_test_broker_base_dir())

    assert server.paths.run_dir.is_relative_to(Path("/tmp"))
    assert not server.paths.run_dir.is_relative_to(Path.home())


@pytest.mark.asyncio
async def test_non_owner_stop_preserves_owner_state_and_control_socket(
    monkeypatch: pytest.MonkeyPatch, short_broker_base_dir: Path
) -> None:
    token = "extension-secret"
    base_dir = short_broker_base_dir
    owner_paths = ensure_run_directory(19777, base_dir=base_dir, prepare_control_endpoint=False)
    owner_paths.extension_secret.write_text(token)
    owner_paths.extension_secret.chmod(0o600)

    owner = BrowserExtensionBrokerServer(19777, base_dir=base_dir, relay_factory=_fake_relay_factory)
    await owner.start()
    try:
        owner_state = read_broker_state(owner_paths)
        assert owner_state is not None
        assert owner_state.lifecycle == "ready"
        state_before = owner_paths.state.read_bytes()
        assert owner.paths.control_socket.is_socket()

        contender = BrowserExtensionBrokerServer(19777, base_dir=base_dir)
        assert owner.paths.control_socket == contender.paths.control_socket

        original_read = broker_server_module.read_broker_state
        read_attempts = 0

        def flaky_read(paths: BrokerPaths):
            nonlocal read_attempts
            read_attempts += 1
            if read_attempts < 3:
                raise OSError("transient state read failure")
            return original_read(paths)

        monkeypatch.setattr(broker_server_module, "read_broker_state", flaky_read)
        monkeypatch.setattr(broker_server_module.time, "sleep", lambda _seconds: None)
        assert owner._owns_published_state()
        assert read_attempts == 3

        await contender.stop()

        state_after = read_broker_state(owner_paths)
        assert state_after is not None
        assert state_after.lifecycle == "ready"
        assert owner_paths.state.read_bytes() == state_before
        assert owner.paths.control_socket.is_socket()

        await owner._cleanup_partial_start()
        stopped_state = read_broker_state(owner.paths)
        assert stopped_state is not None
        assert stopped_state.lifecycle == "stopped"
        assert stopped_state.cleanShutdown is False
        assert not owner.paths.control_socket.exists()
        assert owner._daemon_lock is None
    finally:
        await owner.stop()


@pytest.mark.asyncio
async def test_election_loss_preserves_owner_state_and_control_socket(
    monkeypatch: pytest.MonkeyPatch, short_broker_base_dir: Path
) -> None:
    token = "extension-secret"
    base_dir = short_broker_base_dir
    owner_paths = ensure_run_directory(19777, base_dir=base_dir, prepare_control_endpoint=False)
    owner_paths.extension_secret.write_text(token)
    owner_paths.extension_secret.chmod(0o600)

    owner = BrowserExtensionBrokerServer(19777, base_dir=base_dir, relay_factory=_fake_relay_factory)
    await owner.start()
    real_control_server = owner._control_server
    assert real_control_server is not None

    try:
        contender = BrowserExtensionBrokerServer(19777, base_dir=base_dir)
        assert owner.paths.control_socket == contender.paths.control_socket

        async def blocked_start() -> None:
            await asyncio.Event().wait()

        monkeypatch.setattr(contender, "start", blocked_start)
        monkeypatch.setenv(broker_server_module.STARTER_PID_ENV, "12345")
        monkeypatch.setenv(broker_server_module.STARTER_PROCESS_START_ENV, "starter-marker")
        monkeypatch.setattr(broker_server_module, "process_identity_matches", lambda _pid, _marker: False)

        with pytest.raises(BrowserExtensionBrokerError, match="STARTER_EXITED"):
            await broker_server_module._start_while_starter_alive(contender)
        await contender._cleanup_partial_start()

        owner_state = read_broker_state(owner_paths)
        assert owner_state is not None
        assert owner_state.lifecycle == "ready"
        assert owner.paths.control_socket.is_socket()

        owner._control_server = None
        for mismatch in ("pid", "bootId"):
            publish_broker_state(owner.paths, owner._state(lifecycle="ready", clean_shutdown=False))
            state = read_broker_state(owner.paths)
            assert state is not None
            mismatched_state = replace(
                state,
                **{
                    mismatch: state.pid + 1 if mismatch == "pid" else "foreign-boot-id",
                },
            )
            publish_broker_state(owner.paths, mismatched_state)
            state_before_cleanup = owner.paths.state.read_bytes()

            await owner._cleanup_partial_start()

            assert owner.paths.state.read_bytes() == state_before_cleanup
            assert read_broker_state(owner.paths) == mismatched_state
            assert owner.paths.control_socket.is_socket()
            if mismatch == "pid":
                daemon_lock = broker_server_module.OwnerFileLock(owner.paths.daemon_lock)
                assert daemon_lock.acquire(blocking=False)
                owner._daemon_lock = daemon_lock
    finally:
        real_control_server.close()
        await real_control_server.wait_closed()
        await owner.stop()


@pytest.mark.asyncio
async def test_server_allows_multiple_clients_without_exposing_pairing_material(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(broker_server_module, "MAX_AUTHENTICATED_CLIENTS", 2)
    monkeypatch.setattr(broker_server_module, "MAX_PENDING_CONNECTIONS", 1)
    opened: list[str] = []
    extension_secret = "extension-secret-sentinel"
    server = BrowserExtensionBrokerServer(
        19777,
        base_dir=_test_broker_base_dir(),
        pairing_opener=lambda url: not opened.append(url),
    )
    relay = FakeRelay(extension_secret, 19777, server._handle_extension_event, server._handle_disconnect)
    server._relay = relay
    first = BrokerClient(19777, _ignore_event, base_dir=_test_broker_base_dir(), auto_spawn=False)
    second = BrokerClient(19777, _ignore_event, base_dir=_test_broker_base_dir(), auto_spawn=False)
    third = BrokerClient(19777, _ignore_event, base_dir=_test_broker_base_dir(), auto_spawn=False)
    first_server_task = await _connect_over_socketpair(server, first)
    await _eventually(lambda: server._pending_connections == 0)
    second_server_task = await _connect_over_socketpair(server, second)
    await _eventually(lambda: server._pending_connections == 0)
    try:
        relay.scoped_tabs = [{"tabId": 7}, {"tabId": 8}]
        await relay.hello()
        first_status = await first.broker_status()
        second_status = await second.broker_status()
        pairing_result = await first.begin_pairing()
        first_response = await first.request("tabs.activate", {"tabId": 7})
        second_response = await second.request("tabs.activate", {"tabId": 8})

        assert first_status["clientCount"] == 2
        assert second_status["clientCount"] == 2
        assert first._client_id is not None
        assert second._client_id is not None
        assert first._client_id != second._client_id
        assert pairing_result["opened"] is True
        assert opened == ["http://127.0.0.1:19777/pair#pairing-nonce-sentinel"]
        assert first_response == {"op": "tabs.activate", "args": {"tabId": 7}, "timeout": 30.0}
        assert second_response == {"op": "tabs.activate", "args": {"tabId": 8}, "timeout": 30.0}
        response_payloads = [first_status, second_status, pairing_result, first_response, second_response]
        payload_repr = repr(response_payloads)
        assert extension_secret not in payload_repr
        assert "pairing-nonce-sentinel" not in payload_repr
        assert "pairingUrl" not in payload_repr

        with pytest.raises(BrowserExtensionBrokerError) as error_info:
            await _connect_over_socketpair(server, third)
        assert error_info.value.code == "BROKER_BUSY"
    finally:
        await third.stop()
        await second.stop()
        await first.stop()
        await asyncio.wait_for(second_server_task, 1.0)
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
    server = BrowserExtensionBrokerServer(19777, base_dir=_test_broker_base_dir())
    relay = FakeRelay(token, 19777, server._handle_extension_event, server._handle_disconnect)
    server._relay = relay
    client = BrokerClient(19777, _ignore_event, base_dir=_test_broker_base_dir(), auto_spawn=False)
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
    original_server = BrowserExtensionBrokerServer(19777, base_dir=_test_broker_base_dir())
    original_server._relay = FakeRelay(
        "extension-secret",
        19777,
        original_server._handle_extension_event,
        original_server._handle_disconnect,
    )
    client = BrokerClient(19777, _ignore_event, base_dir=_test_broker_base_dir(), auto_spawn=False)
    original_server_task = await _connect_over_socketpair(original_server, client)
    original_client_id = client._client_id
    await original_server.stop()
    await asyncio.wait_for(original_server_task, 1.0)
    await _eventually(lambda: not client.broker_connected)

    restarted_server = BrowserExtensionBrokerServer(19777, base_dir=_test_broker_base_dir())
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
            brokerGeneration=BROKER_GENERATION,
            pid=123,
            processStart="marker",
        ),
    )
    monkeypatch.setattr("skyvern.browser_extension.broker_client.process_identity_matches", lambda _pid, _marker: True)
    monkeypatch.setattr("skyvern.browser_extension.broker_client._open_control_connection", open_restarted_connection)

    try:
        await restarted_relay.hello()
        await _eventually(lambda: not restarted_server._extension_reset_quarantined)
        restarted_relay.scoped_tabs = [{"tabId": 7}]
        await restarted_relay.hello()
        await client.start()
        await restarted_server._approve_client(client._client_id)
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
async def test_cached_client_surfaces_broker_busy_when_fresh_enrollment_hits_cap(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(broker_server_module, "MAX_AUTHENTICATED_CLIENTS", 1)
    server = BrowserExtensionBrokerServer(19777, base_dir=_test_broker_base_dir())
    server._relay = FakeRelay("extension-secret", 19777, server._handle_extension_event, server._handle_disconnect)
    active_client = BrokerClient(19777, _ignore_event, base_dir=_test_broker_base_dir(), auto_spawn=False)
    active_server_task = await _connect_over_socketpair(server, active_client)
    stale_client = BrokerClient(19777, _ignore_event, base_dir=_test_broker_base_dir(), auto_spawn=False)
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
            brokerGeneration=BROKER_GENERATION,
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
        assert active_client.broker_connected
    finally:
        await stale_client.stop()
        await active_client.stop()
        for task in attempted_connections:
            await asyncio.wait_for(task, 1.0)
        await asyncio.wait_for(active_server_task, 1.0)
        await server.stop()


@pytest.mark.asyncio
async def test_known_client_with_bad_proof_remains_auth_failed() -> None:
    server = BrowserExtensionBrokerServer(19777, base_dir=_test_broker_base_dir())
    server._relay = FakeRelay("extension-secret", 19777, server._handle_extension_event, server._handle_disconnect)
    enrolled = BrokerClient(19777, _ignore_event, base_dir=_test_broker_base_dir(), auto_spawn=False)
    enrolled_server_task = await _connect_over_socketpair(server, enrolled)
    client_id = enrolled._client_id
    await enrolled.stop()
    await asyncio.wait_for(enrolled_server_task, 1.0)
    assert client_id is not None

    attacker = BrokerClient(19777, _ignore_event, base_dir=_test_broker_base_dir(), auto_spawn=False)
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
    server = BrowserExtensionBrokerServer(19777, base_dir=_test_broker_base_dir(), pairing_opener=lambda _url: True)
    relay = FakeRelay("extension-secret", 19777, server._handle_extension_event, server._handle_disconnect)
    server._relay = relay
    mcp = BrokerClient(19777, _ignore_event, base_dir=_test_broker_base_dir(), auto_spawn=False)
    operator = BrokerClient(19777, _ignore_event, base_dir=_test_broker_base_dir(), auto_spawn=False, operator=True)
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


def test_local_extension_build_hash_reflects_source_edits_without_restart(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An editable-install daemon can outlive edits to its own extension source; the
    local build hash must not cache a value from before the process started."""
    extension_dir = tmp_path / "extension"
    shutil.copytree(EXTENSION_DIR, extension_dir)
    monkeypatch.setattr(broker_server_module, "EXTENSION_DIR", extension_dir)

    baseline = broker_server_module._local_extension_build_hash()

    (extension_dir / "service_worker.js").write_text(
        (extension_dir / "service_worker.js").read_text() + "\n// edited\n"
    )

    assert broker_server_module._local_extension_build_hash() != baseline


@pytest.mark.asyncio
async def test_broker_status_reports_extension_build_currency() -> None:
    server = BrowserExtensionBrokerServer(19777, base_dir=_test_broker_base_dir())
    relay = FakeRelay(
        "extension-secret", 19777, server._handle_extension_event, server._handle_disconnect, auto_connect=False
    )
    server._relay = relay
    operator = BrokerClient(19777, _ignore_event, base_dir=_test_broker_base_dir(), auto_spawn=False, operator=True)
    operator_server_task = await _connect_over_socketpair(server, operator)
    try:
        unknown_status = await operator.broker_status()
        assert unknown_status["extensionConnected"] is False
        assert unknown_status["extensionBuild"] == "unknown"
        assert unknown_status["extensionReportedBuildHash"] is None
        local_short_hash = unknown_status["extensionBuildHash"]
        assert local_short_hash == compute_extension_source_hash(EXTENSION_DIR)[:12]

        # A connected, current-protocol extension that reports no hash predates
        # build_hash.json entirely - the same-version-different-bytes skew this
        # check exists to catch - and must be flagged, not shrugged off as unknown.
        assert relay.extension.build_hash is None
        await relay.hello()
        legacy_hello_status = await operator.broker_status()
        assert legacy_hello_status["extensionConnected"] is True
        assert legacy_hello_status["extensionBuild"] == "stale"
        assert legacy_hello_status["extensionReportedBuildHash"] is None

        relay.extension.build_hash = compute_extension_source_hash(EXTENSION_DIR)
        await relay.hello()
        current_status = await operator.broker_status()
        assert current_status["extensionBuild"] == "current"
        assert current_status["extensionReportedBuildHash"] == local_short_hash

        relay.extension.build_hash = "0" * 64
        await relay.hello()
        stale_status = await operator.broker_status()
        assert stale_status["extensionBuild"] == "stale"
        assert stale_status["extensionReportedBuildHash"] == "0" * 12
        assert stale_status["extensionBuildHash"] == local_short_hash
    finally:
        await operator.stop()
        await asyncio.wait_for(operator_server_task, 1.0)
        await server.stop()


@pytest.mark.asyncio
async def test_only_operator_connection_can_cancel_another_principals_pairing() -> None:
    server = BrowserExtensionBrokerServer(19777, base_dir=_test_broker_base_dir(), pairing_opener=lambda _url: True)
    relay = FakeRelay("extension-secret", 19777, server._handle_extension_event, server._handle_disconnect)
    server._relay = relay
    mcp = BrokerClient(19777, _ignore_event, base_dir=_test_broker_base_dir(), auto_spawn=False)
    operator = BrokerClient(19777, _ignore_event, base_dir=_test_broker_base_dir(), auto_spawn=False, operator=True)
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
    server = BrowserExtensionBrokerServer(19777, base_dir=_test_broker_base_dir())
    relay = FakeRelay("extension-secret", 19777, server._handle_extension_event, server._handle_disconnect)
    server._relay = relay
    mcp = BrokerClient(19777, _ignore_event, base_dir=_test_broker_base_dir(), auto_spawn=False)
    operator = BrokerClient(19777, _ignore_event, base_dir=_test_broker_base_dir(), auto_spawn=False, operator=True)
    mcp_server_task = await _connect_over_socketpair(server, mcp)
    relay.scoped_tabs = [{"tabId": 17}, {"tabId": 23}]
    await relay.hello()
    await mcp.request("debugger.attach", {"tabId": 17})
    await mcp.request("debugger.attach", {"tabId": 23})
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
    server = BrowserExtensionBrokerServer(
        19777, base_dir=_test_broker_base_dir(), pairing_opener=lambda url: not opened.append(url)
    )
    relay = FakeRelay("extension-secret", 19777, server._handle_extension_event, server._handle_disconnect)
    server._relay = relay
    first = BrokerClient(19777, _ignore_event, base_dir=_test_broker_base_dir(), auto_spawn=False, operator=True)
    second = BrokerClient(19777, _ignore_event, base_dir=_test_broker_base_dir(), auto_spawn=False, operator=True)
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
async def test_pairing_approval_clears_flow_but_preserves_operator_rate_limit() -> None:
    server = BrowserExtensionBrokerServer(19777, base_dir=_test_broker_base_dir(), pairing_opener=lambda _url: True)
    relay = FakeRelay("extension-secret", 19777, server._handle_extension_event, server._handle_disconnect)
    server._relay = relay
    first = BrokerClient(19777, _ignore_event, base_dir=_test_broker_base_dir(), auto_spawn=False, operator=True)
    second = BrokerClient(19777, _ignore_event, base_dir=_test_broker_base_dir(), auto_spawn=False, operator=True)
    first_server_task = await _connect_over_socketpair(server, first)
    await first.begin_pairing()
    offer = await server._handle_pairing_complete()
    assert offer is not None
    assert server._pairing_owner == "operator"
    await relay.emit_event("pairing.approved", {"approvalNonce": offer["approvalNonce"]})
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
async def test_pairing_grant_approves_connected_clients(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setenv("HOME", str(tmp_path))
    server = BrowserExtensionBrokerServer(19777, base_dir=_test_broker_base_dir(), pairing_opener=lambda _url: True)
    server._broker_auth_token = "extension-secret"
    relay = FakeRelay("extension-secret", 19777, server._handle_extension_event, server._handle_disconnect)
    server._relay = relay
    first = BrokerClient(19777, _ignore_event, base_dir=_test_broker_base_dir(), auto_spawn=False)
    second = BrokerClient(19777, _ignore_event, base_dir=_test_broker_base_dir(), auto_spawn=False)
    first_server_task = await _connect_over_socketpair(server, first, auto_approve=False)
    second_server_task = await _connect_over_socketpair(server, second, auto_approve=False)
    try:
        assert (await first.broker_status())["approved"] is False
        assert (await second.broker_status())["approved"] is False
        assert await first.wait_connected(0.0) is False

        with pytest.raises(BrowserExtensionBrokerError) as error_info:
            await first.request("tabs.create", {"url": "about:blank"})
        assert error_info.value.code == "APPROVAL_REQUIRED"

        await first.begin_pairing()
        first_offer = await server._handle_pairing_complete()
        assert first_offer is not None
        assert first_offer["requestFingerprint"] == server._client_fingerprint(first._client_id)
        wait_task = asyncio.create_task(first.wait_connected(1.0))
        await asyncio.sleep(0)
        assert not wait_task.done()
        await relay.emit_event(
            "pairing.approved",
            {"approvalNonce": first_offer["approvalNonce"]},
        )
        assert await wait_task is True
        await relay.emit_event(
            "pairing.approved",
            {"approvalNonce": first_offer["approvalNonce"]},
        )

        assert (await first.broker_status())["approved"] is True
        assert await second.wait_connected(1.0) is True
        assert (await second.broker_status())["approved"] is True
        first_response = await first.request("tabs.create", {"url": "about:blank"})
        assert first_response["op"] == "tabs.create"
        second_response = await second.request("tabs.create", {"url": "about:blank"})
        assert second_response["op"] == "tabs.create"
        assert relay.sent_events[-2:] == [
            (
                "pairing.approved_ack",
                {"approvalNonce": first_offer["approvalNonce"], "approved": True},
            ),
            (
                "pairing.approved_ack",
                {"approvalNonce": first_offer["approvalNonce"], "approved": True},
            ),
        ]
    finally:
        await second.stop()
        await first.stop()
        await asyncio.wait_for(second_server_task, 1.0)
        await asyncio.wait_for(first_server_task, 1.0)
        await server.stop()


@pytest.mark.asyncio
async def test_client_approval_expires_when_its_broker_session_disconnects() -> None:
    server = BrowserExtensionBrokerServer(19777, base_dir=_test_broker_base_dir(), pairing_opener=lambda _url: True)
    relay = FakeRelay("extension-secret", 19777, server._handle_extension_event, server._handle_disconnect)
    server._relay = relay
    client = BrokerClient(19777, _ignore_event, base_dir=_test_broker_base_dir(), auto_spawn=False)
    first_server_task = await _connect_over_socketpair(server, client, auto_approve=False)
    await client.begin_pairing()
    offer = await server._handle_pairing_complete()
    assert offer is not None
    await relay.emit_event("pairing.approved", {"approvalNonce": offer["approvalNonce"]})
    assert (await client.broker_status())["approved"] is True
    await client.stop()
    await asyncio.wait_for(first_server_task, 1.0)

    second_server_task = await _connect_over_socketpair(server, client, auto_approve=False)
    try:
        assert (await client.broker_status())["approved"] is False
        with pytest.raises(BrowserExtensionBrokerError) as error_info:
            await client.request("tabs.create", {"url": "about:blank"})
        assert error_info.value.code == "APPROVAL_REQUIRED"
        assert (await client.begin_pairing())["active"] is True
    finally:
        await client.stop()
        await asyncio.wait_for(second_server_task, 1.0)
        await server.stop()


@pytest.mark.asyncio
async def test_client_pairing_requires_current_extension_protocol() -> None:
    server = BrowserExtensionBrokerServer(19777, base_dir=_test_broker_base_dir(), pairing_opener=lambda _url: True)
    extension = FakeExtension()
    extension.protocol_version = 1
    relay = FakeRelay(
        "extension-secret",
        19777,
        server._handle_extension_event,
        server._handle_disconnect,
        extension=extension,
    )
    relay.connected = True
    server._extension_reset_quarantined = False
    server._extension_reset_error = None
    server._relay = relay
    client = BrokerClient(19777, _ignore_event, base_dir=_test_broker_base_dir(), auto_spawn=False)
    server_task = await _connect_over_socketpair(server, client, auto_approve=False)
    try:
        with pytest.raises(BrowserExtensionBrokerError) as error_info:
            await client.begin_pairing()

        assert error_info.value.code == "EXTENSION_UPGRADE_REQUIRED"
    finally:
        await client.stop()
        await asyncio.wait_for(server_task, 1.0)
        await server.stop()


@pytest.mark.asyncio
async def test_client_auth_names_outdated_extension_before_reset_recovery() -> None:
    server = BrowserExtensionBrokerServer(19777, base_dir=_test_broker_base_dir())
    extension = FakeExtension()
    extension.protocol_version = 1
    relay = FakeRelay(
        "extension-secret",
        19777,
        server._handle_extension_event,
        server._handle_disconnect,
        extension=extension,
    )
    relay.connected = True
    server._relay = relay
    client = BrokerClient(19777, _ignore_event, base_dir=_test_broker_base_dir(), auto_spawn=False)
    try:
        with pytest.raises(BrowserExtensionBrokerError) as error_info:
            await _connect_over_socketpair(server, client, auto_approve=False)

        assert error_info.value.code == "EXTENSION_UPGRADE_REQUIRED"
    finally:
        await client.stop()
        await server.stop()


@pytest.mark.asyncio
async def test_other_clients_pending_request_survives_release_and_does_not_block_enrollment() -> None:
    server = BrowserExtensionBrokerServer(19777, base_dir=_test_broker_base_dir())
    relay = BlockingRelay("extension-secret", 19777, server._handle_extension_event, server._handle_disconnect)
    server._relay = relay
    first = BrokerClient(19777, _ignore_event, base_dir=_test_broker_base_dir(), auto_spawn=False)
    second = BrokerClient(19777, _ignore_event, base_dir=_test_broker_base_dir(), auto_spawn=False)
    third = BrokerClient(19777, _ignore_event, base_dir=_test_broker_base_dir(), auto_spawn=False)
    first_server_task = await _connect_over_socketpair(server, first)
    second_server_task = await _connect_over_socketpair(server, second)
    third_server_task: asyncio.Task[None] | None = None
    relay.scoped_tabs = [{"tabId": 7}]
    request_task = asyncio.create_task(first.request("tabs.activate", {"tabId": 7}))
    try:
        await asyncio.wait_for(relay.request_started.wait(), 1.0)
        reset_count = len(relay.reset_frames)

        await second.stop()
        await asyncio.wait_for(second_server_task, 1.0)
        assert not request_task.done()

        third_server_task = await _connect_over_socketpair(server, third)
        assert third.broker_connected
        assert not request_task.done()
        assert len(relay.reset_frames) == reset_count

        relay.release_request.set()
        assert await request_task == {"op": "tabs.activate", "args": {"tabId": 7}, "timeout": 30.0}
        await _eventually(lambda: not server._forwarded_tasks)
    finally:
        relay.release_request.set()
        with suppress(BrowserExtensionNotConnectedError):
            await request_task
        await third.stop()
        await second.stop()
        await first.stop()
        if third_server_task is not None:
            await asyncio.wait_for(third_server_task, 1.0)
        await asyncio.wait_for(second_server_task, 1.0)
        await asyncio.wait_for(first_server_task, 1.0)
        await server.stop()


@pytest.mark.asyncio
@pytest.mark.parametrize("release_path", ["abrupt_eof", "clean_stop", "cancelled_flow"])
async def test_client_release_frees_leases_without_extension_reset(release_path: str) -> None:
    server = BrowserExtensionBrokerServer(19777, base_dir=_test_broker_base_dir())
    relay = ControlledResetRelay("extension-secret", 19777, server._handle_extension_event, server._handle_disconnect)
    server._relay = relay
    first = BrokerClient(19777, _ignore_event, base_dir=_test_broker_base_dir(), auto_spawn=False)
    second = BrokerClient(19777, _ignore_event, base_dir=_test_broker_base_dir(), auto_spawn=False)
    first_server_task = await _connect_over_socketpair(server, first)
    second_server_task: asyncio.Task[None] | None = None
    assert first._client_id is not None
    await server._grant_lease(71, first._client_id, origin="created")
    await server._grant_lease(72, first._client_id, origin="shared")
    await server._grant_lease(73, first._client_id, origin="claimed")
    reset_count = len(relay.reset_frames)
    sweep_count = relay.extension.reset_sweep_count
    try:
        if release_path == "clean_stop":
            await first.stop()
        elif release_path == "abrupt_eof":
            assert first._writer is not None
            first._writer.transport.abort()
        else:
            first_server_task.cancel()
        await asyncio.wait_for(first_server_task, 1.0)

        second_server_task = await asyncio.wait_for(_connect_over_socketpair(server, second), 1.0)
        await _eventually(lambda: len(relay.requests) == 3)

        assert second.broker_connected
        assert server._leases == {}
        assert relay.requests == [
            ("tabs.remove", {"tabId": 71}),
            ("debugger.detach", {"tabId": 72}),
            ("debugger.detach", {"tabId": 73}),
        ]
        assert len(relay.reset_frames) == reset_count
        assert relay.extension.reset_sweep_count == sweep_count
    finally:
        await second.stop()
        await first.stop()
        if second_server_task is not None:
            await asyncio.wait_for(second_server_task, 1.0)
        await asyncio.wait_for(first_server_task, 1.0)
        await server.stop()


@pytest.mark.asyncio
async def test_restarted_daemon_resets_surviving_extension_snapshot_before_exposure() -> None:
    extension = FakeExtension()
    original_server = BrowserExtensionBrokerServer(19777, base_dir=_test_broker_base_dir())
    original_relay = ControlledResetRelay(
        "extension-secret",
        19777,
        original_server._handle_extension_event,
        original_server._handle_disconnect,
        extension=extension,
    )
    original_server._relay = original_relay
    original_client = BrokerClient(19777, _ignore_event, base_dir=_test_broker_base_dir(), auto_spawn=False)
    original_server_task = await _connect_over_socketpair(original_server, original_client)
    original_reset = original_relay.reset_frames[0]

    original_relay.scoped_tabs = [{"tabId": 71, "url": "https://private.test", "title": "Private"}]
    original_relay.attached_tabs = {71}
    await original_relay.hello()
    await original_client.request("tabs.activate", {"tabId": 71})
    await _eventually(
        lambda: original_client.scoped_tabs == [{"tabId": 71, "url": "https://private.test", "title": "Private"}]
    )
    prior_sweep_count = extension.reset_sweep_count
    await original_server.stop()
    await asyncio.wait_for(original_server_task, 1.0)
    assert extension.scoped_tabs
    assert extension.attached_tabs == {71}

    restarted_server = BrowserExtensionBrokerServer(19777, base_dir=_test_broker_base_dir())
    restarted_relay = ControlledResetRelay(
        "extension-secret",
        19777,
        restarted_server._handle_extension_event,
        restarted_server._handle_disconnect,
        extension=extension,
        auto_connect=False,
    )
    restarted_server._relay = restarted_relay
    restarted_client = BrokerClient(19777, _ignore_event, base_dir=_test_broker_base_dir(), auto_spawn=False)
    restarted_server_task = await _connect_over_socketpair(restarted_server, restarted_client)
    ready_task = asyncio.create_task(restarted_client.wait_connected(1.0))

    await restarted_relay.hello()
    assert await ready_task
    assert restarted_client.scoped_tabs == []
    assert extension.scoped_tabs == []
    assert extension.attached_tabs == set()
    assert extension.reset_sweep_count == prior_sweep_count + 1
    assert restarted_relay.reset_frames[0]["generation"] == original_reset["generation"]
    assert restarted_relay.reset_frames[0]["epoch"] != original_reset["epoch"]

    await restarted_client.stop()
    await asyncio.wait_for(restarted_server_task, 1.0)
    await restarted_server.stop()


@pytest.mark.asyncio
async def test_new_daemon_epoch_executes_reset_when_generation_restarts_below_prior_run() -> None:
    extension = FakeExtension()
    original_server = BrowserExtensionBrokerServer(19777, base_dir=_test_broker_base_dir())
    original_relay = ControlledResetRelay(
        "extension-secret",
        19777,
        original_server._handle_extension_event,
        original_server._handle_disconnect,
        extension=extension,
    )
    original_server._relay = original_relay
    original_client = BrokerClient(19777, _ignore_event, base_dir=_test_broker_base_dir(), auto_spawn=False)
    original_server_task = await _connect_over_socketpair(original_server, original_client)

    original_relay.connected = False
    await original_server._handle_disconnect()
    await original_relay.hello()
    await _eventually(lambda: not original_server._extension_reset_quarantined)
    prior_reset = original_relay.reset_frames[-1]
    assert prior_reset["generation"] > 0
    await original_client.stop()
    await asyncio.wait_for(original_server_task, 1.0)
    await original_server.stop()

    extension.scoped_tabs = [{"tabId": 71, "url": "https://private.test", "title": "Private"}]
    extension.attached_tabs = {71}
    restarted_server = BrowserExtensionBrokerServer(19777, base_dir=_test_broker_base_dir())
    restarted_relay = ControlledResetRelay(
        "extension-secret",
        19777,
        restarted_server._handle_extension_event,
        restarted_server._handle_disconnect,
        extension=extension,
        auto_connect=False,
    )
    restarted_server._relay = restarted_relay
    restarted_client = BrokerClient(19777, _ignore_event, base_dir=_test_broker_base_dir(), auto_spawn=False)
    restarted_server_task = await _connect_over_socketpair(restarted_server, restarted_client)

    await restarted_relay.hello()
    assert await restarted_client.wait_connected(1.0)
    assert restarted_client.scoped_tabs == []
    assert restarted_relay.reset_frames[0]["generation"] < prior_reset["generation"]
    assert restarted_relay.reset_frames[0]["epoch"] != prior_reset["epoch"]
    assert extension.scoped_tabs == []
    assert extension.attached_tabs == set()

    await restarted_client.stop()
    await asyncio.wait_for(restarted_server_task, 1.0)
    await restarted_server.stop()


@pytest.mark.asyncio
async def test_quarantine_suppresses_extension_events_until_reset_ack() -> None:
    received: list[tuple[str, dict]] = []

    async def capture_event(event: str, params: dict) -> None:
        received.append((event, params))

    server = BrowserExtensionBrokerServer(19777, base_dir=_test_broker_base_dir())
    relay = ControlledResetRelay(
        "extension-secret",
        19777,
        server._handle_extension_event,
        server._handle_disconnect,
        auto_connect=False,
    )
    server._relay = relay
    client = BrokerClient(19777, capture_event, base_dir=_test_broker_base_dir(), auto_spawn=False)
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
    await _eventually(lambda: received == [("extension.hello", {"scopedTabs": []})])
    assert "private" not in repr(received)

    relay.scoped_tabs = [{"tabId": 72}]
    await client.request("tabs.activate", {"tabId": 72})
    await _eventually(lambda: any(event == "scope.tabAdded" for event, _params in received))
    received.clear()
    await relay.emit_event("debugger.event", {"tabId": 72, "method": "Runtime.executionContextCreated", "params": {}})
    await _eventually(lambda: len(received) == 1)
    assert received[0][0] == "debugger.event"

    await client.stop()
    await asyncio.wait_for(server_task, 1.0)
    await server.stop()


@pytest.mark.asyncio
async def test_relay_reconnect_resets_before_active_client_sees_new_traffic() -> None:
    received: list[tuple[str, dict]] = []

    async def capture_event(event: str, params: dict) -> None:
        received.append((event, params))

    server = BrowserExtensionBrokerServer(19777, base_dir=_test_broker_base_dir())
    relay = ControlledResetRelay("extension-secret", 19777, server._handle_extension_event, server._handle_disconnect)
    server._relay = relay
    client = BrokerClient(19777, capture_event, base_dir=_test_broker_base_dir(), auto_spawn=False)
    server_task = await _connect_over_socketpair(server, client)
    relay.scoped_tabs = [{"tabId": 71, "url": "https://private.test", "title": "Private"}]
    relay.attached_tabs = {71}
    await relay.hello()
    await client.request("tabs.activate", {"tabId": 71})
    await _eventually(lambda: client.scoped_tabs == [{"tabId": 71, "url": "https://private.test", "title": "Private"}])

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
    assert relay.extension.attached_tabs == set()
    await client.stop()
    await asyncio.wait_for(server_task, 1.0)
    await server.stop()


@pytest.mark.asyncio
async def test_detach_failure_keeps_daemon_quarantined_and_fails_enrollment() -> None:
    server = BrowserExtensionBrokerServer(19777, base_dir=_test_broker_base_dir())
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
    client = BrokerClient(19777, _ignore_event, base_dir=_test_broker_base_dir(), auto_spawn=False)
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
    server = BrowserExtensionBrokerServer(19777, base_dir=_test_broker_base_dir())
    relay = ControlledResetRelay(
        "extension-secret",
        19777,
        server._handle_extension_event,
        server._handle_disconnect,
        auto_connect=False,
    )
    server._relay = relay
    first = BrokerClient(19777, _ignore_event, base_dir=_test_broker_base_dir(), auto_spawn=False)
    second = BrokerClient(19777, _ignore_event, base_dir=_test_broker_base_dir(), auto_spawn=False)
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
async def test_client_enrollment_proceeds_when_extension_disconnects_during_reset() -> None:
    server = BrowserExtensionBrokerServer(19777, base_dir=_test_broker_base_dir())
    relay = ControlledResetRelay("extension-secret", 19777, server._handle_extension_event, server._handle_disconnect)
    server._relay = relay
    first = BrokerClient(19777, _ignore_event, base_dir=_test_broker_base_dir(), auto_spawn=False)
    second = BrokerClient(19777, _ignore_event, base_dir=_test_broker_base_dir(), auto_spawn=False)
    first_server_task = await _connect_over_socketpair(server, first)
    second_server_task: asyncio.Task[None] | None = None

    relay.connected = False
    await server._handle_disconnect()
    relay.extension.reset_started.clear()
    relay.extension.reset_gate.clear()
    await relay.hello()
    await asyncio.wait_for(relay.extension.reset_started.wait(), 1.0)
    reset_identity = (server._extension_reset_epoch, server._extension_reset_generation)

    relay.connected = False
    relay.extension_protocol_version = None
    await server._handle_disconnect()
    relay.auto_connect = False
    try:
        second_server_task = await asyncio.wait_for(_connect_over_socketpair(server, second), 1.0)

        assert second.broker_connected
        assert not second.connected
        assert first.broker_connected
        assert (server._extension_reset_epoch, server._extension_reset_generation) == reset_identity
        assert server._extension_reset_quarantined
    finally:
        await second.stop()
        await first.stop()
        if second_server_task is not None:
            await asyncio.wait_for(second_server_task, 1.0)
        await asyncio.wait_for(first_server_task, 1.0)
        await server.stop()


@pytest.mark.asyncio
async def test_late_extension_reconnect_is_reset_before_successor_sees_snapshot(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(broker_server_module, "EXTENSION_RESET_TIMEOUT_SECONDS", 0.01)
    server = BrowserExtensionBrokerServer(19777, base_dir=_test_broker_base_dir())
    relay = ControlledResetRelay(
        "extension-secret",
        19777,
        server._handle_extension_event,
        server._handle_disconnect,
        auto_connect=False,
    )
    server._relay = relay
    first = BrokerClient(19777, _ignore_event, base_dir=_test_broker_base_dir(), auto_spawn=False)
    second = BrokerClient(19777, _ignore_event, base_dir=_test_broker_base_dir(), auto_spawn=False)
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
    server = BrowserExtensionBrokerServer(19777, base_dir=_test_broker_base_dir())
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
    first = BrokerClient(19777, _ignore_event, base_dir=_test_broker_base_dir(), auto_spawn=False)
    second = BrokerClient(19777, _ignore_event, base_dir=_test_broker_base_dir(), auto_spawn=False)
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
    server = BrowserExtensionBrokerServer(19777, base_dir=_test_broker_base_dir())
    relay = FakeRelay("extension-secret", 19777, server._handle_extension_event, server._handle_disconnect)
    relay.extension.protocol_version = 1
    relay.extension_protocol_version = 1
    server._relay = relay
    client = BrokerClient(19777, _ignore_event, base_dir=_test_broker_base_dir(), auto_spawn=False)
    server_task = await _connect_over_socketpair(server, client)
    initial_connection_cycles = relay.connection_cycles
    warnings.clear()

    relay.scoped_tabs = [{"tabId": 71}]
    relay.attached_tabs = {71}
    relay.connected = False
    await server._handle_disconnect()
    await relay.hello()
    await _eventually(lambda: not server._extension_reset_quarantined)

    assert relay.connection_cycles == initial_connection_cycles + 1
    assert relay.reset_frames == []
    assert client.scoped_tabs == []
    assert warnings == [
        (
            "browser_extension_protocol_skew",
            {"extension_protocol": 1, "broker_protocol": 2, "fallback": "cycle_only"},
        )
    ]
    relay.connected = True
    assert (await client.broker_status())["extensionConnected"] is False
    with pytest.raises(BrowserExtensionBrokerError) as error_info:
        await client.request("tabs.create", {"url": "about:blank"})
    assert error_info.value.code == "EXTENSION_UPGRADE_REQUIRED"
    await client.stop()
    await asyncio.wait_for(server_task, 1.0)
    await server.stop()


@pytest.mark.asyncio
async def test_v2_replacement_during_v1_cycle_still_requires_reset_ack() -> None:
    server = BrowserExtensionBrokerServer(19777, base_dir=_test_broker_base_dir())
    relay = ReplacingProtocolRelay("extension-secret", 19777, server._handle_extension_event, server._handle_disconnect)
    server._relay = relay
    first = BrokerClient(19777, _ignore_event, base_dir=_test_broker_base_dir(), auto_spawn=False)
    second = BrokerClient(19777, _ignore_event, base_dir=_test_broker_base_dir(), auto_spawn=False)
    first_server_task = await _connect_over_socketpair(server, first)
    initial_connection_cycles = relay.connection_cycles
    initial_reset_count = len(relay.reset_frames)
    second_server_task: asyncio.Task[None] | None = None
    try:
        relay.extension.protocol_version = 1
        relay.extension_protocol_version = 1
        relay.scoped_tabs = [{"tabId": 71}]
        relay.attached_tabs = {71}
        relay.connected = False
        await server._handle_disconnect()
        relay.extension.reset_started.clear()
        relay.extension.reset_gate.clear()
        await relay.hello()
        await asyncio.wait_for(relay.extension.reset_started.wait(), 1.0)

        assert relay.connection_cycles == initial_connection_cycles + 1
        assert len(relay.reset_frames) == initial_reset_count + 1
        assert server._extension_reset_quarantined
        with pytest.raises(BrowserExtensionBrokerError) as error_info:
            await first.request("debugger.send", {"tabId": 71, "method": "Runtime.enable"})
        assert error_info.value.code == "EXTENSION_RESET_IN_PROGRESS"

        relay.extension.reset_gate.set()
        assert await first.wait_connected(1.0)
        second_server_task = await _connect_over_socketpair(server, second)
        assert second.broker_connected
        assert second.scoped_tabs == []
        assert not server._extension_reset_quarantined
    finally:
        relay.extension.reset_gate.set()
        await second.stop()
        await first.stop()
        if second_server_task is not None:
            await asyncio.wait_for(second_server_task, 1.0)
        await asyncio.wait_for(first_server_task, 1.0)
        await server.stop()


@pytest.mark.asyncio
async def test_reset_reack_after_socket_drop_unblocks_successor_without_second_sweep() -> None:
    server = BrowserExtensionBrokerServer(19777, base_dir=_test_broker_base_dir())
    relay = ExtensionRelayServer(
        "extension-secret",
        19777,
        server._handle_extension_event,
        server._handle_disconnect,
        control_pairing_only=True,
    )
    extension = FakeExtension()
    initial_websocket = FakeExtensionWebSocket(relay, extension)
    relay._websocket = initial_websocket  # type: ignore[assignment]
    relay.extension_protocol_version = 2
    server._relay = relay

    await initial_websocket.send_hello()
    await _eventually(lambda: not server._extension_reset_quarantined)
    initial_sweep_count = extension.reset_sweep_count
    extension.scoped_tabs = [{"tabId": 71, "url": "https://private.test", "title": "Private"}]
    extension.attached_tabs = {71}
    relay.scoped_tabs = list(extension.scoped_tabs)
    extension.reset_started.clear()
    extension.reset_gate.clear()

    await initial_websocket.close(code=1001, message=b"transport lost")
    await relay._handle_disconnect(initial_websocket)  # type: ignore[arg-type]
    recovery_websocket = FakeExtensionWebSocket(relay, extension)
    await relay._activate_connection(recovery_websocket, 2)  # type: ignore[arg-type]
    await recovery_websocket.send_hello()
    await asyncio.wait_for(extension.reset_started.wait(), 1.0)
    reset_identity = (server._extension_reset_epoch, server._extension_reset_generation)

    await recovery_websocket.close(code=1001, message=b"transport lost")
    await relay._handle_disconnect(recovery_websocket)  # type: ignore[arg-type]
    extension.reset_gate.set()
    await _eventually(lambda: extension.reset_sweep_count == initial_sweep_count + 1)
    await _eventually(lambda: not recovery_websocket.reset_tasks)

    assert (extension.last_reset_epoch, extension.last_reset_generation) == reset_identity
    assert extension.last_reset_ok is True
    assert server._extension_reset_quarantined
    assert (server._extension_reset_epoch, server._extension_reset_generation) == reset_identity

    successor_websocket = FakeExtensionWebSocket(relay, extension)
    await relay._activate_connection(successor_websocket, 2)  # type: ignore[arg-type]
    await successor_websocket.send_hello()
    await _eventually(lambda: not server._extension_reset_quarantined)

    replayed_resets = [frame for frame in successor_websocket.frames if frame.get("type") == "extension.reset"]
    assert [(frame["epoch"], frame["generation"]) for frame in replayed_resets] == [reset_identity]
    assert extension.reset_sweep_count == initial_sweep_count + 1

    successor = BrokerClient(19777, _ignore_event, base_dir=_test_broker_base_dir(), auto_spawn=False)
    successor_server_task = await _connect_over_socketpair(server, successor)
    assert successor.broker_connected
    assert successor.scoped_tabs == []
    await successor.stop()
    await asyncio.wait_for(successor_server_task, 1.0)
    await server.stop()


@pytest.mark.asyncio
async def test_reset_send_deadline_surfaces_structured_timeout(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(broker_server_module, "EXTENSION_RESET_TIMEOUT_SECONDS", 0.01)
    server = BrowserExtensionBrokerServer(19777, base_dir=_test_broker_base_dir())
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
    first = BrokerClient(19777, _ignore_event, base_dir=_test_broker_base_dir(), auto_spawn=False)
    first_server_task = await _connect_over_socketpair(server, first)

    relay.connected = False
    await server._handle_disconnect()
    relay.connected = True
    await relay.hello()
    await asyncio.wait_for(relay.reset_send_started.wait(), 0.2)
    await _eventually(lambda: server._extension_reset_error == "EXTENSION_RESET_TIMEOUT")

    assert relay.reset_send_cancelled.is_set()
    second = BrokerClient(19777, _ignore_event, base_dir=_test_broker_base_dir(), auto_spawn=False)
    with pytest.raises(BrowserExtensionBrokerError) as error_info:
        await _connect_over_socketpair(server, second)
    assert error_info.value.code == "EXTENSION_RESET_TIMEOUT"
    assert first.broker_connected
    await second.stop()
    await first.stop()
    await asyncio.wait_for(first_server_task, 1.0)
    await server.stop()


@pytest.mark.asyncio
async def test_owner_release_allows_reenrollment_while_timed_out_request_awaits_terminal() -> None:
    server = BrowserExtensionBrokerServer(19777, base_dir=_test_broker_base_dir())
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
    relay.scoped_tabs = [{"tabId": 7}]
    first = BrokerClient(19777, _ignore_event, base_dir=_test_broker_base_dir(), auto_spawn=False)
    second = BrokerClient(19777, _ignore_event, base_dir=_test_broker_base_dir(), auto_spawn=False)
    first_server_task = await _connect_over_socketpair(server, first)
    sweep_count = extension.reset_sweep_count

    with pytest.raises(ExtensionRequestError, match="timed out"):
        await first.request("tabs.activate", {"tabId": 7}, timeout=0.1)
    assert relay.pending_request_count == 1
    await first.stop()
    await asyncio.wait_for(first_server_task, 1.0)

    second_server_task = await asyncio.wait_for(_connect_over_socketpair(server, second), 1.0)
    assert second.broker_connected
    assert second.scoped_tabs == []
    # The released tab stays fenced while the departed owner's request awaits terminal:
    # no cleanup frame has been sent and a successor cannot claim it yet.
    request_frames = [frame for frame in websocket.frames if frame.get("type") == "request"]
    assert [frame["op"] for frame in request_frames] == ["tabs.activate"]
    with pytest.raises(BrowserExtensionBrokerError) as claim_error:
        await second.request("debugger.send", {"tabId": 7, "method": "Runtime.evaluate"}, timeout=1.0)
    assert claim_error.value.code == "LEASE_HELD"
    assert extension.reset_sweep_count == sweep_count

    # The extension answers the stale request; only then does the drain send cleanup.
    await websocket._send_to_relay(
        {"v": 2, "type": "response", "id": request_frames[0]["id"], "ok": True, "result": {}}
    )
    await _eventually(lambda: len([frame for frame in websocket.frames if frame.get("type") == "request"]) == 2)
    request_frames = [frame for frame in websocket.frames if frame.get("type") == "request"]
    assert [frame["op"] for frame in request_frames] == ["tabs.activate", "debugger.detach"]
    await websocket._send_to_relay(
        {
            "v": 2,
            "type": "event",
            "event": "scope.tabRemoved",
            "params": {"tabId": 7, "reason": "detached"},
        }
    )
    await websocket._send_to_relay(
        {"v": 2, "type": "response", "id": request_frames[1]["id"], "ok": True, "result": {}}
    )
    await _eventually(lambda: relay.pending_request_count == 0)
    await _eventually(lambda: 7 not in server._leases)

    relay.scoped_tabs = [{"tabId": 7}]
    await websocket._send_to_relay(
        {
            "v": 2,
            "type": "event",
            "event": "scope.tabAdded",
            "params": {"tabId": 7, "url": "https://example.test", "origin": "shared"},
        }
    )
    # With the drain complete the successor can claim the freed tab.
    claim_task = asyncio.create_task(second.request("tabs.activate", {"tabId": 7}, timeout=1.0))
    await _eventually(lambda: len([frame for frame in websocket.frames if frame.get("type") == "request"]) == 3)
    request_frames = [frame for frame in websocket.frames if frame.get("type") == "request"]
    assert request_frames[2]["op"] == "tabs.activate"
    await websocket._send_to_relay(
        {"v": 2, "type": "response", "id": request_frames[2]["id"], "ok": True, "result": {}}
    )
    assert await asyncio.wait_for(claim_task, 1.0) == {}

    await second.stop()
    await asyncio.wait_for(second_server_task, 1.0)
    await server.stop()


@pytest.mark.asyncio
async def test_failed_release_cleanup_keeps_tab_fenced() -> None:
    server = BrowserExtensionBrokerServer(19777, base_dir=_test_broker_base_dir())
    relay = FakeRelay("extension-secret", 19777, server._handle_extension_event, server._handle_disconnect)
    original_request = relay.request

    async def request_with_failed_cleanup(
        op: str,
        args: dict,
        timeout: float = 30.0,
        *,
        retain_until_terminal: bool = False,
        on_registered: Callable[[], None] | None = None,
        on_terminal: Callable[[], None] | None = None,
    ) -> dict:
        if op == "debugger.detach":
            relay.requests.append((op, dict(args)))
            raise ExtensionRequestError("CDP_ERROR", "cleanup failed")
        return await original_request(
            op,
            args,
            timeout,
            retain_until_terminal=retain_until_terminal,
            on_registered=on_registered,
            on_terminal=on_terminal,
        )

    relay.request = request_with_failed_cleanup  # type: ignore[method-assign]
    server._relay = relay
    first = BrokerClient(19777, _ignore_event, base_dir=_test_broker_base_dir(), auto_spawn=False)
    second = BrokerClient(19777, _ignore_event, base_dir=_test_broker_base_dir(), auto_spawn=False)
    first_server_task = await _connect_over_socketpair(server, first)
    relay.scoped_tabs = [{"tabId": 7}]

    await first.request("tabs.activate", {"tabId": 7})
    await first.stop()
    await asyncio.wait_for(first_server_task, 1.0)
    await _eventually(lambda: not server._cleanup_tasks)

    lease = server._leases[7]
    assert lease.draining
    assert ("debugger.detach", {"tabId": 7}) in relay.requests

    second_server_task = await _connect_over_socketpair(server, second)
    with pytest.raises(BrowserExtensionBrokerError) as claim_error:
        await second.request("tabs.activate", {"tabId": 7})
    assert claim_error.value.code == "LEASE_HELD"

    await second.stop()
    await asyncio.wait_for(second_server_task, 1.0)
    await server.stop()


@pytest.mark.asyncio
async def test_already_detached_shared_cleanup_frees_tab_for_next_client() -> None:
    server = BrowserExtensionBrokerServer(19777, base_dir=_test_broker_base_dir())
    relay = FakeRelay("extension-secret", 19777, server._handle_extension_event, server._handle_disconnect)
    original_request = relay.request

    async def request_with_idempotent_detach(
        op: str,
        args: dict,
        timeout: float = 30.0,
        *,
        retain_until_terminal: bool = False,
        on_registered: Callable[[], None] | None = None,
        on_terminal: Callable[[], None] | None = None,
    ) -> dict:
        if op == "debugger.detach":
            relay.requests.append((op, dict(args)))
            raise ExtensionRequestError("DEBUGGER_DETACHED", "The debugger is not attached to this tab.")
        return await original_request(
            op,
            args,
            timeout,
            retain_until_terminal=retain_until_terminal,
            on_registered=on_registered,
            on_terminal=on_terminal,
        )

    relay.request = request_with_idempotent_detach  # type: ignore[method-assign]
    server._relay = relay
    first = BrokerClient(19777, _ignore_event, base_dir=_test_broker_base_dir(), auto_spawn=False)
    second_server_task: asyncio.Task[None] | None = None
    second = BrokerClient(19777, _ignore_event, base_dir=_test_broker_base_dir(), auto_spawn=False)
    first_server_task = await _connect_over_socketpair(server, first)
    relay.scoped_tabs = [{"tabId": 7}]
    try:
        await first.request("tabs.activate", {"tabId": 7})
        await first.stop()
        await asyncio.wait_for(first_server_task, 1.0)
        await _eventually(lambda: not server._cleanup_tasks)

        assert 7 not in server._leases
        assert ("debugger.detach", {"tabId": 7}) in relay.requests

        second_server_task = await _connect_over_socketpair(server, second)
        result = await second.request("tabs.activate", {"tabId": 7})
        assert result["op"] == "tabs.activate"
    finally:
        await second.stop()
        await first.stop()
        if second_server_task is not None:
            await asyncio.wait_for(second_server_task, 1.0)
        await asyncio.wait_for(first_server_task, 1.0)
        await server.stop()


@pytest.mark.asyncio
async def test_successful_shared_detach_waits_for_scope_removal() -> None:
    server = BrowserExtensionBrokerServer(19777, base_dir=_test_broker_base_dir())
    relay = FakeRelay("extension-secret", 19777, server._handle_extension_event, server._handle_disconnect)
    original_request = relay.request

    async def request_without_scope_removal(
        op: str,
        args: dict,
        timeout: float = 30.0,
        *,
        retain_until_terminal: bool = False,
        on_registered: Callable[[], None] | None = None,
        on_terminal: Callable[[], None] | None = None,
    ) -> dict:
        if op == "debugger.detach":
            relay.requests.append((op, dict(args)))
            return {"op": op, "args": args, "timeout": timeout}
        return await original_request(
            op,
            args,
            timeout,
            retain_until_terminal=retain_until_terminal,
            on_registered=on_registered,
            on_terminal=on_terminal,
        )

    relay.request = request_without_scope_removal  # type: ignore[method-assign]
    server._relay = relay
    client = BrokerClient(19777, _ignore_event, base_dir=_test_broker_base_dir(), auto_spawn=False)
    server_task = await _connect_over_socketpair(server, client)
    relay.scoped_tabs = [{"tabId": 7}]
    try:
        await client.request("tabs.activate", {"tabId": 7})
        await client.stop()
        await asyncio.wait_for(server_task, 1.0)
        await _eventually(lambda: not server._cleanup_tasks)

        assert server._leases[7].draining
        await server._handle_extension_event("scope.tabRemoved", {"tabId": 7, "reason": "detached"})
        assert 7 not in server._leases
    finally:
        await client.stop()
        await asyncio.wait_for(server_task, 1.0)
        await server.stop()


@pytest.mark.asyncio
async def test_scope_removal_stays_fenced_until_old_request_is_terminal() -> None:
    server = BrowserExtensionBrokerServer(19777, base_dir=_test_broker_base_dir())
    relay = FakeRelay("extension-secret", 19777, server._handle_extension_event, server._handle_disconnect)
    original_request = relay.request
    request_started = asyncio.Event()
    finish_request = asyncio.Event()

    async def request_with_blocked_command(
        op: str,
        args: dict,
        timeout: float = 30.0,
        *,
        retain_until_terminal: bool = False,
        on_registered: Callable[[], None] | None = None,
        on_terminal: Callable[[], None] | None = None,
    ) -> dict:
        if op == "debugger.send":
            relay.requests.append((op, dict(args)))
            if on_registered is not None:
                on_registered()
            request_started.set()
            try:
                await finish_request.wait()
            finally:
                if on_terminal is not None:
                    on_terminal()
            return {"op": op, "args": args, "timeout": timeout}
        return await original_request(
            op,
            args,
            timeout,
            retain_until_terminal=retain_until_terminal,
            on_registered=on_registered,
            on_terminal=on_terminal,
        )

    relay.request = request_with_blocked_command  # type: ignore[method-assign]
    server._relay = relay
    first = BrokerClient(19777, _ignore_event, base_dir=_test_broker_base_dir(), auto_spawn=False)
    second = BrokerClient(19777, _ignore_event, base_dir=_test_broker_base_dir(), auto_spawn=False)
    first_server_task = await _connect_over_socketpair(server, first)
    second_server_task = await _connect_over_socketpair(server, second)
    relay.scoped_tabs = [{"tabId": 7, "url": "https://shared.example.test"}]
    pending_request: asyncio.Task[dict] | None = None
    try:
        await first.request("tabs.activate", {"tabId": 7})
        pending_request = asyncio.create_task(
            first.request("debugger.send", {"tabId": 7, "method": "Runtime.evaluate"}, timeout=2.0)
        )
        await asyncio.wait_for(request_started.wait(), 1.0)

        relay.scoped_tabs = []
        await server._handle_extension_event("scope.tabRemoved", {"tabId": 7, "reason": "unshared"})
        assert server._leases[7].draining
        assert server._tab_request_counts == {7: 1}

        relay.scoped_tabs = [{"tabId": 7, "url": "https://shared.example.test"}]
        await server._handle_extension_event(
            "scope.tabAdded",
            {"tabId": 7, "url": "https://shared.example.test", "origin": "shared"},
        )
        with pytest.raises(BrowserExtensionBrokerError) as claim_error:
            await second.request("tabs.activate", {"tabId": 7})
        assert claim_error.value.code == "LEASE_HELD"

        finish_request.set()
        await asyncio.wait_for(pending_request, 1.0)
        await _eventually(lambda: 7 not in server._leases)
        result = await second.request("tabs.activate", {"tabId": 7})
        assert result["op"] == "tabs.activate"
    finally:
        finish_request.set()
        if pending_request is not None:
            await asyncio.gather(pending_request, return_exceptions=True)
        await second.stop()
        await first.stop()
        await asyncio.wait_for(second_server_task, 1.0)
        await asyncio.wait_for(first_server_task, 1.0)
        await server.stop()


@pytest.mark.asyncio
async def test_created_scope_removal_closes_tab_after_old_request_is_terminal() -> None:
    server = BrowserExtensionBrokerServer(19777, base_dir=_test_broker_base_dir())
    relay = FakeRelay("extension-secret", 19777, server._handle_extension_event, server._handle_disconnect)
    original_request = relay.request
    request_started = asyncio.Event()
    finish_request = asyncio.Event()

    async def request_with_blocked_command(
        op: str,
        args: dict,
        timeout: float = 30.0,
        *,
        retain_until_terminal: bool = False,
        on_registered: Callable[[], None] | None = None,
        on_terminal: Callable[[], None] | None = None,
    ) -> dict:
        if op == "debugger.send":
            relay.requests.append((op, dict(args)))
            if on_registered is not None:
                on_registered()
            request_started.set()
            try:
                await finish_request.wait()
            finally:
                if on_terminal is not None:
                    on_terminal()
            return {"op": op, "args": args, "timeout": timeout}
        return await original_request(
            op,
            args,
            timeout,
            retain_until_terminal=retain_until_terminal,
            on_registered=on_registered,
            on_terminal=on_terminal,
        )

    relay.request = request_with_blocked_command  # type: ignore[method-assign]
    server._relay = relay
    first = BrokerClient(19777, _ignore_event, base_dir=_test_broker_base_dir(), auto_spawn=False)
    first_server_task = await _connect_over_socketpair(server, first)
    relay.scoped_tabs = [{"tabId": 7, "url": "https://created.example.test"}]
    assert first._client_id is not None
    await server._grant_lease(7, first._client_id, origin="created")
    pending_request: asyncio.Task[dict] | None = None
    try:
        pending_request = asyncio.create_task(
            first.request("debugger.send", {"tabId": 7, "method": "Runtime.evaluate"}, timeout=2.0)
        )
        await asyncio.wait_for(request_started.wait(), 1.0)

        relay.scoped_tabs = []
        await server._handle_extension_event("scope.tabRemoved", {"tabId": 7, "reason": "detached"})
        assert server._leases[7].draining
        assert ("tabs.remove", {"tabId": 7}) not in relay.requests

        finish_request.set()
        await asyncio.wait_for(pending_request, 1.0)
        await _eventually(lambda: 7 not in server._leases)
        assert ("tabs.remove", {"tabId": 7}) in relay.requests
    finally:
        finish_request.set()
        if pending_request is not None:
            await asyncio.gather(pending_request, return_exceptions=True)
        await first.stop()
        await asyncio.wait_for(first_server_task, 1.0)
        await server.stop()


@pytest.mark.asyncio
async def test_timed_out_tab_create_stays_fenced_without_blocking_popup_routing() -> None:
    server = BrowserExtensionBrokerServer(19777, base_dir=_test_broker_base_dir())
    relay = FakeRelay("extension-secret", 19777, server._handle_extension_event, server._handle_disconnect)
    original_request = relay.request
    create_started = asyncio.Event()
    emit_late_tab = asyncio.Event()
    tab_added = asyncio.Event()
    finish_create = asyncio.Event()
    tab_removed = asyncio.Event()

    async def delayed_create(
        op: str,
        args: dict,
        timeout: float | None = 30.0,
        *,
        retain_until_terminal: bool = False,
        on_registered: Callable[[], None] | None = None,
        on_terminal: Callable[[], None] | None = None,
    ) -> dict:
        if op == "tabs.create":
            assert timeout is None
            relay.requests.append((op, dict(args)))
            if on_registered is not None:
                on_registered()
            create_started.set()
            await emit_late_tab.wait()
            relay.scoped_tabs = [*relay.scoped_tabs, {"tabId": 91, "url": "about:blank", "title": ""}]
            await server._handle_extension_event(
                "scope.tabAdded",
                {"tabId": 91, "url": "about:blank", "title": "", "origin": "created"},
            )
            tab_added.set()
            await finish_create.wait()
            if on_terminal is not None:
                on_terminal()
            return {"tabId": 91}
        if op == "tabs.remove":
            relay.requests.append((op, dict(args)))
            removed_tab_id = args.get("tabId")
            relay.scoped_tabs = [tab for tab in relay.scoped_tabs if tab.get("tabId") != removed_tab_id]
            await server._handle_extension_event(
                "scope.tabRemoved",
                {"tabId": removed_tab_id, "reason": "closed"},
            )
            if removed_tab_id == 91:
                tab_removed.set()
            return {}
        return await original_request(
            op,
            args,
            timeout,
            retain_until_terminal=retain_until_terminal,
            on_registered=on_registered,
            on_terminal=on_terminal,
        )

    relay.request = delayed_create  # type: ignore[method-assign]
    server._relay = relay
    first = BrokerClient(19777, _ignore_event, base_dir=_test_broker_base_dir(), auto_spawn=False)
    second = BrokerClient(19777, _ignore_event, base_dir=_test_broker_base_dir(), auto_spawn=False)
    first_server_task = await _connect_over_socketpair(server, first)
    second_server_task = await _connect_over_socketpair(server, second)
    relay.scoped_tabs = [{"tabId": 7, "url": "https://opener.example.test", "title": "Opener"}]
    await first.request("tabs.activate", {"tabId": 7})
    first_client_id = first._client_id
    assert first_client_id is not None
    try:
        with pytest.raises(ExtensionRequestError, match="timed out"):
            await first.request("tabs.create", {"url": "about:blank"}, timeout=0.01)
        assert create_started.is_set()
        assert server._pending_create_count == 1

        relay.scoped_tabs = [
            *relay.scoped_tabs,
            {"tabId": 93, "url": "https://shared.example.test", "title": "Shared"},
        ]
        await server._handle_extension_event(
            "scope.tabAdded",
            {"tabId": 93, "url": "https://shared.example.test", "title": "Shared", "origin": "shared"},
        )
        shared_lease = await second.ensure_root_lease()
        assert shared_lease is not None and shared_lease["tabId"] == 93
        assert 93 not in server._pending_tab_events
        relay.scoped_tabs = [
            *relay.scoped_tabs,
            {"tabId": 92, "url": "https://popup.example.test", "title": "Popup"},
        ]
        await server._handle_extension_event(
            "tabs.created",
            {"tabId": 92, "openerTabId": 7, "url": "https://popup.example.test"},
        )
        assert server._leases[92].client_id == first_client_id
        assert 92 not in server._pending_tab_events

        emit_late_tab.set()
        await asyncio.wait_for(tab_added.wait(), 1.0)
        with pytest.raises(BrowserExtensionBrokerError) as claim_error:
            await second.request("tabs.activate", {"tabId": 91})
        assert claim_error.value.code == "LEASE_HELD"

        finish_create.set()
        await asyncio.wait_for(tab_removed.wait(), 1.0)
        await _eventually(lambda: 91 not in server._leases and server._pending_create_count == 0)
        assert ("tabs.remove", {"tabId": 91}) in relay.requests
        assert 91 not in server._pending_tab_events
    finally:
        emit_late_tab.set()
        finish_create.set()
        await second.stop()
        await first.stop()
        await asyncio.wait_for(second_server_task, 1.0)
        await asyncio.wait_for(first_server_task, 1.0)
        await server.stop()


@pytest.mark.asyncio
async def test_tab_create_rejects_before_forwarding_when_correlation_capacity_is_full(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(broker_server_module, "MAX_PENDING_TAB_EVENT_TABS", 2)
    server = BrowserExtensionBrokerServer(19777, base_dir=_test_broker_base_dir())
    relay = BlockingRelay("extension-secret", 19777, server._handle_extension_event, server._handle_disconnect)
    server._relay = relay
    server._extension_supports_scope_origins = True
    first = BrokerClient(19777, _ignore_event, base_dir=_test_broker_base_dir(), auto_spawn=False)
    second = BrokerClient(19777, _ignore_event, base_dir=_test_broker_base_dir(), auto_spawn=False)
    first_server_task = await _connect_over_socketpair(server, first)
    second_server_task = await _connect_over_socketpair(server, second)
    first_create: asyncio.Task[dict] | None = None
    try:
        first_create = asyncio.create_task(first.request("tabs.create", {"url": "about:blank"}, timeout=2.0))
        await asyncio.wait_for(relay.request_started.wait(), 1.0)
        relay.scoped_tabs = [{"tabId": 91, "url": "about:blank", "title": ""}]
        await server._handle_extension_event(
            "scope.tabAdded",
            {"tabId": 91, "url": "about:blank", "title": "", "origin": "created"},
        )
        assert server._pending_create_count == 1
        assert 91 in server._pending_tab_events

        with pytest.raises(BrowserExtensionBrokerError) as error_info:
            await second.request("tabs.create", {"url": "about:blank"}, timeout=1.0)

        assert error_info.value.code == "RESOURCE_LIMIT"
        assert server._pending_create_count == 1
        assert [op for op, _args in relay.requests].count("tabs.create") == 1
    finally:
        relay.release_request.set()
        if first_create is not None:
            await asyncio.gather(first_create, return_exceptions=True)
        await second.stop()
        await first.stop()
        await asyncio.wait_for(second_server_task, 1.0)
        await asyncio.wait_for(first_server_task, 1.0)
        await server.stop()


@pytest.mark.asyncio
async def test_legacy_scope_additions_stay_globally_fenced_while_create_is_pending() -> None:
    server = BrowserExtensionBrokerServer(19777, base_dir=_test_broker_base_dir())
    relay = BlockingRelay("extension-secret", 19777, server._handle_extension_event, server._handle_disconnect)
    server._relay = relay
    first = BrokerClient(19777, _ignore_event, base_dir=_test_broker_base_dir(), auto_spawn=False)
    second = BrokerClient(19777, _ignore_event, base_dir=_test_broker_base_dir(), auto_spawn=False)
    first_server_task = await _connect_over_socketpair(server, first)
    second_server_task = await _connect_over_socketpair(server, second)
    first_create: asyncio.Task[dict] | None = None
    try:
        first_create = asyncio.create_task(first.request("tabs.create", {"url": "about:blank"}, timeout=2.0))
        await asyncio.wait_for(relay.request_started.wait(), 1.0)
        relay.scoped_tabs = [{"tabId": 91, "url": "https://shared.example.test", "title": "Shared"}]
        server._extension_supports_scope_origins = False
        await server._handle_extension_event(
            "scope.tabAdded",
            {"tabId": 91, "url": "https://shared.example.test", "title": "Shared"},
        )

        assert 91 not in server._pending_tab_events
        with pytest.raises(BrowserExtensionBrokerError) as error_info:
            await second.request("tabs.activate", {"tabId": 91}, timeout=1.0)
        assert error_info.value.code == "LEASE_HELD"
        assert relay.requests == [("tabs.create", {"url": "about:blank"})]

        relay.release_request.set()
        if first_create is not None:
            await first_create
            first_create = None
        await second.request("tabs.activate", {"tabId": 91})
        assert server._leases[91].client_id == second._client_id
    finally:
        relay.release_request.set()
        if first_create is not None:
            await asyncio.gather(first_create, return_exceptions=True)
        await second.stop()
        await first.stop()
        await asyncio.wait_for(second_server_task, 1.0)
        await asyncio.wait_for(first_server_task, 1.0)
        await server.stop()


@pytest.mark.asyncio
async def test_popup_from_draining_opener_stays_fenced_until_closed() -> None:
    server = BrowserExtensionBrokerServer(19777, base_dir=_test_broker_base_dir())
    relay = FakeRelay("extension-secret", 19777, server._handle_extension_event, server._handle_disconnect)
    original_request = relay.request
    remove_started = asyncio.Event()
    finish_remove = asyncio.Event()

    async def controlled_cleanup(
        op: str,
        args: dict,
        timeout: float | None = 30.0,
        *,
        retain_until_terminal: bool = False,
        on_registered: Callable[[], None] | None = None,
        on_terminal: Callable[[], None] | None = None,
    ) -> dict:
        if op == "tabs.remove" and args.get("tabId") == 8:
            relay.requests.append((op, dict(args)))
            remove_started.set()
            await finish_remove.wait()
            relay.scoped_tabs = [tab for tab in relay.scoped_tabs if tab.get("tabId") != 8]
            await server._handle_extension_event("scope.tabRemoved", {"tabId": 8, "reason": "closed"})
            return {}
        return await original_request(
            op,
            args,
            timeout,
            retain_until_terminal=retain_until_terminal,
            on_registered=on_registered,
            on_terminal=on_terminal,
        )

    relay.request = controlled_cleanup  # type: ignore[method-assign]
    server._relay = relay
    first = BrokerClient(19777, _ignore_event, base_dir=_test_broker_base_dir(), auto_spawn=False)
    second = BrokerClient(19777, _ignore_event, base_dir=_test_broker_base_dir(), auto_spawn=False)
    first_server_task = await _connect_over_socketpair(server, first)
    second_server_task = await _connect_over_socketpair(server, second)
    first_client_id = first._client_id
    assert first_client_id is not None
    relay.scoped_tabs = [
        {"tabId": 7, "url": "https://opener.example.test", "title": "Opener"},
        {"tabId": 8, "url": "https://popup.example.test", "title": "Popup"},
    ]
    opener = await server._grant_lease(7, first_client_id, origin="created")
    opener.draining = True
    try:
        await server._handle_extension_event(
            "tabs.created",
            {"tabId": 8, "openerTabId": 7, "url": "https://popup.example.test"},
        )
        await asyncio.wait_for(remove_started.wait(), 1.0)

        popup = server._leases[8]
        assert popup.client_id == first_client_id
        assert popup.origin == "created"
        assert popup.draining
        with pytest.raises(BrowserExtensionBrokerError) as claim_error:
            await second.request("tabs.activate", {"tabId": 8})
        assert claim_error.value.code == "LEASE_HELD"

        finish_remove.set()
        await _eventually(lambda: 8 not in server._leases)
        assert ("tabs.remove", {"tabId": 8}) in relay.requests
    finally:
        finish_remove.set()
        await server._free_lease(7)
        await second.stop()
        await first.stop()
        await asyncio.wait_for(second_server_task, 1.0)
        await asyncio.wait_for(first_server_task, 1.0)
        await server.stop()


@pytest.mark.asyncio
@pytest.mark.parametrize(("origin", "cleanup_op"), [("created", "tabs.remove"), ("shared", "debugger.detach")])
async def test_explicit_lease_release_uses_authoritative_origin(origin: str, cleanup_op: str) -> None:
    server = BrowserExtensionBrokerServer(19777, base_dir=_test_broker_base_dir())
    relay = FakeRelay("extension-secret", 19777, server._handle_extension_event, server._handle_disconnect)
    server._relay = relay
    client = BrokerClient(19777, _ignore_event, base_dir=_test_broker_base_dir(), auto_spawn=False)
    server_task = await _connect_over_socketpair(server, client)
    client_id = client._client_id
    assert client_id is not None
    await server._grant_lease(7, client_id, origin=origin)

    await client.release_tab(7)
    await _eventually(lambda: 7 not in server._leases)

    assert (cleanup_op, {"tabId": 7}) in relay.requests
    await client.stop()
    await asyncio.wait_for(server_task, 1.0)
    await server.stop()


@pytest.mark.asyncio
@pytest.mark.parametrize("limit_name", ["MAX_REQUESTS_PER_CLIENT", "MAX_GLOBAL_REQUESTS"])
async def test_retained_timed_out_extension_requests_remain_bounded(
    monkeypatch: pytest.MonkeyPatch,
    limit_name: str,
) -> None:
    monkeypatch.setattr(broker_server_module, limit_name, 1)
    server = BrowserExtensionBrokerServer(19777, base_dir=_test_broker_base_dir())
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
    relay.extension_protocol_version = 2
    server._relay = relay
    server._extension_reset_quarantined = False
    client = BrokerClient(19777, _ignore_event, base_dir=_test_broker_base_dir(), auto_spawn=False)
    server_task = await _connect_over_socketpair(server, client)
    relay.scoped_tabs = [{"tabId": 7}, {"tabId": 8}, {"tabId": 9}]
    try:
        with pytest.raises(ExtensionRequestError, match="timed out"):
            await client.request("tabs.activate", {"tabId": 7}, timeout=0.1)
        assert relay.pending_request_count == 1
        assert client._client_id is not None
        active = server._clients[client._client_id]
        assert len(active.request_ids) == 1
        assert len(active.outstanding_request_ids) == 1
        assert server._global_requests == 1
        assert server._global_outstanding_requests == 1

        with pytest.raises(ExtensionRequestError, match="timed out"):
            await client.request("tabs.activate", {"tabId": 8}, timeout=0.1)
        assert len(websocket.requests) == 2
        assert relay.pending_request_count == 2
        assert len(active.request_ids) == 2
        assert len(active.outstanding_request_ids) == 2
        assert server._global_requests == 2
        assert server._global_outstanding_requests == 2

        await relay._handle_text_frame(
            relay._websocket,
            json.dumps({"v": 2, "type": "response", "id": websocket.requests[0]["id"], "ok": True, "result": {}}),
        )
        await _eventually(lambda: relay.pending_request_count == 1)
        assert len(active.request_ids) == 1
        assert len(active.outstanding_request_ids) == 1
        assert server._global_requests == 1
        assert server._global_outstanding_requests == 1

        await relay._handle_text_frame(
            relay._websocket,
            json.dumps({"v": 2, "type": "response", "id": websocket.requests[1]["id"], "ok": True, "result": {}}),
        )
        await _eventually(lambda: relay.pending_request_count == 0)
        await _eventually(lambda: not active.request_ids and server._global_requests == 0)
        assert not active.outstanding_request_ids
        assert server._global_outstanding_requests == 0

        with pytest.raises(ExtensionRequestError, match="timed out"):
            await client.request("tabs.activate", {"tabId": 9}, timeout=0.1)
        assert len(websocket.requests) == 3
    finally:
        await client.stop()
        await asyncio.wait_for(server_task, 1.0)
        await relay._handle_disconnect(relay._websocket)
        await server.stop()


@pytest.mark.asyncio
async def test_retained_timed_out_request_holds_inbound_bytes_until_terminal(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    server = BrowserExtensionBrokerServer(19777, base_dir=_test_broker_base_dir())
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
    relay.extension_protocol_version = 2
    server._relay = relay
    server._extension_reset_quarantined = False
    client = BrokerClient(19777, _ignore_event, base_dir=_test_broker_base_dir(), auto_spawn=False)
    server_task = await _connect_over_socketpair(server, client)
    relay.scoped_tabs = [{"tabId": 7}]
    try:
        with pytest.raises(ExtensionRequestError, match="timed out"):
            await client.request("debugger.send", {"tabId": 7, "params": {"padding": "x" * 4096}}, timeout=0.1)
        assert client._client_id is not None
        active = server._clients[client._client_id]
        assert active.inbound_bytes > 4096
        assert server._global_inbound_bytes == active.inbound_bytes

        monkeypatch.setattr(broker_server_module, "MAX_CLIENT_INBOUND_BYTES", active.inbound_bytes)
        with pytest.raises(BrowserExtensionNotConnectedError):
            await client.request("debugger.send", {"tabId": 7, "params": {"padding": "y" * 4096}}, timeout=0.01)
        assert [request["op"] for request in websocket.requests].count("debugger.send") == 1
        await asyncio.wait_for(server_task, 1.0)

        await relay._handle_text_frame(
            relay._websocket,
            json.dumps({"v": 2, "type": "response", "id": websocket.requests[0]["id"], "ok": True, "result": {}}),
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
    monkeypatch.setattr(broker_server_module, "TAB_REQUEST_QUEUE_WAIT_SECONDS", 0.01)
    server = BrowserExtensionBrokerServer(19777, base_dir=_test_broker_base_dir())
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
    relay.extension_protocol_version = 2
    server._relay = relay
    server._extension_reset_quarantined = False
    client = BrokerClient(19777, _ignore_event, base_dir=_test_broker_base_dir(), auto_spawn=False)
    server_task = await _connect_over_socketpair(server, client)
    relay.scoped_tabs = [{"tabId": 7}, {"tabId": 8}]
    try:
        with pytest.raises(ExtensionRequestError, match="timed out"):
            await client.request("tabs.activate", {"tabId": 7}, timeout=0.1)
        assert server._tab_request_counts == {7: 1}

        with pytest.raises(BrowserExtensionBrokerError, match="COMMAND_TIMEOUT: Request expired while queued"):
            await client.request("debugger.send", {"tabId": 7}, timeout=0.01)
        assert len(websocket.requests) == 1

        with pytest.raises(ExtensionRequestError, match="timed out"):
            await client.request("tabs.activate", {"tabId": 8}, timeout=0.1)
        assert server._tab_request_counts == {7: 1, 8: 1}

        await relay._handle_text_frame(
            relay._websocket,
            json.dumps({"v": 2, "type": "response", "id": websocket.requests[0]["id"], "ok": True, "result": {}}),
        )
        await _eventually(lambda: server._tab_request_counts == {8: 1})

        with pytest.raises(ExtensionRequestError, match="timed out"):
            await client.request("debugger.send", {"tabId": 7}, timeout=0.1)
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
    monkeypatch.setattr(broker_server_module, "TAB_REQUEST_QUEUE_WAIT_SECONDS", 0.01)
    server = BrowserExtensionBrokerServer(19777, base_dir=_test_broker_base_dir())
    relay = BlockingRelay("extension-secret", 19777, server._handle_extension_event, server._handle_disconnect)
    server._relay = relay
    client = BrokerClient(19777, _ignore_event, base_dir=_test_broker_base_dir(), auto_spawn=False)
    server_task = await _connect_over_socketpair(server, client)
    relay.scoped_tabs = [{"tabId": 5}]
    assert client._client_id is not None
    connection = server._clients[client._client_id]
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
async def test_tab_request_queue_forwards_fifo_when_a_slot_frees() -> None:
    server, relay, client, server_task, connection = await _tab_queue_setup()
    requests = [{"op": "tabs.activate", "args": {"tabId": 7, "index": index}, "timeout": 30.0} for index in range(33)]
    tasks = [asyncio.create_task(server._dispatch(connection, "extension.request", request)) for request in requests]
    try:
        await _eventually(lambda: len(relay.requests) == 32)
        await _eventually(lambda: len(server._tab_request_queues.get(7, ())) == 1)
        assert not tasks[32].done()

        relay.request_gates[0].set()
        await _eventually(lambda: len(relay.requests) == 33)
        assert relay.requests[32][1]["index"] == 32

        for gate in relay.request_gates:
            gate.set()
        results = await asyncio.gather(*tasks)
        assert [result["args"]["index"] for result in results] == list(range(33))
        assert server._tab_request_queues == {}
        assert server._tab_request_counts == {}
    finally:
        for gate in relay.request_gates:
            gate.set()
        await asyncio.gather(*tasks, return_exceptions=True)
        await client.stop()
        await asyncio.wait_for(server_task, 1.0)
        await server.stop()


@pytest.mark.asyncio
async def test_tab_request_queue_wakes_all_waiters_after_all_slots_free() -> None:
    server, relay, client, server_task, connection = await _tab_queue_setup()
    requests = [{"op": "tabs.activate", "args": {"tabId": 7, "index": index}, "timeout": 30.0} for index in range(36)]
    tasks = [asyncio.create_task(server._dispatch(connection, "extension.request", request)) for request in requests]
    try:
        await _eventually(lambda: len(relay.requests) == 32)
        await _eventually(lambda: len(server._tab_request_queues.get(7, ())) == 4)

        for gate in tuple(relay.request_gates):
            gate.set()
        await _eventually(lambda: len(relay.requests) == 36)
        assert not server._tab_request_queues.get(7)
        assert server._tab_request_counts == {7: 4}

        for gate in tuple(relay.request_gates):
            gate.set()
        results = await asyncio.gather(*tasks)
        assert [result["args"]["index"] for result in results] == list(range(36))
        assert server._tab_request_counts == {}
    finally:
        for gate in tuple(relay.request_gates):
            gate.set()
        await asyncio.gather(*tasks, return_exceptions=True)
        await client.stop()
        await asyncio.wait_for(server_task, 1.0)
        await server.stop()


@pytest.mark.asyncio
async def test_tab_request_queue_wakes_only_available_slots_in_fifo_order() -> None:
    server, relay, client, server_task, connection = await _tab_queue_setup()
    requests = [{"op": "tabs.activate", "args": {"tabId": 7, "index": index}, "timeout": 30.0} for index in range(72)]
    tasks = [asyncio.create_task(server._dispatch(connection, "extension.request", request)) for request in requests]
    try:
        await _eventually(lambda: len(relay.requests) == 32)
        await _eventually(lambda: len(server._tab_request_queues.get(7, ())) == 40)

        for gate in tuple(relay.request_gates):
            gate.set()
        await _eventually(lambda: len(relay.requests) == 64)
        assert len(server._tab_request_queues.get(7, ())) == 8
        assert server._tab_request_counts == {7: 32}
        assert [request[1]["index"] for request in relay.requests] == list(range(64))

        for gate in tuple(relay.request_gates[32:]):
            gate.set()
        await _eventually(lambda: len(relay.requests) == 72)
        assert not server._tab_request_queues.get(7)
        for gate in tuple(relay.request_gates[64:]):
            gate.set()

        results = await asyncio.gather(*tasks)
        assert [result["args"]["index"] for result in results] == list(range(72))
        assert server._tab_request_counts == {}
    finally:
        for gate in tuple(relay.request_gates):
            gate.set()
        await asyncio.gather(*tasks, return_exceptions=True)
        await client.stop()
        await asyncio.wait_for(server_task, 1.0)
        await server.stop()


@pytest.mark.asyncio
async def test_read_requests_admits_tab_queue_beyond_per_client_active_budget() -> None:
    server, relay, client, server_task, connection = await _tab_queue_setup()
    requests = [{"op": "tabs.activate", "args": {"tabId": 7, "index": index}, "timeout": 30.0} for index in range(33)]
    tasks = [
        asyncio.create_task(client.request(request["op"], request["args"], timeout=request["timeout"]))
        for request in requests
    ]
    try:
        await _eventually(lambda: len(relay.requests) == 32)
        await _eventually(lambda: len(server._tab_request_queues.get(7, ())) == 1)
        assert len(connection.request_ids) == 32
        assert len(connection.queued_request_ids) == 1
        assert len(connection.outstanding_request_ids) == 33
        assert server._global_requests == 32
        assert server._global_queued_requests == 1
        assert server._global_outstanding_requests == 33

        relay.request_gates[0].set()
        await _eventually(lambda: len(relay.requests) == 33)
        assert relay.requests[32][1]["index"] == 32
        assert len(connection.request_ids) == 32
        assert not connection.queued_request_ids
        assert len(connection.outstanding_request_ids) == 32
        assert server._global_requests == 32
        assert server._global_queued_requests == 0
        assert server._global_outstanding_requests == 32

        for gate in tuple(relay.request_gates):
            gate.set()
        results = await asyncio.gather(*tasks)
        assert [result["args"]["index"] for result in results] == list(range(33))
        await _eventually(lambda: server._global_requests == 0)
        assert not connection.outstanding_request_ids
        assert server._global_outstanding_requests == 0
    finally:
        for gate in tuple(relay.request_gates):
            gate.set()
        await asyncio.gather(*tasks, return_exceptions=True)
        await client.stop()
        await asyncio.wait_for(server_task, 1.0)
        await server.stop()


@pytest.mark.asyncio
async def test_read_requests_bounds_outstanding_requests_across_tabs() -> None:
    server, relay, client, server_task, connection = await _tab_queue_setup()
    tab_ids = list(range(32))
    relay.scoped_tabs = [{"tabId": tab_id} for tab_id in tab_ids]
    assert client._client_id is not None
    for tab_id in tab_ids[1:]:
        await server._grant_lease(tab_id, client._client_id, origin="shared")

    maximum_outstanding = 0
    original_handle = server._handle_charged_request

    async def tracked_handle(
        request_connection: _ClientConnection,
        request_id: str,
        frame: dict,
        size: int,
        *,
        request_started_at: float | None = None,
    ) -> None:
        nonlocal maximum_outstanding
        maximum_outstanding = max(maximum_outstanding, len(request_connection.outstanding_request_ids))
        await original_handle(
            request_connection,
            request_id,
            frame,
            size,
            request_started_at=request_started_at,
        )

    server._handle_charged_request = tracked_handle  # type: ignore[method-assign]
    tasks = [
        asyncio.create_task(
            client.request(
                "tabs.activate",
                {"tabId": tab_id, "index": index},
                timeout=30.0,
            )
        )
        for tab_id in tab_ids
        for index in range(32)
    ]
    try:
        await _eventually(lambda: len(connection.outstanding_request_ids) == 160)
        await _eventually(lambda: len(relay.requests) == 160)
        await _eventually(lambda: sum(task.done() for task in tasks) == 1024 - 160)
        assert maximum_outstanding == broker_server_module.MAX_OUTSTANDING_REQUESTS_PER_CLIENT
        assert server._global_outstanding_requests == 160
        assert len(connection.request_ids) == 160

        for gate in tuple(relay.request_gates):
            gate.set()
        results = await asyncio.gather(*tasks, return_exceptions=True)
        successes = [result for result in results if isinstance(result, dict)]
        errors = [result for result in results if isinstance(result, BrowserExtensionBrokerError)]
        assert len(successes) == 160
        assert len(errors) == 1024 - 160
        assert all(error.code == "RESOURCE_LIMIT" for error in errors)
        await _eventually(lambda: not connection.outstanding_request_ids and server._global_outstanding_requests == 0)
        assert not connection.request_ids
        assert not connection.queued_request_ids
        assert server._global_requests == 0
        assert server._global_queued_requests == 0
        assert connection.inbound_bytes == 0
        assert server._global_inbound_bytes == 0
    finally:
        for gate in tuple(relay.request_gates):
            gate.set()
        await asyncio.gather(*tasks, return_exceptions=True)
        await client.stop()
        await asyncio.wait_for(server_task, 1.0)
        await server.stop()


@pytest.mark.asyncio
async def test_read_requests_enforces_global_outstanding_budget_at_ingress(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(broker_server_module, "MAX_GLOBAL_OUTSTANDING_REQUESTS", 1)
    server, relay, client, server_task, connection = await _tab_queue_setup()
    first = asyncio.create_task(client.request("tabs.activate", {"tabId": 7, "index": 0}))
    second: asyncio.Task[dict] | None = None
    try:
        await _eventually(lambda: len(relay.requests) == 1)
        assert len(connection.outstanding_request_ids) == 1
        assert server._global_outstanding_requests == 1

        second = asyncio.create_task(client.request("tabs.activate", {"tabId": 7, "index": 1}))
        with pytest.raises(BrowserExtensionBrokerError) as error_info:
            await second
        assert error_info.value.code == "RESOURCE_LIMIT"
        assert len(connection.outstanding_request_ids) == 1
        assert server._global_outstanding_requests == 1

        relay.request_gates[0].set()
        await first
        await _eventually(lambda: not connection.outstanding_request_ids and server._global_outstanding_requests == 0)
    finally:
        for gate in tuple(relay.request_gates):
            gate.set()
        tasks = [task for task in (first, second) if task is not None]
        await asyncio.gather(*tasks, return_exceptions=True)
        await client.stop()
        await asyncio.wait_for(server_task, 1.0)
        await server.stop()


@pytest.mark.asyncio
async def test_queued_tab_requests_admit_beyond_active_client_and_global_caps(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(broker_server_module, "MAX_GLOBAL_REQUESTS", 32)
    monkeypatch.setattr(broker_server_module, "MAX_REQUESTS_PER_TAB", 1)
    server, relay, client, server_task, connection = await _tab_queue_setup()
    active_started = asyncio.Event()
    release_active = asyncio.Event()
    active_dispatches = 0
    original_dispatch = server._dispatch

    async def blocking_dispatch(
        dispatch_connection: _ClientConnection,
        op: str,
        args: dict,
        **kwargs: object,
    ) -> dict:
        nonlocal active_dispatches
        if op == "broker.status":
            active_dispatches += 1
            if active_dispatches == 32:
                active_started.set()
            await release_active.wait()
        return await original_dispatch(dispatch_connection, op, args, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(server, "_dispatch", blocking_dispatch)
    active_tasks = [asyncio.create_task(client.broker_status()) for _ in range(32)]
    tab_tasks = [
        asyncio.create_task(client.request("tabs.activate", {"tabId": 7, "index": index})) for index in range(3)
    ]
    try:
        await asyncio.wait_for(active_started.wait(), 1.0)
        await _eventually(lambda: len(connection.request_ids) == 33)
        await _eventually(lambda: len(relay.requests) == 1)
        await _eventually(lambda: len(server._tab_request_queues.get(7, ())) == 2)
        assert len(connection.request_ids) == 33
        assert server._global_requests == 33
        assert len(connection.queued_request_ids) == 2
        assert server._global_queued_requests == 2
        assert len(connection.outstanding_request_ids) == 35
        assert server._global_outstanding_requests == 35

        relay.request_gates[0].set()
        await _eventually(lambda: len(relay.requests) == 2)
        relay.request_gates[1].set()
        await _eventually(lambda: len(relay.requests) == 3)
        relay.request_gates[2].set()
        results = await asyncio.gather(*tab_tasks)
        assert [result["args"]["index"] for result in results] == [0, 1, 2]
        await _eventually(lambda: server._tab_request_counts == {})
        assert len(connection.request_ids) == 32
        assert not connection.queued_request_ids
        assert server._global_requests == 32
        assert server._global_queued_requests == 0
        assert len(connection.outstanding_request_ids) == 32
        assert server._global_outstanding_requests == 32
    finally:
        release_active.set()
        for gate in tuple(relay.request_gates):
            gate.set()
        await asyncio.gather(*tab_tasks, *active_tasks, return_exceptions=True)
        await client.stop()
        await asyncio.wait_for(server_task, 1.0)
        await server.stop()


@pytest.mark.asyncio
async def test_tab_slot_released_when_admission_callback_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(broker_server_module, "MAX_REQUESTS_PER_TAB", 1)
    server, relay, client, server_task, connection = await _tab_queue_setup()
    assert server._reserve_tab_request(7)
    request = {"op": "tabs.activate", "args": {"tabId": 7}, "timeout": 30.0}

    def fail_on_admitted() -> None:
        raise BrowserExtensionBrokerError("INTERNAL", "admission callback failed")

    failed = asyncio.create_task(
        server._dispatch(connection, "extension.request", request, on_admitted=fail_on_admitted)
    )
    next_request = asyncio.create_task(server._dispatch(connection, "extension.request", request))
    try:
        await _eventually(lambda: len(server._tab_request_queues.get(7, ())) == 2)
        server._release_tab_request(7)
        with pytest.raises(BrowserExtensionBrokerError, match="admission callback failed"):
            await failed
        await _eventually(lambda: len(relay.requests) == 1)
        assert server._tab_request_counts == {7: 1}

        relay.request_gates[0].set()
        result = await next_request
        assert result["args"]["tabId"] == 7
        assert server._tab_request_counts == {}
    finally:
        for gate in tuple(relay.request_gates):
            gate.set()
        await asyncio.gather(failed, next_request, return_exceptions=True)
        await client.stop()
        await asyncio.wait_for(server_task, 1.0)
        await server.stop()


@pytest.mark.asyncio
async def test_tab_slot_released_when_request_is_cancelled_before_forwarding(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(broker_server_module, "MAX_REQUESTS_PER_TAB", 1)
    server, relay, client, server_task, connection = await _tab_queue_setup()
    assert server._reserve_tab_request(7)
    request = {"op": "tabs.activate", "args": {"tabId": 7}, "timeout": 30.0}

    def cancel_on_admitted() -> None:
        current_task = asyncio.current_task()
        assert current_task is not None
        current_task.cancel()
        raise asyncio.CancelledError

    cancelled = asyncio.create_task(
        server._dispatch(connection, "extension.request", request, on_admitted=cancel_on_admitted)
    )
    next_request = asyncio.create_task(server._dispatch(connection, "extension.request", request))
    try:
        await _eventually(lambda: len(server._tab_request_queues.get(7, ())) == 2)
        server._release_tab_request(7)
        with pytest.raises(asyncio.CancelledError):
            await cancelled
        await _eventually(lambda: len(relay.requests) == 1)
        assert server._tab_request_counts == {7: 1}

        relay.request_gates[0].set()
        await next_request
        assert server._tab_request_counts == {}
    finally:
        for gate in tuple(relay.request_gates):
            gate.set()
        await asyncio.gather(cancelled, next_request, return_exceptions=True)
        await client.stop()
        await asyncio.wait_for(server_task, 1.0)
        await server.stop()


@pytest.mark.asyncio
async def test_queued_tab_request_forwards_with_remaining_timeout(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(broker_server_module, "MAX_REQUESTS_PER_TAB", 1)
    clock = [0.0]
    server, relay, client, server_task, connection = await _tab_queue_setup(time_source=lambda: clock[0])
    first = asyncio.create_task(
        server._dispatch(
            connection,
            "extension.request",
            {"op": "tabs.activate", "args": {"tabId": 7, "index": 0}, "timeout": 30.0},
        )
    )
    second = asyncio.create_task(
        server._dispatch(
            connection,
            "extension.request",
            {"op": "tabs.activate", "args": {"tabId": 7, "index": 1}, "timeout": 5.0},
        )
    )
    try:
        await _eventually(lambda: len(server._tab_request_queues.get(7, ())) == 1)
        clock[0] = 0.25
        relay.request_gates[0].set()
        await _eventually(lambda: len(relay.requests) == 2)
        assert relay.request_timeouts[1] == pytest.approx(4.75)
        relay.request_gates[1].set()
        await first
        result = await second
        assert result["timeout"] == pytest.approx(4.75)
    finally:
        for gate in tuple(relay.request_gates):
            gate.set()
        await asyncio.gather(first, second, return_exceptions=True)
        await client.stop()
        await asyncio.wait_for(server_task, 1.0)
        await server.stop()


@pytest.mark.asyncio
async def test_unqueued_tab_request_forwards_with_remaining_timeout_after_lease_claim(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    clock = [0.0]
    server, relay, client, server_task, connection = await _tab_queue_setup(time_source=lambda: clock[0])
    original_claim = server._claim_tab_lease

    async def delayed_claim(*args: object, **kwargs: object) -> None:
        clock[0] = 1.25
        await original_claim(*args, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(server, "_claim_tab_lease", delayed_claim)
    request = {"op": "tabs.activate", "args": {"tabId": 7, "index": 0}, "timeout": 5.0}
    task = asyncio.create_task(
        server._dispatch(
            connection,
            "extension.request",
            request,
            request_started_at=0.0,
        )
    )
    try:
        await _eventually(lambda: len(relay.requests) == 1)
        assert relay.request_timeouts[0] == pytest.approx(3.75)
        relay.request_gates[0].set()
        result = await task
        assert result["timeout"] == pytest.approx(3.75)
    finally:
        for gate in tuple(relay.request_gates):
            gate.set()
        await asyncio.gather(task, return_exceptions=True)
        await client.stop()
        await asyncio.wait_for(server_task, 1.0)
        await server.stop()


@pytest.mark.asyncio
async def test_queued_tab_request_expiry_is_rejected_before_forwarding(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(broker_server_module, "MAX_REQUESTS_PER_TAB", 1)
    clock = [0.0]
    server, relay, client, server_task, connection = await _tab_queue_setup(time_source=lambda: clock[0])
    first = asyncio.create_task(
        server._dispatch(
            connection,
            "extension.request",
            {"op": "tabs.activate", "args": {"tabId": 7, "index": 0}, "timeout": 30.0},
        )
    )
    second = asyncio.create_task(
        server._dispatch(
            connection,
            "extension.request",
            {"op": "tabs.activate", "args": {"tabId": 7, "index": 1}, "timeout": 5.0},
        )
    )
    try:
        await _eventually(lambda: len(server._tab_request_queues.get(7, ())) == 1)
        clock[0] = 5.0
        relay.request_gates[0].set()
        with pytest.raises(BrowserExtensionBrokerError) as error_info:
            await second
        assert error_info.value.code == "COMMAND_TIMEOUT"
        assert error_info.value.message == "Request expired while queued"
        assert len(relay.requests) == 1
        await first
    finally:
        for gate in tuple(relay.request_gates):
            gate.set()
        await asyncio.gather(first, second, return_exceptions=True)
        await client.stop()
        await asyncio.wait_for(server_task, 1.0)
        await server.stop()


@pytest.mark.asyncio
async def test_tab_request_queue_fairness_keeps_newcomer_behind_notified_waiters(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(broker_server_module, "MAX_REQUESTS_PER_TAB", 1)
    server, relay, client, server_task, connection = await _tab_queue_setup()
    hold_a = asyncio.Event()
    hold_b = asyncio.Event()
    hold_c = asyncio.Event()
    admitted: list[str] = []

    async def wait_and_hold(name: str, release: asyncio.Event) -> None:
        await server._wait_for_tab_request_slot(connection, 7, 30.0)
        admitted.append(name)
        await release.wait()
        server._release_tab_request(7)

    assert server._reserve_tab_request(7)
    a = asyncio.create_task(wait_and_hold("A", hold_a))
    b = asyncio.create_task(wait_and_hold("B", hold_b))
    c: asyncio.Task[None] | None = None
    try:
        await _eventually(lambda: len(server._tab_request_queues.get(7, ())) == 2)
        server._release_tab_request(7)
        c = asyncio.create_task(wait_and_hold("C", hold_c))
        await _eventually(lambda: admitted == ["A"])
        hold_a.set()
        await _eventually(lambda: admitted == ["A", "B"])
        hold_b.set()
        await _eventually(lambda: admitted == ["A", "B", "C"])
        hold_c.set()
        await asyncio.gather(a, b, c)
        assert admitted == ["A", "B", "C"]
        assert server._tab_request_counts == {}
    finally:
        hold_a.set()
        hold_b.set()
        hold_c.set()
        tasks = [task for task in (a, b, c) if task is not None]
        await asyncio.gather(*tasks, return_exceptions=True)
        await client.stop()
        await asyncio.wait_for(server_task, 1.0)
        await server.stop()


@pytest.mark.asyncio
async def test_cancelled_notified_tab_waiter_passes_reserved_slot_to_next(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(broker_server_module, "MAX_REQUESTS_PER_TAB", 1)
    server, relay, client, server_task, connection = await _tab_queue_setup()
    assert server._reserve_tab_request(7)
    a = asyncio.create_task(server._wait_for_tab_request_slot(connection, 7, 30.0))
    b = asyncio.create_task(server._wait_for_tab_request_slot(connection, 7, 30.0))
    try:
        await _eventually(lambda: len(server._tab_request_queues.get(7, ())) == 2)
        server._release_tab_request(7)
        waiter = server._tab_request_queues[7][0]
        assert waiter.notified
        assert waiter.slot_reserved
        assert not a.done()
        assert a.cancel()
        with pytest.raises(asyncio.CancelledError):
            await a
        await _eventually(lambda: b.done())
        assert b.exception() is None
        assert server._tab_request_counts == {7: 1}
        server._release_tab_request(7)
        await b
        assert server._tab_request_counts == {}
    finally:
        a.cancel()
        b.cancel()
        await asyncio.gather(a, b, return_exceptions=True)
        await client.stop()
        await asyncio.wait_for(server_task, 1.0)
        await server.stop()


@pytest.mark.asyncio
async def test_shutdown_fails_notified_waiter_and_rejects_new_tab_admission(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(broker_server_module, "MAX_REQUESTS_PER_TAB", 1)
    server, relay, client, server_task, connection = await _tab_queue_setup()
    assert server._reserve_tab_request(7)
    waiter_task = asyncio.create_task(server._wait_for_tab_request_slot(connection, 7, 30.0))
    try:
        await _eventually(lambda: len(server._tab_request_queues.get(7, ())) == 1)
        server._release_tab_request(7)
        waiter = server._tab_request_queues[7][0]
        assert waiter.notified
        assert waiter.slot_reserved
        server._stopping = True
        server._fail_all_queued_tab_requests(
            BrowserExtensionBrokerError("BROKER_STOPPING", "Browser-extension broker is stopping")
        )
        with pytest.raises(BrowserExtensionBrokerError) as error_info:
            await waiter_task
        assert error_info.value.code == "BROKER_STOPPING"
        assert server._tab_request_queues == {}
        assert server._tab_request_counts == {}
        with pytest.raises(BrowserExtensionBrokerError) as admission_error:
            await server._wait_for_tab_request_slot(connection, 7, 30.0)
        assert admission_error.value.code == "BROKER_STOPPING"
    finally:
        waiter_task.cancel()
        await asyncio.gather(waiter_task, return_exceptions=True)
        await client.stop()
        await asyncio.wait_for(server_task, 1.0)
        server._stopping = False
        await server.stop()


@pytest.mark.asyncio
async def test_read_requests_enforces_per_client_queued_request_budget(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(broker_server_module, "MAX_REQUESTS_PER_CLIENT", 1)
    monkeypatch.setattr(broker_server_module, "MAX_QUEUED_REQUESTS_PER_CLIENT", 1)
    monkeypatch.setattr(broker_server_module, "MAX_REQUESTS_PER_TAB", 1)
    server, relay, client, server_task, connection = await _tab_queue_setup()
    first = asyncio.create_task(client.request("tabs.activate", {"tabId": 7, "index": 0}, timeout=30.0))
    second: asyncio.Task[dict] | None = None
    third: asyncio.Task[dict] | None = None
    try:
        await _eventually(lambda: len(relay.requests) == 1)
        second = asyncio.create_task(client.request("tabs.activate", {"tabId": 7, "index": 1}, timeout=30.0))
        await _eventually(lambda: len(server._tab_request_queues.get(7, ())) == 1)
        third = asyncio.create_task(client.request("tabs.activate", {"tabId": 7, "index": 2}, timeout=30.0))
        assert third is not None
        with pytest.raises(BrowserExtensionBrokerError) as error_info:
            await third
        assert error_info.value.code == "RESOURCE_LIMIT"
        assert len(connection.queued_request_ids) == 1
        assert len(connection.outstanding_request_ids) == 2
        assert server._global_queued_requests == 1
        assert server._global_outstanding_requests == 2

        relay.request_gates[0].set()
        await first
        await _eventually(lambda: len(relay.requests) == 2)
        relay.request_gates[1].set()
        assert second is not None
        await second
        await _eventually(lambda: server._global_outstanding_requests == 0)
        assert not connection.outstanding_request_ids
    finally:
        for gate in tuple(relay.request_gates):
            gate.set()
        tasks = [task for task in (first, second, third) if task is not None]
        await asyncio.gather(*tasks, return_exceptions=True)
        await client.stop()
        await asyncio.wait_for(server_task, 1.0)
        await server.stop()


@pytest.mark.asyncio
async def test_tab_request_queue_cap_returns_resource_limit_and_logs_once(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(broker_server_module, "MAX_REQUESTS_PER_TAB", 1)
    monkeypatch.setattr(broker_server_module, "MAX_QUEUED_REQUESTS_PER_TAB", 1)
    server, relay, client, server_task, connection = await _tab_queue_setup()
    first = asyncio.create_task(
        server._dispatch(
            connection,
            "extension.request",
            {"op": "tabs.activate", "args": {"tabId": 7, "index": 0}, "timeout": 30.0},
        )
    )
    second = asyncio.create_task(
        server._dispatch(
            connection,
            "extension.request",
            {"op": "tabs.activate", "args": {"tabId": 7, "index": 1}, "timeout": 30.0},
        )
    )
    logs: list[tuple[str, dict[str, object]]] = []

    def capture_log(event: str, **fields: object) -> None:
        logs.append((event, fields))

    monkeypatch.setattr(broker_server_module.LOG, "info", capture_log)
    try:
        await _eventually(lambda: len(server._tab_request_queues.get(7, ())) == 1)
        with pytest.raises(BrowserExtensionBrokerError) as error_info:
            await server._dispatch(
                connection,
                "extension.request",
                {"op": "tabs.activate", "args": {"tabId": 7, "index": 2}, "timeout": 30.0},
            )
        assert error_info.value.code == "RESOURCE_LIMIT"
        assert [(event, fields["tab_id"]) for event, fields in logs] == [
            ("browser_extension_tab_request_queue_full", 7)
        ]
        assert len(server._tab_request_queues[7]) == 1

        relay.request_gates[0].set()
        await first
        await _eventually(lambda: len(relay.requests) == 2)
        relay.request_gates[1].set()
        await second
        assert server._tab_request_queues == {}
    finally:
        for gate in relay.request_gates:
            gate.set()
        await asyncio.gather(first, second, return_exceptions=True)
        await client.stop()
        await asyncio.wait_for(server_task, 1.0)
        await server.stop()


@pytest.mark.asyncio
async def test_tab_request_queue_wait_expiry_does_not_change_slot_accounting(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(broker_server_module, "MAX_REQUESTS_PER_TAB", 1)
    clock = [0.0]
    server, relay, client, server_task, connection = await _tab_queue_setup(
        time_source=lambda: clock[0],
    )
    first = asyncio.create_task(
        server._dispatch(
            connection,
            "extension.request",
            {"op": "tabs.activate", "args": {"tabId": 7, "index": 0}, "timeout": 30.0},
        )
    )
    second = asyncio.create_task(
        server._dispatch(
            connection,
            "extension.request",
            {"op": "tabs.activate", "args": {"tabId": 7, "index": 1}, "timeout": 30.0},
        )
    )
    try:
        await _eventually(lambda: len(server._tab_request_queues.get(7, ())) == 1)
        assert server._tab_request_counts == {7: 1}
        clock[0] = TAB_REQUEST_QUEUE_WAIT_SECONDS
        server._tab_request_queues[7][0].future.set_result(None)
        with pytest.raises(BrowserExtensionBrokerError) as error_info:
            await second
        assert error_info.value.code == "RESOURCE_LIMIT"
        assert server._tab_request_counts == {7: 1}
        assert server._tab_request_queues == {}
        assert len(relay.requests) == 1

        relay.request_gates[0].set()
        await first
        assert server._tab_request_counts == {}
    finally:
        for gate in relay.request_gates:
            gate.set()
        await asyncio.gather(first, second, return_exceptions=True)
        await client.stop()
        await asyncio.wait_for(server_task, 1.0)
        await server.stop()


@pytest.mark.asyncio
async def test_client_disconnect_fails_queued_tab_requests_without_leaks(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(broker_server_module, "MAX_REQUESTS_PER_TAB", 1)
    server, relay, client, server_task, connection = await _tab_queue_setup()
    first = asyncio.create_task(
        server._dispatch(
            connection,
            "extension.request",
            {"op": "tabs.activate", "args": {"tabId": 7, "index": 0}, "timeout": 30.0},
        )
    )
    queued = asyncio.create_task(
        server._dispatch(
            connection,
            "extension.request",
            {"op": "tabs.activate", "args": {"tabId": 7, "index": 1}, "timeout": 30.0},
        )
    )
    try:
        await _eventually(lambda: len(server._tab_request_queues.get(7, ())) == 1)
        await server._connection_closed(connection)
        with pytest.raises(BrowserExtensionNotConnectedError):
            await queued
        assert server._tab_request_queues == {}

        relay.request_gates[0].set()
        await first
        await _eventually(lambda: server._tab_request_counts == {})
    finally:
        for gate in relay.request_gates:
            gate.set()
        await asyncio.gather(first, queued, return_exceptions=True)
        await client.stop()
        await asyncio.wait_for(server_task, 1.0)
        await server.stop()


@pytest.mark.asyncio
@pytest.mark.parametrize("tab_id", [5.5, "5.0", "5.5", {"value": 5}])
async def test_invalid_tab_ids_are_rejected_before_forwarding(tab_id: object) -> None:
    server = BrowserExtensionBrokerServer(19777, base_dir=_test_broker_base_dir())
    relay = BlockingRelay("extension-secret", 19777, server._handle_extension_event, server._handle_disconnect)
    server._relay = relay
    client = BrokerClient(19777, _ignore_event, base_dir=_test_broker_base_dir(), auto_spawn=False)
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
    server = BrowserExtensionBrokerServer(19777, base_dir=_test_broker_base_dir())
    server._relay = FakeRelay("extension-secret", 19777, server._handle_extension_event, server._handle_disconnect)
    client = BrokerClient(19777, _ignore_event, base_dir=_test_broker_base_dir(), auto_spawn=False)
    server_socket, client_socket = socket.socketpair()
    server_reader, server_writer = await asyncio.open_connection(sock=server_socket)
    client_reader, client_writer = await asyncio.open_connection(sock=client_socket)
    server_task = asyncio.create_task(server._handle_connection(server_reader, server_writer))
    await client._authenticate(client_reader, client_writer)
    client_id = client._client_id
    assert client_id is not None
    try:
        await write_frame(
            client_writer,
            {"v": 1, "type": frame_type, "padding": "x" * CONTROL_FRAME_LIMIT},
        )
        await asyncio.wait_for(server_task, 1.0)
        assert client_id not in server._clients
    finally:
        client_writer.close()
        with suppress(BrokenPipeError):
            await client_writer.wait_closed()
        await server.stop()


@pytest.mark.asyncio
async def test_oversized_control_prefix_is_rejected_without_waiting_for_declared_body() -> None:
    server = BrowserExtensionBrokerServer(19777, base_dir=_test_broker_base_dir())
    server._relay = FakeRelay("extension-secret", 19777, server._handle_extension_event, server._handle_disconnect)
    client = BrokerClient(19777, _ignore_event, base_dir=_test_broker_base_dir(), auto_spawn=False)
    server_socket, client_socket = socket.socketpair()
    server_reader, server_writer = await asyncio.open_connection(sock=server_socket)
    client_reader, client_writer = await asyncio.open_connection(sock=client_socket)
    server_task = asyncio.create_task(server._handle_connection(server_reader, server_writer))
    await client._authenticate(client_reader, client_writer)
    client_id = client._client_id
    assert client_id is not None
    try:
        client_writer.write((1024 * 1024).to_bytes(4, "big") + b'{"v":1,"type":"pong","padding":')
        await client_writer.drain()
        await asyncio.wait_for(server_task, 1.0)
        assert client_id not in server._clients
    finally:
        client_writer.close()
        await client_writer.wait_closed()
        await server.stop()


@pytest.mark.asyncio
async def test_failed_sender_does_not_leak_active_client_slot(monkeypatch: pytest.MonkeyPatch) -> None:
    server = BrowserExtensionBrokerServer(19777, base_dir=_test_broker_base_dir())
    server._relay = FakeRelay("extension-secret", 19777, server._handle_extension_event, server._handle_disconnect)
    client = BrokerClient(19777, _ignore_event, base_dir=_test_broker_base_dir(), auto_spawn=False)
    server_task = await _connect_over_socketpair(server, client)

    async def fail_write(*_args: object) -> None:
        raise BrokenPipeError

    monkeypatch.setattr(server, "_write_encoded", fail_write)
    assert client._client_id is not None
    active = server._clients[client._client_id]
    await server._send_event(active, "extension.disconnected", {})
    await _eventually(lambda: active.sender_task is not None and active.sender_task.done())
    await client.stop()
    await asyncio.wait_for(server_task, 1.0)
    assert active.client_id not in server._clients
    await server.stop()


@pytest.mark.asyncio
async def test_peer_reset_during_close_releases_connection_accounting() -> None:
    class Transport:
        def __init__(self) -> None:
            self.abort_calls = 0

        def abort(self) -> None:
            self.abort_calls += 1

    class ResetWriter:
        def __init__(self) -> None:
            self.transport = Transport()

        def close(self) -> None:
            return None

        async def wait_closed(self) -> None:
            raise ConnectionResetError

    server = BrowserExtensionBrokerServer(19777, base_dir=_test_broker_base_dir())
    reader = asyncio.StreamReader()
    writer = ResetWriter()
    connection = _ClientConnection("reset", 1, reader, writer)  # type: ignore[arg-type]
    encoded = encode_frame(event_frame("queued", {}))
    assert await server._reserve_output(connection, len(encoded))
    connection.output_queue.put_nowait((encoded, None))
    connection.queued_request_ids.add("queued-request")
    connection.outstanding_request_ids.add("queued-request")
    server._clients[connection.client_id] = connection
    server._connections[id(connection)] = connection
    server._global_queued_requests = 1
    server._global_outstanding_requests = 1

    await server._connection_closed(connection)

    assert writer.transport.abort_calls == 1
    assert connection.client_id not in server._clients
    assert id(connection) not in server._connections
    assert not connection.queued_request_ids
    assert not connection.outstanding_request_ids
    assert connection.inbound_bytes == 0
    assert connection.queued_output_bytes == 0
    assert server._global_queued_requests == 0
    assert server._global_outstanding_requests == 0
    assert server._global_inbound_bytes == 0
    assert server._global_output_bytes == 0


@pytest.mark.asyncio
async def test_peer_reset_during_final_response_write_completes_connection_cleanup(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class Transport:
        def __init__(self) -> None:
            self.abort_calls = 0

        def abort(self) -> None:
            self.abort_calls += 1

    class ResetOnResponseWriter:
        def __init__(self) -> None:
            self.transport = Transport()
            self.drain_calls = 0

        def write(self, _data: bytes) -> None:
            return None

        async def drain(self) -> None:
            self.drain_calls += 1
            if self.drain_calls == 2:
                raise ConnectionResetError

        def close(self) -> None:
            return None

        async def wait_closed(self) -> None:
            raise ConnectionResetError

    server = BrowserExtensionBrokerServer(19777, base_dir=_test_broker_base_dir())
    monkeypatch.setattr(server, "_verify_peer_uid", lambda _writer: True)
    reader = asyncio.StreamReader()
    reader.feed_data(
        encode_frame(
            request_frame(
                "enroll",
                "client.enroll",
                {"clientNonce": new_nonce()},
            ),
            max_size=8 * 1024,
        )
    )
    reader.feed_eof()
    writer = ResetOnResponseWriter()

    await server._handle_connection(reader, writer)  # type: ignore[arg-type]

    assert writer.drain_calls == 2
    assert writer.transport.abort_calls == 1
    assert server._pending_connections == 0
    assert server._credentials == {}
    assert server._clients == {}
    assert server._connections == {}


@pytest.mark.asyncio
async def test_failed_enrollment_response_never_publishes_slot_or_hangs_stop(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    server = BrowserExtensionBrokerServer(19777, base_dir=_test_broker_base_dir())
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
    client = BrokerClient(19777, _ignore_event, base_dir=_test_broker_base_dir(), auto_spawn=False)
    server_socket, client_socket = socket.socketpair()
    server_reader, server_writer = await asyncio.open_connection(sock=server_socket)
    client_reader, client_writer = await asyncio.open_connection(sock=client_socket)
    server_task = asyncio.create_task(server._handle_connection(server_reader, server_writer))
    try:
        with pytest.raises(EOFError):
            await client._authenticate(client_reader, client_writer)
        await asyncio.wait_for(server_task, 1.0)
        assert server._clients == {}
        assert server._credentials == {}
        monkeypatch.setattr(broker_server_module, "write_frame", original_write_frame)
        replacement = BrokerClient(19777, _ignore_event, base_dir=_test_broker_base_dir(), auto_spawn=False)
        replacement_server_task = await _connect_over_socketpair(server, replacement)
        await replacement.stop()
        await asyncio.wait_for(replacement_server_task, 1.0)
        await asyncio.wait_for(server.stop(), 1.0)
    finally:
        client_writer.close()
        await client_writer.wait_closed()


@pytest.mark.asyncio
async def test_failed_reconnect_response_leaves_old_client_current(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    server = BrowserExtensionBrokerServer(19777, base_dir=_test_broker_base_dir())
    relay = FakeRelay("extension-secret", 19777, server._handle_extension_event, server._handle_disconnect)
    server._relay = relay
    active = BrokerClient(19777, _ignore_event, base_dir=_test_broker_base_dir(), auto_spawn=False)
    active_server_task = await _connect_over_socketpair(server, active)
    relay.scoped_tabs = [{"tabId": 7}]
    await active.request("tabs.activate", {"tabId": 7})
    client_id = active._client_id
    assert client_id is not None
    active_connection = server._clients[client_id]

    replacement = BrokerClient(19777, _ignore_event, base_dir=_test_broker_base_dir(), auto_spawn=False)
    replacement._client_id = client_id
    replacement._recovery_secret = active._recovery_secret
    original_write_frame = broker_server_module.write_frame
    writes = 0

    async def fail_reconnect_response(*args: object, **kwargs: object) -> int:
        nonlocal writes
        writes += 1
        if writes == 2:
            raise BrokenPipeError
        return await original_write_frame(*args, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(broker_server_module, "write_frame", fail_reconnect_response)
    with pytest.raises(EOFError):
        await _connect_over_socketpair(server, replacement)

    assert server._clients[client_id] is active_connection
    assert not active_connection.closed
    assert server._leases[7].client_id == client_id
    assert not server._leases[7].draining
    monkeypatch.setattr(broker_server_module, "write_frame", original_write_frame)
    assert await active.request("tabs.activate", {"tabId": 7}) == {
        "op": "tabs.activate",
        "args": {"tabId": 7},
        "timeout": 30.0,
    }

    await replacement.stop()
    await active.stop()
    await asyncio.wait_for(active_server_task, 1.0)
    await server.stop()


@pytest.mark.asyncio
async def test_global_request_cap_includes_operator_connections(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(broker_server_module, "MAX_GLOBAL_REQUESTS", 1)
    server = BrowserExtensionBrokerServer(19777, base_dir=_test_broker_base_dir())
    server._relay = FakeRelay("extension-secret", 19777, server._handle_extension_event, server._handle_disconnect)
    first = BrokerClient(19777, _ignore_event, base_dir=_test_broker_base_dir(), auto_spawn=False, operator=True)
    second = BrokerClient(19777, _ignore_event, base_dir=_test_broker_base_dir(), auto_spawn=False, operator=True)
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


@pytest.mark.asyncio
async def test_global_output_cap_includes_operator_connections(monkeypatch: pytest.MonkeyPatch) -> None:
    size = len(encode_frame(event_frame("test", {"value": "x" * 32})))
    monkeypatch.setattr(broker_server_module, "MAX_GLOBAL_OUTPUT_BYTES", size)
    server = BrowserExtensionBrokerServer(19777, base_dir=_test_broker_base_dir())
    reader = asyncio.StreamReader()

    class Writer:
        def close(self) -> None:
            return None

        async def wait_closed(self) -> None:
            return None

    first = _ClientConnection("first", 1, reader, Writer(), operator=True)  # type: ignore[arg-type]
    second = _ClientConnection("second", 1, reader, Writer(), operator=True)  # type: ignore[arg-type]
    server._connections[id(first)] = first
    server._connections[id(second)] = second

    assert await server._reserve_output(first, size)
    first.output_queue.put_nowait((b"x" * size, None))
    assert await server._reserve_output(second, size)
    assert first.closed
    assert second.queued_output_bytes == size
    server._release_output(second, size)
    assert server._global_output_bytes == 0


@pytest.mark.asyncio
async def test_global_output_cap_evicts_largest_non_sender_before_enqueuing_sender(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class Writer:
        def __init__(self) -> None:
            self.writes: list[bytes] = []
            self.closed = False

        def write(self, data: bytes) -> None:
            self.writes.append(data)

        async def drain(self) -> None:
            return None

        def close(self) -> None:
            self.closed = True

        async def wait_closed(self) -> None:
            return None

    server = BrowserExtensionBrokerServer(19777, base_dir=_test_broker_base_dir())
    monkeypatch.setattr(broker_server_module, "MAX_CLIENT_OUTPUT_BYTES", 10)
    reader = asyncio.StreamReader()
    victim_writer = Writer()
    sender_writer = Writer()
    victim = _ClientConnection("victim", 1, reader, victim_writer)  # type: ignore[arg-type]
    sender = _ClientConnection("sender", 1, reader, sender_writer)  # type: ignore[arg-type]
    victim_bytes = 50
    frame = encode_frame(event_frame("healthy", {}))
    monkeypatch.setattr(broker_server_module, "MAX_GLOBAL_OUTPUT_BYTES", victim_bytes + len(frame) - 1)
    assert await server._reserve_output(victim, victim_bytes)
    victim.output_queue.put_nowait((b"v" * victim_bytes, None))
    server._clients[victim.client_id] = victim
    server._connections[id(victim)] = victim
    sender.sender_task = asyncio.create_task(server._event_writer(sender))
    server._clients[sender.client_id] = sender
    server._connections[id(sender)] = sender

    await server._send_event(sender, "healthy", {})
    await _eventually(lambda: len(sender_writer.writes) == 1)

    assert victim.closed
    assert victim_writer.closed
    assert victim.client_id not in server._clients
    assert id(victim) not in server._connections
    assert not sender.closed
    assert sender_writer.writes == [frame]
    assert sender.queued_output_bytes == 0
    assert server._global_output_bytes == 0

    await server._connection_closed(sender)


@pytest.mark.asyncio
async def test_closed_output_holder_releases_budget_before_writer_close(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class Transport:
        def __init__(self) -> None:
            self.abort_calls = 0

        def abort(self) -> None:
            self.abort_calls += 1

    class FailingWriter:
        def __init__(self) -> None:
            self.transport = Transport()
            self.wait_closed_started = asyncio.Event()

        def write(self, _data: bytes) -> None:
            return None

        async def drain(self) -> None:
            raise ConnectionResetError

        def close(self) -> None:
            return None

        async def wait_closed(self) -> None:
            self.wait_closed_started.set()
            await asyncio.Future()

    class HealthyWriter:
        def __init__(self) -> None:
            self.started = asyncio.Event()
            self.release = asyncio.Event()
            self.writes: list[bytes] = []
            self.closed = False

        def write(self, data: bytes) -> None:
            self.writes.append(data)

        async def drain(self) -> None:
            self.started.set()
            await self.release.wait()

        def close(self) -> None:
            self.closed = True

        async def wait_closed(self) -> None:
            return None

    server = BrowserExtensionBrokerServer(19777, base_dir=_test_broker_base_dir())
    server.client_close_grace_seconds = 0.02
    reader = asyncio.StreamReader()
    failing_writer = FailingWriter()
    failing = _ClientConnection("failing", 1, reader, failing_writer)  # type: ignore[arg-type]
    first_frame = encode_frame(event_frame("first", {}))
    backlog = b"b" * 8192
    healthy_frame = encode_frame(event_frame("healthy", {"payload": "h" * 4096}))
    monkeypatch.setattr(
        broker_server_module,
        "MAX_GLOBAL_OUTPUT_BYTES",
        len(backlog) + len(healthy_frame) - 1,
    )
    assert await server._reserve_output(failing, len(first_frame))
    failing.output_queue.put_nowait((first_frame, None))
    assert await server._reserve_output(failing, len(backlog))
    failing.output_queue.put_nowait((backlog, None))
    failing.sender_task = asyncio.create_task(server._event_writer(failing))
    server._connections[id(failing)] = failing

    await asyncio.wait_for(failing_writer.wait_closed_started.wait(), 1.0)
    assert failing.closed
    assert failing.queued_output_bytes == 0
    assert server._global_output_bytes == 0

    healthy_writer = HealthyWriter()
    healthy = _ClientConnection("healthy", 1, reader, healthy_writer)  # type: ignore[arg-type]
    healthy.sender_task = asyncio.create_task(server._event_writer(healthy))
    server._connections[id(healthy)] = healthy
    await server._send_event(healthy, "healthy", {"payload": "h" * 4096})
    await asyncio.wait_for(healthy_writer.started.wait(), 1.0)
    assert not healthy.closed
    assert healthy.queued_output_bytes == len(healthy_frame)
    assert healthy.output_in_flight_bytes == len(healthy_frame)
    assert server._global_output_bytes == len(healthy_frame)

    server._mark_connection_closed(healthy)
    assert healthy.queued_output_bytes == 0
    assert healthy.output_in_flight_bytes == 0
    assert server._global_output_bytes == 0

    healthy_writer.release.set()
    await asyncio.wait_for(healthy.sender_task, 1.0)
    assert healthy.queued_output_bytes == 0
    assert healthy.output_in_flight_bytes == 0
    assert server._global_output_bytes == 0
    await server._connection_closed(healthy)
    await asyncio.wait_for(failing.sender_task, 1.0)
    await server._connection_closed(failing)
    assert healthy.queued_output_bytes == 0
    assert failing.queued_output_bytes == 0
    assert server._global_output_bytes == 0


@pytest.mark.asyncio
async def test_post_reserve_closed_connection_releases_output_without_enqueueing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    server = BrowserExtensionBrokerServer(19777, base_dir=_test_broker_base_dir())
    real_reserve_output = server._reserve_output

    async def reserve_then_close(connection: _ClientConnection, size: int) -> bool:
        await asyncio.sleep(0)
        reserved = await real_reserve_output(connection, size)
        server._mark_connection_closed(connection)
        return reserved

    monkeypatch.setattr(server, "_reserve_output", reserve_then_close)
    reader = asyncio.StreamReader()
    event_connection = _ClientConnection("event", 1, reader, MagicMock())  # type: ignore[arg-type]
    frame = event_frame("event", {})

    await server._send_event(event_connection, "event", {})

    assert event_connection.closed
    assert event_connection.queued_output_bytes == 0
    assert server._global_output_bytes == 0
    assert event_connection.output_queue.empty()

    send_connection = _ClientConnection("send", 1, reader, MagicMock())  # type: ignore[arg-type]
    send_connection.sender_task = asyncio.current_task()
    with pytest.raises(BrowserExtensionNotConnectedError, match="Broker client disconnected"):
        await asyncio.wait_for(server._send(send_connection, frame), 1.0)

    assert send_connection.closed
    assert send_connection.queued_output_bytes == 0
    assert server._global_output_bytes == 0
    assert send_connection.output_queue.empty()


@pytest.mark.asyncio
async def test_global_output_cap_closes_sender_when_sender_is_largest_holder(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class Writer:
        def __init__(self) -> None:
            self.closed = False

        def close(self) -> None:
            self.closed = True

        async def wait_closed(self) -> None:
            return None

    server = BrowserExtensionBrokerServer(19777, base_dir=_test_broker_base_dir())
    monkeypatch.setattr(broker_server_module, "MAX_CLIENT_OUTPUT_BYTES", 10)
    reader = asyncio.StreamReader()
    other = _ClientConnection("other", 1, reader, Writer())  # type: ignore[arg-type]
    sender_writer = Writer()
    sender = _ClientConnection("sender", 1, reader, sender_writer)  # type: ignore[arg-type]
    other_bytes = 20
    sender_bytes = 30
    frame = encode_frame(event_frame("overflow", {}))
    monkeypatch.setattr(broker_server_module, "MAX_GLOBAL_OUTPUT_BYTES", sender_bytes + other_bytes)
    assert await server._reserve_output(other, other_bytes)
    other.output_queue.put_nowait((b"o" * other_bytes, None))
    assert await server._reserve_output(sender, sender_bytes)
    sender.output_queue.put_nowait((b"s" * sender_bytes, None))
    server._clients[other.client_id] = other
    server._connections[id(other)] = other
    server._clients[sender.client_id] = sender
    server._connections[id(sender)] = sender

    await server._send_event(sender, "overflow", {})

    assert sender.closed
    assert sender_writer.closed
    assert sender.client_id not in server._clients
    assert id(sender) not in server._connections
    assert not other.closed
    assert other.queued_output_bytes == other_bytes
    assert server._global_output_bytes == other_bytes
    assert frame not in getattr(sender_writer, "writes", [])

    await server._connection_closed(other)
    assert server._global_output_bytes == 0


@pytest.mark.asyncio
async def test_client_output_budget_admits_one_operation_frame_and_backpressures_multiples() -> None:
    server = BrowserExtensionBrokerServer(19777, base_dir=_test_broker_base_dir())
    reader = asyncio.StreamReader()

    class Writer:
        def close(self) -> None:
            return None

    connection = _ClientConnection("client", 1, reader, Writer())  # type: ignore[arg-type]
    first_frame_size = MAX_CLIENT_OUTPUT_BYTES - 1
    second_frame_size = MAX_ENCODED_CONTROL_FRAME_BYTES

    assert MAX_CLIENT_OUTPUT_BYTES == 16 * 1024 * 1024
    assert CLIENT_OUTPUT_RECOVERY_BYTES == MAX_CLIENT_OUTPUT_BYTES // 2
    assert await server._reserve_output(connection, first_frame_size)
    assert await server._reserve_output(connection, second_frame_size)
    assert connection.pressure_since is not None
    assert connection.queued_output_bytes == first_frame_size + second_frame_size
    server._release_output(connection, second_frame_size)
    server._release_output(connection, first_frame_size)
    assert connection.queued_output_bytes == 0
    assert server._global_output_bytes == 0


@pytest.mark.asyncio
async def test_client_output_watchdog_allows_recent_write_progress(monkeypatch: pytest.MonkeyPatch) -> None:
    clock = [0.0]
    server = BrowserExtensionBrokerServer(
        19777,
        base_dir=_test_broker_base_dir(),
        time_source=lambda: clock[0],
    )
    reader = asyncio.StreamReader()

    class ProgressWriter:
        def __init__(self) -> None:
            self.started = asyncio.Event()
            self.completed = asyncio.Event()
            self.release = asyncio.Event()
            self.drain_calls = 0
            self.successful_writes = 0

        def write(self, _data: bytes) -> None:
            return None

        async def drain(self) -> None:
            self.drain_calls += 1
            self.started.set()
            await self.release.wait()
            self.release.clear()
            self.successful_writes += 1
            self.completed.set()

        def close(self) -> None:
            return None

        async def wait_closed(self) -> None:
            return None

    writer = ProgressWriter()
    connection = _ClientConnection(
        "progressing",
        1,
        reader,
        writer,  # type: ignore[arg-type]
        last_output_progress=-1.0,
    )
    encoded = encode_frame(event_frame("progress", {"value": "x"}))
    monkeypatch.setattr(broker_server_module, "MAX_CLIENT_OUTPUT_BYTES", len(encoded) * 2)
    monkeypatch.setattr(broker_server_module, "CLIENT_OUTPUT_RECOVERY_BYTES", len(encoded) // 2)
    for _ in range(5):
        assert await server._reserve_output(connection, len(encoded))
        connection.output_queue.put_nowait((encoded, None))
    server._connections[id(connection)] = connection
    connection.sender_task = asyncio.create_task(server._event_writer(connection))

    try:
        await asyncio.wait_for(writer.started.wait(), 1.0)
        for write_number, now in enumerate((0.0, 10.0, 20.0, 30.0), start=1):
            clock[0] = now
            writer.completed.clear()
            writer.release.set()
            await asyncio.wait_for(writer.completed.wait(), 1.0)
            for _ in range(10):
                if connection.last_output_progress == now:
                    break
                await asyncio.sleep(0)
            assert connection.last_output_progress == now
            await server._check_output_stalls()
            assert not connection.closed
            assert writer.successful_writes == write_number
            if write_number < 4:
                for _ in range(10):
                    if writer.drain_calls >= write_number + 1:
                        break
                    await asyncio.sleep(0)
                assert writer.drain_calls >= write_number + 1

        writer.completed.clear()
        writer.release.set()
        await asyncio.wait_for(writer.completed.wait(), 1.0)
        for _ in range(10):
            if connection.queued_output_bytes == 0:
                break
            await asyncio.sleep(0)
        assert connection.queued_output_bytes == 0
    finally:
        await server._connection_closed(connection)


@pytest.mark.asyncio
async def test_client_close_aborts_stalled_writer_and_closes_clients_concurrently(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    reader = asyncio.StreamReader()

    class Transport:
        def __init__(self) -> None:
            self.abort_calls = 0

        def abort(self) -> None:
            self.abort_calls += 1

    class CloseTracker:
        def __init__(self) -> None:
            self.started_count = 0
            self.all_started = asyncio.Event()

        def entered_close(self) -> None:
            self.started_count += 1
            if self.started_count == 2:
                self.all_started.set()

    class ControlledWriter:
        def __init__(self, tracker: CloseTracker | None = None) -> None:
            self.transport = Transport()
            self.close_calls = 0
            self.wait_closed_calls = 0
            self.wait_closed_future = asyncio.get_running_loop().create_future()
            self.tracker = tracker

        def close(self) -> None:
            self.close_calls += 1
            if self.tracker is not None:
                self.tracker.entered_close()

        def wait_closed(self) -> asyncio.Future[None]:
            self.wait_closed_calls += 1
            return self.wait_closed_future

    server = BrowserExtensionBrokerServer(19777, base_dir=_test_broker_base_dir())
    server.client_close_grace_seconds = 0.0
    writer = ControlledWriter()
    connection = _ClientConnection("stalled", 1, reader, writer)  # type: ignore[arg-type]
    encoded = encode_frame(event_frame("queued", {}))
    assert await server._reserve_output(connection, len(encoded))
    connection.output_queue.put_nowait((encoded, None))
    server._connections[id(connection)] = connection

    await server._close_connection(connection)
    assert writer.close_calls == 1
    assert writer.wait_closed_calls == 1
    assert writer.wait_closed_future.cancelled()
    assert writer.transport.abort_calls == 1
    assert connection.queued_output_bytes == 0
    assert server._global_output_bytes == 0
    assert id(connection) not in server._connections

    tracker = CloseTracker()
    concurrent_server = BrowserExtensionBrokerServer(19777, base_dir=_test_broker_base_dir())
    concurrent_server.client_close_grace_seconds = 0.0
    concurrent_connections: list[tuple[_ClientConnection, ControlledWriter]] = []
    for client_id in ("stalled-one", "stalled-two"):
        concurrent_writer = ControlledWriter(tracker)
        concurrent_connection = _ClientConnection(
            client_id,
            1,
            reader,
            concurrent_writer,  # type: ignore[arg-type]
        )
        assert await concurrent_server._reserve_output(concurrent_connection, len(encoded))
        concurrent_connection.output_queue.put_nowait((encoded, None))
        concurrent_server._clients[client_id] = concurrent_connection
        concurrent_server._connections[id(concurrent_connection)] = concurrent_connection
        concurrent_connections.append((concurrent_connection, concurrent_writer))

    original_connection_closed = concurrent_server._connection_closed
    close_tasks: list[asyncio.Task[None]] = []

    async def tracked_connection_closed(connection: _ClientConnection) -> None:
        current_task = asyncio.current_task()
        assert current_task is not None
        close_tasks.append(current_task)
        await original_connection_closed(connection)

    monkeypatch.setattr(concurrent_server, "_connection_closed", tracked_connection_closed)
    stop_task = asyncio.create_task(concurrent_server.stop())
    await tracker.all_started.wait()
    assert len(close_tasks) == 2
    assert all(not task.done() for task in close_tasks)
    assert not stop_task.done()
    await stop_task
    for concurrent_connection, concurrent_writer in concurrent_connections:
        assert concurrent_writer.close_calls == 1
        assert concurrent_writer.wait_closed_calls == 1
        assert concurrent_writer.wait_closed_future.cancelled()
        assert concurrent_writer.transport.abort_calls == 1
        assert concurrent_connection.queued_output_bytes == 0
        assert concurrent_connection.client_id not in concurrent_server._clients
        assert id(concurrent_connection) not in concurrent_server._connections
    assert concurrent_server._global_output_bytes == 0


@pytest.mark.asyncio
async def test_client_close_does_not_wait_past_grace_for_uncancellable_request() -> None:
    reader = asyncio.StreamReader()
    cleanup_release = asyncio.Event()
    cancellation_started = asyncio.Event()

    class Writer:
        def __init__(self) -> None:
            self.close_calls = 0
            self.wait_closed_calls = 0

        def close(self) -> None:
            self.close_calls += 1

        async def wait_closed(self) -> None:
            self.wait_closed_calls += 1

    async def ignore_cancellation() -> None:
        try:
            await asyncio.Future()
        except asyncio.CancelledError:
            cancellation_started.set()
            await cleanup_release.wait()

    server = BrowserExtensionBrokerServer(19777, base_dir=_test_broker_base_dir())
    server.client_close_grace_seconds = 0.02
    writer = Writer()
    connection = _ClientConnection("slow-close", 1, reader, writer)  # type: ignore[arg-type]
    connection.queued_request_ids.add("queued-request")
    server._global_queued_requests = 1
    server._connections[id(connection)] = connection
    request_task = asyncio.create_task(ignore_cancellation())
    connection.request_tasks.add(request_task)

    try:
        await asyncio.wait_for(server._close_connection(connection), 0.5)
        assert cancellation_started.is_set()
        assert not request_task.done()
        assert writer.close_calls == 1
        assert writer.wait_closed_calls == 1
        assert not connection.queued_request_ids
        assert server._global_queued_requests == 0
    finally:
        cleanup_release.set()
        await asyncio.gather(request_task, return_exceptions=True)
        await server._close_connection(connection)
        await server.stop()


@pytest.mark.asyncio
async def test_cancelled_client_close_aborts_writer_and_removes_tracking() -> None:
    reader = asyncio.StreamReader()
    cleanup_release = asyncio.Event()
    cancellation_started = asyncio.Event()

    class Transport:
        def __init__(self) -> None:
            self.abort_calls = 0

        def abort(self) -> None:
            self.abort_calls += 1

    class Writer:
        def __init__(self) -> None:
            self.transport = Transport()
            self.close_calls = 0

        def close(self) -> None:
            self.close_calls += 1

        async def wait_closed(self) -> None:
            await asyncio.Future()

    async def ignore_cancellation() -> None:
        try:
            await asyncio.Future()
        except asyncio.CancelledError:
            cancellation_started.set()
            await cleanup_release.wait()

    server = BrowserExtensionBrokerServer(19777, base_dir=_test_broker_base_dir())
    writer = Writer()
    connection = _ClientConnection("cancelled-close", 1, reader, writer)  # type: ignore[arg-type]
    connection.queued_request_ids.add("queued-request")
    connection.outstanding_request_ids.add("queued-request")
    server._global_queued_requests = 1
    server._global_outstanding_requests = 1
    server._connections[id(connection)] = connection
    request_task = asyncio.create_task(ignore_cancellation())
    connection.request_tasks.add(request_task)

    close_task = asyncio.create_task(server._close_connection(connection))
    try:
        await asyncio.wait_for(cancellation_started.wait(), 1.0)
        close_task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await close_task
        assert writer.close_calls == 1
        assert writer.transport.abort_calls == 1
        assert id(connection) not in server._connections
        assert not connection.queued_request_ids
        assert not connection.outstanding_request_ids
        assert server._global_queued_requests == 0
        assert server._global_outstanding_requests == 0

        cleanup_release.set()
        await request_task
        await server._close_connection(connection)
        assert writer.close_calls == 1
        assert writer.transport.abort_calls == 1
    finally:
        cleanup_release.set()
        await asyncio.gather(request_task, return_exceptions=True)
        if not close_task.done():
            close_task.cancel()
            await asyncio.gather(close_task, return_exceptions=True)
        await server.stop()


@pytest.mark.asyncio
async def test_global_output_cap_closes_current_request_task_without_cancellation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class Transport:
        def __init__(self) -> None:
            self.abort_calls = 0

        def abort(self) -> None:
            self.abort_calls += 1

    class ControlledWriter:
        def __init__(self) -> None:
            self.transport = Transport()
            self.wait_closed_future = asyncio.get_running_loop().create_future()

        def close(self) -> None:
            return None

        def wait_closed(self) -> asyncio.Future[None]:
            return self.wait_closed_future

    async def hold_sender() -> None:
        await asyncio.Future()

    server = BrowserExtensionBrokerServer(19777, base_dir=_test_broker_base_dir())
    server.client_close_grace_seconds = 0.0
    reader = asyncio.StreamReader()
    writer = ControlledWriter()
    connection = _ClientConnection("overflow", 1, reader, writer)  # type: ignore[arg-type]
    sender_task = asyncio.create_task(hold_sender())
    connection.sender_task = sender_task
    server._clients[connection.client_id] = connection
    server._connections[id(connection)] = connection

    request_frame = {
        "v": 1,
        "type": "request",
        "id": "overflow-request",
        "op": "broker.status",
        "args": {},
    }
    request_size = len(encode_frame(request_frame))
    connection.request_ids.add("overflow-request")
    connection.inbound_bytes = request_size
    server._global_inbound_bytes = request_size
    server._global_requests = 1

    async def successful_dispatch(
        _connection: _ClientConnection,
        _op: str,
        _args: dict,
    ) -> dict:
        return {"ok": True}

    monkeypatch.setattr(server, "_dispatch", successful_dispatch)
    monkeypatch.setattr(broker_server_module, "MAX_GLOBAL_OUTPUT_BYTES", 0)

    request_task = asyncio.create_task(
        server._handle_charged_request(connection, "overflow-request", request_frame, request_size)
    )
    connection.request_tasks.add(request_task)
    request_task.add_done_callback(connection.request_tasks.discard)
    await request_task

    assert not request_task.cancelled()
    assert request_task.exception() is None
    assert writer.transport.abort_calls == 1
    assert connection.client_id not in server._clients
    assert id(connection) not in server._connections
    assert connection.request_ids == set()
    assert connection.inbound_bytes == 0
    assert server._global_inbound_bytes == 0
    assert server._global_requests == 0
    assert connection.queued_output_bytes == 0
    assert server._global_output_bytes == 0

    await server._connection_closed(connection)
    await server._close_connection(connection)
    assert writer.transport.abort_calls == 1


@pytest.mark.asyncio
async def test_client_output_pressure_has_hysteresis(monkeypatch: pytest.MonkeyPatch) -> None:
    logs: list[tuple[str, dict[str, object]]] = []

    def capture_log(event: str, **fields: object) -> None:
        logs.append((event, fields))

    monkeypatch.setattr(broker_server_module.LOG, "warning", capture_log)
    monkeypatch.setattr(broker_server_module.LOG, "info", capture_log)
    monkeypatch.setattr(broker_server_module, "MAX_CLIENT_OUTPUT_BYTES", 100)
    monkeypatch.setattr(broker_server_module, "CLIENT_OUTPUT_RECOVERY_BYTES", 50)
    server = BrowserExtensionBrokerServer(19777, base_dir=_test_broker_base_dir())
    connection = _ClientConnection("hysteresis", 1, asyncio.StreamReader(), MagicMock())  # type: ignore[arg-type]

    assert await server._reserve_output(connection, 101)
    for _ in range(5):
        server._release_output(connection, 2)
        assert connection.queued_output_bytes == 99
        assert await server._reserve_output(connection, 2)
        assert connection.queued_output_bytes == 101

    pressure_logs = [event for event, _fields in logs if event == "browser_extension_client_output_pressure"]
    recovered_logs = [event for event, _fields in logs if event == "browser_extension_client_output_recovered"]
    assert pressure_logs == ["browser_extension_client_output_pressure"]
    assert recovered_logs == []

    server._release_output(connection, 101)
    recovered_logs = [event for event, _fields in logs if event == "browser_extension_client_output_recovered"]
    assert recovered_logs == ["browser_extension_client_output_recovered"]
    assert server._global_output_bytes == 0


@pytest.mark.asyncio
async def test_client_output_pressure_sheds_events_preserves_order_and_recovers(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    logs: list[tuple[str, dict[str, object]]] = []

    def capture_log(event: str, **fields: object) -> None:
        logs.append((event, fields))

    monkeypatch.setattr(broker_server_module.LOG, "warning", capture_log)
    monkeypatch.setattr(broker_server_module.LOG, "info", capture_log)
    server = BrowserExtensionBrokerServer(19777, base_dir=_test_broker_base_dir())
    reader = asyncio.StreamReader()

    class BlockingWriter:
        def __init__(self) -> None:
            self.started = asyncio.Event()
            self.release = asyncio.Event()
            self.writes: list[bytes] = []
            self.closed = False

        def write(self, data: bytes) -> None:
            self.writes.append(data)

        async def drain(self) -> None:
            self.started.set()
            await self.release.wait()

        def close(self) -> None:
            self.closed = True

        async def wait_closed(self) -> None:
            return None

    writer = BlockingWriter()
    connection = _ClientConnection("client", 1, reader, writer)  # type: ignore[arg-type]
    connection.sender_task = asyncio.create_task(server._event_writer(connection))
    server._connections[id(connection)] = connection
    expected: list[str] = []
    expected_shed = 0
    threshold_crossing_index: int | None = None

    def event_params(index: int, method: str) -> dict[str, object]:
        return {
            "event": "debugger.event",
            "params": {"method": method, "params": {"index": index, "payload": "x" * (10 * 1024)}},
        }

    first_params = event_params(0, "Network.loadingFinished")
    expected.append("event:0")
    await server._send_event(connection, "extension.event", first_params)
    await asyncio.wait_for(writer.started.wait(), 1.0)

    response_task: asyncio.Task[None] | None = None
    for index in range(1, 2000):
        if index == 1000:
            expected.append("response")
            response_task = asyncio.create_task(
                server._send(
                    connection,
                    {
                        "v": 1,
                        "type": "response",
                        "id": "response-in-the-middle",
                        "ok": True,
                    },
                )
            )
            await asyncio.sleep(0)
            assert not response_task.done()

        method = "Network.dataReceived" if index % 4 == 0 else "Network.loadingFinished"
        params = event_params(index, method)
        was_under_pressure = connection.pressure_since is not None
        await server._send_event(connection, "extension.event", params)
        if was_under_pressure and method == "Network.dataReceived":
            expected_shed += 1
        else:
            expected.append(f"event:{index}")
            if was_under_pressure is False and connection.pressure_since is not None:
                threshold_crossing_index = index

    assert threshold_crossing_index is not None
    assert not connection.closed
    assert connection.shed_event_count == expected_shed
    assert any(event == "browser_extension_client_output_pressure" for event, _fields in logs)

    writer.release.set()
    if response_task is not None:
        await asyncio.wait_for(response_task, 2.0)
    await _eventually(lambda: connection.queued_output_bytes == 0 and len(writer.writes) == len(expected))

    actual: list[str] = []
    for encoded in writer.writes:
        frame = decode_frame(encoded[4:])
        if frame["type"] == "response":
            actual.append("response")
        else:
            frame_params = frame["params"]
            assert isinstance(frame_params, dict)
            debugger_params = frame_params["params"]
            assert isinstance(debugger_params, dict)
            actual.append(f"event:{debugger_params['params']['index']}")
    assert actual == expected
    assert f"event:{threshold_crossing_index}" in actual
    recovered = [fields for event, fields in logs if event == "browser_extension_client_output_recovered"]
    assert len(recovered) == 1
    assert recovered[0]["shed_event_count"] == expected_shed

    await server._connection_closed(connection)


@pytest.mark.asyncio
async def test_client_output_watchdog_global_cap_and_bounded_stop(monkeypatch: pytest.MonkeyPatch) -> None:
    logs: list[tuple[str, dict[str, object]]] = []

    def capture_log(event: str, **fields: object) -> None:
        logs.append((event, fields))

    monkeypatch.setattr(broker_server_module.LOG, "warning", capture_log)
    clock = [0.0]
    stalled_server = BrowserExtensionBrokerServer(
        19777,
        base_dir=_test_broker_base_dir(),
        time_source=lambda: clock[0],
    )
    reader = asyncio.StreamReader()

    class Writer:
        def __init__(self, *, block: bool = False) -> None:
            self.block = block
            self.closed = False

        def write(self, _data: bytes) -> None:
            return None

        async def drain(self) -> None:
            if self.block:
                await asyncio.Future()

        def close(self) -> None:
            self.closed = True

        async def wait_closed(self) -> None:
            return None

    stalled_connection = _ClientConnection(
        "stalled",
        1,
        reader,
        Writer(),  # type: ignore[arg-type]
        last_output_progress=0.0,
    )
    stalled_server._connections[id(stalled_connection)] = stalled_connection
    stalled_output_size = MAX_CLIENT_OUTPUT_BYTES + 1
    assert await stalled_server._reserve_output(stalled_connection, stalled_output_size)
    stalled_connection.output_queue.put_nowait((b"x" * stalled_output_size, None))
    assert stalled_connection.pressure_since == 0.0
    clock[0] = CLIENT_OUTPUT_STALL_SECONDS
    await stalled_server._check_output_stalls()
    assert stalled_connection.closed
    assert stalled_connection.queued_output_bytes == 0
    assert stalled_server._global_output_bytes == 0
    assert any(event == "browser_extension_client_output_stalled" for event, _fields in logs)

    valid_frame_server = BrowserExtensionBrokerServer(19777, base_dir=_test_broker_base_dir())
    valid_frame_connection = _ClientConnection("large", 1, reader, Writer())  # type: ignore[arg-type]
    assert await valid_frame_server._reserve_output(valid_frame_connection, MAX_ENCODED_OPERATION_FRAME_BYTES)
    valid_frame_server._release_output(valid_frame_connection, MAX_ENCODED_OPERATION_FRAME_BYTES)
    assert valid_frame_server._global_output_bytes == 0

    global_server = BrowserExtensionBrokerServer(19777, base_dir=_test_broker_base_dir())
    global_connection = _ClientConnection("global", 1, reader, Writer())  # type: ignore[arg-type]
    global_connection.sender_task = asyncio.create_task(global_server._event_writer(global_connection))
    global_server._connections[id(global_connection)] = global_connection
    cap_frame = encode_frame(event_frame("global-cap", {}))
    monkeypatch.setattr(broker_server_module, "MAX_GLOBAL_OUTPUT_BYTES", len(cap_frame) - 1)
    await global_server._send_event(global_connection, "global-cap", {})
    assert global_connection.closed
    assert global_connection.queued_output_bytes == 0
    assert global_server._global_output_bytes == 0
    assert any(event == "browser_extension_client_output_global_cap" for event, _fields in logs)

    monkeypatch.setattr(broker_server_module, "MAX_GLOBAL_OUTPUT_BYTES", 64 * 1024 * 1024)
    stop_server = BrowserExtensionBrokerServer(19777, base_dir=_test_broker_base_dir())
    stop_connection = _ClientConnection(
        "never-drains",
        1,
        reader,
        Writer(block=True),  # type: ignore[arg-type]
    )
    stop_connection.sender_task = asyncio.create_task(stop_server._event_writer(stop_connection))
    stop_server._clients[stop_connection.client_id] = stop_connection
    stop_server._connections[id(stop_connection)] = stop_connection
    started = asyncio.get_running_loop().time()
    await asyncio.wait_for(stop_server.stop(), 3.0)
    assert asyncio.get_running_loop().time() - started < 3.0
    assert stop_connection.queued_output_bytes == 0
    assert stop_server._global_output_bytes == 0


@pytest.mark.asyncio
async def test_completed_request_ids_are_released() -> None:
    server = BrowserExtensionBrokerServer(19777, base_dir=_test_broker_base_dir())
    server._relay = FakeRelay("extension-secret", 19777, server._handle_extension_event, server._handle_disconnect)
    client = BrokerClient(19777, _ignore_event, base_dir=_test_broker_base_dir(), auto_spawn=False)
    server_task = await _connect_over_socketpair(server, client)
    try:
        for _ in range(3):
            await client.broker_status()
        assert client._client_id is not None
        active = server._clients[client._client_id]
        await _eventually(lambda: not active.request_ids)
    finally:
        await client.stop()
        await asyncio.wait_for(server_task, 1.0)
        await server.stop()


@pytest.mark.asyncio
async def test_extension_hello_does_not_orphan_pending_pairing_nonce() -> None:
    server = BrowserExtensionBrokerServer(19777, base_dir=_test_broker_base_dir(), pairing_opener=lambda _url: True)
    relay = FakeRelay("extension-secret", 19777, server._handle_extension_event, server._handle_disconnect)
    server._relay = relay
    client = BrokerClient(19777, _ignore_event, base_dir=_test_broker_base_dir(), auto_spawn=False)
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
    *,
    auto_approve: bool = True,
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
    if auto_approve and not client._operator:
        await server._approve_client(client._client_id)
    return server_task


async def _tab_queue_setup(
    *,
    time_source: Callable[[], float] | None = None,
) -> tuple[BrowserExtensionBrokerServer, QueueRelay, BrokerClient, asyncio.Task[None], _ClientConnection]:
    server = BrowserExtensionBrokerServer(
        19777,
        base_dir=_test_broker_base_dir(),
        time_source=time_source,
    )
    relay = QueueRelay("extension-secret", 19777, server._handle_extension_event, server._handle_disconnect)
    server._relay = relay
    client = BrokerClient(19777, _ignore_event, base_dir=_test_broker_base_dir(), auto_spawn=False)
    server_task = await _connect_over_socketpair(server, client)
    relay.scoped_tabs = [{"tabId": 7}]
    client_id = client._client_id
    assert client_id is not None
    await server._grant_lease(7, client_id, origin="shared")
    return server, relay, client, server_task, server._clients[client_id]
