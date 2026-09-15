"""Tests for task_run status write-through sync."""

from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock, MagicMock

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncEngine, async_sessionmaker

from skyvern.forge.sdk.db.agent_db import AgentDB
from skyvern.forge.sdk.db.models import TaskRunModel, WorkflowRunModel
from skyvern.forge.sdk.db.repositories.tasks import TasksRepository


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
    await agent_db.tasks.sync_task_run_status(
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
    await agent_db.tasks.sync_task_run_status(
        organization_id="org_1",
        run_id="nonexistent",
        status="failed",
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("current_status", ["completed", "failed", "running"])
@pytest.mark.parametrize("run_id, run_type", [("wr_retry", "workflow_run"), ("tsk_v2_retry", "task_v2")])
async def test_delayed_workflow_sync_preserves_current_attempt(
    sqlite_engine: AsyncEngine, current_status: str, run_id: str, run_type: str
) -> None:
    factory = async_sessionmaker(sqlite_engine, expire_on_commit=False)
    repo = TasksRepository(session_factory=factory, debug_enabled=False)
    first_started_at = datetime(2026, 1, 1, tzinfo=UTC).replace(tzinfo=None)
    first_finished_at = first_started_at + timedelta(minutes=1)
    current_started_at = first_finished_at + timedelta(minutes=1)
    current_finished_at = current_started_at + timedelta(minutes=1) if current_status != "running" else None
    async with factory() as session:
        workflow_run = WorkflowRunModel(
            workflow_run_id="wr_retry",
            workflow_id="w_retry",
            workflow_permanent_id="wpid_retry",
            organization_id="org_1",
            status="failed",
            started_at=first_started_at,
            finished_at=first_finished_at,
        )
        task_run = TaskRunModel(run_id=run_id, task_run_type=run_type, organization_id="org_1", status="running")
        session.add_all([workflow_run, task_run])
        await session.commit()

        # The fire-and-forget sync captures attempt 1 before the retry decision.
        delayed_payload = {
            "organization_id": "org_1",
            "run_id": run_id,
            "source_workflow_run_id": workflow_run.workflow_run_id,
            "status": workflow_run.status,
            "started_at": workflow_run.started_at,
            "finished_at": workflow_run.finished_at,
        }

        # Retry preparation reopens the same source row; attempt 2 then advances.
        workflow_run.status = "queued"
        workflow_run.finished_at = None
        await session.commit()
        workflow_run.status = current_status
        workflow_run.started_at = current_started_at
        workflow_run.finished_at = current_finished_at
        await session.commit()

    await repo.sync_task_run_status(
        organization_id="org_1",
        run_id=run_id,
        source_workflow_run_id="wr_retry",
        status=current_status,
        started_at=current_started_at,
        finished_at=current_finished_at,
    )
    async with factory() as session:
        current = await session.scalar(select(TaskRunModel).where(TaskRunModel.run_id == run_id))
        assert (current.status, current.started_at, current.finished_at) == (
            current_status,
            current_started_at,
            current_finished_at,
        )

    # Deliver the older payload only after attempt 2 has already been synced.
    await repo.sync_task_run_status(**delayed_payload)
    async with factory() as session:
        current = await session.scalar(select(TaskRunModel).where(TaskRunModel.run_id == run_id))
        assert (current.status, current.started_at, current.finished_at) == (
            current_status,
            current_started_at,
            current_finished_at,
        )


def test_terminal_statuses_match_run_status():
    """Guard: TERMINAL_STATUSES and RunStatus.is_final() must agree.

    If this fails, a new terminal status was added to one but not the other.
    Update TERMINAL_STATUSES in skyvern/schemas/runs.py (the single source of truth).
    """
    from skyvern.forge.sdk.schemas.runs import TERMINAL_STATUSES
    from skyvern.schemas.runs import RunStatus

    assert set(TERMINAL_STATUSES) == {s.value for s in RunStatus if s.is_final()}
