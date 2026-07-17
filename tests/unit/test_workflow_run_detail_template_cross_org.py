from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from skyvern.forge import app
from skyvern.forge.sdk.workflow.models.workflow import (
    Workflow,
    WorkflowDefinition,
    WorkflowRunResponseBase,
    WorkflowRunStatus,
    WorkflowStatus,
)
from skyvern.forge.sdk.workflow.service import WorkflowService

TEMPLATE_ORG_ID = "o_template"
CALLER_ORG_ID = "o_caller"


def _make_template_workflow() -> Workflow:
    now = datetime.now(timezone.utc)
    return Workflow(
        workflow_id="w_tmpl",
        organization_id=TEMPLATE_ORG_ID,
        title="Job Application Workflow",
        workflow_permanent_id="wpid_tmpl",
        version=1,
        is_saved_task=False,
        workflow_definition=WorkflowDefinition(parameters=[], blocks=[]),
        created_at=now,
        modified_at=now,
        deleted_at=None,
        status=WorkflowStatus.published,
    )


def _make_workflow_run() -> SimpleNamespace:
    now = datetime.now(timezone.utc)
    return SimpleNamespace(
        workflow_run_id="wr_tmpl",
        workflow_id="w_tmpl",
        organization_id=CALLER_ORG_ID,
        status=WorkflowRunStatus.running,
        failure_reason=None,
        failure_category=None,
        retried_from_workflow_run_id=None,
        proxy_location=None,
        webhook_callback_url=None,
        webhook_failure_reason=None,
        totp_verification_url=None,
        totp_identifier=None,
        extra_http_headers=None,
        cdp_connect_headers=None,
        queued_at=None,
        started_at=now,
        finished_at=None,
        created_at=now,
        modified_at=now,
        credits_used=0,
        cached_credits_used=0,
        browser_session_id=None,
        browser_profile_id=None,
        max_screenshot_scrolls=None,
        browser_address=None,
        run_with=None,
        script_run=None,
    )


@pytest.mark.asyncio
async def test_template_run_detail_resolves_via_run_join(monkeypatch: pytest.MonkeyPatch) -> None:
    template_workflow = _make_template_workflow()
    workflow_run = _make_workflow_run()

    get_by_run = AsyncMock(return_value=template_workflow)
    # The caller-org-scoped permanent-id lookup would miss the template workflow (owned by
    # another org). If the builder still depended on it, this None would 404 the page.
    get_by_wpid = AsyncMock(return_value=None)

    monkeypatch.setattr(
        app,
        "DATABASE",
        SimpleNamespace(
            workflows=SimpleNamespace(
                get_workflow_for_workflow_run=get_by_run,
                get_workflow_by_permanent_id=get_by_wpid,
            ),
            observer=SimpleNamespace(get_task_v2_by_workflow_run_id=AsyncMock(return_value=None)),
            tasks=SimpleNamespace(get_tasks_by_workflow_run_id=AsyncMock(return_value=[])),
            workflow_runs=SimpleNamespace(
                get_workflow_run_parameters=AsyncMock(return_value=[]),
                get_workflow_run_block_errors=AsyncMock(return_value=[]),
                get_workflow_run_retried_by=AsyncMock(return_value=None),
            ),
        ),
    )

    # A real service instance: the stub app's WORKFLOW_SERVICE is a lazy auto-mock, so we must
    # exercise the actual build_workflow_run_status_response rather than app.WORKFLOW_SERVICE.
    service = WorkflowService()
    monkeypatch.setattr(service, "get_workflow_run", AsyncMock(return_value=workflow_run))
    monkeypatch.setattr(service, "get_recent_workflow_screenshot_urls", AsyncMock(return_value=[]))
    monkeypatch.setattr(
        service, "get_output_parameter_workflow_run_output_parameter_tuples", AsyncMock(return_value=[])
    )
    monkeypatch.setattr(service, "_fetch_recording_urls", AsyncMock(return_value=([], False)))
    monkeypatch.setattr(service, "_fetch_downloaded_files", AsyncMock(return_value=([], None)))

    response = await service.build_workflow_run_status_response(
        workflow_permanent_id="wpid_tmpl",
        workflow_run_id="wr_tmpl",
        organization_id=CALLER_ORG_ID,
    )

    assert isinstance(response, WorkflowRunResponseBase)
    assert response.workflow_run_id == "wr_tmpl"
    assert response.workflow_title == "Job Application Workflow"

    # Resolution must go through the run join (scoped by the run's org), not the caller-org
    # permanent-id lookup that returns None for cross-org template workflows.
    get_by_run.assert_awaited_once()
    assert get_by_run.await_args.args[0] == "wr_tmpl"
    assert get_by_run.await_args.kwargs.get("organization_id") == CALLER_ORG_ID
    get_by_wpid.assert_not_awaited()
