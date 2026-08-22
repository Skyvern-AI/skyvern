from __future__ import annotations

import asyncio
import os
import socket
import subprocess
import sys
import threading
from collections.abc import Awaitable, Callable
from contextlib import suppress
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

from skyvern.browser_extension import broker_client as broker_client_module
from skyvern.browser_extension.auth import compute_broker_proof
from skyvern.browser_extension.broker_client import BrokerClient
from skyvern.browser_extension.broker_protocol import (
    BROKER_GENERATION,
    PREAUTH_FRAME_LIMIT,
    event_frame,
    read_frame,
    response_frame,
    write_frame,
)
from skyvern.browser_extension.broker_server import BrowserExtensionBrokerServer
from skyvern.browser_extension.broker_state import ensure_run_directory
from skyvern.browser_extension.errors import (
    BrowserExtensionBrokerError,
    BrowserExtensionNotConnectedError,
    ExtensionRequestError,
)


class EventRelay:
    def __init__(
        self,
        _token: str,
        port: int,
        on_event: Callable[[str, dict], Awaitable[None]],
        on_disconnect: Callable[[], Awaitable[None]] | None,
    ) -> None:
        self.bound_port = port
        self.scoped_tabs: list[dict] = []
        self.connected = False
        self.on_event = on_event
        self.on_disconnect = on_disconnect
        self.pending_request_count = 0
        self.extension_protocol_version: int | None = 2
        self.extension_connection_generation = 1

    async def start(self) -> None:
        return None

    async def stop(self) -> None:
        return None

    async def wait_connected(self, _timeout: float) -> bool:
        return self.connected

    async def cycle_connection(self, _timeout: float) -> bool:
        self.connected = False
        self.scoped_tabs = []
        return True

    async def send_reset(self, epoch: str, generation: int) -> bool:
        self.scoped_tabs = []
        await self.on_event(
            "extension.reset_ack",
            {"epoch": epoch, "generation": generation, "ok": True, "failedTabCount": 0},
        )
        return self.connected

    async def hello(self) -> None:
        self.connected = True
        await self.on_event(
            "extension.hello",
            {"protocolVersion": 2, "extensionVersion": "test", "scopedTabs": list(self.scoped_tabs)},
        )

    async def wait_pending_requests(self, _timeout: float) -> bool:
        return True

    async def request(
        self,
        _op: str,
        _args: dict,
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
        return {"timeout": timeout}

    def get_or_create_pairing_nonce(self) -> str:
        return "nonce"

    def cancel_pairing_nonce(self) -> None:
        return None


class ErrorRelay(EventRelay):
    async def request(
        self,
        _op: str,
        _args: dict,
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
        raise ExtensionRequestError("TAB_NOT_SCOPED", "Tab is not shared")


class LargeResponseRelay(EventRelay):
    payload_size = 64 * 1024

    async def request(
        self,
        _op: str,
        _args: dict,
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
        return {"payload": "x" * self.payload_size}


class TwentyMiBResponseRelay(LargeResponseRelay):
    payload_size = 20 * 1024 * 1024


class CancelledLargeResponseRelay(EventRelay):
    def __init__(
        self,
        token: str,
        port: int,
        on_event: Callable[[str, dict], Awaitable[None]],
        on_disconnect: Callable[[], Awaitable[None]] | None,
    ) -> None:
        super().__init__(token, port, on_event, on_disconnect)
        self.large_started = asyncio.Event()
        self.release_large = asyncio.Event()
        self.other_started = asyncio.Event()
        self.release_other = asyncio.Event()

    async def request(
        self,
        op: str,
        _args: dict,
        timeout: float = 30.0,
        *,
        retain_until_terminal: bool = False,
        on_registered: Callable[[], None] | None = None,
        on_terminal: Callable[[], None] | None = None,
    ) -> dict:
        if on_registered is not None:
            on_registered()
        if op == "debugger.send":
            self.large_started.set()
            await self.release_large.wait()
            if on_terminal is not None:
                on_terminal()
            return {"payload": "x" * (64 * 1024)}
        self.other_started.set()
        await self.release_other.wait()
        if on_terminal is not None:
            on_terminal()
        return {"timeout": timeout}


@pytest.mark.asyncio
async def test_client_is_relay_compatible_and_fences_stale_disconnect() -> None:
    server = BrowserExtensionBrokerServer(19778)
    events: list[tuple[str, dict]] = []

    async def on_event(event: str, params: dict) -> None:
        events.append((event, params))

    relay = EventRelay("extension-secret", 19778, server._handle_extension_event, server._handle_disconnect)
    server._relay = relay
    client = BrokerClient(19778, on_event, auto_spawn=False)
    server_task = await _connect_over_socketpair(server, client)
    try:
        await _eventually(lambda: client.connected)
        assert client.scoped_tabs == []
        assert events[-1] == ("extension.hello", {"scopedTabs": []})

        relay.scoped_tabs = [{"tabId": 11, "url": "https://example.test", "title": "Example"}]
        hello_count = len(events)
        await relay.hello()
        await _eventually(lambda: len(events) > hello_count)
        assert events[-1] == ("extension.hello", {"scopedTabs": []})

        leased_tab = await client.ensure_root_lease()
        assert leased_tab == relay.scoped_tabs[0]
        assert client.scoped_tabs == [leased_tab]

        old_generation = client._transport_generation
        client._transport_generation += 1
        await client._handle_event(
            {"v": 1, "type": "event", "event": "extension.disconnected", "params": {}},
            old_generation,
        )
        assert client.connected
    finally:
        await client.stop()
        await asyncio.wait_for(server_task, 1.0)
        await server.stop()


@pytest.mark.asyncio
async def test_connected_is_published_only_after_synthetic_hello_snapshot() -> None:
    server = BrowserExtensionBrokerServer(19778)
    observed_tabs: list[list[dict[str, Any]]] = []

    async def on_event(event: str, _params: dict) -> None:
        if event == "extension.hello":
            assert not client.connected
            observed_tabs.append(list(client.scoped_tabs))

    relay = EventRelay("extension-secret", 19778, server._handle_extension_event, server._handle_disconnect)
    server._relay = relay
    await relay.hello()
    await _eventually(lambda: not server._extension_reset_quarantined)
    relay.scoped_tabs = [{"tabId": 11, "url": "https://example.test", "title": "Example"}]
    client = BrokerClient(19778, on_event, auto_spawn=False)
    server_task = await _connect_over_socketpair(server, client)
    try:
        await _eventually(lambda: client.connected)
        assert observed_tabs == [[]]
        assert client.scoped_tabs == []

        leased_tab = await client.ensure_root_lease()
        assert leased_tab == relay.scoped_tabs[0]
        assert client.scoped_tabs == [leased_tab]
    finally:
        await client.stop()
        await asyncio.wait_for(server_task, 1.0)
        await server.stop()


@pytest.mark.asyncio
async def test_client_preserves_extension_request_error_type() -> None:
    server = BrowserExtensionBrokerServer(19778)
    relay = ErrorRelay("extension-secret", 19778, server._handle_extension_event, server._handle_disconnect)
    server._relay = relay
    client = BrokerClient(19778, _ignore_event, auto_spawn=False)
    server_task = await _connect_over_socketpair(server, client)
    relay.scoped_tabs = [{"tabId": 11}]
    try:
        with pytest.raises(ExtensionRequestError) as error_info:
            await client.request("tabs.activate", {"tabId": 11})
        assert error_info.value.code == "TAB_NOT_SCOPED"
    finally:
        await client.stop()
        await asyncio.wait_for(server_task, 1.0)
        await server.stop()


@pytest.mark.asyncio
async def test_client_accepts_large_response_only_for_extension_request() -> None:
    server = BrowserExtensionBrokerServer(19778)
    relay = LargeResponseRelay(
        "extension-secret",
        19778,
        server._handle_extension_event,
        server._handle_disconnect,
    )
    server._relay = relay
    client = BrokerClient(19778, _ignore_event, auto_spawn=False)
    server_task = await _connect_over_socketpair(server, client)
    relay.scoped_tabs = [{"tabId": 11}]
    try:
        result = await client.request("debugger.send", {"tabId": 11})
        assert result == {"payload": "x" * (64 * 1024)}
    finally:
        await client.stop()
        await asyncio.wait_for(server_task, 1.0)
        await server.stop()


@pytest.mark.asyncio
async def test_client_delivers_twenty_mib_response_on_empty_output_queue() -> None:
    server = BrowserExtensionBrokerServer(19778)
    relay = TwentyMiBResponseRelay(
        "extension-secret",
        19778,
        server._handle_extension_event,
        server._handle_disconnect,
    )
    server._relay = relay
    client = BrokerClient(19778, _ignore_event, auto_spawn=False)
    server_task = await _connect_over_socketpair(server, client)
    relay.scoped_tabs = [{"tabId": 11}]
    try:
        result = await client.request("debugger.send", {"tabId": 11})

        assert len(result["payload"]) == 20 * 1024 * 1024
        assert client.broker_connected
    finally:
        await client.stop()
        await asyncio.wait_for(server_task, 1.0)
        await server.stop()


@pytest.mark.asyncio
async def test_cancelled_large_response_is_discarded_without_closing_transport() -> None:
    server = BrowserExtensionBrokerServer(19778)
    relay = CancelledLargeResponseRelay(
        "extension-secret",
        19778,
        server._handle_extension_event,
        server._handle_disconnect,
    )
    server._relay = relay
    client = BrokerClient(19778, _ignore_event, auto_spawn=False)
    server_task = await _connect_over_socketpair(server, client)
    relay.scoped_tabs = [{"tabId": 11}]
    large_request = asyncio.create_task(client.request("debugger.send", {"tabId": 11}))
    other_request: asyncio.Task[dict] | None = None
    try:
        await asyncio.wait_for(relay.large_started.wait(), 1.0)
        large_request.cancel()
        with pytest.raises(asyncio.CancelledError):
            await large_request
        assert client._large_response_ids == {"c-1"}

        other_request = asyncio.create_task(client.request("tabs.activate", {"tabId": 11}))
        await asyncio.wait_for(relay.other_started.wait(), 1.0)
        relay.release_large.set()
        await _eventually(lambda: "c-1" not in client._large_response_ids)

        assert client.broker_connected
        assert client._large_response_ids == {"c-2"}
        assert not other_request.done()
        relay.release_other.set()
        assert await other_request == {"timeout": 30.0}
        assert not client._large_response_ids
        assert client.broker_connected
    finally:
        relay.release_large.set()
        relay.release_other.set()
        for task in (large_request, other_request):
            if task is not None and not task.done():
                task.cancel()
                with suppress(asyncio.CancelledError):
                    await task
        await client.stop()
        await asyncio.wait_for(server_task, 1.0)
        await server.stop()


@pytest.mark.asyncio
async def test_cancellation_during_drain_keeps_late_large_response_authorized(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    server = BrowserExtensionBrokerServer(19778)
    relay = CancelledLargeResponseRelay(
        "extension-secret",
        19778,
        server._handle_extension_event,
        server._handle_disconnect,
    )
    server._relay = relay
    client = BrokerClient(19778, _ignore_event, auto_spawn=False)
    server_task = await _connect_over_socketpair(server, client)
    relay.scoped_tabs = [{"tabId": 11}]
    writer = client._writer
    assert writer is not None
    original_drain = writer.drain
    drain_started = asyncio.Event()
    block_next_drain = True

    async def controlled_drain() -> None:
        nonlocal block_next_drain
        if block_next_drain:
            block_next_drain = False
            drain_started.set()
            await asyncio.Event().wait()
        await original_drain()

    monkeypatch.setattr(writer, "drain", controlled_drain)
    large_request = asyncio.create_task(client.request("debugger.send", {"tabId": 11}))
    other_request: asyncio.Task[dict] | None = None
    try:
        await asyncio.wait_for(drain_started.wait(), 1.0)
        await asyncio.wait_for(relay.large_started.wait(), 1.0)
        large_request.cancel()
        with pytest.raises(asyncio.CancelledError):
            await large_request
        assert client._large_response_ids == {"c-1"}

        other_request = asyncio.create_task(client.request("tabs.activate", {"tabId": 11}))
        await asyncio.wait_for(relay.other_started.wait(), 1.0)
        relay.release_large.set()
        await _eventually(lambda: "c-1" not in client._large_response_ids)

        assert client.broker_connected
        assert client._large_response_ids == {"c-2"}
        assert not other_request.done()
        relay.release_other.set()
        assert await other_request == {"timeout": 30.0}
        assert client.broker_connected
    finally:
        relay.release_large.set()
        relay.release_other.set()
        for task in (large_request, other_request):
            if task is not None and not task.done():
                task.cancel()
                with suppress(asyncio.CancelledError):
                    await task
        await client.stop()
        await asyncio.wait_for(server_task, 1.0)
        await server.stop()


@pytest.mark.asyncio
@pytest.mark.parametrize("extra_field", ["recoverySecret", "unexpected"])
async def test_reauthentication_rejects_excess_response_fields(extra_field: str) -> None:
    client = BrokerClient(19778, _ignore_event, auto_spawn=False)
    client._client_id = "stored-client"
    client._recovery_secret = "stored-secret"

    def response(args: dict[str, Any], server_nonce: str) -> dict[str, Any]:
        client_nonce = args["clientNonce"]
        proof_secret = "attacker-secret" if extra_field == "recoverySecret" else "stored-secret"
        result: dict[str, Any] = {
            "clientId": "stored-client",
            "connectionGeneration": 2,
            "brokerGeneration": BROKER_GENERATION,
            "brokerProof": compute_broker_proof(
                proof_secret, client_nonce, server_nonce, "stored-client", BROKER_GENERATION
            ),
        }
        result[extra_field] = "attacker-secret"
        return result

    with pytest.raises(BrowserExtensionBrokerError, match="AUTH_FAILED"):
        await _authenticate_against_fake_broker(client, response)


@pytest.mark.asyncio
async def test_reauthentication_rejects_attacker_substituted_client_id() -> None:
    client = BrokerClient(19778, _ignore_event, auto_spawn=False)
    client._client_id = "stored-client"
    client._recovery_secret = "stored-secret"

    def response(args: dict[str, Any], server_nonce: str) -> dict[str, Any]:
        client_nonce = args["clientNonce"]
        return {
            "clientId": "attacker-client",
            "connectionGeneration": 2,
            "brokerGeneration": BROKER_GENERATION,
            "brokerProof": compute_broker_proof(
                "stored-secret", client_nonce, server_nonce, "attacker-client", BROKER_GENERATION
            ),
        }

    with pytest.raises(BrowserExtensionBrokerError, match="AUTH_FAILED"):
        await _authenticate_against_fake_broker(client, response)


@pytest.mark.asyncio
async def test_failed_reauthentication_never_falls_back_to_enrollment(monkeypatch: pytest.MonkeyPatch) -> None:
    client = BrokerClient(19778, _ignore_event, auto_spawn=False)
    client._client_id = "stored-client"
    client._recovery_secret = "stored-secret"

    class Writer:
        def close(self) -> None:
            return None

        async def wait_closed(self) -> None:
            return None

    open_connection = AsyncMock(return_value=(object(), Writer()))
    authenticate = AsyncMock(side_effect=BrowserExtensionBrokerError("AUTH_FAILED", "rejected"))
    monkeypatch.setattr(
        broker_client_module,
        "read_broker_state",
        lambda _paths: SimpleNamespace(
            lifecycle="ready",
            externalPort=19778,
            controlEndpoint=str(client.paths.control_socket),
            protocolMin=1,
            protocolMax=1,
            brokerGeneration=BROKER_GENERATION,
            pid=123,
            processStart="marker",
        ),
    )
    monkeypatch.setattr(broker_client_module, "process_identity_matches", lambda _pid, _marker: True)
    monkeypatch.setattr(broker_client_module, "_open_control_connection", open_connection)
    monkeypatch.setattr(client, "_authenticate", authenticate)

    with pytest.raises(BrowserExtensionBrokerError, match="AUTH_FAILED"):
        await client._connect()

    open_connection.assert_awaited_once_with(client.paths)
    authenticate.assert_awaited_once()
    assert client._client_id == "stored-client"
    assert client._recovery_secret == "stored-secret"


@pytest.mark.asyncio
async def test_stale_ready_state_with_dead_process_is_spawnable(monkeypatch: pytest.MonkeyPatch) -> None:
    client = BrokerClient(19778, _ignore_event)
    monkeypatch.setattr(
        broker_client_module,
        "read_broker_state",
        lambda _paths: SimpleNamespace(
            lifecycle="ready",
            externalPort=19778,
            controlEndpoint=str(client.paths.control_socket),
            protocolMin=1,
            protocolMax=1,
            brokerGeneration=BROKER_GENERATION,
            pid=999_999,
            processStart="dead-marker",
        ),
    )
    monkeypatch.setattr(broker_client_module, "process_identity_matches", lambda _pid, _marker: False)
    open_connection = AsyncMock(side_effect=AssertionError("dead state must not be connected"))
    monkeypatch.setattr(broker_client_module, "_open_control_connection", open_connection)

    with pytest.raises(BrowserExtensionNotConnectedError, match="not running"):
        await client._connect()

    open_connection.assert_not_awaited()


@pytest.mark.asyncio
async def test_client_retains_and_reaps_spawned_broker_process(monkeypatch: pytest.MonkeyPatch) -> None:
    class SpawnedProcess:
        returncode: int | None = None
        was_polled = False

        def poll(self) -> int | None:
            self.was_polled = True
            return self.returncode

    client = BrokerClient(19778, _ignore_event)
    process = SpawnedProcess()
    connect = AsyncMock(side_effect=[BrowserExtensionNotConnectedError("not running"), None])
    monkeypatch.setattr(client, "_connect", connect)
    monkeypatch.setattr(broker_client_module, "_ensure_broker_process", lambda _port, _paths: process)

    await client.start()

    assert client._spawned_process is process
    process.returncode = -9
    await client.stop()
    assert process.was_polled
    assert client._spawned_process is None


@pytest.mark.asyncio
async def test_client_stop_hands_live_spawned_process_to_daemon_waiter() -> None:
    wait_started = threading.Event()
    release_wait = threading.Event()
    wait_finished = threading.Event()
    waiter_was_daemon: list[bool] = []

    class SpawnedProcess:
        def poll(self) -> None:
            return None

        def wait(self) -> int:
            waiter_was_daemon.append(threading.current_thread().daemon)
            wait_started.set()
            release_wait.wait()
            wait_finished.set()
            return 0

    client = BrokerClient(19778, _ignore_event)
    client._spawned_process = SpawnedProcess()  # type: ignore[assignment]

    try:
        await client.stop()
        assert client._spawned_process is None
        assert await asyncio.to_thread(wait_started.wait, 1.0)
        assert waiter_was_daemon == [True]
    finally:
        release_wait.set()
        assert await asyncio.to_thread(wait_finished.wait, 1.0)


@pytest.mark.asyncio
async def test_cancelled_start_registers_late_spawn_for_reaping(monkeypatch: pytest.MonkeyPatch) -> None:
    spawn_started = threading.Event()
    release_spawn = threading.Event()
    wait_started = threading.Event()

    class SpawnedProcess:
        def poll(self) -> None:
            return None

        def wait(self) -> int:
            wait_started.set()
            return 0

    process = SpawnedProcess()

    def blocked_spawn(_port: int, _paths: object) -> object:
        spawn_started.set()
        release_spawn.wait()
        return process

    client = BrokerClient(19778, _ignore_event)
    monkeypatch.setattr(client, "_connect", AsyncMock(side_effect=BrowserExtensionNotConnectedError("not running")))
    monkeypatch.setattr(broker_client_module, "_ensure_broker_process", blocked_spawn)
    start_task = asyncio.create_task(client.start())
    try:
        assert await asyncio.to_thread(spawn_started.wait, 1.0)
        start_task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await start_task
        await client.stop()

        release_spawn.set()
        assert await asyncio.to_thread(wait_started.wait, 1.0)
        assert client._spawned_process is None
    finally:
        release_spawn.set()


def test_post_publication_startup_failure_is_backed_off(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    paths = ensure_run_directory(19778, base_dir=tmp_path / "run")
    process = MagicMock()
    popen = MagicMock(return_value=process)
    fingerprint = MagicMock(side_effect=["before-spawn", "published-by-child", "published-by-child"])
    monkeypatch.setattr(broker_client_module, "_broker_is_reachable", lambda _paths: False)
    monkeypatch.setattr(broker_client_module, "state_fingerprint", fingerprint)
    monkeypatch.setattr(broker_client_module.subprocess, "Popen", popen)
    monkeypatch.setattr(
        broker_client_module,
        "read_readiness",
        MagicMock(return_value={"status": "ERROR", "code": "STARTUP_FAILED"}),
    )
    monkeypatch.setattr(broker_client_module, "_terminate_spawned_process", lambda _process: None)

    with pytest.raises(BrowserExtensionBrokerError, match="STARTUP_FAILED"):
        broker_client_module._ensure_broker_process(19778, paths)
    with pytest.raises(BrowserExtensionBrokerError, match="temporarily backed off"):
        broker_client_module._ensure_broker_process(19778, paths)

    assert fingerprint.call_count == 3
    popen.assert_called_once()
    assert popen.call_args.args[0][:3] == [
        sys.executable,
        "-m",
        "skyvern.browser_extension.broker_daemon",
    ]


def test_first_spawn_leaves_extension_credentials_for_daemon_child(
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
    monkeypatch.setenv("HOME", str(home))
    paths = ensure_run_directory(19778, base_dir=tmp_path / "run")
    process = MagicMock()
    monkeypatch.setattr(broker_client_module, "_broker_is_reachable", lambda _paths: False)
    popen = MagicMock(return_value=process)
    monkeypatch.setattr(broker_client_module.subprocess, "Popen", popen)
    monkeypatch.setattr(
        broker_client_module,
        "read_readiness",
        MagicMock(return_value={"status": "READY", "port": 19778}),
    )

    spawned = broker_client_module._ensure_broker_process(19778, paths)

    assert spawned is process
    assert legacy_path.read_text() == "legacy-secret"
    assert not paths.extension_secret.exists()
    assert not paths.leases.exists()
    assert broker_client_module.SPAWN_LOCK_FD_ENV in popen.call_args.kwargs["env"]
    assert (
        int(popen.call_args.kwargs["env"][broker_client_module.SPAWN_LOCK_FD_ENV]) in popen.call_args.kwargs["pass_fds"]
    )


def test_spawn_rejects_ready_response_for_a_different_port(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    paths = ensure_run_directory(19778, base_dir=tmp_path / "run")
    process = MagicMock()
    monkeypatch.setattr(broker_client_module, "_broker_is_reachable", lambda _paths: False)
    monkeypatch.setattr(broker_client_module.subprocess, "Popen", MagicMock(return_value=process))
    monkeypatch.setattr(
        broker_client_module,
        "read_readiness",
        MagicMock(return_value={"status": "READY", "port": 19779}),
    )
    terminate = MagicMock()
    monkeypatch.setattr(broker_client_module, "_terminate_spawned_process", terminate)

    with pytest.raises(BrowserExtensionBrokerError, match="readiness response is invalid") as error_info:
        broker_client_module._ensure_broker_process(19778, paths)

    assert error_info.value.code == "INVALID_READINESS"
    terminate.assert_called_once_with(process)


def test_client_only_surfaces_daemon_auto_enable_failure(
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
    monkeypatch.setenv("HOME", str(home))
    paths = ensure_run_directory(19778, base_dir=tmp_path / "run")
    paths.leases.write_text("not-json")
    paths.leases.chmod(0o600)
    process = MagicMock()
    popen = MagicMock(return_value=process)
    monkeypatch.setattr(broker_client_module, "_broker_is_reachable", lambda _paths: False)
    monkeypatch.setattr(broker_client_module.subprocess, "Popen", popen)
    monkeypatch.setattr(
        broker_client_module,
        "read_readiness",
        MagicMock(return_value={"status": "ERROR", "code": "UNSAFE_STATE"}),
    )
    monkeypatch.setattr(broker_client_module, "_terminate_spawned_process", lambda _process: None)

    with pytest.raises(BrowserExtensionBrokerError, match="UNSAFE_STATE"):
        broker_client_module._ensure_broker_process(19778, paths)

    assert legacy_path.read_text() == "legacy-secret"
    assert not paths.extension_secret.exists()
    popen.assert_called_once()


def test_client_module_contains_no_extension_credential_access() -> None:
    source = Path(broker_client_module.__file__).read_text()

    assert "extension_secret" not in source
    assert "enable_broker_state" not in source


@pytest.mark.asyncio
@pytest.mark.parametrize("missing_field", ["clientId", "recoverySecret", "brokerGeneration", "brokerProof"])
async def test_enrollment_response_schema_is_exact(missing_field: str) -> None:
    client = BrokerClient(19778, _ignore_event, auto_spawn=False)

    def response(args: dict[str, Any], server_nonce: str) -> dict[str, Any]:
        client_nonce = args["clientNonce"]
        result: dict[str, Any] = {
            "clientId": "new-client",
            "recoverySecret": "new-secret",
            "connectionGeneration": 1,
            "brokerGeneration": BROKER_GENERATION,
            "brokerProof": compute_broker_proof(
                "new-secret", client_nonce, server_nonce, "new-client", BROKER_GENERATION
            ),
        }
        result.pop(missing_field)
        return result

    with pytest.raises(BrowserExtensionBrokerError, match="AUTH_FAILED"):
        await _authenticate_against_fake_broker(client, response)


@pytest.mark.asyncio
async def test_enrollment_response_rejects_excess_fields() -> None:
    client = BrokerClient(19778, _ignore_event, auto_spawn=False)

    def response(args: dict[str, Any], server_nonce: str) -> dict[str, Any]:
        client_nonce = args["clientNonce"]
        return {
            "clientId": "new-client",
            "recoverySecret": "new-secret",
            "connectionGeneration": 1,
            "brokerGeneration": BROKER_GENERATION,
            "brokerProof": compute_broker_proof(
                "new-secret", client_nonce, server_nonce, "new-client", BROKER_GENERATION
            ),
            "unexpected": True,
        }

    with pytest.raises(BrowserExtensionBrokerError, match="AUTH_FAILED"):
        await _authenticate_against_fake_broker(client, response)


def test_daemon_spawn_uses_sanitized_environment_and_trusted_working_directory(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    desktop_environment = {
        "DISPLAY": ":7",
        "WAYLAND_DISPLAY": "wayland-1",
        "XAUTHORITY": "/run/user/1000/xauthority",
        "XDG_RUNTIME_DIR": "/run/user/1000",
    }
    for name, value in desktop_environment.items():
        monkeypatch.setenv(name, value)
    monkeypatch.setenv("PYTHONPATH", "/attacker-controlled")
    monkeypatch.setenv("SKYVERN_API_KEY", "must-not-be-inherited")
    monkeypatch.setenv("SKYVERN_BROWSER_EXTENSION_BROKER", "1")

    environment = broker_client_module._daemon_environment(17, 18)

    assert environment["SKYVERN_BROWSER_EXTENSION_BROKER_READY_FD"] == "17"
    assert environment["SKYVERN_BROWSER_EXTENSION_BROKER_SPAWN_LOCK_FD"] == "18"
    assert environment["SKYVERN_BROWSER_EXTENSION_BROKER_STARTER_PID"] == str(os.getpid())
    assert environment["SKYVERN_BROWSER_EXTENSION_BROKER_STARTER_PROCESS_START"]
    assert environment["SKYVERN_BROWSER_EXTENSION_BROKER"] == "1"
    assert {name: environment[name] for name in desktop_environment} == desktop_environment
    assert "PYTHONPATH" not in environment
    assert "SKYVERN_API_KEY" not in environment
    assert broker_client_module._daemon_working_directory() == Path(broker_client_module.__file__).resolve().parents[2]

    for name in desktop_environment:
        monkeypatch.delenv(name)
    environment_without_desktop = broker_client_module._daemon_environment(17, 18)

    assert desktop_environment.keys().isdisjoint(environment_without_desktop)


def test_daemon_entrypoint_import_does_not_repopulate_sanitized_environment() -> None:
    environment = broker_client_module._daemon_environment(17, 18)
    script = """
import os
before = set(os.environ)
import skyvern.browser_extension.broker_daemon
added = set(os.environ) - before
sensitive = {
    name
    for name in added
    if name.endswith(("_KEY", "_TOKEN", "_SECRET")) or name == "DATABASE_STRING"
}
assert not sensitive
assert not added
"""

    subprocess.run(
        [sys.executable, "-c", script],
        check=True,
        cwd=broker_client_module._daemon_working_directory(),
        env=environment,
    )


@pytest.mark.asyncio
async def test_control_connection_rechecks_run_directory_after_socket_connect(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    paths = ensure_run_directory(19778, base_dir=tmp_path / "run")
    server_socket, client_socket = socket.socketpair()
    server_reader, server_writer = await asyncio.open_connection(sock=server_socket)
    client_reader, client_writer = await asyncio.open_connection(sock=client_socket)

    async def swap_during_connect(
        *_args: object, **_kwargs: object
    ) -> tuple[asyncio.StreamReader, asyncio.StreamWriter]:
        paths.run_dir.rename(paths.run_dir.parent / "replaced")
        paths.run_dir.mkdir(mode=0o700)
        return client_reader, client_writer

    monkeypatch.setattr(broker_client_module, "_validate_control_endpoint", lambda _paths: None)
    monkeypatch.setattr(broker_client_module.asyncio, "open_unix_connection", swap_during_connect)
    try:
        with pytest.raises(BrowserExtensionBrokerError, match="identity changed"):
            await broker_client_module._open_control_connection(paths)
    finally:
        server_writer.close()
        await server_writer.wait_closed()
        del server_reader


async def _eventually(predicate: Callable[[], bool]) -> None:
    for _ in range(100):
        if predicate():
            return
        await asyncio.sleep(0.01)
    raise AssertionError("condition did not become true")


async def _ignore_event(_event: str, _params: dict) -> None:
    return None


async def _authenticate_against_fake_broker(
    client: BrokerClient,
    response_builder: Callable[[dict[str, Any], str], dict[str, Any]],
) -> None:
    server_socket, client_socket = socket.socketpair()
    server_reader, server_writer = await asyncio.open_connection(sock=server_socket)
    client_reader, client_writer = await asyncio.open_connection(sock=client_socket)
    server_nonce = "A" * 43

    async def fake_broker() -> None:
        try:
            await write_frame(
                server_writer,
                event_frame("auth.challenge", {"serverNonce": server_nonce, "brokerGeneration": BROKER_GENERATION}),
                max_size=PREAUTH_FRAME_LIMIT,
            )
            request, _size = await read_frame(server_reader, max_size=PREAUTH_FRAME_LIMIT)
            await write_frame(
                server_writer,
                response_frame(request["id"], response_builder(request["args"], server_nonce)),
                max_size=PREAUTH_FRAME_LIMIT,
            )
        finally:
            server_writer.close()
            await server_writer.wait_closed()

    task = asyncio.create_task(fake_broker())
    try:
        await client._authenticate(client_reader, client_writer)
    finally:
        client_writer.close()
        await client_writer.wait_closed()
        await task


async def _connect_over_socketpair(
    server: BrowserExtensionBrokerServer,
    client: BrokerClient,
) -> asyncio.Task[None]:
    relay = server.relay
    if isinstance(relay, EventRelay) and not relay.connected:
        await relay.hello()
        await _eventually(lambda: not server._extension_reset_quarantined)
    server_socket, client_socket = socket.socketpair()
    server_reader, server_writer = await asyncio.open_connection(sock=server_socket)
    client_reader, client_writer = await asyncio.open_connection(sock=client_socket)
    server_task = asyncio.create_task(server._handle_connection(server_reader, server_writer))
    connection_generation = await client._authenticate(client_reader, client_writer)
    client._reader = client_reader
    client._writer = client_writer
    client._connection_generation = connection_generation
    client._transport_generation += 1
    client._reader_task = asyncio.create_task(
        client._read_loop(client_reader, client_writer, client._transport_generation)
    )
    await server._approve_client(client._client_id)
    return server_task
