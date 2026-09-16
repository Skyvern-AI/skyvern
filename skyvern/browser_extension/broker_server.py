from __future__ import annotations

import asyncio
import errno
import math
import os
import secrets
import stat
import time
from collections import deque
from collections.abc import Awaitable, Callable
from contextlib import suppress
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Protocol

import structlog

from skyvern.browser_extension.auth import compute_broker_proof, verify_client_proof
from skyvern.browser_extension.broker_protocol import (
    BROKER_GENERATION,
    BROKER_PROTOCOL_VERSION,
    CLIENT_CLOSE_GRACE_SECONDS,
    CLIENT_OUTPUT_RECOVERY_BYTES,
    CLIENT_OUTPUT_STALL_SECONDS,
    CONTROL_FRAME_LIMIT,
    MAX_AUTHENTICATED_CLIENTS,
    MAX_CLIENT_INBOUND_BYTES,
    MAX_CLIENT_OUTPUT_BYTES,
    MAX_GLOBAL_INBOUND_BYTES,
    MAX_GLOBAL_OUTPUT_BYTES,
    MAX_GLOBAL_OUTSTANDING_REQUESTS,
    MAX_GLOBAL_QUEUED_REQUESTS,
    MAX_GLOBAL_REQUESTS,
    MAX_OUTSTANDING_REQUESTS_PER_CLIENT,
    MAX_PENDING_CONNECTIONS,
    MAX_QUEUED_REQUESTS_PER_CLIENT,
    MAX_QUEUED_REQUESTS_PER_TAB,
    MAX_REQUESTS_PER_CLIENT,
    MAX_REQUESTS_PER_TAB,
    OPERATION_FRAME_LIMIT,
    PREAUTH_FRAME_LIMIT,
    READ_TIMEOUT_SECONDS,
    SHEDDABLE_EVENT_METHODS,
    TAB_REQUEST_QUEUE_WAIT_SECONDS,
    encode_frame,
    error_frame,
    event_frame,
    is_valid_nonce,
    new_nonce,
    parse_request,
    peer_uid_from_transport,
    read_frame,
    response_frame,
    write_frame,
)
from skyvern.browser_extension.broker_state import (
    BROKER_BUILD_FINGERPRINT,
    STATE_SCHEMA_VERSION,
    BrokerPaths,
    BrokerState,
    OwnerFileLock,
    broker_paths,
    clear_startup_failure,
    current_process_start_marker,
    enable_broker_state_locked,
    ensure_run_directory,
    process_identity_matches,
    publish_broker_state,
    read_broker_state,
    read_extension_secret,
    remove_control_socket,
    reset_lease_journal,
    run_directory_identity,
    validate_run_directory,
    write_lease_journal,
    write_readiness,
)
from skyvern.browser_extension.errors import (
    BrowserExtensionBrokerError,
    BrowserExtensionError,
    BrowserExtensionNotConnectedError,
    ExtensionRequestError,
)
from skyvern.browser_extension.package_extension import EXTENSION_DIR, compute_extension_source_hash
from skyvern.browser_extension.protocol import ALLOWED_OPS, LEGACY_PROTOCOL_VERSION, PROTOCOL_VERSION
from skyvern.browser_extension.relay import ExtensionRelayServer
from skyvern.browser_extension.workstation_grant import (
    WorkstationGrant,
    load_workstation_grant,
    remove_workstation_grant,
    workstation_grant_path,
    write_workstation_grant,
)

LOG = structlog.get_logger(__name__)
READY_FD_ENV = "SKYVERN_BROWSER_EXTENSION_BROKER_READY_FD"
STARTER_PID_ENV = "SKYVERN_BROWSER_EXTENSION_BROKER_STARTER_PID"
STARTER_PROCESS_START_ENV = "SKYVERN_BROWSER_EXTENSION_BROKER_STARTER_PROCESS_START"
SPAWN_LOCK_FD_ENV = "SKYVERN_BROWSER_EXTENSION_BROKER_SPAWN_LOCK_FD"
PAIRING_TTL_SECONDS = 120.0
CONTROL_PING_INTERVAL_SECONDS = 20.0
CONTROL_INBOUND_TIMEOUT_SECONDS = 45.0
EXTENSION_RESET_TIMEOUT_SECONDS = 5.0
MAX_PENDING_TAB_EVENT_TABS = 64
MAX_PENDING_TAB_EVENTS_PER_TAB = 16
TAB_REQUEST_EXACT_TIMEOUT_TOLERANCE_SECONDS = 0.25
_LEASED_OPS = frozenset(
    {"debugger.attach", "debugger.send", "debugger.detach", "dom.evaluate", "tabs.activate", "tabs.remove"}
)
_WORKSTATION_GRANT_OPS = frozenset({"workstation.grant", "workstation.revoke"})
_APPROVAL_SOURCE_INTERACTIVE = "interactive"
_APPROVAL_SOURCE_GRANT = "grant"
_BUILD_HASH_SHORT_LENGTH = 12
_OWNERSHIP_STATE_READ_ATTEMPTS = 3
_OWNERSHIP_STATE_READ_RETRY_SECONDS = 0.05


def _default_time_source() -> float:
    try:
        return asyncio.get_running_loop().time()
    except RuntimeError:
        return time.monotonic()


def _local_extension_build_hash() -> str:
    # Recomputed per status call (not cached): an editable-install daemon can outlive
    # edits to its own extension source, and status is not a hot path.
    return compute_extension_source_hash(EXTENSION_DIR)


class Relay(Protocol):
    bound_port: int
    scoped_tabs: list[dict[str, Any]]
    extension_protocol_version: int | None
    extension_build_hash: str | None
    extension_connection_generation: int

    @property
    def connected(self) -> bool: ...

    async def start(self) -> None: ...

    async def stop(self) -> None: ...

    async def wait_connected(self, timeout: float) -> bool: ...

    async def cycle_connection(self, timeout: float) -> bool: ...

    async def send_reset(self, epoch: str, generation: int) -> bool: ...
    async def send_event(self, event: str, params: dict[str, Any]) -> bool: ...

    @property
    def pending_request_count(self) -> int: ...

    async def wait_pending_requests(self, timeout: float) -> bool: ...

    async def request(
        self,
        op: str,
        args: dict[str, Any],
        timeout: float | None = 30.0,
        *,
        retain_until_terminal: bool = False,
        on_registered: Callable[[], None] | None = None,
        on_terminal: Callable[[], None] | None = None,
    ) -> dict[str, Any]: ...

    def get_or_create_pairing_nonce(self) -> str: ...

    def cancel_pairing_nonce(self) -> None: ...


RelayFactory = Callable[
    [
        str,
        int,
        Callable[[str, dict], Awaitable[None]],
        Callable[[], Awaitable[None]] | None,
        Callable[[], Awaitable[dict[str, str] | None]] | None,
    ],
    Relay,
]


@dataclass(slots=True)
class _Credential:
    client_id: str
    recovery_secret: str
    connection_generation: int = 0
    approved: bool = False
    approval_source: str | None = None
    approval_event: asyncio.Event = field(default_factory=asyncio.Event)


@dataclass(slots=True)
class _ClientConnection:
    client_id: str
    generation: int
    reader: asyncio.StreamReader
    writer: asyncio.StreamWriter
    operator: bool = False
    write_lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    request_ids: set[str] = field(default_factory=set)
    queued_request_ids: set[str] = field(default_factory=set)
    outstanding_request_ids: set[str] = field(default_factory=set)
    request_tasks: set[asyncio.Task[None]] = field(default_factory=set)
    inbound_bytes: int = 0
    queued_output_bytes: int = 0
    queued_output_frames: int = 0
    output_queue: asyncio.Queue[tuple[bytes, asyncio.Future[None] | None]] = field(default_factory=asyncio.Queue)
    output_in_flight_bytes: int = 0
    sender_task: asyncio.Task[None] | None = None
    keepalive_task: asyncio.Task[None] | None = None
    last_inbound: float = field(default_factory=time.monotonic)
    pressure_since: float | None = None
    # A connection created by the server is initialized with its injected clock at
    # authentication time. Zero also keeps direct test construction in the same clock domain.
    last_output_progress: float = 0.0
    shed_event_count: int = 0
    closed: bool = False
    close_started: bool = False
    ownership_released: bool = False


@dataclass(slots=True)
class _TabLease:
    tab_id: int
    client_id: str
    origin: str  # "shared" (user-scoped tab adopted by an agent) or "created" (broker-created)
    granted_at: float = field(default_factory=time.time)
    draining: bool = False

    def to_json(self) -> dict[str, Any]:
        return {
            "tabId": self.tab_id,
            "clientId": self.client_id,
            "origin": self.origin,
            "grantedAt": self.granted_at,
        }


@dataclass(slots=True)
class _QueuedTabRequest:
    connection: _ClientConnection
    future: asyncio.Future[None]
    error: BrowserExtensionError | None = None
    notified: bool = False
    slot_reserved: bool = False
    admitted: bool = False


class BrowserExtensionBrokerServer:
    """Multi-client broker: shared daemon, per-client tab leases (SKY-13757 spec-v3 milestone 2)."""

    def __init__(
        self,
        port: int,
        *,
        base_dir: Path | None = None,
        relay_factory: RelayFactory | None = None,
        pairing_opener: Callable[[str], bool] | None = None,
        time_source: Callable[[], float] | None = None,
    ) -> None:
        self.port = port
        self.paths = broker_paths(port, base_dir=base_dir)
        self._relay_factory = relay_factory
        self._pairing_opener = pairing_opener
        self._relay: Relay | None = None
        self._broker_auth_token: str | None = None
        self._workstation_grant: WorkstationGrant | None = None
        self._control_server: asyncio.AbstractServer | None = None
        self._daemon_lock: OwnerFileLock | None = None
        self._credentials: dict[str, _Credential] = {}
        self._clients: dict[str, _ClientConnection] = {}
        self._connections: dict[int, _ClientConnection] = {}
        self._client_lock = asyncio.Lock()
        self._leases: dict[int, _TabLease] = {}
        self._create_lock = asyncio.Lock()
        self._journal_lock = asyncio.Lock()
        self._pending_create_count = 0
        self._pending_tab_events: dict[int, list[tuple[str, dict]]] = {}
        self._cleanup_tasks: set[asyncio.Task[None]] = set()
        self._lease_drain_tasks: dict[int, asyncio.Task[None]] = {}
        self._extension_reset_error: str | None = None
        self._extension_supports_scope_origins = False
        self._extension_reset_quarantined = True
        self._extension_reset_epoch = secrets.token_hex(16)
        self._extension_reset_generation = 0
        self._extension_reset_ack_identity: tuple[str, int] | None = None
        self._extension_reset_failed_identity: tuple[str, int] | None = None
        self._extension_reset_event = asyncio.Event()
        self._reset_recovery_task: asyncio.Task[None] | None = None
        self._pending_connections = 0
        self._global_inbound_bytes = 0
        self._global_output_bytes = 0
        self._global_requests = 0
        self._global_queued_requests = 0
        self._global_outstanding_requests = 0
        self._tab_request_counts: dict[int, int] = {}
        self._tab_idle_events: dict[int, asyncio.Event] = {}
        self._tab_request_queues: dict[int, deque[_QueuedTabRequest]] = {}
        self._shutdown_event = asyncio.Event()
        self._stopping = False
        self.time_source: Callable[[], float] = time_source or _default_time_source
        self.client_close_grace_seconds = CLIENT_CLOSE_GRACE_SECONDS
        self._output_watchdog_task: asyncio.Task[None] | None = None
        self._boot_id = secrets.token_hex(16)
        self._process_start = current_process_start_marker()
        self._forwarded_tasks: set[asyncio.Task[dict[str, Any]]] = set()
        self._pairing_owner: str | None = None
        self._pairing_expires_at = 0.0
        self._pairing_approval_nonce: str | None = None
        self._approved_pairing_nonces: dict[str, float] = {}
        self._pairing_begins: deque[float] = deque()
        self._principal_pairing_begin: dict[str, float] = {}
        self._connection_tokens = 32.0
        self._connection_token_updated = time.monotonic()
        self._run_identity: tuple[int, int] | None = None

    @property
    def relay(self) -> Relay | None:
        return self._relay

    @property
    def running(self) -> bool:
        return self._control_server is not None and not self._stopping

    async def start(self) -> None:
        if self._control_server is not None:
            return
        if os.name != "posix":
            raise BrowserExtensionBrokerError("UNSUPPORTED_PLATFORM", "Browser-extension broker requires POSIX")

        self.paths = ensure_run_directory(self.port, base_dir=self.paths.run_dir.parent)
        self._run_identity = run_directory_identity(self.paths)
        daemon_lock = OwnerFileLock(self.paths.daemon_lock)
        if not daemon_lock.acquire(blocking=False):
            raise BrowserExtensionBrokerError("BROKER_ALREADY_RUNNING", "Browser-extension broker is already running")
        self._daemon_lock = daemon_lock
        try:
            extension_secret = read_extension_secret(self.paths)
            self._broker_auth_token = extension_secret
            self._workstation_grant = self._load_workstation_grant()
            stale_leases = reset_lease_journal(self.paths)
            if stale_leases:
                LOG.warning(
                    "browser_extension_broker_stale_leases_archived",
                    count=stale_leases,
                    archive=str(self.paths.leases_stale),
                )
            relay = self._make_relay(extension_secret)
            self._relay = relay
            try:
                await relay.start()
            except OSError as exc:
                if exc.errno == errno.EADDRINUSE:
                    raise BrowserExtensionBrokerError(
                        "PORT_IN_USE", "Browser-extension port is already in use"
                    ) from exc
                raise

            try:
                self._control_server = await asyncio.start_unix_server(
                    self._handle_connection,
                    path=str(self.paths.control_socket),
                    limit=OPERATION_FRAME_LIMIT + 4,
                    start_serving=False,
                )
                os.chmod(self.paths.control_socket, 0o600)
                self._validate_control_socket()
            except BaseException:
                await relay.stop()
                self._relay = None
                raise

            validate_run_directory(self.paths, expected_identity=self._run_identity)
            publish_broker_state(self.paths, self._state(lifecycle="ready", clean_shutdown=False))
            await self._control_server.start_serving()
            self._output_watchdog_task = asyncio.create_task(self._output_stall_watchdog())
            clear_startup_failure(self.paths)
            LOG.info("browser_extension_broker_started", port=self.port, generation=BROKER_GENERATION)
        except BaseException:
            await self._cleanup_partial_start()
            raise

    async def serve(self) -> None:
        await self.start()
        await self._shutdown_event.wait()
        await self.stop()

    async def request_stop(self) -> None:
        self._shutdown_event.set()

    async def stop(self) -> None:
        if self._stopping:
            return
        self._stopping = True
        self._fail_all_queued_tab_requests(
            BrowserExtensionBrokerError("BROKER_STOPPING", "Browser-extension broker is stopping")
        )
        output_watchdog_task = self._output_watchdog_task
        self._output_watchdog_task = None
        if output_watchdog_task is not None:
            output_watchdog_task.cancel()
            with suppress(asyncio.CancelledError):
                await output_watchdog_task
        reset_recovery_task = self._reset_recovery_task
        if reset_recovery_task is not None and not reset_recovery_task.done():
            reset_recovery_task.cancel()
            with suppress(asyncio.CancelledError):
                await reset_recovery_task
        control_server = self._control_server
        self._control_server = None
        if control_server is not None:
            control_server.close()
            await control_server.wait_closed()

        connections = {id(connection): connection for connection in self._connections.values()}
        connections.update((id(connection), connection) for connection in self._clients.values())
        clients: tuple[_ClientConnection, ...] = tuple(connections.values())
        draining_tasks = [
            asyncio.wait_for(self._send(client, event_frame("broker.draining", {})), 2.0)
            for client in clients
            if client.sender_task is not None and not client.closed
        ]
        if draining_tasks:
            await asyncio.gather(*draining_tasks, return_exceptions=True)

        for task in tuple(self._cleanup_tasks):
            task.cancel()
            with suppress(asyncio.CancelledError, Exception):
                await task

        clean_shutdown = True
        if self._forwarded_tasks:
            _done, pending = await asyncio.wait(self._forwarded_tasks, timeout=30.0)
            clean_shutdown = not pending
            for forwarded_task in pending:
                forwarded_task.cancel()

        relay = self._relay
        if relay is not None and not await relay.wait_pending_requests(30.0):
            clean_shutdown = False

        if clients:
            await asyncio.gather(
                *(self._connection_closed(client) for client in clients),
                return_exceptions=True,
            )
        self._relay = None
        if relay is not None:
            await relay.stop()

        self._cleanup_published_state(clean_shutdown=clean_shutdown)
        if self._daemon_lock is not None:
            self._daemon_lock.release()
            self._daemon_lock = None
        LOG.info("browser_extension_broker_stopped", port=self.port)

    def _make_relay(self, extension_secret: str) -> Relay:
        if self._relay_factory is not None:
            return self._relay_factory(
                extension_secret,
                self.port,
                self._handle_extension_event,
                self._handle_disconnect,
                self._handle_pairing_complete,
            )
        return ExtensionRelayServer(
            extension_secret,
            self.port,
            self._handle_extension_event,
            self._handle_disconnect,
            control_pairing_only=True,
            on_pairing_complete=self._handle_pairing_complete,
        )

    async def _cleanup_partial_start(self) -> None:
        output_watchdog_task = self._output_watchdog_task
        self._output_watchdog_task = None
        if output_watchdog_task is not None:
            output_watchdog_task.cancel()
            with suppress(asyncio.CancelledError):
                await output_watchdog_task
        server = self._control_server
        self._control_server = None
        if server is not None:
            server.close()
            await server.wait_closed()
        connections = {id(connection): connection for connection in self._connections.values()}
        connections.update((id(connection), connection) for connection in self._clients.values())
        if connections:
            await asyncio.gather(
                *(self._connection_closed(connection) for connection in connections.values()),
                return_exceptions=True,
            )
        relay = self._relay
        self._relay = None
        if relay is not None:
            with suppress(Exception):
                await relay.stop()
        self._cleanup_published_state(clean_shutdown=False)
        if self._daemon_lock is not None:
            self._daemon_lock.release()
            self._daemon_lock = None

    def _cleanup_published_state(self, *, clean_shutdown: bool) -> None:
        if not self._owns_published_state():
            return
        try:
            validate_run_directory(self.paths, expected_identity=self._run_identity)
            publish_broker_state(self.paths, self._state(lifecycle="stopped", clean_shutdown=clean_shutdown))
        except (BrowserExtensionBrokerError, OSError):
            LOG.warning("browser_extension_broker_state_cleanup_failed", port=self.port, exc_info=True)
        else:
            with suppress(BrowserExtensionBrokerError, OSError):
                remove_control_socket(self.paths)

    def _owns_published_state(self) -> bool:
        daemon_lock = self._daemon_lock
        if daemon_lock is None or daemon_lock.fd is None:
            LOG.warning(
                "browser_extension_broker_artifact_ownership_lost",
                port=self.port,
                reason="lifecycle_lock_not_held",
            )
            return False
        state: BrokerState | None = None
        for attempt in range(_OWNERSHIP_STATE_READ_ATTEMPTS):
            try:
                state = read_broker_state(self.paths)
            except (BrowserExtensionBrokerError, OSError):
                if attempt + 1 == _OWNERSHIP_STATE_READ_ATTEMPTS:
                    LOG.warning(
                        "browser_extension_broker_artifact_ownership_lost",
                        port=self.port,
                        reason="state_unreadable",
                        exc_info=True,
                    )
                    return False
                time.sleep(_OWNERSHIP_STATE_READ_RETRY_SECONDS)
            else:
                break
        if state is None:
            LOG.warning(
                "browser_extension_broker_artifact_ownership_lost",
                port=self.port,
                reason="state_missing",
            )
            return False
        if state.pid != os.getpid() or state.bootId != self._boot_id:
            LOG.warning(
                "browser_extension_broker_artifact_ownership_lost",
                port=self.port,
                reason="state_owner_mismatch",
                state_pid=state.pid,
                state_boot_id=state.bootId,
            )
            return False
        return True

    async def _handle_connection(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        if (
            self._pending_connections >= MAX_PENDING_CONNECTIONS
            or not self._consume_connection_token()
            or not self._verify_peer_uid(writer)
        ):
            await self._close_writer(writer)
            return
        self._pending_connections += 1
        handshake_pending = True
        connection: _ClientConnection | None = None
        try:
            connection = await self._authenticate(reader, writer)
            self._pending_connections -= 1
            handshake_pending = False
            await self._read_requests(connection)
        except (EOFError, ConnectionError, OSError, asyncio.TimeoutError, asyncio.CancelledError):
            pass
        except BrowserExtensionBrokerError as exc:
            LOG.info("browser_extension_broker_connection_rejected", code=exc.code)
        except Exception:
            LOG.error("browser_extension_broker_connection_failed", code="INTERNAL")
        finally:
            if handshake_pending:
                self._pending_connections -= 1
            if connection is not None:
                await self._connection_closed(connection)
            else:
                await self._close_writer(writer)

    async def _authenticate(
        self,
        reader: asyncio.StreamReader,
        writer: asyncio.StreamWriter,
    ) -> _ClientConnection:
        server_nonce = new_nonce()
        await write_frame(
            writer,
            event_frame(
                "auth.challenge",
                {"serverNonce": server_nonce, "brokerGeneration": BROKER_GENERATION},
            ),
            max_size=PREAUTH_FRAME_LIMIT,
        )
        frame, _size = await read_frame(reader, max_size=PREAUTH_FRAME_LIMIT, timeout=READ_TIMEOUT_SECONDS)
        request_id, op, args = parse_request(frame)
        operator = op == "client.enroll" and args.get("operator") is True
        allowed_keys = (
            {"clientNonce", "operator"}
            if operator
            else {"clientNonce"}
            if op == "client.enroll"
            else {"clientId", "clientNonce", "proof"}
        )
        if set(args) != allowed_keys:
            error = BrowserExtensionBrokerError("AUTH_FAILED", "Broker authentication failed")
            await write_frame(writer, error_frame(request_id, error), max_size=PREAUTH_FRAME_LIMIT)
            raise error
        client_nonce = args.get("clientNonce")
        if client_nonce is None or not is_valid_nonce(client_nonce):
            error = BrowserExtensionBrokerError("AUTH_FAILED", "Broker authentication failed")
            await write_frame(writer, error_frame(request_id, error), max_size=PREAUTH_FRAME_LIMIT)
            raise error

        credential: _Credential | None = None
        previous: _ClientConnection | None = None
        async with self._client_lock:
            relay = self._relay
            if not operator and self._extension_reset_quarantined and relay is not None and relay.connected:
                if relay.extension_protocol_version != PROTOCOL_VERSION:
                    error = BrowserExtensionBrokerError(
                        "EXTENSION_UPGRADE_REQUIRED",
                        "Reload the current Skyvern Agent extension before connecting this agent",
                    )
                else:
                    self._start_reset_recovery()
                    reset_code = self._extension_reset_error or "EXTENSION_RESET_IN_PROGRESS"
                    error = BrowserExtensionBrokerError(
                        reset_code,
                        "Extension state reset did not complete; retry after the extension reset completes",
                        retry_after=0.1 if reset_code == "EXTENSION_RESET_IN_PROGRESS" else None,
                    )
                await write_frame(writer, error_frame(request_id, error), max_size=PREAUTH_FRAME_LIMIT)
                raise error
            if op == "client.enroll":
                if not operator and len(self._clients) >= MAX_AUTHENTICATED_CLIENTS:
                    error = BrowserExtensionBrokerError(
                        "BROKER_BUSY",
                        f"Browser-extension broker already has {MAX_AUTHENTICATED_CLIENTS} clients",
                    )
                    await write_frame(writer, error_frame(request_id, error), max_size=PREAUTH_FRAME_LIMIT)
                    raise error
                credential = _Credential(
                    client_id=secrets.token_hex(16),
                    recovery_secret=secrets.token_urlsafe(32),
                )
            elif op == "client.authenticate":
                client_id = args.get("clientId")
                proof = args.get("proof")
                if not isinstance(client_id, str) or not _is_valid_client_id(client_id):
                    error = BrowserExtensionBrokerError("AUTH_FAILED", "Broker authentication failed")
                    await write_frame(writer, error_frame(request_id, error), max_size=PREAUTH_FRAME_LIMIT)
                    raise error
                credential = self._credentials.get(client_id)
                if credential is None:
                    error = BrowserExtensionBrokerError("UNKNOWN_CLIENT", "Broker client identity is unknown")
                    await write_frame(writer, error_frame(request_id, error), max_size=PREAUTH_FRAME_LIMIT)
                    raise error
                valid = isinstance(proof, str) and verify_client_proof(
                    credential.recovery_secret,
                    server_nonce,
                    client_nonce,
                    credential.client_id,
                    BROKER_GENERATION,
                    proof,
                )
                if not valid:
                    error = BrowserExtensionBrokerError("AUTH_FAILED", "Broker authentication failed")
                    await write_frame(writer, error_frame(request_id, error), max_size=PREAUTH_FRAME_LIMIT)
                    raise error
                if (
                    not operator
                    and credential.client_id not in self._clients
                    and (len(self._clients) >= MAX_AUTHENTICATED_CLIENTS)
                ):
                    error = BrowserExtensionBrokerError(
                        "BROKER_BUSY",
                        f"Browser-extension broker already has {MAX_AUTHENTICATED_CLIENTS} clients",
                    )
                    await write_frame(writer, error_frame(request_id, error), max_size=PREAUTH_FRAME_LIMIT)
                    raise error
                previous = self._clients.get(credential.client_id)
            else:
                error = BrowserExtensionBrokerError("AUTH_FAILED", "Broker authentication failed")
                await write_frame(writer, error_frame(request_id, error), max_size=PREAUTH_FRAME_LIMIT)
                raise error

            assert credential is not None
            if not operator:
                # Interactive approval is bound to a CONTINUOUSLY CONNECTED agent: it survives
                # overlap socket replacement (same proven client identity, predecessor still live
                # and approved - required for MV3/network reconnects), and dies on true disconnect.
                # It must NEVER be restorable from a stale approval_source after a disconnect cleared it.
                interactive_approval = (
                    previous is not None
                    and self._clients.get(credential.client_id) is previous
                    and not previous.closed
                    and not previous.ownership_released
                    and credential.approved
                    and credential.approval_source == _APPROVAL_SOURCE_INTERACTIVE
                )
                credential.approved = False
                credential.approval_source = None
                credential.approval_event.clear()
                grant = self._load_workstation_grant()
                self._workstation_grant = grant
                if interactive_approval:
                    credential.approved = True
                    credential.approval_source = _APPROVAL_SOURCE_INTERACTIVE
                    credential.approval_event.set()
                elif grant is not None:
                    credential.approved = True
                    credential.approval_source = _APPROVAL_SOURCE_GRANT
                    credential.approval_event.set()
            credential.connection_generation += 1
            connection = _ClientConnection(
                client_id=credential.client_id,
                generation=credential.connection_generation,
                reader=reader,
                writer=writer,
                operator=operator,
                last_output_progress=self.time_source(),
            )
            result: dict[str, Any] = {
                "clientId": credential.client_id,
                "connectionGeneration": connection.generation,
                "brokerGeneration": BROKER_GENERATION,
                "brokerProof": compute_broker_proof(
                    credential.recovery_secret,
                    client_nonce,
                    server_nonce,
                    credential.client_id,
                    BROKER_GENERATION,
                ),
            }
            if op == "client.enroll":
                result["recoverySecret"] = credential.recovery_secret
            await write_frame(writer, response_frame(request_id, result), max_size=PREAUTH_FRAME_LIMIT)
            try:
                connection.sender_task = asyncio.create_task(self._event_writer(connection))
                connection.keepalive_task = asyncio.create_task(self._control_keepalive(connection))
            except BaseException:
                if connection.sender_task is not None:
                    connection.sender_task.cancel()
                raise
            self._connections[id(connection)] = connection
            if not operator:
                if op == "client.enroll":
                    self._evict_stale_credentials_locked()
                    self._credentials[credential.client_id] = credential
                self._clients[credential.client_id] = connection
                try:
                    if previous is not None:
                        # Publish the successor before fencing the old socket. If the new
                        # authentication response fails, the old client remains current and
                        # keeps its leases instead of leaving a closed slot behind.
                        previous.ownership_released = True
                        await self._close_connection(previous)
                except BaseException:
                    # Ownership transfer failed before the post-handshake cleanup guard.
                    self._release_client_locked(connection)
                    await self._close_connection(connection)
                    raise

        try:
            relay = self._relay
            if (
                not connection.operator
                and credential.approved
                and relay is not None
                and relay.connected
                and relay.extension_protocol_version == PROTOCOL_VERSION
                and not self._extension_reset_quarantined
            ):
                await self._send_client_snapshot(connection)
        except BaseException:
            # If a successor handshake fails after ownership transfer, release the
            # current connection so its credential cannot remain approved-orphaned.
            await self._connection_closed(connection)
            raise
        return connection

    @staticmethod
    def _is_queueable_tab_request(op: str, args: dict[str, Any]) -> bool:
        if op != "extension.request":
            return False
        extension_args = args.get("args")
        return isinstance(extension_args, dict) and "tabId" in extension_args

    def _release_active_request(self, connection: _ClientConnection, request_id: str) -> None:
        if request_id not in connection.request_ids:
            return
        connection.request_ids.discard(request_id)
        self._global_requests = max(0, self._global_requests - 1)

    def _release_queued_request(self, connection: _ClientConnection, request_id: str) -> None:
        if request_id not in connection.queued_request_ids:
            return
        connection.queued_request_ids.discard(request_id)
        self._global_queued_requests = max(0, self._global_queued_requests - 1)

    def _release_outstanding_request(self, connection: _ClientConnection, request_id: str) -> None:
        if request_id not in connection.outstanding_request_ids:
            return
        connection.outstanding_request_ids.discard(request_id)
        self._global_outstanding_requests = max(0, self._global_outstanding_requests - 1)

    async def _read_requests(self, connection: _ClientConnection) -> None:
        while not connection.closed and not self._stopping:
            reserved_size = 0

            def reserve_inbound(declared: int) -> bool:
                nonlocal reserved_size
                if (
                    connection.inbound_bytes + declared > MAX_CLIENT_INBOUND_BYTES
                    or self._global_inbound_bytes + declared > MAX_GLOBAL_INBOUND_BYTES
                ):
                    return False
                connection.inbound_bytes += declared
                self._global_inbound_bytes += declared
                reserved_size = declared
                return True

            try:
                frame, size = await read_frame(
                    connection.reader,
                    max_size=CONTROL_FRAME_LIMIT if connection.operator else OPERATION_FRAME_LIMIT,
                    reserve=reserve_inbound,
                    control_size=CONTROL_FRAME_LIMIT if not connection.operator else None,
                    large_request_op="extension.request" if not connection.operator else None,
                )
            except BaseException:
                self._release_inbound(connection, reserved_size)
                raise
            connection.last_inbound = time.monotonic()
            release_inline = True
            try:
                if frame.get("type") in {"ping", "pong"}:
                    if size > CONTROL_FRAME_LIMIT:
                        raise BrowserExtensionBrokerError("FRAME_TOO_LARGE", "Control frame exceeds the allowed size")
                    if frame.get("type") == "ping":
                        await self._send(connection, {"v": BROKER_PROTOCOL_VERSION, "type": "pong"})
                    continue
                request_id, op, _args = parse_request(frame)
                if op != "extension.request" and size > CONTROL_FRAME_LIMIT:
                    raise BrowserExtensionBrokerError("FRAME_TOO_LARGE", "Control frame exceeds the allowed size")
                if request_id in connection.outstanding_request_ids:
                    raise BrowserExtensionBrokerError("DUPLICATE_REQUEST", "Duplicate broker request id")
                queueable = self._is_queueable_tab_request(op, _args)
                if (
                    len(connection.outstanding_request_ids) >= MAX_OUTSTANDING_REQUESTS_PER_CLIENT
                    or self._global_outstanding_requests >= MAX_GLOBAL_OUTSTANDING_REQUESTS
                ):
                    await self._send_error(
                        connection,
                        request_id,
                        BrowserExtensionBrokerError("RESOURCE_LIMIT", "Too many broker requests are in flight"),
                    )
                    continue
                if queueable:
                    if (
                        len(connection.queued_request_ids) >= MAX_QUEUED_REQUESTS_PER_CLIENT
                        or self._global_queued_requests >= MAX_GLOBAL_QUEUED_REQUESTS
                    ):
                        await self._send_error(
                            connection,
                            request_id,
                            BrowserExtensionBrokerError(
                                "RESOURCE_LIMIT",
                                "Too many broker requests are queued",
                            ),
                        )
                        continue
                else:
                    if (
                        len(connection.request_ids) >= MAX_REQUESTS_PER_CLIENT
                        or self._global_requests >= MAX_GLOBAL_REQUESTS
                    ):
                        await self._send_error(
                            connection,
                            request_id,
                            BrowserExtensionBrokerError("RESOURCE_LIMIT", "Too many broker requests are in flight"),
                        )
                        continue
                connection.outstanding_request_ids.add(request_id)
                self._global_outstanding_requests += 1
                if queueable:
                    connection.queued_request_ids.add(request_id)
                    self._global_queued_requests += 1
                else:
                    connection.request_ids.add(request_id)
                    self._global_requests += 1
                request_started_at = self.time_source()
                try:
                    task = asyncio.create_task(
                        self._handle_charged_request(
                            connection,
                            request_id,
                            frame,
                            size,
                            request_started_at=request_started_at,
                        )
                    )
                except BaseException:
                    if queueable:
                        self._release_queued_request(connection, request_id)
                    else:
                        self._release_active_request(connection, request_id)
                    self._release_outstanding_request(connection, request_id)
                    raise
                connection.request_tasks.add(task)

                def request_done(done: asyncio.Task[None], request_id: str = request_id) -> None:
                    connection.request_tasks.discard(done)
                    if not done.cancelled():
                        return
                    self._release_active_request(connection, request_id)
                    self._release_queued_request(connection, request_id)
                    self._release_outstanding_request(connection, request_id)

                task.add_done_callback(request_done)
                release_inline = False
            finally:
                if release_inline:
                    self._release_inbound(connection, size)

    async def _handle_charged_request(
        self,
        connection: _ClientConnection,
        request_id: str,
        frame: dict[str, Any],
        size: int,
        *,
        request_started_at: float | None = None,
    ) -> None:
        terminal_accounting = False
        terminal_response = False
        handler_done = False
        inbound_released = False
        accounting_released = False
        active = request_id in connection.request_ids

        def release_accounting() -> None:
            nonlocal accounting_released
            if accounting_released:
                return
            accounting_released = True
            if active:
                self._release_active_request(connection, request_id)
            else:
                self._release_queued_request(connection, request_id)
            self._release_outstanding_request(connection, request_id)

        def release_if_terminal() -> None:
            nonlocal inbound_released
            if inbound_released or not handler_done or (terminal_accounting and not terminal_response):
                return
            release_accounting()
            inbound_released = True
            self._release_inbound(connection, size)

        def on_admitted() -> None:
            nonlocal active
            if active or request_id not in connection.queued_request_ids:
                active = True
                return
            self._release_queued_request(connection, request_id)
            connection.request_ids.add(request_id)
            self._global_requests += 1
            active = True

        def on_registered() -> None:
            nonlocal terminal_accounting
            terminal_accounting = True

        def on_terminal() -> None:
            nonlocal terminal_response
            terminal_response = True
            release_accounting()
            release_if_terminal()

        try:
            await self._handle_request(
                connection,
                frame,
                on_admitted=on_admitted,
                on_registered=on_registered,
                on_terminal=on_terminal,
                request_started_at=request_started_at,
            )
        finally:
            handler_done = True
            release_if_terminal()

    async def _handle_request(
        self,
        connection: _ClientConnection,
        frame: dict[str, Any],
        *,
        on_admitted: Callable[[], None],
        on_registered: Callable[[], None],
        on_terminal: Callable[[], None],
        request_started_at: float | None,
    ) -> None:
        request_id = "unknown"
        try:
            request_id, op, args = parse_request(frame)
            if op == "extension.request":
                result = await self._dispatch(
                    connection,
                    op,
                    args,
                    on_admitted=on_admitted,
                    on_registered=on_registered,
                    on_terminal=on_terminal,
                    request_started_at=request_started_at,
                )
            else:
                result = await self._dispatch(connection, op, args)
            if not connection.closed:
                await self._send(connection, response_frame(request_id, result))
            if op == "broker.stop":
                self._shutdown_event.set()
        except BrowserExtensionBrokerError as exc:
            if not connection.closed:
                await self._send_error(connection, request_id, exc)
        except ExtensionRequestError as exc:
            if not connection.closed:
                await self._send_error(connection, request_id, exc)
        except BrowserExtensionNotConnectedError:
            if not connection.closed:
                await self._send_error(
                    connection,
                    request_id,
                    BrowserExtensionBrokerError(
                        "EXTENSION_NOT_CONNECTED", "Skyvern browser extension is not connected"
                    ),
                )
        except Exception:
            LOG.error("browser_extension_broker_request_failed", code="INTERNAL")
            if not connection.closed:
                await self._send_error(
                    connection,
                    request_id,
                    BrowserExtensionBrokerError("INTERNAL", "Broker request failed"),
                )

    async def _dispatch(
        self,
        connection: _ClientConnection,
        op: str,
        args: dict[str, Any],
        *,
        on_admitted: Callable[[], None] | None = None,
        on_registered: Callable[[], None] | None = None,
        on_terminal: Callable[[], None] | None = None,
        request_started_at: float | None = None,
    ) -> dict[str, Any]:
        if not await self._is_current(connection):
            raise BrowserExtensionBrokerError("STALE_CONNECTION", "Broker connection was replaced")
        if connection.operator and op not in {
            "broker.status",
            "broker.stop",
            "pairing.begin",
            "pairing.status",
            "pairing.cancel",
            "workstation.grant",
            "workstation.revoke",
        }:
            raise BrowserExtensionBrokerError("OP_NOT_ALLOWED", "Operator connection cannot forward extension requests")
        relay = self._relay
        if relay is None:
            raise BrowserExtensionBrokerError("BROKER_STOPPING", "Browser-extension broker is stopping")

        if op in _WORKSTATION_GRANT_OPS and not connection.operator:
            # Same-UID CLI authority is intentional: only the operator may
            # manage the per-user workstation grant envelope.
            raise BrowserExtensionBrokerError(
                "OP_NOT_ALLOWED",
                "Only an operator connection may manage workstation approval",
            )
        if op == "broker.status":
            tab_ids = []
            if not connection.operator and not self._extension_reset_quarantined:
                tab_ids = sorted(
                    tab_id for tab_id, lease in self._leases.items() if lease.client_id == connection.client_id
                )
            approved = connection.operator or self._client_approved(connection)
            extension_ready = (
                approved
                and relay.connected
                and relay.extension_protocol_version == PROTOCOL_VERSION
                and not self._extension_reset_quarantined
            )
            local_build_hash = _local_extension_build_hash()
            remote_build_hash = relay.extension_build_hash
            if not extension_ready:
                # No completed hello to compare yet - not an upgrade signal, just no data.
                extension_build = "unknown"
            elif remote_build_hash is None:
                # A connected, current-protocol extension that reports no hash predates
                # build_hash.json entirely - exactly the same-version-different-bytes skew
                # this check exists to catch, so it gets the same actionable "stale" label.
                extension_build = "stale"
            elif remote_build_hash == local_build_hash:
                extension_build = "current"
            else:
                extension_build = "stale"
            return {
                "protocol": BROKER_PROTOCOL_VERSION,
                "generation": BROKER_GENERATION,
                "buildFingerprint": BROKER_BUILD_FINGERPRINT,
                "lifecycle": "draining" if self._stopping else "ready",
                "extensionConnected": extension_ready,
                "approved": approved,
                "clientCount": len(self._clients),
                "tabIds": tab_ids,
                "quarantines": [],
                "extensionBuild": extension_build,
                "extensionBuildHash": local_build_hash[:_BUILD_HASH_SHORT_LENGTH],
                "extensionReportedBuildHash": (
                    remote_build_hash[:_BUILD_HASH_SHORT_LENGTH] if remote_build_hash is not None else None
                ),
            }
        if op == "workstation.grant":
            if args:
                raise BrowserExtensionBrokerError("INVALID_REQUEST", "Workstation grant does not accept arguments")
            return await self._grant_workstation()
        if op == "workstation.revoke":
            if set(args) - {"scope"}:
                raise BrowserExtensionBrokerError("INVALID_REQUEST", "Workstation revoke arguments are invalid")
            scope = args.get("scope", "grant")
            if not isinstance(scope, str) or scope not in {"grant", "all"}:
                raise BrowserExtensionBrokerError(
                    "INVALID_REQUEST",
                    "Workstation revoke scope must be 'grant' or 'all'",
                )
            return self._revoke_workstation(scope=scope)
        if op == "extension.wait_connected":
            timeout = args.get("timeout", 45.0)
            if isinstance(timeout, bool) or not isinstance(timeout, (int, float)) or not 0 <= timeout <= 45:
                raise BrowserExtensionBrokerError("INVALID_REQUEST", "Connection wait timeout is invalid")
            return {"connected": await self._wait_client_ready(connection, relay, float(timeout))}
        if (
            op not in {"pairing.begin", "pairing.status", "pairing.cancel", "broker.stop"}
            and not connection.operator
            and not self._client_approved(connection)
        ):
            raise BrowserExtensionBrokerError(
                "APPROVAL_REQUIRED",
                "Approve this agent's browser session in the Skyvern Agent confirmation tab",
            )
        if (
            op in {"extension.request", "lease.acquire_default", "lease.list"}
            and relay.connected
            and relay.extension_protocol_version != PROTOCOL_VERSION
        ):
            raise BrowserExtensionBrokerError(
                "EXTENSION_UPGRADE_REQUIRED",
                "Reload the current Skyvern Agent extension before using this agent",
            )
        if op == "lease.list":
            if args:
                raise BrowserExtensionBrokerError("INVALID_REQUEST", "Lease list does not accept arguments")
            if self._extension_reset_quarantined:
                raise BrowserExtensionBrokerError(
                    self._extension_reset_error or "EXTENSION_RESET_IN_PROGRESS",
                    "Extension state reset has not completed",
                    retry_after=0.1 if self._extension_reset_error in {None, "EXTENSION_RESET_IN_PROGRESS"} else None,
                )
            listed = await relay.request("tabs.list", {}, timeout=5.0)
            tabs = listed.get("tabs")
            if not isinstance(tabs, list):
                raise BrowserExtensionBrokerError("INVALID_FRAME", "Extension tab list is invalid")
            owned_tab_ids = {
                tab_id
                for tab_id, lease in self._leases.items()
                if not lease.draining and lease.client_id == connection.client_id
            }
            return {
                "tabs": [
                    dict(tab)
                    for tab in tabs
                    if isinstance(tab, dict) and type(tab.get("tabId")) is int and tab["tabId"] in owned_tab_ids
                ]
            }
        if op == "lease.release":
            if set(args) != {"tabId"}:
                raise BrowserExtensionBrokerError("INVALID_REQUEST", "Lease release requires only a tabId")
            release_tab_id = _normalize_tab_id(args["tabId"])
            if release_tab_id is None:
                raise BrowserExtensionBrokerError("INVALID_FRAME", "Lease release tab id is invalid")
            lease = self._leases.get(release_tab_id)
            if lease is None:
                return {"released": False}
            if lease.client_id != connection.client_id:
                raise BrowserExtensionBrokerError("LEASE_HELD", f"Tab {release_tab_id} is leased to another agent")
            lease.draining = True
            self._schedule_lease_drain(lease)
            return {"released": True}
        if op == "extension.request":
            if self._extension_reset_quarantined:
                raise BrowserExtensionBrokerError(
                    self._extension_reset_error or "EXTENSION_RESET_IN_PROGRESS",
                    "Extension state reset has not completed",
                    retry_after=0.1 if self._extension_reset_error in {None, "EXTENSION_RESET_IN_PROGRESS"} else None,
                )
            extension_op = args.get("op")
            extension_args = args.get("args")
            timeout = args.get("timeout", 30.0)
            if (
                not isinstance(extension_op, str)
                or extension_op not in ALLOWED_OPS
                or not isinstance(extension_args, dict)
            ):
                raise BrowserExtensionBrokerError("OP_NOT_ALLOWED", "Extension operation is not allowed")
            if isinstance(timeout, bool) or not isinstance(timeout, (int, float)) or not 0 < timeout <= 30:
                raise BrowserExtensionBrokerError("INVALID_REQUEST", "Extension request timeout is invalid")
            tab_id: int | None = None
            if "tabId" in extension_args:
                tab_id = _normalize_tab_id(extension_args["tabId"])
                if tab_id is None:
                    raise BrowserExtensionBrokerError("INVALID_FRAME", "Extension request tab id is invalid")
                extension_args = {**extension_args, "tabId": tab_id}
            if extension_op == "tabs.list":
                raise BrowserExtensionBrokerError("OP_NOT_ALLOWED", "tabs.list is broker-internal")
            if extension_op == "tabs.create":
                return await self._transactional_create(
                    connection,
                    extension_args,
                    float(timeout),
                    on_registered=on_registered,
                    on_terminal=on_terminal,
                )
            tab_reserved = False
            if extension_op in _LEASED_OPS:
                if tab_id is None:
                    raise BrowserExtensionBrokerError("LEASE_REQUIRED", f"{extension_op} requires a leased tabId")
                await self._claim_tab_lease(connection, tab_id)
            registered = False
            tab_released = False

            def release_tab() -> None:
                nonlocal tab_released
                if tab_id is None or not tab_reserved or tab_released:
                    return
                tab_released = True
                self._release_tab_request(tab_id)

            def forwarded_registered() -> None:
                nonlocal registered
                registered = True
                if on_registered is not None:
                    on_registered()

            def forwarded_terminal() -> None:
                release_tab()
                if on_terminal is not None:
                    on_terminal()

            try:
                if tab_id is not None:
                    remaining_timeout = await self._wait_for_tab_request_slot(
                        connection,
                        tab_id,
                        float(timeout),
                        request_started_at=request_started_at,
                    )
                    tab_reserved = True
                    if on_admitted is not None:
                        on_admitted()
                else:
                    remaining_timeout = float(timeout)
                forwarded = asyncio.create_task(
                    relay.request(
                        extension_op,
                        extension_args,
                        remaining_timeout,
                        retain_until_terminal=True,
                        on_registered=forwarded_registered,
                        on_terminal=forwarded_terminal,
                    )
                )
                self._forwarded_tasks.add(forwarded)
                forwarded.add_done_callback(self._forwarded_tasks.discard)
                return await asyncio.shield(forwarded)
            finally:
                if tab_reserved and not registered:
                    release_tab()
        if op == "pairing.begin":
            return await self._pairing_begin(connection, relay)
        if op == "pairing.status":
            self._expire_pairing(relay)
            pairing_owner = self._pairing_principal(connection)
            return {
                "active": self._pairing_owner is not None,
                "owned": self._pairing_owner == pairing_owner,
                "approved": connection.operator or self._client_approved(connection),
                "extensionConnected": (
                    (connection.operator or self._client_approved(connection))
                    and relay.connected
                    and relay.extension_protocol_version == PROTOCOL_VERSION
                    and not self._extension_reset_quarantined
                ),
            }
        if op == "pairing.cancel":
            self._expire_pairing(relay)
            pairing_owner = self._pairing_principal(connection)
            if self._pairing_owner is None:
                return {"cancelled": False}
            if self._pairing_owner != pairing_owner and not connection.operator:
                raise BrowserExtensionBrokerError("PAIRING_BUSY", "Another broker client owns the pairing flow")
            self._clear_pairing(relay, cancel_nonce=True)
            return {"cancelled": True}
        if op == "lease.acquire_default":
            if self._extension_reset_quarantined:
                raise BrowserExtensionBrokerError(
                    self._extension_reset_error or "EXTENSION_RESET_IN_PROGRESS",
                    "Extension state reset has not completed",
                    retry_after=0.1 if self._extension_reset_error in {None, "EXTENSION_RESET_IN_PROGRESS"} else None,
                )
            return {"tab": await self._acquire_default_lease(connection)}
        if op == "broker.stop":
            return {"stopping": True}
        raise BrowserExtensionBrokerError("OP_NOT_ALLOWED", "Broker operation is not available")

    def _current_auth_token(self) -> str | None:
        return self._broker_auth_token

    def _load_workstation_grant(self) -> WorkstationGrant | None:
        token = self._current_auth_token()
        if token is None:
            return None
        return load_workstation_grant(workstation_grant_path(), token)

    async def _grant_workstation(self) -> dict[str, Any]:
        token = self._current_auth_token()
        if token is None:
            raise BrowserExtensionBrokerError(
                "BROKER_NOT_ENABLED",
                "Browser-extension broker authentication token is unavailable",
            )
        # Local-trust envelope: the broker binds to 127.0.0.1, clients already
        # authenticate with the 0600 local token file, and the pairing click adds
        # HUMAN CONSENT for first use. This grant persists that consent for this OS user.
        grant = write_workstation_grant(workstation_grant_path(), token, source="cli")
        self._workstation_grant = grant
        for client_id in tuple(self._clients):
            await self._approve_client(client_id, source=_APPROVAL_SOURCE_GRANT)
        return {
            "granted": True,
            "source": grant.source,
            "grantedAt": grant.granted_at,
        }

    def _revoke_workstation(self, *, scope: str = "grant") -> dict[str, Any]:
        relay = self._relay
        if relay is not None and self._pairing_owner is not None:
            # Invalidate every pending confirmation before revoking live approvals.
            self._clear_pairing(relay, cancel_nonce=True)
        self._workstation_grant = None
        cleared_grant = 0
        cleared_interactive = 0
        for connection in tuple(self._clients.values()):
            if connection.closed:
                continue
            credential = self._credentials.get(connection.client_id)
            if credential is None:
                continue
            if credential.approval_source == _APPROVAL_SOURCE_GRANT:
                if credential.approved:
                    cleared_grant += 1
            elif scope == "all" and credential.approval_source == _APPROVAL_SOURCE_INTERACTIVE:
                if credential.approved:
                    cleared_interactive += 1
            else:
                continue
            credential.approved = False
            credential.approval_source = None
            credential.approval_event.clear()

        file_removal_error: str | None = None
        try:
            revoked = remove_workstation_grant(workstation_grant_path())
        except BrowserExtensionBrokerError as exc:
            revoked = False
            file_removal_error = (
                "Workstation grant path failed safety validation"
                if exc.code == "UNSAFE_PATH"
                else "Workstation grant file could not be removed"
            )
        except OSError:
            revoked = False
            file_removal_error = "Workstation grant file could not be removed"

        # workstation.revoke default scope clears grant-source approvals only (existing amended contract).
        # New scope "all" also clears interactive approvals and is the operator's full kill switch.
        result: dict[str, Any] = {
            "revoked": revoked,
            "scope": scope,
            "cleared": {
                "grant": cleared_grant,
                "interactive": cleared_interactive,
            },
        }
        if file_removal_error is not None:
            result["file_removal_error"] = file_removal_error
        return result

    async def _pairing_begin(self, connection: _ClientConnection, relay: Relay) -> dict[str, Any]:
        if relay.connected and relay.extension_protocol_version != PROTOCOL_VERSION:
            raise BrowserExtensionBrokerError(
                "EXTENSION_UPGRADE_REQUIRED",
                "Reload the current Skyvern Agent extension before approving this agent",
            )
        now = time.monotonic()
        self._expire_pairing(relay)
        pairing_owner = self._pairing_principal(connection)
        if self._pairing_owner is not None and self._pairing_owner != pairing_owner:
            raise BrowserExtensionBrokerError("PAIRING_BUSY", "Another broker client owns the pairing flow")
        if self._pairing_owner is None:
            while self._pairing_begins and now - self._pairing_begins[0] >= 60.0:
                self._pairing_begins.popleft()
            self._principal_pairing_begin = {
                principal: started_at
                for principal, started_at in self._principal_pairing_begin.items()
                if now - started_at < 60.0
            }
            if len(self._pairing_begins) >= 5:
                raise BrowserExtensionBrokerError("RATE_LIMITED", "Pairing requests are temporarily rate limited")
            last_client_begin = self._principal_pairing_begin.get(pairing_owner)
            if last_client_begin is not None and now - last_client_begin < 60.0:
                raise BrowserExtensionBrokerError("RATE_LIMITED", "Pairing requests are temporarily rate limited")
            self._pairing_begins.append(now)
            self._principal_pairing_begin[pairing_owner] = now
            self._pairing_owner = pairing_owner
            self._pairing_expires_at = now + PAIRING_TTL_SECONDS
            self._pairing_approval_nonce = new_nonce()
        nonce = relay.get_or_create_pairing_nonce()
        pairing_url = f"http://127.0.0.1:{relay.bound_port}/pair#{nonce}"
        opened = self._open_pairing_url(pairing_url)
        result: dict[str, Any] = {
            "active": True,
            "opened": opened,
            "expiresIn": max(0.0, self._pairing_expires_at - now),
        }
        if not opened:
            result["pairingUrl"] = pairing_url
        return result

    def _open_pairing_url(self, url: str) -> bool:
        if self._pairing_opener is not None:
            return self._pairing_opener(url)
        from skyvern.browser_extension.runtime import BrowserExtensionRuntime

        return BrowserExtensionRuntime.open_extension_url(url)

    def _expire_pairing(self, relay: Relay) -> None:
        now = time.monotonic()
        self._approved_pairing_nonces = {
            nonce: expires_at for nonce, expires_at in self._approved_pairing_nonces.items() if now < expires_at
        }
        if self._pairing_owner is not None and now >= self._pairing_expires_at:
            self._clear_pairing(relay, cancel_nonce=True)

    def _clear_pairing(self, relay: Relay, *, cancel_nonce: bool) -> None:
        if cancel_nonce:
            relay.cancel_pairing_nonce()
        self._pairing_owner = None
        self._pairing_expires_at = 0.0
        self._pairing_approval_nonce = None

    async def _handle_pairing_complete(self) -> dict[str, str] | None:
        relay = self._relay
        if relay is None:
            return None
        self._expire_pairing(relay)
        owner = self._pairing_owner
        approval_nonce = self._pairing_approval_nonce
        if owner is None or approval_nonce is None:
            return None
        return {
            "approvalNonce": approval_nonce,
            "requestFingerprint": self._client_fingerprint(owner),
        }

    @staticmethod
    def _client_fingerprint(client_id: str) -> str:
        return client_id[:8] if client_id != "operator" else "operator"

    def _client_approved(self, connection: _ClientConnection) -> bool:
        credential = self._credentials.get(connection.client_id)
        return credential is not None and credential.approved

    async def _approve_client(self, client_id: str, *, source: str = _APPROVAL_SOURCE_INTERACTIVE) -> bool:
        credential = self._credentials.get(client_id)
        connection = self._clients.get(client_id)
        if credential is None or connection is None or connection.closed:
            return False
        if (
            source == _APPROVAL_SOURCE_GRANT
            and credential.approved
            and credential.approval_source == _APPROVAL_SOURCE_INTERACTIVE
        ):
            return True
        credential.approved = True
        credential.approval_source = source
        credential.approval_event.set()
        relay = self._relay
        if (
            relay is not None
            and relay.connected
            and relay.extension_protocol_version == PROTOCOL_VERSION
            and not self._extension_reset_quarantined
        ):
            await self._send_client_snapshot(connection)
        return True

    @staticmethod
    def _pairing_principal(connection: _ClientConnection) -> str:
        return "operator" if connection.operator else connection.client_id

    async def _handle_extension_event(self, event: str, params: dict) -> None:
        if event == "extension.hello":
            self._extension_supports_scope_origins = params.get("scopeEventOrigins") is True
        relay = self._relay
        if event == "pairing.approved":
            await self._handle_pairing_approved(params)
            return
        if event == "extension.reset_ack":
            reset_epoch = params.get("epoch")
            generation = params.get("generation")
            ok = params.get("ok")
            reset_identity = (reset_epoch, generation)
            expected_identity = (self._extension_reset_epoch, self._extension_reset_generation)
            if (
                isinstance(reset_epoch, str)
                and type(generation) is int
                and reset_identity == expected_identity
                and self._extension_reset_quarantined
            ):
                if ok is True:
                    self._extension_reset_ack_identity = expected_identity
                    self._extension_reset_failed_identity = None
                    self._extension_reset_quarantined = False
                    self._extension_reset_error = None
                elif ok is False:
                    self._extension_reset_failed_identity = expected_identity
                    self._extension_reset_error = "EXTENSION_RESET_FAILED"
                self._extension_reset_event.set()
                if ok is True and relay is not None:
                    self._free_all_leases("extension reset completed")
                    await self._broadcast_snapshot()
            return
        if self._extension_reset_quarantined:
            if event == "extension.hello":
                self._start_reset_recovery()
            self._extension_reset_event.set()
            return
        if event == "extension.hello":
            await self._broadcast_snapshot()
            return
        await self._route_tab_event(event, params)

    async def _handle_pairing_approved(self, params: dict) -> None:
        relay = self._relay
        if relay is None:
            return
        self._expire_pairing(relay)
        approval_nonce = params.get("approvalNonce")
        if not isinstance(approval_nonce, str):
            return
        approved = approval_nonce in self._approved_pairing_nonces
        if not approved and secrets.compare_digest(approval_nonce, self._pairing_approval_nonce or ""):
            owner = self._pairing_owner
            approved = owner == "operator" or (
                owner is not None and await self._approve_client(owner, source=_APPROVAL_SOURCE_INTERACTIVE)
            )
            if approved and owner is not None:
                if owner != "operator":
                    self._principal_pairing_begin.pop(owner, None)
                    token = self._current_auth_token()
                    if token is not None:
                        try:
                            grant = write_workstation_grant(
                                workstation_grant_path(),
                                token,
                                source="pairing",
                            )
                        except (BrowserExtensionBrokerError, OSError) as exc:
                            LOG.warning(
                                "grant not persisted, next client will re-prompt",
                                error_type=type(exc).__name__,
                                reason=str(exc),
                                exc_info=True,
                            )
                        else:
                            self._workstation_grant = grant
                            for client_id in tuple(self._clients):
                                await self._approve_client(client_id, source=_APPROVAL_SOURCE_GRANT)
                self._approved_pairing_nonces[approval_nonce] = time.monotonic() + PAIRING_TTL_SECONDS
                self._clear_pairing(relay, cancel_nonce=False)
        if approved:
            await relay.send_event(
                "pairing.approved_ack",
                {"approvalNonce": approval_nonce, "approved": True},
            )

    async def _route_tab_event(self, event: str, params: dict) -> None:
        tab_id = params.get("tabId")
        if type(tab_id) is not int:
            return
        lease = self._leases.get(tab_id)
        if lease is None:
            if event == "tabs.created":
                opener_id = params.get("openerTabId")
                opener = self._leases.get(opener_id) if type(opener_id) is int else None
                if opener is not None:
                    popup_lease = await self._grant_lease(tab_id, opener.client_id, origin="created")
                    if opener.draining:
                        popup_lease.draining = True
                        self._schedule_lease_drain(popup_lease)
                    else:
                        await self._forward_to_owner(opener.client_id, event, params)
                return
            if (
                event == "scope.tabAdded"
                and self._pending_create_count > 0
                and params.get("origin") == "created"
                and self._buffer_pending_tab_event(tab_id, event, params)
            ):
                return
            # Current extensions buffer only labeled creates. Legacy extensions do not
            # label created versus manually shared tabs, so their unowned tabs stay
            # globally fenced until every pending create is terminal.
            return
        if lease.draining:
            # Scope removal proves cleanup completed, but an old request still keeps the fence armed.
            if event == "scope.tabRemoved" and self._tab_request_counts.get(tab_id, 0) == 0:
                await self._free_lease(tab_id)
            return
        if event == "scope.tabRemoved":
            lease.draining = True
            await self._persist_leases()
            cleanup = lease.origin == "created" and params.get("reason") != "closed"
            self._schedule_lease_drain(lease, cleanup=cleanup)
        await self._forward_to_owner(lease.client_id, event, params)

    def _legacy_create_fence_active(self) -> bool:
        return self._pending_create_count > 0 and not self._extension_supports_scope_origins

    def _buffer_pending_tab_event(self, tab_id: int, event: str, params: dict) -> bool:
        buffered = self._pending_tab_events.get(tab_id)
        if buffered is None:
            if len(self._pending_tab_events) >= MAX_PENDING_TAB_EVENT_TABS:
                return False
            buffered = self._pending_tab_events.setdefault(tab_id, [])
        if len(buffered) >= MAX_PENDING_TAB_EVENTS_PER_TAB:
            return False
        buffered.append((event, dict(params)))
        return True

    async def _forward_to_owner(self, client_id: str, event: str, params: dict) -> None:
        connection = self._clients.get(client_id)
        if connection is not None and not connection.closed:
            await self._send_event(connection, "extension.event", {"event": event, "params": params})

    async def _send_client_snapshot(self, connection: _ClientConnection) -> None:
        await self._send_event(
            connection,
            "extension.event",
            {"event": "extension.hello", "params": {"scopedTabs": self._owned_tabs(connection.client_id)}},
        )
        await self._send_event(connection, "extension.connected", {})

    async def _broadcast_snapshot(self) -> None:
        relay = self._relay
        if relay is None or relay.extension_protocol_version != PROTOCOL_VERSION:
            return
        for connection in list(self._clients.values()):
            if connection.closed or not self._client_approved(connection):
                continue
            await self._send_client_snapshot(connection)

    async def _handle_disconnect(self) -> None:
        self._extension_supports_scope_origins = False
        if not self._extension_reset_quarantined:
            self._arm_extension_reset(increment_generation=True)
        else:
            self._extension_reset_error = None
        self._extension_reset_event.set()
        self._free_all_leases("extension disconnected")
        for connection in list(self._clients.values()):
            if not connection.closed and self._client_approved(connection):
                await self._send_event(connection, "extension.disconnected", {})

    async def _connection_closed(self, connection: _ClientConnection) -> None:
        try:
            await self._close_connection(connection)
        finally:
            async with self._client_lock:
                self._release_client_locked(connection)

    async def _close_connection(self, connection: _ClientConnection) -> None:
        if connection.close_started:
            return
        connection.close_started = True
        self._mark_connection_closed(connection)
        writer_close_started = False
        self._fail_queued_tab_requests(
            connection=connection,
            error=BrowserExtensionNotConnectedError("Broker client disconnected while the request was queued"),
        )
        sender_task = connection.sender_task
        try:
            if sender_task is not None and sender_task is not asyncio.current_task():
                sender_task.cancel()
                with suppress(asyncio.CancelledError, Exception):
                    await sender_task
            keepalive_task = connection.keepalive_task
            if keepalive_task is not None and keepalive_task is not asyncio.current_task():
                keepalive_task.cancel()
                with suppress(asyncio.CancelledError, Exception):
                    await keepalive_task
            for task in tuple(connection.request_tasks):
                if task is not asyncio.current_task():
                    task.cancel()
            request_tasks = tuple(task for task in connection.request_tasks if task is not asyncio.current_task())
            if request_tasks:
                await asyncio.wait(request_tasks, timeout=self.client_close_grace_seconds)
            for request_id in tuple(connection.queued_request_ids):
                self._release_queued_request(connection, request_id)
                self._release_outstanding_request(connection, request_id)
            writer_close_started = True
            await self._close_writer(connection.writer)
        finally:
            for request_id in tuple(connection.queued_request_ids):
                self._release_queued_request(connection, request_id)
                self._release_outstanding_request(connection, request_id)
            if not writer_close_started:
                with suppress(Exception):
                    connection.writer.close()
                transport = getattr(connection.writer, "transport", None)
                if transport is not None:
                    with suppress(Exception):
                        transport.abort()
            self._discard_output_queue(connection)
            self._connections.pop(id(connection), None)

    async def _close_writer(self, writer: asyncio.StreamWriter) -> None:
        close_failed = False
        try:
            writer.close()
            await asyncio.wait_for(writer.wait_closed(), self.client_close_grace_seconds)
        except (ConnectionError, OSError, asyncio.TimeoutError):
            close_failed = True
        except BaseException:
            close_failed = True
            raise
        finally:
            if close_failed:
                transport = getattr(writer, "transport", None)
                if transport is not None:
                    with suppress(Exception):
                        transport.abort()

    def _release_client_locked(self, connection: _ClientConnection) -> None:
        if connection.operator:
            return
        if connection.ownership_released:
            # Overlap replacement transferred ownership to a live successor. A failed
            # successor handshake clears approval in its own connection cleanup.
            return
        if self._clients.get(connection.client_id) is not connection:
            return
        connection.ownership_released = True
        del self._clients[connection.client_id]
        credential = self._credentials.get(connection.client_id)
        if credential is not None:
            # Interactive approval is bound to a CONTINUOUSLY CONNECTED agent and
            # dies on true disconnect; clear every approval field before removal.
            credential.approved = False
            credential.approval_source = None
            credential.approval_event.clear()
        if self._pairing_owner == connection.client_id:
            # An in-flight approval stays alive only while its owner is connected.
            relay = self._relay
            if relay is not None:
                self._clear_pairing(relay, cancel_nonce=True)
        owned = [lease for lease in self._leases.values() if lease.client_id == connection.client_id]
        if not owned:
            return
        for lease in owned:
            lease.draining = True
            self._schedule_lease_drain(lease)
        LOG.info(
            "browser_extension_broker_client_leases_released",
            count=len(owned),
            client_generation=connection.generation,
        )

    def _schedule_lease_drain(self, lease: _TabLease, *, cleanup: bool = True) -> None:
        self._fail_queued_tab_requests(
            tab_id=lease.tab_id,
            error=BrowserExtensionBrokerError("LEASE_HELD", f"Tab {lease.tab_id} is being released"),
        )
        existing = self._lease_drain_tasks.get(lease.tab_id)
        if existing is not None and not existing.done():
            return
        task = asyncio.create_task(self._drain_released_lease(lease, cleanup=cleanup))
        self._lease_drain_tasks[lease.tab_id] = task
        self._cleanup_tasks.add(task)

        def drain_done(done: asyncio.Task[None]) -> None:
            self._cleanup_tasks.discard(done)
            if self._lease_drain_tasks.get(lease.tab_id) is done:
                self._lease_drain_tasks.pop(lease.tab_id, None)

        task.add_done_callback(drain_done)

    async def _drain_released_lease(self, lease: _TabLease, *, cleanup: bool = True) -> None:
        """Fence a released tab until its in-flight requests finish, then clean it up and free it.

        Freeing immediately would let a successor claim the tab while the departed client's
        debugger command is still executing, and the trailing cleanup would then detach or
        close the successor's tab. A tab removed from extension scope is already isolated,
        so it only needs the idle fence before release.
        """
        await self._wait_tab_idle(lease.tab_id)
        if self._leases.get(lease.tab_id) is not lease:
            return  # Freed by a reset sweep while draining.
        if not cleanup:
            await self._free_lease(lease.tab_id)
            return
        relay = self._relay
        cleanup_succeeded = False
        await_scope_removal = False
        if not self._stopping and relay is not None and relay.connected and not self._extension_reset_quarantined:
            # Broker-created tabs close; user-shared tabs detach, which revokes their scope
            # (phase-1 consent semantics) without touching any other client's tabs.
            op = "tabs.remove" if lease.origin == "created" else "debugger.detach"
            try:
                await relay.request(op, {"tabId": lease.tab_id}, 10.0)
            except ExtensionRequestError as exc:
                if op == "debugger.detach" and exc.code == "DEBUGGER_DETACHED":
                    cleanup_succeeded = True
                else:
                    LOG.warning(
                        "browser_extension_broker_lease_cleanup_failed",
                        tab_id=lease.tab_id,
                        op=op,
                        error_code=exc.code,
                    )
            except Exception:
                LOG.warning(
                    "browser_extension_broker_lease_cleanup_failed",
                    tab_id=lease.tab_id,
                    op=op,
                )
            else:
                cleanup_succeeded = True
                await_scope_removal = op == "debugger.detach"
        if cleanup_succeeded and not await_scope_removal and self._leases.get(lease.tab_id) is lease:
            await self._free_lease(lease.tab_id)

    async def _wait_tab_idle(self, tab_id: int) -> None:
        if self._tab_request_counts.get(tab_id, 0) == 0:
            return
        event = self._tab_idle_events.setdefault(tab_id, asyncio.Event())
        await event.wait()

    async def _acquire_default_lease(self, connection: _ClientConnection) -> dict[str, Any]:
        relay = self._relay
        if relay is None:
            raise BrowserExtensionBrokerError("BROKER_STOPPING", "Browser-extension broker is stopping")
        async with self._create_lock:
            free = (
                []
                if self._legacy_create_fence_active()
                else [
                    tab
                    for tab in relay.scoped_tabs
                    if type(tab.get("tabId")) is int
                    and tab["tabId"] not in self._leases
                    and tab["tabId"] not in self._pending_tab_events
                ]
            )
            if free:
                tab = dict(min(free, key=lambda entry: entry["tabId"]))
                await self._grant_lease(tab["tabId"], connection.client_id, origin="shared")
                await self._forward_to_owner(connection.client_id, "scope.tabAdded", tab)
                return tab
        created = await self._transactional_create(connection, {"url": "about:blank"}, 15.0)
        tab_id = created.get("tabId")
        if type(tab_id) is not int:
            raise BrowserExtensionBrokerError("INTERNAL", "Extension tab creation returned an invalid tab")
        return {"tabId": tab_id, "url": "about:blank", "title": ""}

    async def _claim_tab_lease(self, connection: _ClientConnection, tab_id: int) -> None:
        """Serialize first-come claims of unleased scoped tabs; reject cross-client touches."""
        lease = self._leases.get(tab_id)
        if lease is not None and not lease.draining and lease.client_id == connection.client_id:
            return
        relay = self._relay
        async with self._create_lock:
            lease = self._leases.get(tab_id)
            if lease is not None:
                if lease.draining:
                    raise BrowserExtensionBrokerError("LEASE_HELD", f"Tab {tab_id} is being released")
                if lease.client_id == connection.client_id:
                    return
                raise BrowserExtensionBrokerError("LEASE_HELD", f"Tab {tab_id} is leased to another agent")
            if self._legacy_create_fence_active():
                raise BrowserExtensionBrokerError(
                    "LEASE_HELD",
                    f"Tab {tab_id} is fenced while a legacy extension create is pending",
                )
            if tab_id in self._pending_tab_events:
                raise BrowserExtensionBrokerError("LEASE_HELD", f"Tab {tab_id} is being created for another agent")
            snapshot = None
            if relay is not None:
                for tab in relay.scoped_tabs:
                    if tab.get("tabId") == tab_id:
                        snapshot = dict(tab)
                        break
            if snapshot is None:
                raise BrowserExtensionBrokerError("LEASE_REQUIRED", f"Tab {tab_id} is not in the controlled scope")
            await self._grant_lease(tab_id, connection.client_id, origin="claimed")
            await self._forward_to_owner(connection.client_id, "scope.tabAdded", snapshot)

    async def _transactional_create(
        self,
        connection: _ClientConnection,
        args: dict[str, Any],
        timeout: float,
        *,
        on_registered: Callable[[], None] | None = None,
        on_terminal: Callable[[], None] | None = None,
    ) -> dict[str, Any]:
        relay = self._relay
        if relay is None:
            raise BrowserExtensionBrokerError("BROKER_STOPPING", "Browser-extension broker is stopping")
        # No lock is held across the extension round-trip: creates from different clients run
        # concurrently and tab events are buffered per tabId until a creator grants its lease.
        # Each create reserves one future correlation slot. Events already awaiting
        # their create response consume their slot until that response claims the tab.
        if self._pending_create_count + len(self._pending_tab_events) >= MAX_PENDING_TAB_EVENT_TABS:
            raise BrowserExtensionBrokerError(
                "RESOURCE_LIMIT",
                "Tab-create correlation capacity is full",
            )
        self._pending_create_count += 1
        pending_create_transferred = False
        try:
            forwarded = asyncio.create_task(
                relay.request(
                    "tabs.create",
                    args,
                    None,
                    retain_until_terminal=True,
                    on_registered=on_registered,
                    on_terminal=on_terminal,
                )
            )
            self._forwarded_tasks.add(forwarded)
            forwarded.add_done_callback(self._forwarded_tasks.discard)
            try:
                result = await asyncio.wait_for(asyncio.shield(forwarded), timeout)
            except TimeoutError:
                # Return the caller's deadline error now, but retain the extension request
                # until its terminal response identifies any tab that must be reclaimed.
                pending_create_transferred = True
                self._schedule_orphan_create_reclaim(forwarded, connection.client_id)
                raise ExtensionRequestError("INTERNAL", "extension request timed out: tabs.create") from None
            except asyncio.CancelledError:
                # The shielded create keeps running; make sure its tab cannot outlive the
                # departed client as an unleased, adoptable orphan.
                pending_create_transferred = True
                self._schedule_orphan_create_reclaim(forwarded, connection.client_id)
                raise
            tab_id = result.get("tabId") if isinstance(result, dict) else None
            if type(tab_id) is int:
                async with self._create_lock:
                    if tab_id not in self._leases:
                        await self._grant_lease(tab_id, connection.client_id, origin="created")
                    buffered_events = self._pending_tab_events.pop(tab_id, [])
                for event, event_params in buffered_events:
                    with suppress(Exception):
                        await self._route_tab_event(event, event_params)
            return result if isinstance(result, dict) else {}
        finally:
            if not pending_create_transferred:
                await self._finish_pending_create()

    def _schedule_orphan_create_reclaim(
        self,
        forwarded: asyncio.Task[dict[str, Any]],
        client_id: str,
    ) -> None:
        task = asyncio.create_task(self._reclaim_orphan_create(forwarded, client_id))
        self._cleanup_tasks.add(task)
        task.add_done_callback(self._cleanup_tasks.discard)

    async def _reclaim_orphan_create(
        self,
        forwarded: asyncio.Task[dict[str, Any]],
        client_id: str,
    ) -> None:
        try:
            try:
                result = await forwarded
            except Exception:
                return
            tab_id = result.get("tabId") if isinstance(result, dict) else None
            if type(tab_id) is not int:
                return
            async with self._create_lock:
                if tab_id in self._leases:
                    return
                lease = await self._grant_lease(tab_id, client_id, origin="created")
                lease.draining = True
                self._pending_tab_events.pop(tab_id, None)
            self._schedule_lease_drain(lease)
        finally:
            await self._finish_pending_create()

    async def _finish_pending_create(self) -> None:
        self._pending_create_count -= 1
        if self._pending_create_count == 0 and self._pending_tab_events:
            leftovers = self._pending_tab_events
            self._pending_tab_events = {}
            for events in leftovers.values():
                for event, event_params in events:
                    with suppress(Exception):
                        await self._route_tab_event(event, event_params)

    def _owned_tabs(self, client_id: str) -> list[dict[str, Any]]:
        relay = self._relay
        if relay is None:
            return []
        owned = []
        for tab in relay.scoped_tabs:
            tab_id = tab.get("tabId")
            if type(tab_id) is not int:
                continue
            lease = self._leases.get(tab_id)
            if lease is not None and not lease.draining and lease.client_id == client_id:
                owned.append(dict(tab))
        return owned

    async def _grant_lease(self, tab_id: int, client_id: str, *, origin: str) -> _TabLease:
        lease = _TabLease(tab_id=tab_id, client_id=client_id, origin=origin)
        self._leases[tab_id] = lease
        await self._persist_leases()
        return lease

    async def _free_lease(self, tab_id: int) -> None:
        self._fail_queued_tab_requests(
            tab_id=tab_id,
            error=BrowserExtensionBrokerError("LEASE_REQUIRED", f"Tab {tab_id} is no longer leased"),
        )
        if self._leases.pop(tab_id, None) is not None:
            await self._persist_leases()

    def _free_all_leases(self, reason: str) -> None:
        self._fail_all_queued_tab_requests(
            BrowserExtensionNotConnectedError("The browser extension disconnected while the request was queued")
        )
        if not self._leases:
            return
        count = len(self._leases)
        self._leases.clear()
        LOG.info("browser_extension_broker_leases_released", count=count, reason=reason)
        self._schedule_journal_write()

    async def _persist_leases(self) -> None:
        async with self._journal_lock:
            snapshot = [lease.to_json() for lease in self._leases.values()]
            try:
                await asyncio.to_thread(write_lease_journal, self.paths, snapshot)
            except Exception:
                LOG.warning("browser_extension_broker_lease_journal_write_failed")

    def _schedule_journal_write(self) -> None:
        task = asyncio.create_task(self._persist_leases())
        self._cleanup_tasks.add(task)
        task.add_done_callback(self._cleanup_tasks.discard)

    def _evict_stale_credentials_locked(self) -> None:
        if len(self._credentials) < MAX_AUTHENTICATED_CLIENTS * 4:
            return
        for client_id in list(self._credentials):
            if len(self._credentials) < MAX_AUTHENTICATED_CLIENTS * 4:
                return
            if client_id not in self._clients:
                del self._credentials[client_id]

    async def _reset_extension_state(self, relay: Relay, epoch: str, generation: int, timeout: float) -> bool | None:
        loop = asyncio.get_running_loop()
        deadline = loop.time() + timeout
        warned_legacy_connections: set[int] = set()
        reset_identity = (epoch, generation)
        while True:
            self._extension_reset_event.clear()
            if self._extension_reset_ack_identity == reset_identity:
                return True
            if self._extension_reset_failed_identity == reset_identity:
                return False
            if reset_identity != (self._extension_reset_epoch, self._extension_reset_generation):
                return False
            if not relay.connected:
                return None
            protocol_version = relay.extension_protocol_version
            connection_generation = relay.extension_connection_generation
            remaining = deadline - loop.time()
            if remaining <= 0:
                return False
            if protocol_version == LEGACY_PROTOCOL_VERSION:
                if connection_generation not in warned_legacy_connections:
                    self._warn_legacy_protocol()
                    warned_legacy_connections.add(connection_generation)
                if not await relay.cycle_connection(remaining):
                    return False
                if (
                    relay.extension_connection_generation == connection_generation
                    or relay.extension_protocol_version is None
                ):
                    return True
                continue
            if protocol_version == PROTOCOL_VERSION:
                try:
                    await asyncio.wait_for(relay.send_reset(epoch, generation), remaining)
                except TimeoutError:
                    return False
            remaining = deadline - loop.time()
            if remaining <= 0:
                return False
            try:
                await asyncio.wait_for(self._extension_reset_event.wait(), remaining)
            except TimeoutError:
                return False

    async def _recover_extension_reset(self, epoch: str, generation: int) -> None:
        relay = self._relay
        if (
            relay is None
            or (epoch, generation) != (self._extension_reset_epoch, self._extension_reset_generation)
            or not self._extension_reset_quarantined
        ):
            return
        self._extension_reset_error = "EXTENSION_RESET_IN_PROGRESS"
        try:
            reset_complete = await self._reset_extension_state(
                relay,
                epoch,
                generation,
                EXTENSION_RESET_TIMEOUT_SECONDS,
            )
        except Exception:
            reset_complete = False
            self._extension_reset_error = "EXTENSION_RESET_FAILED"
            LOG.error("browser_extension_broker_reset_failed", code="EXTENSION_RESET_FAILED")
        if (epoch, generation) != (self._extension_reset_epoch, self._extension_reset_generation):
            return
        if reset_complete is True:
            self._extension_reset_quarantined = False
            self._extension_reset_error = None
        elif reset_complete is None:
            self._extension_reset_error = None
        elif self._extension_reset_error != "EXTENSION_RESET_FAILED":
            self._extension_reset_error = "EXTENSION_RESET_TIMEOUT"
        self._extension_reset_event.set()

    def _start_reset_recovery(self) -> None:
        reset_recovery_task = self._reset_recovery_task
        if reset_recovery_task is not None and not reset_recovery_task.done():
            return
        self._reset_recovery_task = asyncio.create_task(
            self._recover_extension_reset(self._extension_reset_epoch, self._extension_reset_generation)
        )

    def _arm_extension_reset(self, *, increment_generation: bool) -> None:
        if increment_generation:
            self._extension_reset_generation += 1
        self._extension_reset_quarantined = True
        self._extension_reset_error = "EXTENSION_RESET_IN_PROGRESS"
        self._extension_reset_ack_identity = None
        self._extension_reset_failed_identity = None
        self._extension_reset_event.clear()
        reset_recovery_task = self._reset_recovery_task
        if reset_recovery_task is not None and not reset_recovery_task.done():
            reset_recovery_task.cancel()
        self._reset_recovery_task = None

    async def _wait_client_ready(
        self,
        connection: _ClientConnection,
        relay: Relay,
        timeout: float,
    ) -> bool:
        credential = self._credentials.get(connection.client_id)
        if credential is None:
            return False
        if not credential.approved:
            # The first readiness probe must return immediately so the caller can open
            # this client's approval page. A post-pairing probe waits for the click.
            if self._pairing_owner != connection.client_id:
                return False
            loop = asyncio.get_running_loop()
            deadline = loop.time() + timeout
            while not credential.approved:
                if relay.connected and relay.extension_protocol_version != PROTOCOL_VERSION:
                    raise BrowserExtensionBrokerError(
                        "EXTENSION_UPGRADE_REQUIRED",
                        "Reload the current Skyvern Agent extension before approving this agent",
                    )
                remaining = deadline - loop.time()
                if remaining <= 0:
                    return False
                try:
                    await asyncio.wait_for(credential.approval_event.wait(), min(remaining, 0.1))
                except TimeoutError:
                    continue
            timeout = max(0.0, deadline - loop.time())
        return await self._wait_extension_ready(relay, timeout)

    async def _wait_extension_ready(self, relay: Relay, timeout: float) -> bool:
        loop = asyncio.get_running_loop()
        deadline = loop.time() + timeout
        while True:
            if not self._extension_reset_quarantined and relay.connected:
                if relay.extension_protocol_version != PROTOCOL_VERSION:
                    raise BrowserExtensionBrokerError(
                        "EXTENSION_UPGRADE_REQUIRED",
                        "Reload the current Skyvern Agent extension before using this agent",
                    )
                return True
            remaining = deadline - loop.time()
            if remaining <= 0:
                return False
            if not self._extension_reset_quarantined:
                if not await relay.wait_connected(remaining):
                    return False
                if not self._extension_reset_quarantined:
                    return True
                continue
            self._extension_reset_event.clear()
            if not self._extension_reset_quarantined:
                continue
            try:
                await asyncio.wait_for(self._extension_reset_event.wait(), remaining)
            except TimeoutError:
                return False

    @staticmethod
    def _warn_legacy_protocol() -> None:
        LOG.warning(
            "browser_extension_protocol_skew",
            extension_protocol=LEGACY_PROTOCOL_VERSION,
            broker_protocol=PROTOCOL_VERSION,
            fallback="cycle_only",
        )

    async def _is_current(self, connection: _ClientConnection) -> bool:
        if connection.operator:
            return not connection.closed
        async with self._client_lock:
            current = self._clients.get(connection.client_id)
            return current is connection and current.generation == connection.generation and not connection.closed

    async def _send_event(self, connection: _ClientConnection, event: str, params: dict[str, Any]) -> None:
        if connection.closed:
            return
        if connection.pressure_since is not None and self._is_sheddable_event(event, params):
            connection.shed_event_count += 1
            return
        encoded = encode_frame(event_frame(event, params))
        if not await self._reserve_output(connection, len(encoded)):
            await self._connection_closed(connection)
            return
        if connection.closed:
            self._release_output(connection, len(encoded))
            return
        connection.output_queue.put_nowait((encoded, None))

    async def _event_writer(self, connection: _ClientConnection) -> None:
        try:
            while not connection.closed:
                encoded, completion = await connection.output_queue.get()
                connection.output_in_flight_bytes = len(encoded)
                try:
                    await self._write_encoded(connection, encoded)
                    connection.last_output_progress = self.time_source()
                    if completion is not None and not completion.done():
                        completion.set_result(None)
                except BaseException as exc:
                    if completion is not None and not completion.done():
                        completion.set_exception(exc)
                    raise
                finally:
                    if connection.output_in_flight_bytes == len(encoded):
                        connection.output_in_flight_bytes = 0
                        self._release_output(connection, len(encoded))
        except asyncio.CancelledError:
            pass
        except Exception:
            self._mark_connection_closed(connection)
            await self._close_writer(connection.writer)
        finally:
            self._discard_output_queue(connection)

    async def _control_keepalive(self, connection: _ClientConnection) -> None:
        try:
            while not connection.closed:
                await asyncio.sleep(CONTROL_PING_INTERVAL_SECONDS)
                if time.monotonic() - connection.last_inbound >= CONTROL_INBOUND_TIMEOUT_SECONDS:
                    self._mark_connection_closed(connection)
                    await self._close_writer(connection.writer)
                    return
                await self._send(connection, {"v": BROKER_PROTOCOL_VERSION, "type": "ping"})
        except (asyncio.CancelledError, BrowserExtensionNotConnectedError):
            pass

    async def _output_stall_watchdog(self) -> None:
        try:
            while not self._stopping:
                await asyncio.sleep(1.0)
                await self._check_output_stalls()
        except asyncio.CancelledError:
            pass

    async def _check_output_stalls(self) -> None:
        connections = {id(connection): connection for connection in self._connections.values()}
        connections.update((id(connection), connection) for connection in self._clients.values())
        now = self.time_source()
        for connection in tuple(connections.values()):
            pressure_since = connection.pressure_since
            if connection.closed or pressure_since is None:
                continue
            if now - max(pressure_since, connection.last_output_progress) < CLIENT_OUTPUT_STALL_SECONDS:
                continue
            LOG.warning(
                "browser_extension_client_output_stalled",
                client_id=connection.client_id,
                queued_output_bytes=connection.queued_output_bytes,
                shed_event_count=connection.shed_event_count,
            )
            await self._connection_closed(connection)

    async def _send_error(
        self,
        connection: _ClientConnection,
        request_id: str,
        error: BrowserExtensionBrokerError | ExtensionRequestError,
    ) -> None:
        await self._send(connection, error_frame(request_id, error))

    async def _send(self, connection: _ClientConnection, frame: dict[str, Any]) -> None:
        if connection.closed or connection.sender_task is None:
            raise BrowserExtensionNotConnectedError("Broker client disconnected")
        encoded = encode_frame(frame)
        if not await self._reserve_output(connection, len(encoded)):
            await self._connection_closed(connection)
            return
        if connection.closed:
            self._release_output(connection, len(encoded))
            raise BrowserExtensionNotConnectedError("Broker client disconnected")
        completion: asyncio.Future[None] = asyncio.get_running_loop().create_future()
        connection.output_queue.put_nowait((encoded, completion))
        await completion

    async def _write_encoded(self, connection: _ClientConnection, encoded: bytes) -> None:
        async with connection.write_lock:
            if connection.closed:
                raise BrowserExtensionNotConnectedError("Broker client disconnected")
            try:
                connection.writer.write(encoded)
                await connection.writer.drain()
            except (ConnectionError, OSError, asyncio.TimeoutError, RuntimeError) as exc:
                self._mark_connection_closed(connection)
                raise BrowserExtensionNotConnectedError("Broker client disconnected") from exc

    async def _reserve_output(self, connection: _ClientConnection, size: int) -> bool:
        while self._global_output_bytes + size > MAX_GLOBAL_OUTPUT_BYTES:
            victim = self._select_output_victim(connection)
            if victim is None:
                return False
            LOG.warning(
                "browser_extension_client_output_global_cap",
                client_id=connection.client_id,
                reason="global_cap",
                attempted_frame_bytes=size,
                queued_output_frames=connection.queued_output_frames,
                queued_output_bytes=connection.queued_output_bytes,
                global_output_bytes=self._global_output_bytes,
                victim_client_id=victim.client_id,
                victim_queued_output_bytes=victim.queued_output_bytes,
            )
            if victim is connection:
                return False
            await self._connection_closed(victim)
        connection.queued_output_frames += 1
        connection.queued_output_bytes += size
        self._global_output_bytes += size
        if connection.pressure_since is None and connection.queued_output_bytes > MAX_CLIENT_OUTPUT_BYTES:
            connection.pressure_since = self.time_source()
            LOG.warning(
                "browser_extension_client_output_pressure",
                client_id=connection.client_id,
                queued_output_bytes=connection.queued_output_bytes,
            )
        return True

    def _select_output_victim(self, sender: _ClientConnection) -> _ClientConnection | None:
        connections = {id(connection): connection for connection in self._connections.values()}
        connections.update((id(connection), connection) for connection in self._clients.values())
        connections[id(sender)] = sender
        candidates = [
            connection
            for connection in connections.values()
            if not connection.close_started and connection.queued_output_bytes > 0
        ]
        if not candidates:
            return sender if not sender.closed else None
        non_operator_candidates = [connection for connection in candidates if not connection.operator]
        if non_operator_candidates:
            candidates = non_operator_candidates
        return max(
            candidates,
            key=lambda connection: (
                connection.queued_output_bytes,
                connection.pressure_since is not None,
                -connection.pressure_since if connection.pressure_since is not None else float("-inf"),
            ),
        )

    def _release_output(self, connection: _ClientConnection, size: int) -> None:
        connection.queued_output_frames = max(0, connection.queued_output_frames - 1)
        connection.queued_output_bytes = max(0, connection.queued_output_bytes - size)
        self._global_output_bytes = max(0, self._global_output_bytes - size)
        if (
            not connection.closed
            and connection.pressure_since is not None
            and connection.queued_output_bytes <= CLIENT_OUTPUT_RECOVERY_BYTES
        ):
            shed_event_count = connection.shed_event_count
            connection.pressure_since = None
            connection.shed_event_count = 0
            LOG.info(
                "browser_extension_client_output_recovered",
                client_id=connection.client_id,
                queued_output_bytes=connection.queued_output_bytes,
                shed_event_count=shed_event_count,
            )

    @staticmethod
    def _is_sheddable_event(event: str, params: dict[str, Any]) -> bool:
        if event != "extension.event" or params.get("event") != "debugger.event":
            return False
        debugger_params = params.get("params")
        if not isinstance(debugger_params, dict):
            return False
        method = debugger_params.get("method")
        return isinstance(method, str) and method in SHEDDABLE_EVENT_METHODS

    def _discard_output_queue(self, connection: _ClientConnection) -> None:
        while True:
            try:
                encoded, completion = connection.output_queue.get_nowait()
            except asyncio.QueueEmpty:
                break
            self._release_output(connection, len(encoded))
            if completion is not None and not completion.done():
                completion.set_exception(BrowserExtensionNotConnectedError("Broker client disconnected"))
        if connection.output_in_flight_bytes:
            size = connection.output_in_flight_bytes
            connection.output_in_flight_bytes = 0
            self._release_output(connection, size)

    def _mark_connection_closed(self, connection: _ClientConnection) -> None:
        connection.closed = True
        self._discard_output_queue(connection)

    def _release_inbound(self, connection: _ClientConnection, size: int) -> None:
        connection.inbound_bytes = max(0, connection.inbound_bytes - size)
        self._global_inbound_bytes = max(0, self._global_inbound_bytes - size)

    async def _wait_for_tab_request_slot(
        self,
        connection: _ClientConnection,
        tab_id: int,
        timeout: float = 30.0,
        *,
        request_started_at: float | None = None,
    ) -> float:
        if self._stopping:
            raise BrowserExtensionBrokerError("BROKER_STOPPING", "Browser-extension broker is stopping")

        started_at = self.time_source() if request_started_at is None else request_started_at
        request_deadline = started_at + timeout
        queue_deadline = started_at + TAB_REQUEST_QUEUE_WAIT_SECONDS
        queue = self._tab_request_queues.get(tab_id)
        if not queue and self._reserve_tab_request(tab_id):
            remaining = request_deadline - self.time_source()
            if remaining < 0.05:
                self._release_tab_request(tab_id)
                raise BrowserExtensionBrokerError("COMMAND_TIMEOUT", "Request expired while queued")
            return (
                timeout
                if math.isclose(remaining, timeout, abs_tol=TAB_REQUEST_EXACT_TIMEOUT_TOLERANCE_SECONDS)
                else remaining
            )

        queue = self._tab_request_queues.setdefault(tab_id, deque())
        if len(queue) >= MAX_QUEUED_REQUESTS_PER_TAB:
            LOG.info(
                "browser_extension_tab_request_queue_full",
                tab_id=tab_id,
                queued=len(queue),
            )
            raise BrowserExtensionBrokerError(
                "RESOURCE_LIMIT",
                "Too many requests are queued for this tab",
            )

        waiter = _QueuedTabRequest(connection=connection, future=asyncio.get_running_loop().create_future())
        queue.append(waiter)
        try:
            wait_deadline = min(request_deadline, queue_deadline)
            remaining = wait_deadline - self.time_source()
            if remaining <= 0:
                if request_deadline <= queue_deadline:
                    raise BrowserExtensionBrokerError("COMMAND_TIMEOUT", "Request expired while queued")
                raise BrowserExtensionBrokerError(
                    "RESOURCE_LIMIT",
                    "A tab request queue slot did not become available in time",
                )
            try:
                await asyncio.wait_for(asyncio.shield(waiter.future), remaining)
            except TimeoutError as exc:
                if request_deadline <= queue_deadline:
                    raise BrowserExtensionBrokerError("COMMAND_TIMEOUT", "Request expired while queued") from exc
                raise BrowserExtensionBrokerError(
                    "RESOURCE_LIMIT",
                    "A tab request queue slot did not become available in time",
                ) from exc

            current_task = asyncio.current_task()
            if current_task is not None and current_task.cancelling():
                raise asyncio.CancelledError
            if waiter.error is not None:
                raise waiter.error
            if self._stopping:
                raise BrowserExtensionBrokerError("BROKER_STOPPING", "Browser-extension broker is stopping")
            if connection.closed:
                raise BrowserExtensionNotConnectedError("Broker client disconnected while the request was queued")
            lease = self._leases.get(tab_id)
            if lease is None:
                raise BrowserExtensionBrokerError("LEASE_REQUIRED", f"Tab {tab_id} is no longer leased")
            if lease.draining:
                raise BrowserExtensionBrokerError("LEASE_HELD", f"Tab {tab_id} is being released")
            if lease.client_id != connection.client_id:
                raise BrowserExtensionBrokerError("LEASE_HELD", f"Tab {tab_id} is leased to another agent")
            remaining = request_deadline - self.time_source()
            if remaining < 0.05:
                raise BrowserExtensionBrokerError("COMMAND_TIMEOUT", "Request expired while queued")
            if not waiter.slot_reserved:
                raise BrowserExtensionBrokerError(
                    "RESOURCE_LIMIT",
                    "A tab request queue slot was not reserved",
                )
            waiter.admitted = True
            waiter.slot_reserved = False
            return remaining
        finally:
            self._remove_tab_request_waiter(tab_id, waiter)
            if not waiter.future.done():
                waiter.future.cancel()

    def _remove_tab_request_waiter(self, tab_id: int, waiter: _QueuedTabRequest) -> None:
        queue = self._tab_request_queues.get(tab_id)
        if queue is not None:
            with suppress(ValueError):
                queue.remove(waiter)
            if not queue:
                self._tab_request_queues.pop(tab_id, None)
        if waiter.slot_reserved and not waiter.admitted:
            waiter.slot_reserved = False
            self._decrement_tab_request_slot(tab_id)
        self._wake_next_tab_request(tab_id)
        if tab_id not in self._tab_request_queues:
            self._set_tab_idle_if_idle(tab_id)

    def _set_tab_idle_if_idle(self, tab_id: int) -> None:
        if tab_id in self._tab_request_counts:
            return
        idle_event = self._tab_idle_events.pop(tab_id, None)
        if idle_event is not None:
            idle_event.set()

    def _decrement_tab_request_slot(self, tab_id: int) -> None:
        count = self._tab_request_counts.get(tab_id, 0)
        if count <= 1:
            self._tab_request_counts.pop(tab_id, None)
        else:
            self._tab_request_counts[tab_id] = count - 1

    def _wake_next_tab_request(self, tab_id: int) -> None:
        queue = self._tab_request_queues.get(tab_id)
        if queue is None:
            self._set_tab_idle_if_idle(tab_id)
            return
        for waiter in tuple(queue):
            if waiter.admitted:
                with suppress(ValueError):
                    queue.remove(waiter)
                continue
            if waiter.error is not None or waiter.future.cancelled() or (waiter.future.done() and not waiter.notified):
                with suppress(ValueError):
                    queue.remove(waiter)
                if waiter.slot_reserved:
                    waiter.slot_reserved = False
                    self._decrement_tab_request_slot(tab_id)
                continue
            if waiter.slot_reserved or waiter.notified:
                continue
            if not self._reserve_tab_request(tab_id):
                break
            waiter.notified = True
            waiter.slot_reserved = True
            if not waiter.future.done():
                waiter.future.set_result(None)
        if queue is not None and not queue:
            self._tab_request_queues.pop(tab_id, None)
        self._set_tab_idle_if_idle(tab_id)

    def _fail_queued_tab_requests(
        self,
        *,
        error: BrowserExtensionError,
        tab_id: int | None = None,
        connection: _ClientConnection | None = None,
    ) -> None:
        tab_ids: tuple[int, ...]
        if tab_id is not None:
            tab_ids = (tab_id,)
        else:
            tab_ids = tuple(self._tab_request_queues)
        for queued_tab_id in tab_ids:
            queue = self._tab_request_queues.get(queued_tab_id)
            if queue is None:
                continue
            for waiter in queue:
                if connection is not None and waiter.connection is not connection:
                    continue
                waiter.error = error
                if not waiter.future.done():
                    waiter.future.set_result(None)

    def _fail_all_queued_tab_requests(self, error: BrowserExtensionError) -> None:
        self._fail_queued_tab_requests(error=error)

    def _reserve_tab_request(self, tab_id: int) -> bool:
        count = self._tab_request_counts.get(tab_id, 0)
        if count >= MAX_REQUESTS_PER_TAB:
            return False
        self._tab_request_counts[tab_id] = count + 1
        return True

    def _release_tab_request(self, tab_id: int) -> None:
        self._decrement_tab_request_slot(tab_id)
        self._wake_next_tab_request(tab_id)
        self._set_tab_idle_if_idle(tab_id)

    def _verify_peer_uid(self, writer: asyncio.StreamWriter) -> bool:
        transport_socket = writer.get_extra_info("socket")
        if transport_socket is None or not hasattr(os, "getuid"):
            return False
        return peer_uid_from_transport(transport_socket) == os.getuid()

    def _consume_connection_token(self) -> bool:
        now = time.monotonic()
        elapsed = max(0.0, now - self._connection_token_updated)
        self._connection_token_updated = now
        self._connection_tokens = min(32.0, self._connection_tokens + elapsed * 20.0)
        if self._connection_tokens < 1.0:
            return False
        self._connection_tokens -= 1.0
        return True

    def _validate_control_socket(self) -> None:
        socket_stat = self.paths.control_socket.lstat()
        if not stat.S_ISSOCK(socket_stat.st_mode) or stat.S_IMODE(socket_stat.st_mode) != 0o600:
            raise BrowserExtensionBrokerError("UNSAFE_PATH", "Broker control endpoint is not owner-only")
        if hasattr(os, "getuid") and socket_stat.st_uid != os.getuid():
            raise BrowserExtensionBrokerError("UNSAFE_PATH", "Broker control endpoint has the wrong owner")

    def _state(self, *, lifecycle: str, clean_shutdown: bool) -> BrokerState:
        return BrokerState(
            schemaVersion=STATE_SCHEMA_VERSION,
            externalPort=self.port,
            controlEndpoint=str(self.paths.control_socket),
            pid=os.getpid(),
            processStart=self._process_start,
            bootId=self._boot_id,
            lifecycle=lifecycle,
            cleanShutdown=clean_shutdown,
            protocolMin=BROKER_PROTOCOL_VERSION,
            protocolMax=BROKER_PROTOCOL_VERSION,
            features=("multi-client", "tab-leases", "explicit-pairing", "persistent-daemon"),
            brokerGeneration=BROKER_GENERATION,
            buildFingerprint=BROKER_BUILD_FINGERPRINT,
        )


def _is_valid_client_id(value: str) -> bool:
    return len(value) == 32 and all(character in "0123456789abcdef" for character in value)


def _normalize_tab_id(value: object) -> int | None:
    if type(value) is int:
        return value
    if type(value) is float:
        return int(value) if math.isfinite(value) and value.is_integer() else None
    if not isinstance(value, str):
        return None
    normalized = value.strip()
    if not normalized or "_" in normalized:
        return None
    try:
        return int(normalized, 10)
    except ValueError:
        return None


async def run_broker_daemon(port: int, *, base_dir: Path | None = None, ready_fd: int | None = None) -> None:
    inherited_fd = ready_fd
    if inherited_fd is None:
        value = os.environ.get(READY_FD_ENV)
        if value is not None:
            try:
                inherited_fd = int(value)
            except ValueError:
                inherited_fd = None
    paths = ensure_run_directory(port, base_dir=base_dir, prepare_control_endpoint=False)
    spawn_lock: OwnerFileLock | None = None
    server: BrowserExtensionBrokerServer | None = None
    try:
        spawn_lock = _daemon_spawn_lock(paths)
        enable_broker_state_locked(paths)
        server = BrowserExtensionBrokerServer(port, base_dir=base_dir)
        await _start_while_starter_alive(server)
    except BrowserExtensionBrokerError as exc:
        if inherited_fd is not None:
            with suppress(OSError):
                write_readiness(inherited_fd, "ERROR", code=exc.code)
                os.close(inherited_fd)
        raise
    except OSError as exc:
        code = "PORT_IN_USE" if exc.errno == errno.EADDRINUSE else "STARTUP_FAILED"
        if inherited_fd is not None:
            with suppress(OSError):
                write_readiness(inherited_fd, "ERROR", code=code)
                os.close(inherited_fd)
        raise BrowserExtensionBrokerError(code, "Browser-extension broker failed to start") from exc
    else:
        if inherited_fd is not None:
            try:
                write_readiness(inherited_fd, "READY", port=port)
            except OSError as exc:
                await server.stop()
                raise BrowserExtensionBrokerError(
                    "STARTUP_FAILED", "Browser-extension broker readiness receiver disconnected"
                ) from exc
            finally:
                with suppress(OSError):
                    os.close(inherited_fd)
            if server.running:
                try:
                    os.setsid()
                except OSError as exc:
                    await server.stop()
                    raise BrowserExtensionBrokerError(
                        "STARTUP_FAILED", "Browser-extension broker could not detach after readiness"
                    ) from exc
                _detach_startup_stderr()
    finally:
        if spawn_lock is not None:
            spawn_lock.release()

    assert server is not None
    await server._shutdown_event.wait()
    await server.stop()


def _daemon_spawn_lock(paths: BrokerPaths) -> OwnerFileLock:
    inherited_value = os.environ.get(SPAWN_LOCK_FD_ENV)
    if inherited_value is not None:
        try:
            inherited_fd = int(inherited_value)
        except ValueError as exc:
            raise BrowserExtensionBrokerError("STARTUP_FAILED", "Inherited broker lifecycle lock is invalid") from exc
        if inherited_fd < 0:
            raise BrowserExtensionBrokerError("STARTUP_FAILED", "Inherited broker lifecycle lock is invalid")
        return OwnerFileLock.adopt_inherited(paths.spawn_lock, inherited_fd)
    lock = OwnerFileLock(paths.spawn_lock)
    if not lock.acquire():  # pragma: no cover - blocking acquisition returns only after success
        raise BrowserExtensionBrokerError("BROKER_BUSY", "Broker lifecycle lock is busy")
    return lock


async def _start_while_starter_alive(server: BrowserExtensionBrokerServer) -> None:
    starter_pid_value = os.environ.get(STARTER_PID_ENV)
    starter_marker = os.environ.get(STARTER_PROCESS_START_ENV)
    if starter_pid_value is None or starter_marker is None:
        await server.start()
        return
    try:
        starter_pid = int(starter_pid_value)
    except ValueError as exc:
        raise BrowserExtensionBrokerError(
            "STARTUP_FAILED", "Browser-extension broker starter identity is invalid"
        ) from exc
    if starter_pid <= 0 or not starter_marker:
        raise BrowserExtensionBrokerError("STARTUP_FAILED", "Browser-extension broker starter identity is invalid")

    startup_task = asyncio.create_task(server.start())
    watcher_task = asyncio.create_task(_wait_for_starter_exit(starter_pid, starter_marker))
    done, _pending = await asyncio.wait({startup_task, watcher_task}, return_when=asyncio.FIRST_COMPLETED)
    if startup_task in done:
        watcher_task.cancel()
        with suppress(asyncio.CancelledError):
            await watcher_task
        await startup_task
        return

    startup_task.cancel()
    with suppress(asyncio.CancelledError, Exception):
        await startup_task
    await server.stop()
    raise BrowserExtensionBrokerError("STARTER_EXITED", "Browser-extension broker starter exited before readiness")


async def _wait_for_starter_exit(starter_pid: int, starter_marker: str) -> None:
    while os.getppid() == starter_pid and process_identity_matches(starter_pid, starter_marker):
        await asyncio.sleep(0.05)


def _detach_startup_stderr() -> None:
    try:
        null_fd = os.open(os.devnull, os.O_WRONLY)
        try:
            os.dup2(null_fd, 2)
        finally:
            os.close(null_fd)
    except OSError:
        pass
