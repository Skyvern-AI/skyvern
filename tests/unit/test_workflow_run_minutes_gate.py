"""Run minutes count compute, so a workflow run that never reached ``running``
must contribute no minutes (SKY-14608) -- but the exclusion itself is exported,
tagged ``excluded_reason="never_started"``, so the removed cohort stays
observable instead of silently vanishing.

Both terminal writers derive ``duration_seconds`` from
``COALESCE(started_at, created_at)``. On a run finalized straight out of the
queue that fallback measures queue age, and the run held no pod at all -- which
is why the emission, not the duration, is what carries the exclusion. The two
writers carry independent copies of the logic, so both are covered here.

The same rule governs the task_v1 emitter in ``Agent.update_task``, which reads
the task once on entry and finalizes it later: it is covered here too, because
only the post-claim row can say whether the task started.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock, MagicMock

import pytest

from skyvern.forge import agent as agent_module
from skyvern.forge import app
from skyvern.forge.agent import ForgeAgent
from skyvern.forge.sdk.schemas.tasks import Task, TaskStatus
from skyvern.forge.sdk.workflow.models.workflow import WorkflowRunStatus
from skyvern.forge.sdk.workflow.service import WorkflowService


def _make_row(*, started: bool) -> MagicMock:
    now = datetime.now(UTC)
    row = MagicMock()
    row.workflow_run_id = "wr_gate"
    row.workflow_id = "wf_gate"
    row.organization_id = "org_gate"
    row.parent_workflow_run_id = None
    row.created_at = now - timedelta(minutes=30)
    row.started_at = (now - timedelta(minutes=20)) if started else None
    row.status = WorkflowRunStatus.canceled
    row.run_with = None
    row.ai_fallback = False
    row.trigger_type = None
    row.workflow_schedule_id = None
    row.failure_category = None
    return row


@pytest.fixture
def record_run_duration(monkeypatch: pytest.MonkeyPatch) -> AsyncMock:
    emitter = AsyncMock()
    monkeypatch.setattr(app.AGENT_FUNCTION, "record_run_duration", emitter)
    monkeypatch.setattr(WorkflowService, "_apply_completion_run_tags_best_effort", AsyncMock())
    monkeypatch.setattr(WorkflowService, "_schedule_workflow_run_terminal_hooks", MagicMock())
    monkeypatch.setattr(WorkflowService, "_sync_task_run_from_workflow_run", AsyncMock())
    return emitter


def _assert_emission(record_run_duration: AsyncMock, *, started: bool) -> None:
    assert record_run_duration.await_count == 1
    kwargs = record_run_duration.await_args.kwargs
    if started:
        assert kwargs["excluded_reason"] is None
        assert kwargs["duration_seconds"] == pytest.approx(20 * 60, abs=5)
    else:
        # Excluded, not silent: the recorder turns this into a zero-minute sample
        # tagged excluded=never_started, so sums stay compute-only while the
        # exclusion stays countable.
        assert kwargs["excluded_reason"] == "never_started"


@pytest.mark.asyncio
@pytest.mark.parametrize("started", [True, False])
async def test_terminal_write_emits_minutes_only_for_runs_that_started(
    record_run_duration: AsyncMock,
    started: bool,
) -> None:
    await WorkflowService()._after_workflow_run_status_write(_make_row(started=started), WorkflowRunStatus.canceled)

    _assert_emission(record_run_duration, started=started)


@pytest.mark.asyncio
@pytest.mark.parametrize("started", [True, False])
async def test_conditional_cancel_emits_minutes_only_for_runs_that_started(
    monkeypatch: pytest.MonkeyPatch,
    record_run_duration: AsyncMock,
    started: bool,
) -> None:
    row = _make_row(started=started)
    monkeypatch.setattr(
        app.DATABASE.workflow_runs,
        "update_workflow_run_if_not_final",
        AsyncMock(return_value=row),
    )

    await WorkflowService().mark_workflow_run_as_canceled_if_not_final(workflow_run_id="wr_gate")

    _assert_emission(record_run_duration, started=started)


def _make_task(*, status: TaskStatus, started_at: datetime | None, finished_at: datetime | None = None) -> Task:
    now = datetime.now(UTC)
    return Task(
        task_id="tsk_gate",
        organization_id="org_gate",
        url="https://example.com",
        status=status,
        created_at=now - timedelta(minutes=30),
        modified_at=now,
        started_at=started_at,
        finished_at=finished_at,
        workflow_run_id=None,
    )


@pytest.mark.asyncio
async def test_task_v1_emission_reads_started_at_from_the_claim_not_the_entry_read(
    monkeypatch: pytest.MonkeyPatch,
    record_run_duration: AsyncMock,
) -> None:
    """A worker can stamp ``started_at`` between ``update_task``'s entry read and its
    finished_at claim. Deciding the exclusion off the entry read would bill that
    task's real compute as ``never_started`` and emit zero minutes for it.
    """
    now = datetime.now(UTC)
    entry_task = _make_task(status=TaskStatus.queued, started_at=None)
    claimed_task = _make_task(
        status=TaskStatus.canceled,
        started_at=now - timedelta(minutes=9),
        finished_at=now,
    )
    monkeypatch.setattr(app.DATABASE.tasks, "get_task", AsyncMock(return_value=entry_task))
    monkeypatch.setattr(
        app.DATABASE.tasks,
        "update_task_and_claim_finish",
        AsyncMock(return_value=(claimed_task, True)),
    )
    monkeypatch.setattr(agent_module, "save_task_logs", AsyncMock())

    await ForgeAgent().update_task(entry_task, status=TaskStatus.canceled)

    assert record_run_duration.await_count == 1
    kwargs = record_run_duration.await_args.kwargs
    assert kwargs.get("excluded_reason") is None
    assert kwargs["duration_seconds"] == pytest.approx(9 * 60, abs=5)
