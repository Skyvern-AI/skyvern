"""Regression guard: get_workflow_run_response must pass through all WorkflowRun fields."""

from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from skyvern.forge.sdk.workflow.models.workflow import WorkflowRun, WorkflowRunStatus
from skyvern.schemas.runs import RunStatus, ScriptRunResponse
from skyvern.services.workflow_service import get_workflow_run_response


@pytest.mark.asyncio
async def test_get_workflow_run_response_passes_through_all_fields() -> None:
    now = datetime.now(timezone.utc)
    script_run = ScriptRunResponse(
        ai_fallback_triggered=False,
        script_id="s_abc",
        script_revision_id="sr_xyz",
    )
    workflow_run = WorkflowRun(
        workflow_run_id="wr_123",
        workflow_id="w_123",
        workflow_permanent_id="wpid_123",
        organization_id="o_123",
        status=WorkflowRunStatus.completed,
        run_with="code",
        ai_fallback=True,
        browser_session_id="pbs_123",
        browser_profile_id="bp_123",
        max_screenshot_scrolls=5,
        script_run=script_run,
        created_at=now,
        modified_at=now,
        queued_at=now,
        started_at=now,
        finished_at=now,
    )

    status_resp = MagicMock(
        outputs={"key": "value"},
        downloaded_files=None,
        recording_url=None,
        screenshot_urls=None,
        failure_reason=None,
        workflow_title="Test",
        parameters={},
        errors=None,
        total_steps=4,
    )

    with (
        patch(
            "skyvern.services.workflow_service.app.DATABASE.workflow_runs.get_workflow_run",
            new_callable=AsyncMock,
            return_value=workflow_run,
        ),
        patch(
            "skyvern.services.workflow_service.app.WORKFLOW_SERVICE.build_workflow_run_status_response_by_workflow_id",
            new_callable=AsyncMock,
            return_value=status_resp,
        ),
    ):
        resp = await get_workflow_run_response("wr_123", organization_id="o_123")

    assert resp is not None
    assert resp.script_run == script_run
    assert resp.ai_fallback is True
    assert resp.browser_session_id == "pbs_123"
    assert resp.max_screenshot_scrolls == 5
    assert resp.run_with == "code"
    assert resp.status == RunStatus.completed
    assert resp.step_count == 4
    assert resp.run_request is not None
    assert resp.run_request.browser_session_id == "pbs_123"
