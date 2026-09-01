from __future__ import annotations

import asyncio
import hmac
import http.client
import json
import os
import socket
import stat
import subprocess
import sys
import threading
import time
from collections.abc import Awaitable, Callable
from contextlib import suppress
from pathlib import Path
from typing import Any

import psutil

from skyvern.browser_extension.auth import compute_broker_proof, compute_client_proof
from skyvern.browser_extension.broker_protocol import (
    BROKER_GENERATION,
    CONTROL_FRAME_LIMIT,
    OPERATION_FRAME_LIMIT,
    PREAUTH_FRAME_LIMIT,
    encode_frame,
    is_valid_nonce,
    new_nonce,
    peer_uid_from_transport,
    read_frame,
    request_frame,
    write_frame,
)
from skyvern.browser_extension.broker_server import (
    READY_FD_ENV,
    SPAWN_LOCK_FD_ENV,
    STARTER_PID_ENV,
    STARTER_PROCESS_START_ENV,
)
from skyvern.browser_extension.broker_state import (
    STARTUP_TIMEOUT_SECONDS,
    BrokerPaths,
    OwnerFileLock,
    broker_paths,
    clear_startup_failure,
    current_process_start_marker,
    ensure_run_directory,
    matching_startup_failure,
    prepare_startup_log,
    process_identity_matches,
    read_broker_state,
    read_readiness,
    record_startup_failure,
    resolve_control_endpoint,
    run_directory_identity,
    state_fingerprint,
    validate_run_directory,
)
from skyvern.browser_extension.errors import (
    BrowserExtensionBrokerError,
    BrowserExtensionNotConnectedError,
    ExtensionRequestError,
)


class BrokerClient:
    """Relay-compatible broker client; credentials remain process-memory-only per spec-v3.md lines 57-63."""

    def __init__(
        self,
        port: int,
        on_event: Callable[[str, dict], Awaitable[None]],
        on_disconnect: Callable[[], Awaitable[None]] | None = None,
        *,
        base_dir: Path | None = None,
        auto_spawn: bool = True,
        operator: bool = False,
    ) -> None:
        self.port = port
        self.bound_port = port
        self.paths = broker_paths(port, base_dir=base_dir)
        self._on_event = on_event
        self._on_disconnect = on_disconnect
        self._auto_spawn = auto_spawn
        self._operator = operator
        self._reader: asyncio.StreamReader | None = None
        self._writer: asyncio.StreamWriter | None = None
        self._reader_task: asyncio.Task[None] | None = None
        self._spawned_process: subprocess.Popen[bytes] | None = None
        self._spawned_process_lock = threading.Lock()
        self._write_lock = asyncio.Lock()
        self._start_lock = asyncio.Lock()
        self._pending: dict[str, asyncio.Future[dict[str, Any]]] = {}
        self._large_response_ids: set[str] = set()
        self._request_counter = 0
        self._client_id: str | None = None
        self._recovery_secret: str | None = None
        self._connection_generation = 0
        self._transport_generation = 0
        self._extension_connected = asyncio.Event()
        self._closed = False
        self.scoped_tabs: list[dict[str, Any]] = []

    @property
    def connected(self) -> bool:
        return self._extension_connected.is_set() and self._writer is not None and not self._writer.is_closing()

    @property
    def broker_connected(self) -> bool:
        return self._writer is not None and not self._writer.is_closing()

    async def start(self) -> None:
        async with self._start_lock:
            self._reap_spawned_process()
            if self.broker_connected:
                return
            if self._closed:
                raise BrowserExtensionBrokerError("CLIENT_CLOSED", "Broker client is closed")
            if os.name != "posix" or sys.platform == "win32":
                raise BrowserExtensionBrokerError("UNSUPPORTED_PLATFORM", "Browser-extension broker requires POSIX")
            if os.environ.get("SKYVERN_BROWSER_EXTENSION_TOKEN", "").strip():
                raise BrowserExtensionBrokerError(
                    "BROKER_ENV_SECRET_REJECTED",
                    "SKYVERN_BROWSER_EXTENSION_TOKEN is not allowed in broker mode",
                )
            try:
                await self._connect()
            except (FileNotFoundError, ConnectionRefusedError, OSError, BrowserExtensionNotConnectedError):
                if not self._auto_spawn:
                    raise BrowserExtensionNotConnectedError("Browser-extension broker is not running") from None
                await asyncio.to_thread(self._spawn_broker_process)
                await self._connect_with_retry()
            else:
                # A reachable daemon invalidates any cached startup failure (e.g. a
                # BROKER_ALREADY_RUNNING election race recorded by an older build).
                with suppress(Exception):
                    clear_startup_failure(self.paths)

    async def stop(self) -> None:
        self._closed = True
        reader_task = self._reader_task
        self._reader_task = None
        if reader_task is not None:
            reader_task.cancel()
            with suppress(asyncio.CancelledError):
                await reader_task
        writer = self._writer
        self._reader = None
        self._writer = None
        if writer is not None:
            writer.close()
            with suppress(Exception):
                await writer.wait_closed()
        self._disconnect_state()
        self._handoff_spawned_process_reaping()

    async def wait_connected(self, timeout: float) -> bool:
        await self.start()
        if self.connected:
            return True
        result = await self._control_request(
            "extension.wait_connected", {"timeout": min(max(timeout, 0.0), 45.0)}, timeout + 1
        )
        connected = result.get("connected") is True
        if connected:
            self._extension_connected.set()
        return connected

    async def request(self, op: str, args: dict, timeout: float = 30.0) -> dict:
        await self.start()
        result = await self._control_request(
            "extension.request",
            {"op": op, "args": args, "timeout": min(timeout, 30.0)},
            timeout + 1,
        )
        return result

    async def ensure_root_lease(self) -> dict[str, Any] | None:
        """Ensure this client owns at least one tab and return its snapshot.

        A multi-client broker grants a tab lease: it adopts a free user-shared tab or
        creates a scoped about:blank tab. An M1 exclusive broker does not know the
        operation; fall back to its behavior, where the filtered snapshot is already
        exclusive to this client.
        """
        if self.scoped_tabs:
            return dict(self.scoped_tabs[0])
        await self.start()
        try:
            result = await self._control_request("lease.acquire_default", {}, 20.0)
        except BrowserExtensionBrokerError as exc:
            if exc.code == "OP_NOT_ALLOWED":
                return None
            raise
        snapshot = _tab_snapshot(result.get("tab"))
        if snapshot is None:
            return None
        self.scoped_tabs = [tab for tab in self.scoped_tabs if tab["tabId"] != snapshot["tabId"]]
        self.scoped_tabs.append(snapshot)
        return dict(snapshot)

    async def list_scoped_tabs(self) -> list[dict[str, Any]]:
        """Return fresh snapshots for tabs leased to this client."""
        await self.start()
        result = await self._control_request("lease.list", {}, 5.0)
        raw_tabs = result.get("tabs")
        if not isinstance(raw_tabs, list):
            raise BrowserExtensionBrokerError("INVALID_FRAME", "Broker tab list is invalid")
        tabs = _tab_snapshots(raw_tabs)
        self.scoped_tabs = tabs
        return [dict(tab) for tab in tabs]

    async def release_tab(self, tab_id: int) -> None:
        """Release one leased tab according to the broker's authoritative origin."""
        await self.start()
        try:
            await self._control_request("lease.release", {"tabId": tab_id}, 5.0)
        except BrowserExtensionBrokerError as exc:
            if exc.code != "OP_NOT_ALLOWED":
                raise
            # M1 brokers predate lease origins and retain their exclusive-client behavior.
            await self.request("debugger.detach", {"tabId": tab_id}, timeout=2.0)
        self.scoped_tabs = [tab for tab in self.scoped_tabs if tab["tabId"] != tab_id]

    async def broker_status(self) -> dict[str, Any]:
        await self.start()
        return await self._control_request("broker.status", {}, 5.0)

    async def begin_pairing(self) -> dict[str, Any]:
        await self.start()
        return await self._control_request("pairing.begin", {}, 10.0)

    async def pairing_status(self) -> dict[str, Any]:
        await self.start()
        return await self._control_request("pairing.status", {}, 5.0)

    async def cancel_pairing(self) -> dict[str, Any]:
        await self.start()
        return await self._control_request("pairing.cancel", {}, 5.0)

    async def grant_workstation(self) -> dict[str, Any]:
        await self.start()
        return await self._control_request("workstation.grant", {}, 5.0)

    async def revoke_workstation(self, *, scope: str = "grant") -> dict[str, Any]:
        await self.start()
        args = {} if scope == "grant" else {"scope": scope}
        return await self._control_request("workstation.revoke", args, 5.0)

    async def stop_broker(self) -> dict[str, Any]:
        try:
            await self.start()
        except BrowserExtensionBrokerError as exc:
            state = read_broker_state(self.paths)
            if exc.code != "INCOMPATIBLE_BROKER" or state is None or state.brokerGeneration == BROKER_GENERATION:
                raise
            # Unlike status/grant/revoke, stop is explicitly destructive and doesn't need
            # to bring a replacement back up - terminate the unreachable daemon directly.
            await _terminate_incompatible_daemon(state.pid, state.processStart)
            return {"stopping": True}
        return await self._control_request("broker.stop", {}, 5.0)

    async def _connect_with_retry(self) -> None:
        deadline = asyncio.get_running_loop().time() + 2.0
        while True:
            try:
                await self._connect()
                return
            except (FileNotFoundError, ConnectionRefusedError, OSError, BrowserExtensionNotConnectedError):
                if asyncio.get_running_loop().time() >= deadline:
                    raise BrowserExtensionNotConnectedError(
                        "Browser-extension broker did not accept a connection"
                    ) from None
                await asyncio.sleep(0.05)

    async def _connect(self) -> None:
        may_reenroll = True
        while True:
            state = read_broker_state(self.paths)
            if state is None or state.lifecycle != "ready":
                raise BrowserExtensionNotConnectedError("Browser-extension broker is not running")
            if state.externalPort != self.port or state.protocolMin > 1 or state.protocolMax < 1:
                raise BrowserExtensionBrokerError(
                    "INCOMPATIBLE_BROKER", "Running browser-extension broker is incompatible"
                )
            if state.brokerGeneration != BROKER_GENERATION:
                if not self._auto_spawn:
                    # An operator-only client (status/stop/grant/revoke) never spawns a
                    # replacement, so it must not kill a daemon it can't bring back either.
                    raise BrowserExtensionBrokerError(
                        "INCOMPATIBLE_BROKER", "Running browser-extension broker is incompatible"
                    )
                # The auth proof is generation-scoped (see compute_client_proof below), so a
                # mismatched daemon can't be asked over the wire to stop itself - replace it
                # directly and let start()'s auto-spawn path start a compatible one.
                await _terminate_incompatible_daemon(state.pid, state.processStart)
                raise BrowserExtensionNotConnectedError(
                    "Browser-extension broker was running an incompatible generation and has been replaced"
                )
            if not process_identity_matches(state.pid, state.processStart):
                raise BrowserExtensionNotConnectedError("Browser-extension broker is not running")
            connection_paths = resolve_control_endpoint(self.paths, state.controlEndpoint)
            reader, writer = await _open_control_connection(connection_paths)
            reauthenticating = self._client_id is not None and self._recovery_secret is not None
            try:
                connection_generation = await self._authenticate(reader, writer)
            except BaseException as exc:
                writer.close()
                with suppress(Exception):
                    await writer.wait_closed()
                if (
                    may_reenroll
                    and reauthenticating
                    and isinstance(exc, BrowserExtensionBrokerError)
                    and exc.code == "UNKNOWN_CLIENT"
                ):
                    self._client_id = None
                    self._recovery_secret = None
                    may_reenroll = False
                    continue
                raise
            break

        self._reader = reader
        self._writer = writer
        self._connection_generation = connection_generation
        self._transport_generation += 1
        transport_generation = self._transport_generation
        self._reader_task = asyncio.create_task(self._read_loop(reader, writer, transport_generation))

    async def _authenticate(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> int:
        challenge, _size = await read_frame(reader, max_size=PREAUTH_FRAME_LIMIT, timeout=10.0)
        if (
            set(challenge) != {"v", "type", "event", "params"}
            or challenge.get("type") != "event"
            or challenge.get("event") != "auth.challenge"
        ):
            raise BrowserExtensionBrokerError("AUTH_FAILED", "Broker authentication failed")
        params = challenge.get("params")
        if not isinstance(params, dict) or set(params) != {"serverNonce", "brokerGeneration"}:
            raise BrowserExtensionBrokerError("AUTH_FAILED", "Broker authentication failed")
        server_nonce = params.get("serverNonce")
        generation = params.get("brokerGeneration")
        if server_nonce is None or not is_valid_nonce(server_nonce) or generation != BROKER_GENERATION:
            raise BrowserExtensionBrokerError("AUTH_FAILED", "Broker authentication failed")
        client_nonce = new_nonce()
        request_id = "auth-1"
        reauthenticating = self._client_id is not None and self._recovery_secret is not None
        args: dict[str, Any]
        if reauthenticating:
            client_id = self._client_id
            recovery_secret = self._recovery_secret
            if client_id is None or recovery_secret is None:
                raise BrowserExtensionBrokerError("AUTH_FAILED", "Broker authentication failed")
            op = "client.authenticate"
            args = {
                "clientId": client_id,
                "clientNonce": client_nonce,
                "proof": compute_client_proof(
                    recovery_secret,
                    server_nonce,
                    client_nonce,
                    client_id,
                    BROKER_GENERATION,
                ),
            }
        else:
            op = "client.enroll"
            args = {"clientNonce": client_nonce}
            if self._operator:
                args["operator"] = True
        await write_frame(writer, request_frame(request_id, op, args), max_size=PREAUTH_FRAME_LIMIT)
        response, _size = await read_frame(reader, max_size=PREAUTH_FRAME_LIMIT, timeout=10.0)
        expected_response_keys = (
            {"v", "type", "id", "ok", "result"}
            if response.get("ok") is True
            else {
                "v",
                "type",
                "id",
                "ok",
                "error",
            }
        )
        if set(response) != expected_response_keys:
            raise BrowserExtensionBrokerError("AUTH_FAILED", "Broker authentication failed")
        result = _parse_response(response, request_id)
        expected_keys = (
            {"clientId", "connectionGeneration", "brokerGeneration", "brokerProof"}
            if reauthenticating
            else {"clientId", "recoverySecret", "connectionGeneration", "brokerGeneration", "brokerProof"}
        )
        if set(result) != expected_keys:
            raise BrowserExtensionBrokerError("AUTH_FAILED", "Broker authentication failed")
        client_id = result.get("clientId")
        recovery_secret = self._recovery_secret if reauthenticating else result.get("recoverySecret")
        connection_generation = result.get("connectionGeneration")
        broker_generation = result.get("brokerGeneration")
        broker_proof = result.get("brokerProof")
        if (
            not isinstance(client_id, str)
            or not isinstance(recovery_secret, str)
            or type(connection_generation) is not int
            or connection_generation <= 0
            or type(broker_generation) is not int
            or broker_generation != BROKER_GENERATION
            or not isinstance(broker_proof, str)
            or (reauthenticating and client_id != self._client_id)
        ):
            raise BrowserExtensionBrokerError("AUTH_FAILED", "Broker authentication failed")
        expected = compute_broker_proof(
            recovery_secret,
            client_nonce,
            server_nonce,
            client_id,
            BROKER_GENERATION,
        )
        if not secrets_compare(expected, broker_proof):
            raise BrowserExtensionBrokerError("AUTH_FAILED", "Broker authentication failed")
        self._client_id = client_id
        self._recovery_secret = recovery_secret
        return connection_generation

    async def _control_request(self, op: str, args: dict[str, Any], timeout: float) -> dict[str, Any]:
        writer = self._writer
        if writer is None or writer.is_closing():
            raise BrowserExtensionNotConnectedError("Browser-extension broker is not connected")
        self._request_counter += 1
        request_id = f"c-{self._request_counter}"
        future: asyncio.Future[dict[str, Any]] = asyncio.get_running_loop().create_future()
        self._pending[request_id] = future
        if op == "extension.request":
            self._large_response_ids.add(request_id)
        frame = request_frame(request_id, op, args)
        max_size = OPERATION_FRAME_LIMIT if op == "extension.request" else CONTROL_FRAME_LIMIT
        frame_written = False
        try:
            async with self._write_lock:
                encoded = encode_frame(frame, max_size=max_size)
                writer.write(encoded)
                frame_written = True
                await writer.drain()
        except BaseException as exc:
            self._pending.pop(request_id, None)
            if not frame_written or not isinstance(exc, asyncio.CancelledError):
                self._large_response_ids.discard(request_id)
            if not future.done():
                future.cancel()
            raise
        try:
            return await asyncio.wait_for(asyncio.shield(future), timeout)
        except TimeoutError as exc:
            self._pending.pop(request_id, None)
            if not future.done():
                future.cancel()
            raise ExtensionRequestError("INTERNAL", "Broker request timed out") from exc
        except asyncio.CancelledError:
            self._pending.pop(request_id, None)
            if not future.done():
                future.cancel()
            raise

    async def _read_loop(
        self,
        reader: asyncio.StreamReader,
        writer: asyncio.StreamWriter,
        transport_generation: int,
    ) -> None:
        try:
            while True:
                frame, _size = await read_frame(
                    reader,
                    max_size=OPERATION_FRAME_LIMIT,
                    control_size=CONTROL_FRAME_LIMIT,
                    large_response_ids=self._large_response_ids,
                    large_event="extension.event",
                )
                frame_type = frame.get("type")
                if frame_type == "response":
                    self._handle_response(frame)
                elif frame_type == "event":
                    await self._handle_event(frame, transport_generation)
                elif frame_type == "ping":
                    if transport_generation == self._transport_generation and not writer.is_closing():
                        async with self._write_lock:
                            await write_frame(
                                writer,
                                {"v": 1, "type": "pong"},
                                max_size=CONTROL_FRAME_LIMIT,
                            )
                elif frame_type == "pong":
                    continue
                else:
                    raise BrowserExtensionBrokerError("INVALID_FRAME", "Broker sent an invalid frame")
        except (EOFError, ConnectionError, BrokenPipeError, asyncio.CancelledError):
            pass
        except BrowserExtensionBrokerError:
            pass
        finally:
            if transport_generation == self._transport_generation:
                extension_was_connected = self._extension_connected.is_set()
                self._reader = None
                self._writer = None
                self._reader_task = None
                self._disconnect_state()
                if extension_was_connected and self._on_disconnect is not None:
                    with suppress(Exception):
                        await self._on_disconnect()
            writer.close()
            with suppress(Exception):
                await writer.wait_closed()
            self._handoff_spawned_process_reaping()

    def _handle_response(self, frame: dict[str, Any]) -> None:
        request_id = frame.get("id")
        if not isinstance(request_id, str):
            return
        future = self._pending.pop(request_id, None)
        self._large_response_ids.discard(request_id)
        if future is None or future.done():
            return
        try:
            result = _parse_response(frame, request_id)
        except (BrowserExtensionBrokerError, ExtensionRequestError) as exc:
            future.set_exception(exc)
        else:
            future.set_result(result)

    async def _handle_event(self, frame: dict[str, Any], transport_generation: int) -> None:
        if transport_generation != self._transport_generation:
            return
        event = frame.get("event")
        params = frame.get("params")
        if not isinstance(event, str) or not isinstance(params, dict):
            raise BrowserExtensionBrokerError("INVALID_FRAME", "Broker event is invalid")
        if event == "extension.connected":
            self._extension_connected.set()
            return
        if event == "extension.disconnected":
            self._extension_connected.clear()
            self.scoped_tabs = []
            if self._on_disconnect is not None:
                await self._on_disconnect()
            return
        if event == "extension.event":
            inner_event = params.get("event")
            inner_params = params.get("params")
            if not isinstance(inner_event, str) or not isinstance(inner_params, dict):
                raise BrowserExtensionBrokerError("INVALID_FRAME", "Broker extension event is invalid")
            self._update_scoped_tabs(inner_event, inner_params)
            await self._on_event(inner_event, inner_params)
            return
        if event == "broker.draining":
            extension_was_connected = self._extension_connected.is_set()
            self._extension_connected.clear()
            self.scoped_tabs = []
            if extension_was_connected and self._on_disconnect is not None:
                await self._on_disconnect()

    def _update_scoped_tabs(self, event: str, params: dict[str, Any]) -> None:
        if event == "extension.hello":
            tabs = params.get("scopedTabs")
            self.scoped_tabs = [] if not isinstance(tabs, list) else _tab_snapshots(tabs)
        elif event in {"scope.tabAdded", "tabs.created"}:
            snapshot = _tab_snapshot(params)
            if snapshot is not None:
                self.scoped_tabs = [tab for tab in self.scoped_tabs if tab["tabId"] != snapshot["tabId"]]
                self.scoped_tabs.append(snapshot)
        elif event == "scope.tabRemoved" and type(params.get("tabId")) is int:
            self.scoped_tabs = [tab for tab in self.scoped_tabs if tab["tabId"] != params["tabId"]]

    def _disconnect_state(self) -> None:
        self._extension_connected.clear()
        self.scoped_tabs = []
        pending = tuple(self._pending.values())
        self._pending.clear()
        self._large_response_ids.clear()
        for future in pending:
            if not future.done():
                future.set_exception(BrowserExtensionNotConnectedError("Browser-extension broker disconnected"))

    def _reap_spawned_process(self) -> None:
        with self._spawned_process_lock:
            process = self._spawned_process
            if process is not None and process.poll() is not None:
                self._spawned_process = None

    def _handoff_spawned_process_reaping(self) -> None:
        with self._spawned_process_lock:
            process = self._spawned_process
            self._spawned_process = None
        if process is None:
            return
        _arm_spawned_process_reaper(process)

    def _spawn_broker_process(self) -> None:
        process = _ensure_broker_process(self.port, self.paths)
        if process is None:
            return
        with self._spawned_process_lock:
            previous = self._spawned_process
            self._spawned_process = process
            closed = self._closed
        if previous is not None and previous is not process:
            _arm_spawned_process_reaper(previous)
        if closed:
            self._handoff_spawned_process_reaping()


def _arm_spawned_process_reaper(process: subprocess.Popen[bytes]) -> None:
    if process.poll() is not None:
        return
    threading.Thread(
        target=_wait_for_spawned_process,
        args=(process,),
        name="skyvern-extension-broker-reaper",
        daemon=True,
    ).start()


def _ensure_broker_process(port: int, paths: BrokerPaths) -> subprocess.Popen[bytes] | None:
    paths = ensure_run_directory(port, base_dir=paths.run_dir.parent, prepare_control_endpoint=False)
    identity = run_directory_identity(paths)
    spawn_lock = OwnerFileLock(paths.spawn_lock)
    with spawn_lock:
        validate_run_directory(paths, expected_identity=identity)
        if _broker_is_reachable(paths):
            clear_startup_failure(paths)
            return None
        if _recorded_daemon_is_dead(paths):
            # The recorded daemon died; a failure cached against its state is stale.
            clear_startup_failure(paths)
        observed = state_fingerprint(paths)
        cached = matching_startup_failure(paths, observed_state_fingerprint=observed)
        if cached is not None:
            raise BrowserExtensionBrokerError(
                cached.code,
                "Browser-extension broker startup is temporarily backed off",
                retry_after=max(0.0, cached.retryAfter - time.time()),
            )

        log_fd, log_thread = prepare_startup_log(paths)
        try:
            read_fd, write_fd = os.pipe()
        except BaseException:
            os.close(log_fd)
            log_thread.join(timeout=1.0)
            raise
        spawn_lock_fd = spawn_lock.fd
        if spawn_lock_fd is None:
            raise BrowserExtensionBrokerError("STARTUP_FAILED", "Broker lifecycle lock is not held")
        environment = _daemon_environment(write_fd, spawn_lock_fd)
        command = [
            sys.executable,
            "-m",
            "skyvern.browser_extension.broker_daemon",
            "--port",
            str(port),
        ]
        try:
            process = subprocess.Popen(
                command,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=log_fd,
                close_fds=True,
                pass_fds=(write_fd, spawn_lock_fd),
                cwd=str(_daemon_working_directory()),
                env=environment,
            )
        except BaseException as exc:
            os.close(read_fd)
            os.close(write_fd)
            os.close(log_fd)
            log_thread.join(timeout=1.0)
            if isinstance(exc, OSError):
                failure = record_startup_failure(
                    paths,
                    code="STARTUP_FAILED",
                    port=port,
                    observed_state_fingerprint=observed,
                )
                raise BrowserExtensionBrokerError(
                    failure.code,
                    "Browser-extension broker process could not be started",
                    retry_after=failure.retryAfter - time.time(),
                ) from exc
            raise
        else:
            spawn_lock.handoff_to_child()
            os.close(write_fd)
            os.close(log_fd)

        try:
            readiness = read_readiness(read_fd, timeout=STARTUP_TIMEOUT_SECONDS)
            if readiness["status"] == "READY" and readiness["port"] != port:
                raise BrowserExtensionBrokerError(
                    "INVALID_READINESS",
                    "Broker readiness response is invalid",
                )
        except BrowserExtensionBrokerError as exc:
            _terminate_spawned_process(process)
            log_thread.join(timeout=1.0)
            failed_observed = state_fingerprint(paths)
            failure = record_startup_failure(
                paths,
                code=exc.code,
                port=port,
                observed_state_fingerprint=failed_observed,
            )
            raise BrowserExtensionBrokerError(
                failure.code,
                exc.message,
                retry_after=failure.retryAfter - time.time(),
            ) from exc
        finally:
            os.close(read_fd)

        if readiness["status"] == "ERROR":
            _terminate_spawned_process(process)
            log_thread.join(timeout=1.0)
            code_value = readiness.get("code")
            code = code_value if isinstance(code_value, str) else "STARTUP_FAILED"
            if code == "BROKER_ALREADY_RUNNING":
                # Election loss, not a failure: another daemon holds daemon.lock. Give a
                # mid-startup winner a moment to publish readiness, then attach to it.
                deadline = time.monotonic() + 2.0
                while time.monotonic() < deadline:
                    if _broker_is_reachable(paths):
                        clear_startup_failure(paths)
                        return None
                    time.sleep(0.1)
                failure = record_startup_failure(
                    paths,
                    code="BROKER_UNRESPONSIVE",
                    port=port,
                    observed_state_fingerprint=state_fingerprint(paths),
                )
                raise BrowserExtensionBrokerError(
                    failure.code,
                    _unresponsive_broker_message(paths),
                    retry_after=failure.retryAfter - time.time(),
                )
            failed_observed = state_fingerprint(paths)
            failure = record_startup_failure(
                paths,
                code=code,
                port=port,
                observed_state_fingerprint=failed_observed,
            )
            message = _startup_error_message(code)
            if code == "PORT_IN_USE":
                message = f"{message}: {_describe_port_owner(port)}"
            raise BrowserExtensionBrokerError(
                failure.code,
                message,
                retry_after=failure.retryAfter - time.time(),
            )
        log_thread.join(timeout=1.0)
        clear_startup_failure(paths)
        return process


def _recorded_daemon_is_dead(paths: BrokerPaths) -> bool:
    try:
        state = read_broker_state(paths)
    except BrowserExtensionBrokerError:
        return False
    if state is None or state.lifecycle != "ready":
        return False
    return not process_identity_matches(state.pid, state.processStart)


def _unresponsive_broker_message(paths: BrokerPaths) -> str:
    pid_hint = ""
    try:
        state = read_broker_state(paths)
    except BrowserExtensionBrokerError:
        state = None
    if state is not None and process_identity_matches(state.pid, state.processStart):
        pid_hint = f" (pid {state.pid})"
    return (
        f"A browser-extension broker process{pid_hint} holds the daemon lock but is not "
        "accepting control connections; run 'skyvern browser extension-broker-stop' or stop "
        "that process, then retry"
    )


def _describe_port_owner(port: int) -> str:
    owner = _probe_relay_health(port)
    if owner == "relay":
        return (
            f"port {port} is served by a live Skyvern extension relay that is not this broker "
            "(a legacy embedded relay from another MCP session, or a broker from another checkout); "
            "stop that session or set SKYVERN_BROWSER_EXTENSION_PORT to a free port"
        )
    return f"port {port} is owned by a non-Skyvern process; free it or set SKYVERN_BROWSER_EXTENSION_PORT"


def _probe_relay_health(port: int) -> str:
    """Classify the loopback port owner: 'relay' (Skyvern extension relay), 'foreign', or 'none'."""
    try:
        connection = http.client.HTTPConnection("127.0.0.1", port, timeout=0.5)
        try:
            connection.request("GET", "/health")
            response = connection.getresponse()
            payload = response.read(4096)
            status = response.status
        finally:
            connection.close()
    except Exception:
        return "none"
    if status != 200:
        return "foreign"
    try:
        parsed = json.loads(payload)
    except ValueError:
        return "foreign"
    if isinstance(parsed, dict) and parsed.get("service") == "skyvern-browser-extension-relay":
        return "relay"
    return "foreign"


def _daemon_environment(ready_fd: int, spawn_lock_fd: int) -> dict[str, str]:
    inherited_names = (
        "HOME",
        "USER",
        "LOGNAME",
        "PATH",
        "LANG",
        "LC_ALL",
        "LC_CTYPE",
        "TMPDIR",
        "TMP",
        "TEMP",
        "SYSTEMROOT",
        "DISPLAY",
        "WAYLAND_DISPLAY",
        "XAUTHORITY",
        "XDG_RUNTIME_DIR",
    )
    environment = {name: os.environ[name] for name in inherited_names if name in os.environ}
    environment["SKYVERN_BROWSER_EXTENSION_BROKER"] = "1"
    environment[READY_FD_ENV] = str(ready_fd)
    environment[SPAWN_LOCK_FD_ENV] = str(spawn_lock_fd)
    environment[STARTER_PID_ENV] = str(os.getpid())
    environment[STARTER_PROCESS_START_ENV] = current_process_start_marker()
    return environment


def _daemon_working_directory() -> Path:
    return Path(__file__).resolve().parents[2]


def _wait_for_spawned_process(process: subprocess.Popen[bytes]) -> None:
    with suppress(Exception):
        process.wait()


def _terminate_spawned_process(process: subprocess.Popen[bytes]) -> None:
    if process.poll() is not None:
        return
    with suppress(ProcessLookupError):
        process.terminate()
    try:
        process.wait(timeout=2.0)
        return
    except subprocess.TimeoutExpired:
        pass
    with suppress(ProcessLookupError):
        process.kill()
    with suppress(subprocess.TimeoutExpired):
        process.wait(timeout=2.0)


async def _terminate_incompatible_daemon(pid: int, process_start: str) -> None:
    """Terminate a daemon this client did not spawn (no Popen handle to wait on),
    verifying identity via the same pid+start-time marker liveness checks already
    used to detect a crashed daemon elsewhere in this module."""
    if not process_identity_matches(pid, process_start):
        return
    loop = asyncio.get_running_loop()
    for method_name in ("terminate", "kill"):
        with suppress(psutil.Error):
            getattr(psutil.Process(pid), method_name)()
        deadline = loop.time() + 2.0
        while loop.time() < deadline:
            if not process_identity_matches(pid, process_start):
                return
            await asyncio.sleep(0.05)


def _broker_is_reachable(paths: BrokerPaths) -> bool:
    try:
        state = read_broker_state(paths)
        if state is None or state.lifecycle != "ready" or not process_identity_matches(state.pid, state.processStart):
            return False
        connection_paths = resolve_control_endpoint(paths, state.controlEndpoint)
        _validate_control_endpoint(connection_paths)
        probe = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        probe.settimeout(0.25)
        try:
            probe.connect(str(connection_paths.control_socket))
        finally:
            probe.close()
        return True
    except (OSError, BrowserExtensionBrokerError):
        return False


async def _open_control_connection(
    paths: BrokerPaths,
) -> tuple[asyncio.StreamReader, asyncio.StreamWriter]:
    identity = run_directory_identity(paths)
    _validate_control_endpoint(paths)
    reader, writer = await asyncio.open_unix_connection(
        str(paths.control_socket),
        limit=OPERATION_FRAME_LIMIT + 4,
    )
    try:
        transport_socket = writer.get_extra_info("socket")
        if (
            transport_socket is None
            or not hasattr(os, "getuid")
            or peer_uid_from_transport(transport_socket) != os.getuid()
        ):
            raise BrowserExtensionBrokerError("UNSAFE_PATH", "Broker control peer has the wrong owner")
        validate_run_directory(paths, expected_identity=identity)
        _validate_control_endpoint(paths)
    except BaseException:
        writer.close()
        with suppress(Exception):
            await writer.wait_closed()
        raise
    return reader, writer


def _validate_control_endpoint(paths: BrokerPaths) -> None:
    try:
        endpoint_stat = paths.control_socket.lstat()
    except OSError as exc:
        raise BrowserExtensionNotConnectedError("Broker control endpoint is unavailable") from exc
    if (
        not stat.S_ISSOCK(endpoint_stat.st_mode)
        or paths.control_socket.is_symlink()
        or stat.S_IMODE(endpoint_stat.st_mode) != 0o600
        or (hasattr(os, "getuid") and endpoint_stat.st_uid != os.getuid())
    ):
        raise BrowserExtensionBrokerError("UNSAFE_PATH", "Broker control endpoint is not owner-only")


def _parse_response(frame: dict[str, Any], request_id: str) -> dict[str, Any]:
    if frame.get("type") != "response" or frame.get("id") != request_id or type(frame.get("ok")) is not bool:
        raise BrowserExtensionBrokerError("INVALID_FRAME", "Broker response is invalid")
    if frame["ok"]:
        result = frame.get("result")
        if not isinstance(result, dict):
            raise BrowserExtensionBrokerError("INVALID_FRAME", "Broker response result is invalid")
        return result
    error = frame.get("error")
    if not isinstance(error, dict):
        raise BrowserExtensionBrokerError("INTERNAL", "Broker request failed")
    code = error.get("code")
    message = error.get("message")
    retry_after = error.get("retryAfter")
    if not isinstance(code, str) or not isinstance(message, str):
        raise BrowserExtensionBrokerError("INTERNAL", "Broker request failed")
    retry = float(retry_after) if isinstance(retry_after, (int, float)) and not isinstance(retry_after, bool) else None
    if error.get("errorType") == "extension":
        raise ExtensionRequestError(code, message)
    raise BrowserExtensionBrokerError(code, message, retry_after=retry)


def _tab_snapshots(tabs: list[object]) -> list[dict[str, Any]]:
    return [snapshot for tab in tabs if (snapshot := _tab_snapshot(tab)) is not None]


def _tab_snapshot(value: object) -> dict[str, Any] | None:
    if not isinstance(value, dict) or type(value.get("tabId")) is not int:
        return None
    snapshot: dict[str, Any] = {
        "tabId": value["tabId"],
        "url": value.get("url") if isinstance(value.get("url"), str) else "",
        "title": value.get("title") if isinstance(value.get("title"), str) else "",
    }
    if type(value.get("active")) is bool:
        snapshot["active"] = value["active"]
    return snapshot


def _startup_error_message(code: str) -> str:
    if code == "PORT_IN_USE":
        return "Browser-extension port is already in use; the broker will not steal or change it"
    if code == "BROKER_NOT_ENABLED":
        return "Browser-extension broker is not enabled"
    if code == "UNSAFE_STATE":
        return "Browser-extension broker state is unsafe"
    return "Browser-extension broker failed to start"


def secrets_compare(left: str, right: str) -> bool:
    try:
        return hmac.compare_digest(left, right)
    except TypeError:
        return False
