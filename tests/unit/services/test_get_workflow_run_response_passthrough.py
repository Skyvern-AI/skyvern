"""Regression guard: get_workflow_run_response must pass through all WorkflowRun fields."""

from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from skyvern.exceptions import BlockedHost
from skyvern.forge.sdk.routes.agent_protocol import _workflow_run_request_from_workflow_request
from skyvern.forge.sdk.workflow.models.workflow import WorkflowRun, WorkflowRunStatus
from skyvern.schemas.runs import RunStatus, ScriptRunResponse, WorkflowRunRequest
from skyvern.services.workflow_service import get_workflow_run_response, workflow_request_body_from_existing_run


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
    assert resp.script_id == "s_abc"
    assert resp.ai_fallback is True
    assert resp.browser_session_id == "pbs_123"
    assert resp.max_screenshot_scrolls == 5
    assert resp.run_with == "code"
    assert resp.status == RunStatus.completed
    assert resp.step_count == 4
    assert resp.run_request is not None
    assert resp.run_request.browser_session_id == "pbs_123"


def _fresh_run(*, start_fresh: bool, session_id: str | None) -> WorkflowRun:
    now = datetime.now(timezone.utc)
    return WorkflowRun(
        workflow_run_id="wr_f",
        workflow_id="w_f",
        workflow_permanent_id="wpid_f",
        organization_id="o_f",
        status=WorkflowRunStatus.failed,
        browser_session_id=session_id,
        start_fresh_browser=start_fresh,
        created_at=now,
        modified_at=now,
    )


def test_retry_omits_session_id_for_fresh_run() -> None:
    # A fresh run created under FORCE_BROWSER_SESSION carries a generated PBS; the retry must omit it,
    # or the start_fresh + browser_session_id validator rejects the reconstruction.
    body = workflow_request_body_from_existing_run(_fresh_run(start_fresh=True, session_id="pbs_forced"))
    assert body.start_fresh_browser is True
    assert body.browser_session_id is None


def test_retry_keeps_session_id_when_not_fresh() -> None:
    body = workflow_request_body_from_existing_run(_fresh_run(start_fresh=False, session_id="pbs_keep"))
    assert body.browser_session_id == "pbs_keep"


def test_persisted_private_browser_address_is_allowed_only_for_reconstruction() -> None:
    workflow_run = _fresh_run(start_fresh=False, session_id=None)
    workflow_run.browser_address = "ws://10.0.0.5:9222"

    body = workflow_request_body_from_existing_run(workflow_run)
    reconstructed = _workflow_run_request_from_workflow_request(
        workflow_id=workflow_run.workflow_permanent_id,
        title=None,
        workflow_request=body,
    )

    assert body.browser_address == "ws://10.0.0.5:9222"
    assert reconstructed.browser_address == "ws://10.0.0.5:9222"
    with pytest.raises(BlockedHost):
        WorkflowRunRequest(workflow_id="wpid_f", browser_address="ws://10.0.0.5:9222")


@pytest.mark.asyncio
async def test_get_workflow_run_response_reconstructs_private_browser_address() -> None:
    workflow_run = _fresh_run(start_fresh=False, session_id=None)
    workflow_run.browser_address = "ws://10.0.0.5:9222"
    status_resp = MagicMock(
        outputs={},
        downloaded_files=None,
        recording_url=None,
        recording_archived=False,
        screenshot_urls=None,
        failure_reason=None,
        workflow_title="Test",
        parameters={},
        errors=None,
        total_steps=1,
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
        response = await get_workflow_run_response("wr_f", organization_id="o_f")

    assert response is not None
    assert response.run_request is not None
    assert response.run_request.browser_address == "ws://10.0.0.5:9222"


@pytest.mark.asyncio
async def test_get_workflow_run_response_echoes_fresh_and_drops_session() -> None:
    # A fresh run's run_request echoes start_fresh_browser and drops the session/profile so it stays
    # valid under the mutually-exclusive validators (a FORCE_BROWSER_SESSION run has a generated PBS).
    now = datetime.now(timezone.utc)
    workflow_run = WorkflowRun(
        workflow_run_id="wr_f",
        workflow_id="w_f",
        workflow_permanent_id="wpid_f",
        organization_id="o_f",
        status=WorkflowRunStatus.completed,
        browser_session_id="pbs_forced",
        start_fresh_browser=True,
        created_at=now,
        modified_at=now,
    )
    status_resp = MagicMock(
        outputs={},
        downloaded_files=None,
        recording_url=None,
        screenshot_urls=None,
        failure_reason=None,
        workflow_title="T",
        parameters={},
        errors=None,
        total_steps=1,
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
        resp = await get_workflow_run_response("wr_f", organization_id="o_f")

    assert resp is not None
    assert resp.run_request is not None
    assert resp.run_request.start_fresh_browser is True
    assert resp.run_request.browser_session_id is None
    # The top-level field still surfaces the actual session the run used.
    assert resp.browser_session_id == "pbs_forced"
