"""Tests for task_run status write-through sync."""

from unittest.mock import AsyncMock, MagicMock

import pytest

from skyvern.forge.sdk.db.agent_db import AgentDB


@pytest.fixture
def mock_session():
    session = AsyncMock()
    session.__aenter__ = AsyncMock(return_value=session)
    session.__aexit__ = AsyncMock(return_value=False)
    return session


@pytest.fixture
def agent_db(mock_session):
    db = AgentDB.__new__(AgentDB)
    db.Session = MagicMock(return_value=mock_session)
    # Set up tasks repository (sync_task_run_status delegates to self.tasks)
    from skyvern.forge.sdk.db.repositories.tasks import TasksRepository

    tasks = TasksRepository.__new__(TasksRepository)
    tasks.Session = MagicMock(return_value=mock_session)
    tasks.debug_enabled = False
    tasks._is_retryable_error_fn = None
    db.tasks = tasks
    return db


@pytest.mark.asyncio
async def test_sync_task_run_status_updates_matching_row(agent_db, mock_session):
    """sync_task_run_status should UPDATE task_runs where run_id matches."""
    await agent_db.sync_task_run_status(
        organization_id="org_1",
        run_id="wr_123",
        status="failed",
    )
    mock_session.execute.assert_called_once()
    call_args = mock_session.execute.call_args
    # The SQL should be an UPDATE on task_runs
    sql_text = str(call_args[0][0])
    assert "task_runs" in sql_text
    assert "status" in sql_text


@pytest.mark.asyncio
async def test_sync_task_run_status_no_raise_on_error(agent_db, mock_session):
    """sync_task_run_status should swallow exceptions (best-effort)."""
    mock_session.execute.side_effect = Exception("DB error")
    # Should NOT raise
    await agent_db.sync_task_run_status(
        organization_id="org_1",
        run_id="nonexistent",
        status="failed",
    )


def test_terminal_statuses_match_run_status():
    """Guard: TERMINAL_STATUSES and RunStatus.is_final() must agree.

    If this fails, a new terminal status was added to one but not the other.
    Update TERMINAL_STATUSES in skyvern/schemas/runs.py (the single source of truth).
    """
    from skyvern.forge.sdk.schemas.runs import TERMINAL_STATUSES
    from skyvern.schemas.runs import RunStatus

    assert set(TERMINAL_STATUSES) == {s.value for s in RunStatus if s.is_final()}
