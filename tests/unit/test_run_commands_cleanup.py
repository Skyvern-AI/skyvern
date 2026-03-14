from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock, call

import pytest

from skyvern.cli import run_commands


@pytest.fixture(autouse=True)
def _reset_cleanup_state() -> None:
    run_commands._mcp_cleanup_done = False


@pytest.mark.asyncio
async def test_cleanup_mcp_resources_closes_auth_db(monkeypatch: pytest.MonkeyPatch) -> None:
    close_current_session = AsyncMock()
    close_skyvern = AsyncMock()
    close_auth_db = AsyncMock()

    monkeypatch.setattr(run_commands, "close_current_session", close_current_session)
    monkeypatch.setattr(run_commands, "close_skyvern", close_skyvern)
    monkeypatch.setattr(run_commands, "close_auth_db", close_auth_db)

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

    monkeypatch.setattr(run_commands, "close_current_session", close_current_session)
    monkeypatch.setattr(run_commands, "close_skyvern", _failing_close_skyvern)
    monkeypatch.setattr(run_commands, "close_auth_db", close_auth_db)

    with pytest.raises(RuntimeError, match="close failed"):
        await run_commands._cleanup_mcp_resources()

    close_current_session.assert_awaited_once()
    close_auth_db.assert_awaited_once()


def test_cleanup_mcp_resources_sync_runs_without_running_loop(monkeypatch: pytest.MonkeyPatch) -> None:
    cleanup = AsyncMock()
    monkeypatch.setattr(run_commands, "_cleanup_mcp_resources", cleanup)

    run_commands._cleanup_mcp_resources_sync()

    cleanup.assert_awaited_once()
    assert run_commands._mcp_cleanup_done is True


@pytest.mark.asyncio
async def test_cleanup_mcp_resources_sync_skips_when_loop_running(monkeypatch: pytest.MonkeyPatch) -> None:
    cleanup = AsyncMock()
    monkeypatch.setattr(run_commands, "_cleanup_mcp_resources", cleanup)

    run_commands._cleanup_mcp_resources_sync()
    await asyncio.sleep(0)

    cleanup.assert_not_awaited()
    assert run_commands._mcp_cleanup_done is False


def test_cleanup_mcp_resources_sync_keeps_retry_possible_on_task_failure(monkeypatch: pytest.MonkeyPatch) -> None:
    async def failing_cleanup() -> None:
        raise RuntimeError("cleanup failed")

    monkeypatch.setattr(run_commands, "_cleanup_mcp_resources", failing_cleanup)

    run_commands._cleanup_mcp_resources_sync()

    assert run_commands._mcp_cleanup_done is False


def test_run_mcp_calls_blocking_cleanup_in_finally(monkeypatch: pytest.MonkeyPatch) -> None:
    cleanup_blocking = MagicMock()
    register = MagicMock()
    run = MagicMock(side_effect=RuntimeError("boom"))
    set_stateless = MagicMock()

    monkeypatch.setattr(run_commands, "_cleanup_mcp_resources_blocking", cleanup_blocking)
    monkeypatch.setattr(run_commands.atexit, "register", register)
    monkeypatch.setattr(run_commands.mcp, "run", run)
    monkeypatch.setattr(run_commands, "set_stateless_http_mode", set_stateless)

    with pytest.raises(RuntimeError, match="boom"):
        run_commands.run_mcp()

    register.assert_called_once_with(run_commands._cleanup_mcp_resources_sync)
    run.assert_called_once_with(transport="stdio")
    set_stateless.assert_has_calls([call(False), call(False)])
    cleanup_blocking.assert_called_once()


def test_run_mcp_http_transport_wires_auth_middleware(monkeypatch: pytest.MonkeyPatch) -> None:
    cleanup_blocking = MagicMock()
    register = MagicMock()
    run = MagicMock()
    set_stateless = MagicMock()

    monkeypatch.setattr(run_commands, "_cleanup_mcp_resources_blocking", cleanup_blocking)
    monkeypatch.setattr(run_commands.atexit, "register", register)
    monkeypatch.setattr(run_commands.mcp, "run", run)
    monkeypatch.setattr(run_commands, "set_stateless_http_mode", set_stateless)

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
    assert len(middleware) == 1
    assert middleware[0].cls is run_commands.MCPAPIKeyMiddleware
    set_stateless.assert_has_calls([call(True), call(False)])
    cleanup_blocking.assert_called_once()


def test_run_task_tool_registration_points_to_browser_module() -> None:
    tool = run_commands.mcp._tool_manager._tools["skyvern_run_task"]  # type: ignore[attr-defined]
    assert tool.fn.__module__ == "skyvern.cli.mcp_tools.browser"
