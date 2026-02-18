from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock

import pytest

from skyvern.cli import run_commands


@pytest.fixture(autouse=True)
def _reset_cleanup_state() -> None:
    run_commands._mcp_cleanup_done = False


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

    monkeypatch.setattr(run_commands, "_cleanup_mcp_resources_blocking", cleanup_blocking)
    monkeypatch.setattr(run_commands.atexit, "register", register)
    monkeypatch.setattr(run_commands.mcp, "run", run)

    with pytest.raises(RuntimeError, match="boom"):
        run_commands.run_mcp()

    register.assert_called_once_with(run_commands._cleanup_mcp_resources_sync)
    run.assert_called_once_with(transport="stdio")
    cleanup_blocking.assert_called_once()
