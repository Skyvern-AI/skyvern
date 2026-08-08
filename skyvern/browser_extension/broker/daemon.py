from __future__ import annotations

import argparse
import asyncio
import errno
import os
import signal
import subprocess
import sys
import time
from contextlib import suppress
from pathlib import Path
from typing import IO, Any

import structlog

from skyvern.browser_extension.auth import load_or_create_pairing_token
from skyvern.browser_extension.broker.server import BrokerServer
from skyvern.browser_extension.errors import BrowserExtensionError

LOG = structlog.get_logger(__name__)

DAEMON_MODULE = "skyvern.browser_extension.broker.daemon"
IDLE_TIMEOUT_ENV = "SKYVERN_BROWSER_EXTENSION_BROKER_IDLE_SECONDS"
DEFAULT_IDLE_TIMEOUT_SECONDS = 300.0

_SPAWN_LOCK_NAME = "browser_extension_broker.lock"
_LOG_NAME = "browser_extension_broker.log"
_MAX_LOG_BYTES = 4 * 1024 * 1024
_SPAWN_COOLDOWN_SECONDS = 2.0


def _state_dir() -> Path:
    return Path.home() / ".skyvern"


def resolve_idle_timeout_seconds() -> float:
    raw = os.environ.get(IDLE_TIMEOUT_ENV)
    if raw is None:
        return DEFAULT_IDLE_TIMEOUT_SECONDS
    try:
        value = float(raw)
    except ValueError as exc:
        raise BrowserExtensionError(f"{IDLE_TIMEOUT_ENV} must be a number of seconds") from exc
    if value <= 0:
        raise BrowserExtensionError(f"{IDLE_TIMEOUT_ENV} must be greater than zero")
    return value


def spawn_daemon(port: int) -> bool:
    """Launch a detached broker daemon.

    Returns False when another process is already bringing one up — the caller should just keep
    retrying its connection. Losing this race stays harmless regardless: only one daemon can bind
    the port and the rest exit quietly. The lock and its timestamp exist so a workstation running
    dozens of agents does not start dozens of interpreters that immediately exit.
    """
    lock = _acquire_spawn_lock()
    if lock is None:
        return False
    try:
        if _spawned_recently(lock):
            return False
        _stamp_spawn(lock)
        log_file = _open_daemon_log()
        options: dict[str, Any] = {
            "stdin": subprocess.DEVNULL,
            "stdout": log_file if log_file is not None else subprocess.DEVNULL,
            "stderr": subprocess.STDOUT if log_file is not None else subprocess.DEVNULL,
            "close_fds": True,
        }
        if sys.platform == "win32":
            options["creationflags"] = 0x00000208
        else:
            options["start_new_session"] = True
        try:
            subprocess.Popen([sys.executable, "-m", DAEMON_MODULE, "--port", str(port)], **options)
        except OSError:
            return False
        finally:
            if log_file is not None:
                log_file.close()
        return True
    finally:
        _release_spawn_lock(lock)


def _open_daemon_log() -> IO[bytes] | None:
    try:
        _state_dir().mkdir(parents=True, exist_ok=True, mode=0o700)
        path = _state_dir() / _LOG_NAME
        if path.exists() and path.stat().st_size > _MAX_LOG_BYTES:
            path.unlink()
        descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
        return os.fdopen(descriptor, "ab")
    except OSError:
        return None


class _UnlockedSpawnLock:
    """Stand-in for platforms or filesystems where the advisory lock is unavailable."""

    def close(self) -> None:
        return


_SpawnLock = IO[bytes] | _UnlockedSpawnLock


def _acquire_spawn_lock() -> _SpawnLock | None:
    try:
        import fcntl  # noqa: PLC0415
    except ImportError:
        # Windows has no flock; bind election alone still guarantees a single daemon.
        return _UnlockedSpawnLock()

    try:
        _state_dir().mkdir(parents=True, exist_ok=True, mode=0o700)
        descriptor = os.open(_state_dir() / _SPAWN_LOCK_NAME, os.O_RDWR | os.O_CREAT, 0o600)
    except OSError:
        return _UnlockedSpawnLock()
    handle = os.fdopen(descriptor, "r+b")
    try:
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        handle.close()
        return None
    return handle


def _spawned_recently(lock: _SpawnLock) -> bool:
    if isinstance(lock, _UnlockedSpawnLock):
        return False
    try:
        lock.seek(0)
        stamp = float(lock.read(64) or b"0")
    except (OSError, ValueError):
        return False
    elapsed = time.time() - stamp
    return 0 <= elapsed < _SPAWN_COOLDOWN_SECONDS


def _stamp_spawn(lock: _SpawnLock) -> None:
    if isinstance(lock, _UnlockedSpawnLock):
        return
    with suppress(OSError):
        lock.seek(0)
        lock.truncate()
        lock.write(f"{time.time():.3f}".encode("ascii"))
        lock.flush()


def _release_spawn_lock(lock: _SpawnLock) -> None:
    with suppress(OSError, ValueError):
        lock.close()


async def run_daemon(port: int, idle_timeout_seconds: float) -> int:
    token = load_or_create_pairing_token()
    server = BrokerServer(token, port, idle_timeout_seconds=idle_timeout_seconds)
    try:
        await server.start()
    except OSError as exc:
        if exc.errno == errno.EADDRINUSE:
            LOG.info("browser_extension_broker_election_lost", port=port)
            return 0
        raise

    loop = asyncio.get_running_loop()
    for signal_name in ("SIGINT", "SIGTERM"):
        handled_signal = getattr(signal, signal_name, None)
        if handled_signal is None:
            continue
        with suppress(NotImplementedError, ValueError):
            loop.add_signal_handler(handled_signal, server.request_shutdown, "signal")

    try:
        await server.wait_for_shutdown()
    finally:
        await server.stop()
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog=DAEMON_MODULE, description="Skyvern browser-extension bridge broker")
    parser.add_argument("--port", type=int, required=True)
    parser.add_argument("--idle-timeout-seconds", type=float, default=None)
    arguments = parser.parse_args(argv)

    idle_timeout = arguments.idle_timeout_seconds
    if idle_timeout is None:
        idle_timeout = resolve_idle_timeout_seconds()
    try:
        return asyncio.run(run_daemon(arguments.port, idle_timeout))
    except KeyboardInterrupt:
        return 0


if __name__ == "__main__":
    raise SystemExit(main())
