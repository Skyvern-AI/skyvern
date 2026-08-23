from __future__ import annotations

import hashlib
import json
import os
import secrets
import select
import stat
import tempfile
import threading
import time
from contextlib import suppress
from dataclasses import asdict, dataclass, replace
from pathlib import Path
from typing import Any

import psutil

from skyvern.browser_extension.broker_protocol import BROKER_GENERATION
from skyvern.browser_extension.errors import BrowserExtensionBrokerError

try:
    import fcntl
except ImportError:  # pragma: no cover - POSIX-only M1 module is not imported on Windows runtime paths
    fcntl = None  # type: ignore[assignment]

STATE_SCHEMA_VERSION = 1
LEASES_SCHEMA_VERSION = 1
READINESS_LIMIT = 8 * 1024
STARTUP_LOG_LIMIT = 64 * 1024
STARTUP_TIMEOUT_SECONDS = 15.0
BROKER_BUILD_FINGERPRINT = "browser-extension-broker-m2"
_BACKOFF_SECONDS = (1, 2, 4, 8, 16, 30)


@dataclass(frozen=True, slots=True)
class BrokerPaths:
    run_dir: Path
    spawn_lock: Path
    daemon_lock: Path
    state: Path
    leases: Path
    leases_stale: Path
    startup_failure: Path
    extension_secret: Path
    control_socket: Path
    startup_log: Path


@dataclass(frozen=True, slots=True)
class BrokerState:
    schemaVersion: int
    externalPort: int
    controlEndpoint: str
    pid: int
    processStart: str
    bootId: str
    lifecycle: str
    cleanShutdown: bool
    protocolMin: int
    protocolMax: int
    features: tuple[str, ...]
    brokerGeneration: int
    buildFingerprint: str

    def to_json(self) -> dict[str, Any]:
        value = asdict(self)
        value["features"] = list(self.features)
        return value


@dataclass(frozen=True, slots=True)
class StartupFailure:
    schemaVersion: int
    code: str
    port: int
    requestedGeneration: int
    buildFingerprint: str
    observedStateFingerprint: str
    firstFailure: float
    lastFailure: float
    attemptCount: int
    retryAfter: float

    def to_json(self) -> dict[str, Any]:
        return asdict(self)


class OwnerFileLock:
    def __init__(self, path: Path) -> None:
        self.path = path
        self.fd: int | None = None

    def acquire(self, *, blocking: bool = True) -> bool:
        if fcntl is None:
            raise BrowserExtensionBrokerError("UNSUPPORTED_PLATFORM", "Browser-extension broker requires POSIX")
        fd = _open_owner_file(self.path, create=True)
        operation = fcntl.LOCK_EX | (0 if blocking else fcntl.LOCK_NB)
        try:
            fcntl.flock(fd, operation)
        except BlockingIOError:
            os.close(fd)
            return False
        except BaseException:
            os.close(fd)
            raise
        self.fd = fd
        return True

    def release(self) -> None:
        fd = self.fd
        if fd is None:
            return
        self.fd = None
        if fcntl is not None:
            fcntl.flock(fd, fcntl.LOCK_UN)
        os.close(fd)

    @classmethod
    def adopt_inherited(cls, path: Path, fd: int) -> OwnerFileLock:
        if fcntl is None:
            raise BrowserExtensionBrokerError("UNSUPPORTED_PLATFORM", "Browser-extension broker requires POSIX")
        lock = cls(path)
        try:
            file_stat = os.fstat(fd)
            path_stat = path.lstat()
            if (
                not stat.S_ISREG(file_stat.st_mode)
                or (file_stat.st_dev, file_stat.st_ino) != (path_stat.st_dev, path_stat.st_ino)
                or (hasattr(os, "getuid") and file_stat.st_uid != os.getuid())
                or stat.S_IMODE(file_stat.st_mode) != 0o600
            ):
                raise BrowserExtensionBrokerError("UNSAFE_PATH", "Inherited broker lifecycle lock is unsafe")
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            os.close(fd)
            raise BrowserExtensionBrokerError("BROKER_BUSY", "Broker lifecycle lock is busy") from exc
        except BaseException:
            try:
                os.close(fd)
            except OSError:
                pass
            raise
        lock.fd = fd
        return lock

    def handoff_to_child(self) -> None:
        fd = self.fd
        if fd is None:
            raise BrowserExtensionBrokerError("STARTUP_FAILED", "Broker lifecycle lock is not held")
        self.fd = None
        os.close(fd)

    def __enter__(self) -> OwnerFileLock:
        if not self.acquire():  # pragma: no cover - a blocking lock returns only after acquisition
            raise BrowserExtensionBrokerError("BROKER_BUSY", "Broker lifecycle lock is busy")
        return self

    def __exit__(self, _exc_type: object, _exc: object, _traceback: object) -> None:
        self.release()


def broker_paths(port: int, *, base_dir: Path | None = None) -> BrokerPaths:
    if type(port) is not int or not 1 <= port <= 65535:
        raise BrowserExtensionBrokerError("INVALID_PORT", "Browser-extension broker port is invalid")
    base = base_dir if base_dir is not None else Path.home() / ".skyvern" / "run" / "browser-extension"
    run_dir = base / str(port)
    control_socket = run_dir / "control.sock"
    return BrokerPaths(
        run_dir=run_dir,
        spawn_lock=run_dir / "spawn.lock",
        daemon_lock=run_dir / "daemon.lock",
        state=run_dir / "state.json",
        leases=run_dir / "leases.json",
        leases_stale=run_dir / "leases.stale.json",
        startup_failure=run_dir / "startup-failure.json",
        extension_secret=run_dir / "extension.secret",
        control_socket=control_socket,
        startup_log=run_dir / "startup.log",
    )


def ensure_run_directory(
    port: int,
    *,
    base_dir: Path | None = None,
    prepare_control_endpoint: bool = True,
) -> BrokerPaths:
    """Create and validate the owner-only run directory required by spec-v3.md lines 38-46."""
    paths = broker_paths(port, base_dir=base_dir)
    parent = paths.run_dir.parent
    parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    parent_stat = _validate_owner_directory(parent)
    try:
        paths.run_dir.mkdir(mode=0o700)
        _fsync_directory(parent)
    except FileExistsError:
        pass
    current_parent_stat = _validate_owner_directory(parent)
    if (parent_stat.st_dev, parent_stat.st_ino) != (current_parent_stat.st_dev, current_parent_stat.st_ino):
        raise BrowserExtensionBrokerError("UNSAFE_PATH", "Broker run directory parent identity changed")
    _validate_owner_directory(paths.run_dir)
    if prepare_control_endpoint and len(os.fsencode(paths.control_socket)) >= 100:
        paths = _prepare_short_control_endpoint(paths)
    if paths.control_socket.parent != paths.run_dir:
        _validate_owner_directory(paths.control_socket.parent)
    return paths


def resolve_control_endpoint(paths: BrokerPaths, endpoint: str) -> BrokerPaths:
    candidate = Path(endpoint)
    default_endpoint = paths.run_dir / "control.sock"
    if candidate == default_endpoint and len(os.fsencode(default_endpoint)) < 100:
        return replace(paths, control_socket=candidate)
    if len(os.fsencode(default_endpoint)) < 100:
        raise BrowserExtensionBrokerError("UNSAFE_STATE", "Broker control endpoint does not match its run directory")

    shared_root = _shared_temporary_root()
    uid = os.getuid() if hasattr(os, "getuid") else 0
    prefix = f"skyvern-browser-extension-{uid}-"
    parent = candidate.parent
    suffix = parent.name.removeprefix(prefix)
    if (
        candidate.name != "control.sock"
        or parent.parent != shared_root
        or not parent.name.startswith(prefix)
        or len(suffix) < 16
    ):
        raise BrowserExtensionBrokerError("UNSAFE_STATE", "Broker control endpoint is invalid")
    _validate_owner_directory(parent)
    return replace(paths, control_socket=candidate)


def _prepare_short_control_endpoint(paths: BrokerPaths) -> BrokerPaths:
    state = read_broker_state(paths)
    if state is not None:
        try:
            return resolve_control_endpoint(paths, state.controlEndpoint)
        except BrowserExtensionBrokerError:
            pass

    shared_root = _shared_temporary_root()
    uid = os.getuid() if hasattr(os, "getuid") else 0
    prefix = f"skyvern-browser-extension-{uid}-"
    while True:
        directory = shared_root / f"{prefix}{secrets.token_urlsafe(18)}"
        try:
            directory.mkdir(mode=0o700)
        except FileExistsError:
            continue
        _fsync_directory(shared_root)
        _validate_owner_directory(directory)
        return replace(paths, control_socket=directory / "control.sock")


def validate_run_directory(paths: BrokerPaths, *, expected_identity: tuple[int, int] | None = None) -> None:
    run_stat = _validate_owner_directory(paths.run_dir)
    if expected_identity is not None and (run_stat.st_dev, run_stat.st_ino) != expected_identity:
        raise BrowserExtensionBrokerError("UNSAFE_PATH", "Broker run directory identity changed")
    if paths.control_socket.parent != paths.run_dir:
        _validate_owner_directory(paths.control_socket.parent)


def run_directory_identity(paths: BrokerPaths) -> tuple[int, int]:
    run_stat = _validate_owner_directory(paths.run_dir)
    return run_stat.st_dev, run_stat.st_ino


def _read_lease_journal(paths: BrokerPaths) -> list[Any]:
    journal = read_owner_json(paths.leases)
    if not isinstance(journal, dict) or type(journal.get("schemaVersion")) is not int:
        raise BrowserExtensionBrokerError("UNSAFE_STATE", "Broker lease journal is invalid")
    if journal["schemaVersion"] != LEASES_SCHEMA_VERSION:
        raise BrowserExtensionBrokerError("UNSAFE_STATE", "Broker lease journal has an unsupported schema")
    leases = journal.get("leases")
    if not isinstance(leases, list):
        raise BrowserExtensionBrokerError("UNSAFE_STATE", "Broker lease journal is invalid")
    return leases


def validate_lease_journal(paths: BrokerPaths) -> None:
    """Create a missing journal and fail closed on unreadable schemas without touching live entries."""
    if not paths.leases.exists():
        atomic_write_json(paths.leases, {"schemaVersion": LEASES_SCHEMA_VERSION, "leases": []})
        return
    _read_lease_journal(paths)


def reset_lease_journal(paths: BrokerPaths) -> int:
    """Start a daemon lifetime with an empty journal; archive crash residue instead of failing closed.

    Safe only while holding daemon.lock: every daemon lifetime begins with a full extension
    reset, so surviving lease entries describe browser state that no longer exists. The
    residue is archived to leases.stale.json for the operator and the count is returned.
    """
    if not paths.leases.exists():
        atomic_write_json(paths.leases, {"schemaVersion": LEASES_SCHEMA_VERSION, "leases": []})
        return 0
    try:
        leases = _read_lease_journal(paths)
    except BrowserExtensionBrokerError:
        # A torn or foreign journal must not brick daemon startup; move it aside
        # for the operator and start clean.
        with suppress(OSError):
            os.replace(paths.leases, paths.leases_stale.with_suffix(".corrupt"))
        atomic_write_json(paths.leases, {"schemaVersion": LEASES_SCHEMA_VERSION, "leases": []})
        return 0
    if leases:
        atomic_write_json(paths.leases_stale, {"schemaVersion": LEASES_SCHEMA_VERSION, "leases": leases})
        atomic_write_json(paths.leases, {"schemaVersion": LEASES_SCHEMA_VERSION, "leases": []})
    return len(leases)


def write_lease_journal(paths: BrokerPaths, leases: list[dict[str, Any]]) -> None:
    atomic_write_json(paths.leases, {"schemaVersion": LEASES_SCHEMA_VERSION, "leases": leases})


def read_broker_state(paths: BrokerPaths) -> BrokerState | None:
    try:
        value = read_owner_json(paths.state)
    except FileNotFoundError:
        return None
    try:
        features = value["features"]
        if not isinstance(features, list) or not all(isinstance(item, str) for item in features):
            raise ValueError
        state = BrokerState(
            schemaVersion=_required_int(value, "schemaVersion"),
            externalPort=_required_int(value, "externalPort"),
            controlEndpoint=_required_string(value, "controlEndpoint"),
            pid=_required_int(value, "pid"),
            processStart=_required_string(value, "processStart"),
            bootId=_required_string(value, "bootId"),
            lifecycle=_required_string(value, "lifecycle"),
            cleanShutdown=_required_bool(value, "cleanShutdown"),
            protocolMin=_required_int(value, "protocolMin"),
            protocolMax=_required_int(value, "protocolMax"),
            features=tuple(features),
            brokerGeneration=_required_int(value, "brokerGeneration"),
            buildFingerprint=_required_string(value, "buildFingerprint"),
        )
    except (KeyError, TypeError, ValueError) as exc:
        raise BrowserExtensionBrokerError("UNSAFE_STATE", "Broker state is invalid") from exc
    if state.schemaVersion != STATE_SCHEMA_VERSION or state.externalPort <= 0:
        raise BrowserExtensionBrokerError("UNSAFE_STATE", "Broker state is invalid")
    return state


def publish_broker_state(paths: BrokerPaths, state: BrokerState) -> None:
    atomic_write_json(paths.state, state.to_json())


def state_fingerprint(paths: BrokerPaths) -> str:
    try:
        payload = read_owner_bytes(paths.state, limit=1024 * 1024)
    except FileNotFoundError:
        return "missing"
    except BrowserExtensionBrokerError:
        return "invalid"
    return hashlib.sha256(payload).hexdigest()


def record_startup_failure(
    paths: BrokerPaths,
    *,
    code: str,
    port: int,
    observed_state_fingerprint: str,
    requested_generation: int = BROKER_GENERATION,
    build_fingerprint: str = BROKER_BUILD_FINGERPRINT,
    now: float | None = None,
) -> StartupFailure:
    timestamp = time.time() if now is None else now
    previous = read_startup_failure(paths)
    continues_sequence = (
        previous is not None
        and previous.port == port
        and previous.requestedGeneration == requested_generation
        and previous.buildFingerprint == build_fingerprint
    )
    attempt = previous.attemptCount + 1 if continues_sequence and previous is not None else 1
    delay = _BACKOFF_SECONDS[min(attempt - 1, len(_BACKOFF_SECONDS) - 1)]
    failure = StartupFailure(
        schemaVersion=STATE_SCHEMA_VERSION,
        code=code,
        port=port,
        requestedGeneration=requested_generation,
        buildFingerprint=build_fingerprint,
        observedStateFingerprint=observed_state_fingerprint,
        firstFailure=previous.firstFailure if continues_sequence and previous is not None else timestamp,
        lastFailure=timestamp,
        attemptCount=attempt,
        retryAfter=timestamp + delay,
    )
    atomic_write_json(paths.startup_failure, failure.to_json())
    return failure


def read_startup_failure(paths: BrokerPaths) -> StartupFailure | None:
    try:
        value = read_owner_json(paths.startup_failure)
    except FileNotFoundError:
        return None
    try:
        return StartupFailure(
            schemaVersion=_required_int(value, "schemaVersion"),
            code=_required_string(value, "code"),
            port=_required_int(value, "port"),
            requestedGeneration=_required_int(value, "requestedGeneration"),
            buildFingerprint=_required_string(value, "buildFingerprint"),
            observedStateFingerprint=_required_string(value, "observedStateFingerprint"),
            firstFailure=_required_number(value, "firstFailure"),
            lastFailure=_required_number(value, "lastFailure"),
            attemptCount=_required_int(value, "attemptCount"),
            retryAfter=_required_number(value, "retryAfter"),
        )
    except (KeyError, TypeError, ValueError) as exc:
        raise BrowserExtensionBrokerError("UNSAFE_STATE", "Broker startup-failure state is invalid") from exc


def matching_startup_failure(
    paths: BrokerPaths,
    *,
    observed_state_fingerprint: str,
    requested_generation: int = BROKER_GENERATION,
    build_fingerprint: str = BROKER_BUILD_FINGERPRINT,
    now: float | None = None,
) -> StartupFailure | None:
    failure = read_startup_failure(paths)
    timestamp = time.time() if now is None else now
    if failure is None:
        return None
    if (
        failure.requestedGeneration != requested_generation
        or failure.buildFingerprint != build_fingerprint
        or failure.observedStateFingerprint != observed_state_fingerprint
        or failure.retryAfter <= timestamp
    ):
        return None
    return failure


def clear_startup_failure(paths: BrokerPaths) -> None:
    _unlink_owner_file(paths.startup_failure)


def atomic_write_json(path: Path, value: Any) -> None:
    payload = json.dumps(value, separators=(",", ":"), sort_keys=True, ensure_ascii=False, allow_nan=False).encode()
    directory_stat = _validate_owner_directory(path.parent)
    fd, temporary_name = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.")
    temporary = Path(temporary_name)
    try:
        _validate_publication_target(path)
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, "wb") as output:
            fd = -1
            output.write(payload)
            output.flush()
            os.fsync(output.fileno())
        current = _validate_owner_directory(path.parent)
        if (directory_stat.st_dev, directory_stat.st_ino) != (current.st_dev, current.st_ino):
            raise BrowserExtensionBrokerError("UNSAFE_PATH", "Broker run directory changed during publication")
        os.replace(temporary, path)
        _fsync_directory(path.parent)
    finally:
        if fd >= 0:
            os.close(fd)
        temporary.unlink(missing_ok=True)


def atomic_write_secret(path: Path, secret: str) -> None:
    if not secret:
        raise BrowserExtensionBrokerError("INVALID_SECRET", "Broker extension credential is empty")
    directory_stat = _validate_owner_directory(path.parent)
    fd, temporary_name = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.")
    temporary = Path(temporary_name)
    try:
        _validate_publication_target(path)
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as output:
            fd = -1
            output.write(secret)
            output.flush()
            os.fsync(output.fileno())
        current = _validate_owner_directory(path.parent)
        if (directory_stat.st_dev, directory_stat.st_ino) != (current.st_dev, current.st_ino):
            raise BrowserExtensionBrokerError("UNSAFE_PATH", "Broker run directory changed during publication")
        os.replace(temporary, path)
        _fsync_directory(path.parent)
    finally:
        if fd >= 0:
            os.close(fd)
        temporary.unlink(missing_ok=True)


def read_extension_secret(paths: BrokerPaths) -> str:
    try:
        secret = read_owner_bytes(paths.extension_secret, limit=4096).decode("utf-8").strip()
    except FileNotFoundError as exc:
        raise BrowserExtensionBrokerError(
            "BROKER_NOT_ENABLED",
            "Browser-extension broker is not enabled; run `skyvern browser extension-broker-enable`",
        ) from exc
    except UnicodeDecodeError as exc:
        raise BrowserExtensionBrokerError("UNSAFE_STATE", "Broker extension credential is invalid") from exc
    if not secret:
        raise BrowserExtensionBrokerError("UNSAFE_STATE", "Broker extension credential is invalid")
    return secret


def enable_broker_state(port: int, *, base_dir: Path | None = None) -> tuple[BrokerPaths, str]:
    """Copy or create the M1 extension credential without returning its value (spec-v3.md lines 199-205)."""
    paths = ensure_run_directory(port, base_dir=base_dir, prepare_control_endpoint=False)
    with OwnerFileLock(paths.spawn_lock):
        return enable_broker_state_locked(paths)


def enable_broker_state_locked(paths: BrokerPaths) -> tuple[BrokerPaths, str]:
    if os.environ.get("SKYVERN_BROWSER_EXTENSION_TOKEN", "").strip():
        raise BrowserExtensionBrokerError(
            "BROKER_ENV_SECRET_REJECTED",
            "Unset SKYVERN_BROWSER_EXTENSION_TOKEN before enabling broker mode",
        )
    validate_lease_journal(paths)
    legacy_path = _legacy_credential_path()
    try:
        extension_secret = read_extension_secret(paths)
    except BrowserExtensionBrokerError as exc:
        if exc.code != "BROKER_NOT_ENABLED":
            raise
    else:
        try:
            legacy_secret = read_owner_bytes(legacy_path, limit=4096).decode("utf-8").strip()
        except FileNotFoundError:
            _ensure_legacy_credential_directory(legacy_path.parent)
            _atomic_write_legacy_secret(legacy_path, extension_secret)
        except UnicodeDecodeError as exc:
            raise BrowserExtensionBrokerError("UNSAFE_STATE", "Legacy extension credential is invalid") from exc
        else:
            if not legacy_secret or not secrets.compare_digest(legacy_secret, extension_secret):
                raise BrowserExtensionBrokerError("UNSAFE_STATE", "Legacy extension credential does not match broker")
        clear_startup_failure(paths)
        return paths, "existing"

    _ensure_legacy_credential_directory(legacy_path.parent)
    try:
        secret = read_owner_bytes(legacy_path, limit=4096).decode("utf-8").strip()
    except FileNotFoundError:
        secret = secrets.token_urlsafe(32)
        _atomic_write_legacy_secret(legacy_path, secret)
        source = "created"
    except UnicodeDecodeError as exc:
        raise BrowserExtensionBrokerError("UNSAFE_STATE", "Legacy extension credential is invalid") from exc
    else:
        if not secret:
            raise BrowserExtensionBrokerError("UNSAFE_STATE", "Legacy extension credential is invalid")
        source = "copied"

    atomic_write_secret(paths.extension_secret, secret)
    clear_startup_failure(paths)
    return paths, source


def _legacy_credential_path() -> Path:
    return Path.home() / ".skyvern" / "browser_extension_token"


def _ensure_legacy_credential_directory(path: Path) -> None:
    try:
        path.mkdir(mode=0o700, parents=True)
    except FileExistsError:
        pass
    path_stat = path.lstat()
    if (
        not stat.S_ISDIR(path_stat.st_mode)
        or path.is_symlink()
        or (hasattr(os, "getuid") and path_stat.st_uid != os.getuid())
        or stat.S_IMODE(path_stat.st_mode) & 0o022
    ):
        raise BrowserExtensionBrokerError("UNSAFE_PATH", "Legacy extension credential directory is unsafe")


def _atomic_write_legacy_secret(path: Path, secret: str) -> None:
    directory_stat = path.parent.lstat()
    fd, temporary_name = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.")
    temporary = Path(temporary_name)
    try:
        _validate_publication_target(path)
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as output:
            fd = -1
            output.write(secret)
            output.flush()
            os.fsync(output.fileno())
        current = path.parent.lstat()
        if (directory_stat.st_dev, directory_stat.st_ino) != (current.st_dev, current.st_ino):
            raise BrowserExtensionBrokerError("UNSAFE_PATH", "Legacy extension credential directory changed")
        os.replace(temporary, path)
        _fsync_directory(path.parent)
    finally:
        if fd >= 0:
            os.close(fd)
        temporary.unlink(missing_ok=True)


def read_owner_json(path: Path) -> dict[str, Any]:
    payload = read_owner_bytes(path, limit=1024 * 1024)
    try:
        value = json.loads(payload)
    except (json.JSONDecodeError, UnicodeDecodeError) as exc:
        raise BrowserExtensionBrokerError("UNSAFE_STATE", f"Broker artifact {path.name} is invalid") from exc
    if not isinstance(value, dict):
        raise BrowserExtensionBrokerError("UNSAFE_STATE", f"Broker artifact {path.name} is invalid")
    return value


def read_owner_bytes(path: Path, *, limit: int) -> bytes:
    fd = _open_owner_file(path, create=False)
    try:
        chunks: list[bytes] = []
        total = 0
        while True:
            chunk = os.read(fd, min(65536, limit + 1 - total))
            if not chunk:
                return b"".join(chunks)
            chunks.append(chunk)
            total += len(chunk)
            if total > limit:
                raise BrowserExtensionBrokerError("UNSAFE_STATE", f"Broker artifact {path.name} is too large")
    finally:
        os.close(fd)


def write_readiness(fd: int, status: str, **fields: Any) -> None:
    payload = json.dumps({"status": status, **fields}, separators=(",", ":"), allow_nan=False).encode() + b"\n"
    if len(payload) > READINESS_LIMIT:
        payload = b'{"status":"ERROR","code":"INTERNAL"}\n'
    view = memoryview(payload)
    while view:
        written = os.write(fd, view)
        view = view[written:]


def read_readiness(fd: int, *, timeout: float = STARTUP_TIMEOUT_SECONDS) -> dict[str, Any]:
    deadline = time.monotonic() + timeout
    payload = bytearray()
    while True:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise BrowserExtensionBrokerError("STARTUP_TIMEOUT", "Browser-extension broker startup timed out")
        readable, _, _ = select.select([fd], [], [], remaining)
        if not readable:
            raise BrowserExtensionBrokerError("STARTUP_TIMEOUT", "Browser-extension broker startup timed out")
        chunk = os.read(fd, min(1024, READINESS_LIMIT + 1 - len(payload)))
        if not chunk:
            break
        payload.extend(chunk)
        if len(payload) > READINESS_LIMIT:
            raise BrowserExtensionBrokerError("INVALID_READINESS", "Broker readiness response is too large")
        if b"\n" in chunk:
            break
    line = bytes(payload).split(b"\n", 1)[0]
    try:
        value = json.loads(line)
    except (json.JSONDecodeError, UnicodeDecodeError) as exc:
        raise BrowserExtensionBrokerError("INVALID_READINESS", "Broker readiness response is invalid") from exc
    if not isinstance(value, dict) or value.get("status") not in {"READY", "ERROR"}:
        raise BrowserExtensionBrokerError("INVALID_READINESS", "Broker readiness response is invalid")
    if value["status"] == "READY":
        port = value.get("port")
        if type(port) is not int or not 1 <= port <= 65535:
            raise BrowserExtensionBrokerError("INVALID_READINESS", "Broker readiness response is invalid")
    else:
        code = value.get("code")
        if not isinstance(code, str) or not code:
            raise BrowserExtensionBrokerError("INVALID_READINESS", "Broker readiness response is invalid")
    return value


def prepare_startup_log(paths: BrokerPaths) -> tuple[int, threading.Thread]:
    log_fd, existing = _prepare_startup_log_file(paths)
    try:
        read_fd, write_fd = os.pipe()
    except BaseException:
        os.close(log_fd)
        raise
    drain_thread = threading.Thread(
        target=_drain_startup_log,
        args=(read_fd, log_fd, existing),
        name="skyvern-broker-startup-log",
        daemon=True,
    )
    try:
        drain_thread.start()
    except BaseException:
        os.close(read_fd)
        os.close(write_fd)
        os.close(log_fd)
        raise
    return write_fd, drain_thread


def _prepare_startup_log_file(paths: BrokerPaths) -> tuple[int, bytes]:
    flags = os.O_RDWR | os.O_CREAT | getattr(os, "O_NOFOLLOW", 0)
    try:
        fd = os.open(paths.startup_log, flags, 0o600)
    except OSError as exc:
        raise BrowserExtensionBrokerError("UNSAFE_PATH", "Broker startup log is unsafe") from exc
    try:
        os.fchmod(fd, 0o600)
        file_stat = os.fstat(fd)
        path_stat = paths.startup_log.lstat()
        if (
            not stat.S_ISREG(file_stat.st_mode)
            or (file_stat.st_dev, file_stat.st_ino) != (path_stat.st_dev, path_stat.st_ino)
            or (hasattr(os, "getuid") and file_stat.st_uid != os.getuid())
            or stat.S_IMODE(file_stat.st_mode) != 0o600
        ):
            raise BrowserExtensionBrokerError("UNSAFE_PATH", "Broker startup log is unsafe")
        tail_size = min(file_stat.st_size, STARTUP_LOG_LIMIT // 2)
        if tail_size:
            os.lseek(fd, -tail_size, os.SEEK_END)
            existing = os.read(fd, tail_size)
        else:
            existing = b""
        _rewrite_startup_log(fd, existing)
        return fd, existing
    except BaseException:
        os.close(fd)
        raise


def _drain_startup_log(read_fd: int, log_fd: int, existing: bytes) -> None:
    buffered = bytearray(existing)
    try:
        while chunk := os.read(read_fd, 8192):
            buffered.extend(chunk)
            if len(buffered) > STARTUP_LOG_LIMIT:
                del buffered[: len(buffered) - STARTUP_LOG_LIMIT]
            _rewrite_startup_log(log_fd, buffered)
    finally:
        os.close(read_fd)
        os.close(log_fd)


def _rewrite_startup_log(fd: int, payload: bytes | bytearray) -> None:
    os.lseek(fd, 0, os.SEEK_SET)
    os.ftruncate(fd, 0)
    view = memoryview(payload)
    while view:
        view = view[os.write(fd, view) :]


def bound_startup_log(paths: BrokerPaths) -> None:
    flags = os.O_RDWR | getattr(os, "O_NOFOLLOW", 0)
    try:
        fd = os.open(paths.startup_log, flags)
    except FileNotFoundError:
        return
    try:
        file_stat = os.fstat(fd)
        path_stat = paths.startup_log.lstat()
        if (
            not stat.S_ISREG(file_stat.st_mode)
            or (file_stat.st_dev, file_stat.st_ino) != (path_stat.st_dev, path_stat.st_ino)
            or (hasattr(os, "getuid") and file_stat.st_uid != os.getuid())
            or stat.S_IMODE(file_stat.st_mode) != 0o600
        ):
            raise BrowserExtensionBrokerError("UNSAFE_PATH", "Broker startup log is unsafe")
        if file_stat.st_size > STARTUP_LOG_LIMIT:
            os.ftruncate(fd, STARTUP_LOG_LIMIT)
            os.fsync(fd)
    finally:
        os.close(fd)


def current_process_start_marker() -> str:
    return f"{psutil.Process(os.getpid()).create_time():.6f}"


def process_identity_matches(pid: int, marker: str) -> bool:
    try:
        process = psutil.Process(pid)
        return f"{process.create_time():.6f}" == marker and (
            os.name != "posix" or process.status() != psutil.STATUS_ZOMBIE
        )
    except (psutil.Error, OSError, ValueError):
        return False


def _open_owner_file(path: Path, *, create: bool) -> int:
    flags = os.O_RDWR if create else os.O_RDONLY
    if create:
        flags |= os.O_CREAT
    flags |= getattr(os, "O_NOFOLLOW", 0)
    try:
        fd = os.open(path, flags, 0o600)
    except FileNotFoundError:
        raise
    except OSError as exc:
        raise BrowserExtensionBrokerError("UNSAFE_PATH", f"Broker artifact {path.name} is unsafe") from exc
    try:
        file_stat = os.fstat(fd)
        path_stat = path.lstat()
        if not stat.S_ISREG(file_stat.st_mode):
            raise BrowserExtensionBrokerError("UNSAFE_PATH", f"Broker artifact {path.name} must be a regular file")
        if (file_stat.st_dev, file_stat.st_ino) != (path_stat.st_dev, path_stat.st_ino):
            raise BrowserExtensionBrokerError("UNSAFE_PATH", f"Broker artifact {path.name} changed during validation")
        if hasattr(os, "getuid") and file_stat.st_uid != os.getuid():
            raise BrowserExtensionBrokerError("UNSAFE_PATH", f"Broker artifact {path.name} has the wrong owner")
        if stat.S_IMODE(file_stat.st_mode) != 0o600:
            raise BrowserExtensionBrokerError("UNSAFE_PATH", f"Broker artifact {path.name} must have mode 0600")
        return fd
    except BaseException:
        os.close(fd)
        raise


def _validate_publication_target(path: Path) -> None:
    try:
        path_stat = path.lstat()
    except FileNotFoundError:
        return
    if not stat.S_ISREG(path_stat.st_mode) or path.is_symlink():
        raise BrowserExtensionBrokerError("UNSAFE_PATH", f"Broker artifact {path.name} is unsafe")
    if hasattr(os, "getuid") and path_stat.st_uid != os.getuid():
        raise BrowserExtensionBrokerError("UNSAFE_PATH", f"Broker artifact {path.name} has the wrong owner")
    if stat.S_IMODE(path_stat.st_mode) != 0o600:
        raise BrowserExtensionBrokerError("UNSAFE_PATH", f"Broker artifact {path.name} must have mode 0600")


def _shared_temporary_root() -> Path:
    candidate = Path("/private/tmp") if Path("/private/tmp").exists() else Path("/tmp")
    try:
        path_stat = candidate.lstat()
    except OSError as exc:
        raise BrowserExtensionBrokerError("UNSAFE_PATH", "Shared temporary directory cannot be inspected") from exc
    if (
        not stat.S_ISDIR(path_stat.st_mode)
        or candidate.is_symlink()
        or path_stat.st_uid != 0
        or not path_stat.st_mode & stat.S_ISVTX
    ):
        raise BrowserExtensionBrokerError("UNSAFE_PATH", "Shared temporary directory is unsafe")
    flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0)
    fd = os.open(candidate, flags)
    try:
        opened_stat = os.fstat(fd)
        if (path_stat.st_dev, path_stat.st_ino) != (opened_stat.st_dev, opened_stat.st_ino):
            raise BrowserExtensionBrokerError("UNSAFE_PATH", "Shared temporary directory changed during validation")
    finally:
        os.close(fd)
    return candidate


def _validate_owner_directory(path: Path) -> os.stat_result:
    try:
        path_stat = path.lstat()
    except OSError as exc:
        raise BrowserExtensionBrokerError("UNSAFE_PATH", "Broker run directory cannot be inspected") from exc
    if not stat.S_ISDIR(path_stat.st_mode) or path.is_symlink():
        raise BrowserExtensionBrokerError("UNSAFE_PATH", "Broker run directory must be a real directory")
    if hasattr(os, "getuid") and path_stat.st_uid != os.getuid():
        raise BrowserExtensionBrokerError("UNSAFE_PATH", "Broker run directory has the wrong owner")
    if stat.S_IMODE(path_stat.st_mode) != 0o700:
        raise BrowserExtensionBrokerError("UNSAFE_PATH", "Broker run directory must have mode 0700")
    flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0)
    try:
        fd = os.open(path, flags)
    except OSError as exc:
        raise BrowserExtensionBrokerError("UNSAFE_PATH", "Broker run directory is unsafe") from exc
    try:
        opened_stat = os.fstat(fd)
        if (path_stat.st_dev, path_stat.st_ino) != (opened_stat.st_dev, opened_stat.st_ino):
            raise BrowserExtensionBrokerError("UNSAFE_PATH", "Broker run directory changed during validation")
    finally:
        os.close(fd)
    return path_stat


def _fsync_directory(path: Path) -> None:
    flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0)
    fd = os.open(path, flags)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def _unlink_owner_file(path: Path) -> None:
    try:
        path_stat = path.lstat()
    except FileNotFoundError:
        return
    if not stat.S_ISREG(path_stat.st_mode) or path.is_symlink():
        raise BrowserExtensionBrokerError("UNSAFE_PATH", f"Broker artifact {path.name} is unsafe")
    if hasattr(os, "getuid") and path_stat.st_uid != os.getuid():
        raise BrowserExtensionBrokerError("UNSAFE_PATH", f"Broker artifact {path.name} has the wrong owner")
    path.unlink()
    _fsync_directory(path.parent)


def remove_control_socket(paths: BrokerPaths) -> None:
    try:
        socket_stat = paths.control_socket.lstat()
    except FileNotFoundError:
        return
    if not stat.S_ISSOCK(socket_stat.st_mode) or paths.control_socket.is_symlink():
        raise BrowserExtensionBrokerError("UNSAFE_PATH", "Broker control endpoint is unsafe")
    if hasattr(os, "getuid") and socket_stat.st_uid != os.getuid():
        raise BrowserExtensionBrokerError("UNSAFE_PATH", "Broker control endpoint has the wrong owner")
    paths.control_socket.unlink()
    _fsync_directory(paths.control_socket.parent)


def _required_string(value: dict[str, Any], key: str) -> str:
    item = value[key]
    if not isinstance(item, str) or not item:
        raise ValueError
    return item


def _required_int(value: dict[str, Any], key: str) -> int:
    item = value[key]
    if type(item) is not int:
        raise ValueError
    return item


def _required_bool(value: dict[str, Any], key: str) -> bool:
    item = value[key]
    if type(item) is not bool:
        raise ValueError
    return item


def _required_number(value: dict[str, Any], key: str) -> float:
    item = value[key]
    if isinstance(item, bool) or not isinstance(item, (int, float)):
        raise ValueError
    return float(item)
