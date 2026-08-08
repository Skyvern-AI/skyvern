from __future__ import annotations

import errno
import subprocess
import sys
from pathlib import Path
from unittest.mock import MagicMock

import pytest

import skyvern.browser_extension.broker.daemon as daemon_module
from skyvern.browser_extension.broker.daemon import (
    DAEMON_MODULE,
    DEFAULT_IDLE_TIMEOUT_SECONDS,
    IDLE_TIMEOUT_ENV,
    resolve_idle_timeout_seconds,
    run_daemon,
    spawn_daemon,
)
from skyvern.browser_extension.errors import BrowserExtensionError


@pytest.fixture
def state_home(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Path:
    monkeypatch.setattr(daemon_module.Path, "home", staticmethod(lambda: tmp_path))
    return tmp_path


def test_the_daemon_is_launched_detached_from_the_agent_that_spawned_it(
    monkeypatch: pytest.MonkeyPatch, state_home: Path
) -> None:
    popen = MagicMock()
    monkeypatch.setattr(daemon_module.subprocess, "Popen", popen)
    monkeypatch.setattr(daemon_module.sys, "platform", "darwin")

    assert spawn_daemon(19777)

    command, options = popen.call_args.args[0], popen.call_args.kwargs
    assert command == [sys.executable, "-m", DAEMON_MODULE, "--port", "19777"]
    assert options["start_new_session"] is True
    assert options["stdin"] is subprocess.DEVNULL
    assert options["close_fds"] is True


def test_a_spawn_storm_collapses_to_a_single_launch(monkeypatch: pytest.MonkeyPatch, state_home: Path) -> None:
    popen = MagicMock()
    monkeypatch.setattr(daemon_module.subprocess, "Popen", popen)

    results = [spawn_daemon(19777) for _ in range(12)]

    assert results.count(True) == 1
    assert popen.call_count == 1


def test_a_second_spawn_is_allowed_once_the_cooldown_lapses(monkeypatch: pytest.MonkeyPatch, state_home: Path) -> None:
    popen = MagicMock()
    monkeypatch.setattr(daemon_module.subprocess, "Popen", popen)
    clock = [1_000.0]
    monkeypatch.setattr(daemon_module.time, "time", lambda: clock[0])

    assert spawn_daemon(19777)
    assert not spawn_daemon(19777)
    clock[0] += daemon_module._SPAWN_COOLDOWN_SECONDS
    assert spawn_daemon(19777)

    assert popen.call_count == 2


def test_the_daemon_log_is_private_to_the_operator(monkeypatch: pytest.MonkeyPatch, state_home: Path) -> None:
    monkeypatch.setattr(daemon_module.subprocess, "Popen", MagicMock())

    assert spawn_daemon(19777)

    log_path = state_home / ".skyvern" / "browser_extension_broker.log"
    lock_path = state_home / ".skyvern" / "browser_extension_broker.lock"
    assert log_path.stat().st_mode & 0o777 == 0o600
    assert lock_path.stat().st_mode & 0o777 == 0o600


def test_a_failed_launch_is_reported_rather_than_raised(monkeypatch: pytest.MonkeyPatch, state_home: Path) -> None:
    monkeypatch.setattr(daemon_module.subprocess, "Popen", MagicMock(side_effect=OSError("no exec")))

    assert not spawn_daemon(19777)


@pytest.mark.asyncio
async def test_losing_the_bind_race_exits_quietly(monkeypatch: pytest.MonkeyPatch) -> None:
    class LosingServer:
        def __init__(self, *args: object, **kwargs: object) -> None:
            self.stopped = False

        async def start(self) -> None:
            raise OSError(errno.EADDRINUSE, "address in use")

        async def stop(self) -> None:
            self.stopped = True

    monkeypatch.setattr(daemon_module, "BrokerServer", LosingServer)
    monkeypatch.setattr(daemon_module, "load_or_create_pairing_token", lambda: "daemon-test-token")

    assert await run_daemon(19777, 300.0) == 0


@pytest.mark.asyncio
async def test_a_real_bind_failure_is_not_swallowed(monkeypatch: pytest.MonkeyPatch) -> None:
    class BrokenServer:
        def __init__(self, *args: object, **kwargs: object) -> None:
            pass

        async def start(self) -> None:
            raise OSError(errno.EACCES, "permission denied")

        async def stop(self) -> None:
            return

    monkeypatch.setattr(daemon_module, "BrokerServer", BrokenServer)
    monkeypatch.setattr(daemon_module, "load_or_create_pairing_token", lambda: "daemon-test-token")

    with pytest.raises(OSError, match="permission denied"):
        await run_daemon(19777, 300.0)


def test_the_idle_window_is_configurable_and_validated(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv(IDLE_TIMEOUT_ENV, raising=False)
    assert resolve_idle_timeout_seconds() == DEFAULT_IDLE_TIMEOUT_SECONDS

    monkeypatch.setenv(IDLE_TIMEOUT_ENV, "45")
    assert resolve_idle_timeout_seconds() == 45.0

    monkeypatch.setenv(IDLE_TIMEOUT_ENV, "0")
    with pytest.raises(BrowserExtensionError, match="greater than zero"):
        resolve_idle_timeout_seconds()

    monkeypatch.setenv(IDLE_TIMEOUT_ENV, "soon")
    with pytest.raises(BrowserExtensionError, match="number of seconds"):
        resolve_idle_timeout_seconds()
