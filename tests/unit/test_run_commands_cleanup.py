from __future__ import annotations

import shutil
import threading
from unittest.mock import AsyncMock, MagicMock, call

import pytest

from skyvern.cli import run_commands
from skyvern.library import local_browser_profile


@pytest.fixture(autouse=True)
def _reset_cleanup_state() -> None:
    run_commands._mcp_cleanup_done = False
    run_commands._mcp_cleanup_in_progress = False
    run_commands._mcp_eof_shutdown_requested = False


@pytest.mark.asyncio
async def test_cleanup_mcp_resources_closes_auth_db(monkeypatch: pytest.MonkeyPatch) -> None:
    close_current_session = AsyncMock()
    close_skyvern = AsyncMock()
    close_auth_db = AsyncMock()

    monkeypatch.setattr("skyvern.cli.core.session_manager.close_current_session", close_current_session)
    monkeypatch.setattr("skyvern.cli.core.client.close_skyvern", close_skyvern)
    monkeypatch.setattr("skyvern.cli.core.mcp_http_auth.close_auth_db", close_auth_db)

    await run_commands._cleanup_mcp_resources()

    close_current_session.assert_awaited_once()
    close_skyvern.assert_awaited_once()
    close_auth_db.assert_awaited_once()


@pytest.mark.asyncio
async def test_cleanup_mcp_resources_closes_auth_db_on_skyvern_close_error(monkeypatch: pytest.MonkeyPatch) -> None:
    close_current_session = AsyncMock()
    close_auth_db = AsyncMock()

    async def _failing_close_skyvern() -> None:
        raise RuntimeError("close failed")

    monkeypatch.setattr("skyvern.cli.core.session_manager.close_current_session", close_current_session)
    monkeypatch.setattr("skyvern.cli.core.client.close_skyvern", _failing_close_skyvern)
    monkeypatch.setattr("skyvern.cli.core.mcp_http_auth.close_auth_db", close_auth_db)

    with pytest.raises(RuntimeError, match="close failed"):
        await run_commands._cleanup_mcp_resources()

    close_current_session.assert_awaited_once()
    close_auth_db.assert_awaited_once()


@pytest.mark.parametrize("deleted", [True, False], ids=["deleted", "deferred"])
def test_cleanup_mcp_resources_sync_routes_owned_profile_through_shared_cleanup(
    monkeypatch: pytest.MonkeyPatch,
    deleted: bool,
) -> None:
    cleanup = AsyncMock()
    profile = MagicMock(name="profile")
    profile_cleanup = MagicMock(return_value=deleted)
    terminate = MagicMock()
    rmtree = MagicMock(side_effect=AssertionError("run_commands must not delete profiles directly"))
    thread = MagicMock()
    monkeypatch.setattr(run_commands, "_cleanup_mcp_resources", cleanup)
    monkeypatch.setattr(
        run_commands,
        "_current_local_browser_identity",
        lambda: ("/tmp/skyvern-browser-owned", True, profile),
    )
    monkeypatch.setattr(local_browser_profile, "cleanup_local_browser_profile", profile_cleanup)
    monkeypatch.setattr(local_browser_profile, "terminate_local_browser_processes", terminate)
    monkeypatch.setattr(shutil, "rmtree", rmtree)
    monkeypatch.setattr(run_commands.threading, "Thread", thread)

    run_commands._cleanup_mcp_resources_sync()

    cleanup.assert_not_awaited()
    thread.assert_not_called()
    profile_cleanup.assert_called_once_with(profile)
    terminate.assert_not_called()
    rmtree.assert_not_called()
    assert run_commands._mcp_cleanup_done is True


def test_cleanup_mcp_resources_sync_keeps_threaded_graceful_cleanup_without_local_browser(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class RecordingThread(threading.Thread):
        joined_with: float | None = None

        def join(self, timeout: float | None = None) -> None:
            type(self).joined_with = timeout
            super().join(timeout)

    cleanup = AsyncMock()
    monkeypatch.setattr(run_commands, "_cleanup_mcp_resources", cleanup)
    monkeypatch.setattr(run_commands, "_current_local_browser_identity", lambda: None)
    monkeypatch.setattr(run_commands.threading, "Thread", RecordingThread)

    run_commands._cleanup_mcp_resources_sync()

    cleanup.assert_awaited_once()
    assert RecordingThread.joined_with == run_commands._MCP_GRACEFUL_CLEANUP_TIMEOUT_SECONDS == 5.0
    assert run_commands._mcp_cleanup_done is True


def test_cleanup_mcp_resources_sync_suppresses_task_failure(monkeypatch: pytest.MonkeyPatch) -> None:
    async def failing_cleanup() -> None:
        raise RuntimeError("cleanup failed")

    monkeypatch.setattr(run_commands, "_cleanup_mcp_resources", failing_cleanup)

    run_commands._cleanup_mcp_resources_sync()

    assert run_commands._mcp_cleanup_done is True


def test_cleanup_mcp_resources_blocking_ignores_reentrant_call(monkeypatch: pytest.MonkeyPatch) -> None:
    cleanup = AsyncMock()

    def identify() -> None:
        assert run_commands._mcp_cleanup_in_progress is True
        run_commands._cleanup_mcp_resources_blocking()
        return None

    identify = MagicMock(side_effect=identify)
    monkeypatch.setattr(run_commands, "_cleanup_mcp_resources", cleanup)
    monkeypatch.setattr(run_commands, "_current_local_browser_identity", identify)

    run_commands._cleanup_mcp_resources_blocking()

    identify.assert_called_once_with()
    cleanup.assert_awaited_once_with()


@pytest.mark.parametrize("terminated", [True, False], ids=["terminated", "termination_deferred"])
def test_cleanup_mcp_resources_sync_preserves_explicit_user_data_dir(
    monkeypatch: pytest.MonkeyPatch,
    terminated: bool,
) -> None:
    cleanup = AsyncMock()
    profile_cleanup = MagicMock()
    terminate = MagicMock(return_value=terminated)
    rmtree = MagicMock(side_effect=AssertionError("explicit user data must not be deleted"))
    monkeypatch.setattr(run_commands, "_cleanup_mcp_resources", cleanup)
    monkeypatch.setattr(
        run_commands,
        "_current_local_browser_identity",
        lambda: ("/tmp/skyvern-browser-explicit", False, None),
    )
    monkeypatch.setattr(local_browser_profile, "cleanup_local_browser_profile", profile_cleanup)
    monkeypatch.setattr(local_browser_profile, "terminate_local_browser_processes", terminate)
    monkeypatch.setattr(shutil, "rmtree", rmtree)

    run_commands._cleanup_mcp_resources_sync()

    cleanup.assert_not_awaited()
    profile_cleanup.assert_not_called()
    terminate.assert_called_once_with("/tmp/skyvern-browser-explicit")
    rmtree.assert_not_called()


def test_stdin_eof_watcher_allows_native_clean_return(monkeypatch: pytest.MonkeyPatch) -> None:
    poller = MagicMock()
    poller.poll.return_value = [(123, run_commands.select.POLLHUP)]
    monkeypatch.setattr(run_commands.select, "poll", lambda: poller)
    request_shutdown, force_exit = MagicMock(), MagicMock()
    stop = MagicMock(**{"is_set.side_effect": [False, False, True]})

    run_commands._watch_stdin_eof(
        stop,
        MagicMock(**{"wait.return_value": False}),
        stdin_fd=123,
        request_shutdown=request_shutdown,
        force_exit=force_exit,
    )

    request_shutdown.assert_not_called()
    force_exit.assert_not_called()


@pytest.mark.parametrize("deleted", [True, False], ids=["deleted", "deferred"])
def test_stdin_eof_watcher_force_exits_after_shared_profile_cleanup(
    monkeypatch: pytest.MonkeyPatch,
    deleted: bool,
) -> None:
    events: list[str] = []
    poller = MagicMock()
    poller.poll.return_value = [(123, run_commands.select.POLLHUP)]
    profile = MagicMock(name="profile")
    profile_cleanup = MagicMock(side_effect=lambda _profile: events.append("cleanup") or deleted)
    terminate = MagicMock()
    rmtree = MagicMock(side_effect=AssertionError("run_commands must not delete profiles directly"))
    force_exit = MagicMock(side_effect=lambda _code: events.append("exit"))
    monkeypatch.setattr(run_commands.select, "poll", lambda: poller)
    monkeypatch.setattr(
        run_commands,
        "_current_local_browser_identity",
        lambda: ("/tmp/owned", True, profile),
    )
    monkeypatch.setattr(local_browser_profile, "cleanup_local_browser_profile", profile_cleanup)
    monkeypatch.setattr(local_browser_profile, "terminate_local_browser_processes", terminate)
    monkeypatch.setattr(shutil, "rmtree", rmtree)

    run_commands._watch_stdin_eof(
        threading.Event(),
        threading.Event(),
        stdin_fd=123,
        request_shutdown=MagicMock(),
        force_exit=force_exit,
        native_eof_grace=0,
        shutdown_timeout=0,
    )

    profile_cleanup.assert_called_once_with(profile)
    terminate.assert_not_called()
    rmtree.assert_not_called()
    force_exit.assert_called_once_with(0)
    assert events == ["cleanup", "exit"]
    assert run_commands._mcp_eof_shutdown_requested is True


def test_mcp_eof_shutdown_ceiling_exceeds_worst_case_cleanup() -> None:
    # The EOF watcher's os._exit(0) preempts cleanup unconditionally, so this must cover the cloud path.
    assert run_commands._MCP_EOF_SHUTDOWN_TIMEOUT_SECONDS > (
        run_commands._MCP_GRACEFUL_CLEANUP_TIMEOUT_SECONDS
        + local_browser_profile.PROCESS_KILL_TIMEOUT_SECONDS
        + local_browser_profile.PROFILE_DELETE_TIMEOUT_SECONDS
    )


@pytest.mark.parametrize(
    ("signum", "eof_shutdown", "expected_exit_code"),
    [
        ("SIGTERM", False, 143),
        ("SIGTERM", True, 143),
        ("SIGINT", False, 130),
        ("SIGINT", True, 0),
    ],
)
def test_mcp_shutdown_signal_uses_source_specific_exit_code(
    monkeypatch: pytest.MonkeyPatch,
    signum: str,
    eof_shutdown: bool,
    expected_exit_code: int,
) -> None:
    cleanup, force_exit = MagicMock(), MagicMock()
    run_commands._mcp_eof_shutdown_requested = eof_shutdown
    monkeypatch.setattr(run_commands, "_cleanup_mcp_resources_blocking", cleanup)
    monkeypatch.setattr(run_commands.os, "_exit", force_exit)

    run_commands._handle_mcp_shutdown_signal(getattr(run_commands.signal, signum), None)

    cleanup.assert_called_once_with()
    force_exit.assert_called_once_with(expected_exit_code)


def test_mcp_shutdown_signal_exits_even_when_cleanup_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    force_exit = MagicMock()
    run_commands._mcp_eof_shutdown_requested = False
    monkeypatch.setattr(
        run_commands, "_cleanup_mcp_resources_blocking", MagicMock(side_effect=RuntimeError("cleanup blew up"))
    )
    monkeypatch.setattr(run_commands.os, "_exit", force_exit)

    with pytest.raises(RuntimeError, match="cleanup blew up"):
        run_commands._handle_mcp_shutdown_signal(run_commands.signal.SIGTERM, None)

    force_exit.assert_called_once_with(143)


def test_mcp_shutdown_signal_does_not_exit_during_cleanup(monkeypatch: pytest.MonkeyPatch) -> None:
    cleanup, force_exit = MagicMock(), MagicMock()
    run_commands._mcp_cleanup_in_progress = True
    monkeypatch.setattr(run_commands, "_cleanup_mcp_resources_blocking", cleanup)
    monkeypatch.setattr(run_commands.os, "_exit", force_exit)

    run_commands._handle_mcp_shutdown_signal(run_commands.signal.SIGINT, None)

    cleanup.assert_not_called()
    force_exit.assert_not_called()


def test_run_mcp_sigterm_calls_blocking_cleanup_in_finally(monkeypatch: pytest.MonkeyPatch) -> None:
    cleanup_blocking, force_exit = MagicMock(), MagicMock(side_effect=KeyboardInterrupt)
    register = MagicMock()
    signal_install = MagicMock(return_value=run_commands.signal.SIG_DFL)
    run = MagicMock(side_effect=lambda **_kwargs: run_commands._handle_mcp_shutdown_signal(15, None))
    set_stateless = MagicMock()
    eof_event = MagicMock()

    monkeypatch.setattr(run_commands, "_cleanup_mcp_resources_blocking", cleanup_blocking)
    monkeypatch.setattr(run_commands, "_start_stdin_eof_watcher", lambda: (eof_event, eof_event))
    monkeypatch.setattr(run_commands.os, "_exit", force_exit)
    monkeypatch.setattr(run_commands.signal, "signal", signal_install)
    monkeypatch.setattr(run_commands.atexit, "register", register)
    monkeypatch.setattr("skyvern.cli.mcp_tools.mcp.run", run)
    monkeypatch.setattr("skyvern.cli.core.session_manager.set_stateless_http_mode", set_stateless)

    with pytest.raises(KeyboardInterrupt):
        run_commands.run_mcp()

    register.assert_called_once_with(run_commands._cleanup_mcp_resources_sync)
    assert call(run_commands.signal.SIGTERM, run_commands._handle_mcp_shutdown_signal) in signal_install.call_args_list
    run.assert_called_once_with(transport="stdio")
    set_stateless.assert_has_calls([call(False), call(False)])
    assert cleanup_blocking.call_count == 2 and force_exit.call_args == call(143)


def test_run_mcp_restores_signal_handlers_after_cleanup(monkeypatch: pytest.MonkeyPatch) -> None:
    events: list[str] = []
    originals = {run_commands.signal.SIGINT: object(), run_commands.signal.SIGTERM: object()}

    def install_signal_handler(handled_signal: run_commands.signal.Signals, handler: object) -> object:
        if handler is run_commands._handle_mcp_shutdown_signal:
            return originals[handled_signal]
        events.append("restore")
        return handler

    monkeypatch.setattr(run_commands.signal, "signal", install_signal_handler)
    monkeypatch.setattr(run_commands, "_start_stdin_eof_watcher", lambda: (MagicMock(), MagicMock()))
    monkeypatch.setattr(run_commands, "_cleanup_mcp_resources_blocking", lambda: events.append("cleanup"))
    monkeypatch.setattr(run_commands.atexit, "register", MagicMock())
    monkeypatch.setattr("skyvern.cli.mcp_tools.mcp.run", MagicMock())

    run_commands.run_mcp()

    assert events == ["cleanup", "restore", "restore"]


def test_run_mcp_stdin_eof_invokes_blocking_cleanup(monkeypatch: pytest.MonkeyPatch) -> None:
    cleanup_blocking = MagicMock()
    request_shutdown, force_exit = MagicMock(), MagicMock()
    eof_detected = threading.Event()
    poller = MagicMock()
    poller.poll.side_effect = lambda _timeout: (eof_detected.set(), [(123, run_commands.select.POLLHUP)])[1]

    def return_on_eof(**_kwargs: object) -> None:
        assert eof_detected.wait(1)

    monkeypatch.setattr(run_commands, "_cleanup_mcp_resources_blocking", cleanup_blocking)
    monkeypatch.setattr(run_commands._thread, "interrupt_main", request_shutdown)
    monkeypatch.setattr(run_commands.os, "_exit", force_exit)
    monkeypatch.setattr(run_commands.select, "poll", lambda: poller)
    monkeypatch.setattr(run_commands.atexit, "register", MagicMock())
    monkeypatch.setattr("skyvern.cli.mcp_tools.mcp.run", return_on_eof)

    run_commands.run_mcp()

    cleanup_blocking.assert_called_once_with()
    request_shutdown.assert_not_called()
    force_exit.assert_not_called()


def test_run_mcp_http_transport_wires_auth_middleware(monkeypatch: pytest.MonkeyPatch) -> None:
    from skyvern.cli.core.mcp_http_auth import MCPAPIKeyMiddleware  # noqa: PLC0415

    cleanup_blocking = MagicMock()
    register = MagicMock()
    run = MagicMock()
    set_stateless = MagicMock()

    monkeypatch.setattr(run_commands, "_cleanup_mcp_resources_blocking", cleanup_blocking)
    monkeypatch.setattr(run_commands.atexit, "register", register)
    monkeypatch.setattr("skyvern.cli.mcp_tools.mcp.run", run)
    monkeypatch.setattr("skyvern.cli.core.session_manager.set_stateless_http_mode", set_stateless)

    run_commands.run_mcp(
        transport="streamable-http",
        host="127.0.0.1",
        port=9010,
        path="mcp",
        stateless_http=True,
    )

    register.assert_called_once_with(run_commands._cleanup_mcp_resources_sync)
    run.assert_called_once()
    kwargs = run.call_args.kwargs
    assert kwargs["transport"] == "streamable-http"
    assert kwargs["host"] == "127.0.0.1"
    assert kwargs["port"] == 9010
    assert kwargs["path"] == "/mcp"
    assert kwargs["stateless_http"] is True
    middleware = kwargs["middleware"]
    assert len(middleware) == 2
    assert middleware[0].cls is run_commands._ServerCardMiddleware
    assert middleware[1].cls is MCPAPIKeyMiddleware
    set_stateless.assert_has_calls([call(True), call(False)])
    cleanup_blocking.assert_called_once()


@pytest.mark.asyncio
async def test_run_task_tool_registration_points_to_browser_module() -> None:
    from skyvern.cli.mcp_tools import mcp  # noqa: PLC0415

    tool = await mcp.get_tool("skyvern_run_task")
    assert tool is not None
    assert tool.fn.__module__ == "skyvern.cli.mcp_tools.browser"
