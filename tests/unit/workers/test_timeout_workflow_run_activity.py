import importlib
from datetime import datetime, timezone
from unittest.mock import AsyncMock, patch

import pytest

from skyvern.forge.sdk.workflow.models.workflow import WorkflowRun, WorkflowRunStatus
from workers.run_parameters import TimeoutWorkflowRunParams

importlib.import_module("cloud")
timeout_workflow_run_activity = importlib.import_module(
    "workers.temporal_v2_worker.activities"
).timeout_workflow_run_activity


def _make_wr(status: str = "running") -> WorkflowRun:
    now = datetime.now(timezone.utc)
    return WorkflowRun(
        workflow_run_id="wr_1",
        workflow_id="wf_1",
        workflow_permanent_id="wpid_1",
        organization_id="o_1",
        status=WorkflowRunStatus(status),
        created_at=now,
        modified_at=now,
    )


@pytest.mark.asyncio
async def test_timeout_activity_does_not_send_webhook_anymore() -> None:
    params = TimeoutWorkflowRunParams(workflow_run_id="wr_1", organization_id="o_1")
    fake_run = _make_wr(status="running")

    with (
        patch("skyvern.forge.app.DATABASE.workflow_runs.get_workflow_run", AsyncMock(return_value=fake_run)),
        patch(
            "skyvern.forge.app.WORKFLOW_SERVICE.mark_workflow_run_as_timed_out",
            AsyncMock(return_value=_make_wr(status="timed_out")),
        ),
        patch("skyvern.forge.app.WORKFLOW_SERVICE.execute_workflow_webhook", AsyncMock()) as webhook_mock,
    ):
        await timeout_workflow_run_activity(params)

    webhook_mock.assert_not_awaited()


@pytest.mark.asyncio
async def test_timeout_activity_still_marks_db_as_timed_out() -> None:
    params = TimeoutWorkflowRunParams(workflow_run_id="wr_1", organization_id="o_1")
    fake_run = _make_wr(status="running")
    mark_mock = AsyncMock(return_value=_make_wr(status="timed_out"))
    with (
        patch("skyvern.forge.app.DATABASE.workflow_runs.get_workflow_run", AsyncMock(return_value=fake_run)),
        patch("skyvern.forge.app.WORKFLOW_SERVICE.mark_workflow_run_as_timed_out", mark_mock),
    ):
        await timeout_workflow_run_activity(params)
    mark_mock.assert_awaited_once_with("wr_1")
