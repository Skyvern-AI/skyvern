from __future__ import annotations

import asyncio
import inspect
import shutil
import threading
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock, call

import pytest
from fastapi import HTTPException
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.routing import Route
from starlette.testclient import TestClient

from skyvern.cli import run_commands
from skyvern.library import local_browser_profile


@pytest.fixture(autouse=True)
def _reset_cleanup_state() -> None:
    run_commands._mcp_cleanup_done = False
    run_commands._mcp_cleanup_in_progress = False
    run_commands._mcp_eof_shutdown_requested = False
    run_commands._mcp_main_task = None
    run_commands._mcp_shutdown_exit_code = None


@pytest.mark.asyncio
async def test_cleanup_mcp_resources_closes_auth_db(monkeypatch: pytest.MonkeyPatch) -> None:
    order: list[str] = []
    shutdown_action_log_worker = AsyncMock(side_effect=lambda: order.append("action_log"))
    close_all_sessions = AsyncMock(side_effect=lambda: order.append("session"))
    close_skyvern = AsyncMock(side_effect=lambda: order.append("client"))
    close_auth_db = AsyncMock(side_effect=lambda: order.append("auth"))

    monkeypatch.setattr("skyvern.cli.core.action_log.shutdown_action_log_worker", shutdown_action_log_worker)
    monkeypatch.setattr("skyvern.cli.core.session_manager.close_all_sessions", close_all_sessions)
    monkeypatch.setattr("skyvern.cli.core.client.close_skyvern", close_skyvern)
    monkeypatch.setattr("skyvern.cli.core.mcp_http_auth.close_auth_db", close_auth_db)

    await run_commands._cleanup_mcp_resources()

    close_all_sessions.assert_awaited_once()
    close_skyvern.assert_awaited_once()
    close_auth_db.assert_awaited_once()
    assert order == ["action_log", "session", "client", "auth"]


@pytest.mark.asyncio
async def test_cleanup_mcp_resources_closes_auth_db_on_skyvern_close_error(monkeypatch: pytest.MonkeyPatch) -> None:
    close_all_sessions = AsyncMock()
    close_auth_db = AsyncMock()

    async def _failing_close_skyvern() -> None:
        raise RuntimeError("close failed")

    monkeypatch.setattr("skyvern.cli.core.session_manager.close_all_sessions", close_all_sessions)
    monkeypatch.setattr("skyvern.cli.core.client.close_skyvern", _failing_close_skyvern)
    monkeypatch.setattr("skyvern.cli.core.mcp_http_auth.close_auth_db", close_auth_db)

    with pytest.raises(RuntimeError, match="close failed"):
        await run_commands._cleanup_mcp_resources()

    close_all_sessions.assert_awaited_once()
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
    run_commands._mcp_eof_shutdown_requested = eof_shutdown

    with pytest.raises(SystemExit) as exc_info:
        run_commands._handle_mcp_shutdown_signal(getattr(run_commands.signal, signum), None)

    assert exc_info.value.code == expected_exit_code
    assert run_commands._mcp_shutdown_exit_code == expected_exit_code


def test_mcp_shutdown_signal_does_not_exit_during_cleanup(monkeypatch: pytest.MonkeyPatch) -> None:
    run_commands._mcp_cleanup_in_progress = True

    run_commands._handle_mcp_shutdown_signal(run_commands.signal.SIGINT, None)


@pytest.mark.parametrize(
    ("signum", "eof_shutdown", "expected_exit_code"),
    [("SIGTERM", False, 143), ("SIGINT", True, 0)],
)
def test_run_mcp_signal_drains_before_sibling_cancellation(
    monkeypatch: pytest.MonkeyPatch,
    signum: str,
    eof_shutdown: bool,
    expected_exit_code: int,
) -> None:
    cleanup_calls = 0
    run_loop: asyncio.AbstractEventLoop | None = None
    sibling: asyncio.Task[None] | None = None
    register = MagicMock()
    run = AsyncMock()
    set_stateless = MagicMock()
    eof_event = MagicMock()

    async def wait_forever() -> None:
        await asyncio.Event().wait()

    async def run_async(**_kwargs: object) -> None:
        nonlocal run_loop, sibling
        run_loop = asyncio.get_running_loop()
        sibling = asyncio.create_task(wait_forever())
        run_commands._mcp_eof_shutdown_requested = eof_shutdown
        run_loop.call_later(0.01, run_commands.signal.raise_signal, getattr(run_commands.signal, signum))
        await asyncio.Event().wait()

    async def cleanup() -> None:
        nonlocal cleanup_calls
        assert asyncio.get_running_loop() is run_loop
        assert sibling is not None
        assert sibling.cancelling() == 0
        cleanup_calls += 1
        sibling.cancel()
        await asyncio.gather(sibling, return_exceptions=True)

    run.side_effect = run_async
    monkeypatch.setattr(run_commands, "_cleanup_mcp_resources", cleanup)
    monkeypatch.setattr(run_commands, "_start_stdin_eof_watcher", lambda: (eof_event, eof_event))
    monkeypatch.setattr(run_commands.atexit, "register", register)
    monkeypatch.setattr("skyvern.cli.mcp_tools.mcp.run_async", run)
    monkeypatch.setattr("skyvern.cli.core.session_manager.set_stateless_http_mode", set_stateless)

    with pytest.raises(SystemExit) as exc_info:
        run_commands.run_mcp()

    assert exc_info.value.code == expected_exit_code
    register.assert_called_once_with(run_commands._cleanup_mcp_resources_sync)
    run.assert_awaited_once_with(transport="stdio")
    assert cleanup_calls == 1
    assert run_commands._mcp_main_task is None
    assert run_commands._mcp_shutdown_exit_code == expected_exit_code
    set_stateless.assert_has_calls([call(False), call(False)])

    run_commands._cleanup_mcp_resources_sync()
    assert cleanup_calls == 1


def test_run_mcp_restores_signal_handlers_after_cleanup(monkeypatch: pytest.MonkeyPatch) -> None:
    events: list[str] = []
    run_loop: asyncio.AbstractEventLoop | None = None
    originals = {run_commands.signal.SIGINT: object(), run_commands.signal.SIGTERM: object()}

    async def run_async(**_kwargs: object) -> None:
        nonlocal run_loop
        run_loop = asyncio.get_running_loop()

    async def cleanup() -> None:
        assert asyncio.get_running_loop() is run_loop
        events.append("cleanup")

    def install_signal_handler(handled_signal: run_commands.signal.Signals, handler: object) -> object:
        if handler is run_commands._handle_mcp_shutdown_signal:
            return originals[handled_signal]
        events.append("restore")
        return handler

    monkeypatch.setattr(run_commands.signal, "signal", install_signal_handler)
    monkeypatch.setattr(run_commands, "_start_stdin_eof_watcher", lambda: (MagicMock(), MagicMock()))
    monkeypatch.setattr(run_commands, "_cleanup_mcp_resources", cleanup)
    monkeypatch.setattr(run_commands.atexit, "register", MagicMock())
    monkeypatch.setattr("skyvern.cli.mcp_tools.mcp.run_async", run_async)

    run_commands.run_mcp()

    assert events[-3:] == ["cleanup", "restore", "restore"]


def test_run_mcp_stdin_eof_invokes_original_loop_cleanup(monkeypatch: pytest.MonkeyPatch) -> None:
    cleanup = AsyncMock()
    request_shutdown, force_exit = MagicMock(), MagicMock()
    eof_detected = threading.Event()
    poller = MagicMock()
    poller.poll.side_effect = lambda _timeout: (eof_detected.set(), [(123, run_commands.select.POLLHUP)])[1]

    async def return_on_eof(**_kwargs: object) -> None:
        assert await asyncio.to_thread(eof_detected.wait, 1)

    monkeypatch.setattr(run_commands, "_cleanup_mcp_resources", cleanup)
    monkeypatch.setattr(run_commands._thread, "interrupt_main", request_shutdown)
    monkeypatch.setattr(run_commands.os, "_exit", force_exit)
    monkeypatch.setattr(run_commands.select, "poll", lambda: poller)
    monkeypatch.setattr(run_commands.atexit, "register", MagicMock())
    monkeypatch.setattr("skyvern.cli.mcp_tools.mcp.run_async", return_on_eof)

    run_commands.run_mcp()

    cleanup.assert_awaited_once_with()
    request_shutdown.assert_not_called()
    force_exit.assert_not_called()


def test_run_mcp_http_transport_wires_auth_middleware(monkeypatch: pytest.MonkeyPatch) -> None:
    from skyvern.cli.core.mcp_http_auth import MCPAPIKeyMiddleware  # noqa: PLC0415
    from skyvern.cli.mcp_tools.origin_middleware import OriginValidationMiddleware  # noqa: PLC0415

    cleanup = AsyncMock()
    register = MagicMock()
    run = AsyncMock()
    set_stateless = MagicMock()

    monkeypatch.setattr(run_commands, "_cleanup_mcp_resources", cleanup)
    monkeypatch.setattr(run_commands.atexit, "register", register)
    monkeypatch.setattr("skyvern.cli.mcp_tools.mcp.run_async", run)
    monkeypatch.setattr("skyvern.cli.core.session_manager.set_stateless_http_mode", set_stateless)

    run_commands.run_mcp(
        transport="streamable-http",
        host="0.0.0.0",
        port=9010,
        path="mcp",
        stateless_http=True,
    )

    register.assert_called_once_with(run_commands._cleanup_mcp_resources_sync)
    run.assert_awaited_once()
    kwargs = run.call_args.kwargs
    assert kwargs["transport"] == "streamable-http"
    # Wildcard exposure stays reachable, but only when the caller asks for it.
    assert kwargs["host"] == "0.0.0.0"
    assert kwargs["port"] == 9010
    assert kwargs["path"] == "/mcp"
    assert kwargs["stateless_http"] is True
    assert [entry.cls for entry in kwargs["middleware"]] == [
        run_commands._ServerCardMiddleware,
        OriginValidationMiddleware,
        MCPAPIKeyMiddleware,
    ]
    set_stateless.assert_has_calls([call(True), call(False)])
    cleanup.assert_awaited_once()


def test_run_mcp_http_defaults_to_loopback() -> None:
    assert inspect.signature(run_commands.run_mcp).parameters["host"].default == "127.0.0.1"


def _compose_standalone_mcp_http_app(monkeypatch: pytest.MonkeyPatch, inner: Any) -> Any:
    """Return the standalone MCP ASGI stack exactly as `run_mcp` composes it."""
    run = AsyncMock()
    monkeypatch.setattr(run_commands, "_cleanup_mcp_resources", AsyncMock())
    monkeypatch.setattr(run_commands.atexit, "register", MagicMock())
    monkeypatch.setattr("skyvern.cli.mcp_tools.mcp.run_async", run)

    run_commands.run_mcp(transport="streamable-http", port=9010, path="mcp")

    app = inner
    for entry in reversed(run.call_args.kwargs["middleware"]):
        app = entry.cls(app, *entry.args, **entry.kwargs)
    return app


def test_standalone_mcp_origin_validation_precedes_api_key_auth(monkeypatch: pytest.MonkeyPatch) -> None:
    from skyvern.cli.core import mcp_http_auth  # noqa: PLC0415

    valid_api_key = "STORMBREAKER-valid-key"
    hostile_api_key = "STORMBREAKER-hostile-key"
    forged_api_key = "STORMBREAKER-forged-key"
    auth_db = object()
    resolved_api_keys: list[str] = []
    reached_mcp: list[str] = []

    async def resolve_org_from_api_key(api_key: str, db: object, **_: object) -> SimpleNamespace:
        assert db is auth_db
        resolved_api_keys.append(api_key)
        if api_key not in {valid_api_key, hostile_api_key}:
            raise HTTPException(status_code=401, detail="Invalid API key")
        return SimpleNamespace(
            organization=SimpleNamespace(organization_id="STORMBREAKER-org"),
            token=SimpleNamespace(token_type=mcp_http_auth.OrganizationAuthTokenType.api),
        )

    monkeypatch.setattr(mcp_http_auth, "get_auth_db", lambda: auth_db)
    monkeypatch.setattr(mcp_http_auth, "resolve_org_from_api_key", resolve_org_from_api_key)
    mcp_http_auth._api_key_validation_cache.clear()

    async def mcp_endpoint(request: Request) -> JSONResponse:
        reached_mcp.append(request.headers.get("origin", ""))
        return JSONResponse({"ok": True})

    client = TestClient(
        _compose_standalone_mcp_http_app(
            monkeypatch,
            Starlette(routes=[Route("/mcp", mcp_endpoint, methods=["GET", "POST"])]),
        )
    )

    def post(api_key: str, origin: str | None) -> Any:
        headers = {"x-api-key": api_key}
        if origin is not None:
            headers["origin"] = origin
        return client.post("/mcp", headers=headers)

    # Positive controls: the product still works for the origins it must serve.
    assert post(valid_api_key, None).status_code == 200
    assert post(valid_api_key, "http://localhost:5173").status_code == 200
    assert post(valid_api_key, "https://claude.ai").status_code == 200
    assert reached_mcp == ["", "http://localhost:5173", "https://claude.ai"]
    assert resolved_api_keys == [valid_api_key]  # later calls served from the validation cache

    # Hostile origins, including hosts that merely start with an allowed value.
    for hostile_origin in (
        "https://STORMBREAKER.attacker.invalid",
        "https://claude.ai.STORMBREAKER.attacker.invalid",
        "http://127.0.0.1.STORMBREAKER.attacker.invalid",
        "http://localhost.STORMBREAKER.attacker.invalid",
    ):
        response = post(hostile_api_key, hostile_origin)
        assert response.status_code == 403, hostile_origin
        assert response.json() == {"error": "forbidden_origin", "detail": "Origin not allowed"}

    assert hostile_api_key not in resolved_api_keys
    assert len(reached_mcp) == 3

    # Auth still runs — and still rejects — behind an allowed origin.
    assert post(forged_api_key, "https://claude.ai").status_code == 401
    assert forged_api_key in resolved_api_keys
    assert len(reached_mcp) == 3

    # Server-card discovery stays public, ahead of both gates.
    card = client.get("/.well-known/mcp/server-card.json", headers={"origin": "https://STORMBREAKER.attacker.invalid"})
    assert card.status_code == 200
    assert card.headers["access-control-allow-origin"] == "*"


@pytest.mark.asyncio
async def test_run_task_tool_registration_points_to_browser_module() -> None:
    from skyvern.cli.mcp_tools import mcp  # noqa: PLC0415

    tool = await mcp.get_tool("skyvern_run_task")
    assert tool is not None
    assert tool.fn.__module__ == "skyvern.cli.mcp_tools.browser"
