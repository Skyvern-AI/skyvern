from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock

import httpx
import pytest
from fastapi import FastAPI

from skyvern.forge import app
from skyvern.forge.sdk.routes import agent_protocol
from skyvern.forge.sdk.services import org_auth_service
from skyvern.forge.sdk.workflow.models.workflow import (
    Workflow,
    WorkflowDefinition,
    WorkflowRun,
    WorkflowRunResponseBase,
    WorkflowRunStatus,
    WorkflowRunWithWorkflowResponse,
    WorkflowStatus,
)
from skyvern.forge.sdk.workflow.service import WorkflowService
from tests.unit.force_stub_app import make_workflow_run_attempts_fake

TEMPLATE_ORG_ID = "o_template"
CALLER_ORG_ID = "o_caller"


def _make_template_workflow() -> Workflow:
    now = datetime.now(UTC)
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


def _make_workflow_run(
    workflow_run_id: str = "wr_tmpl",
    organization_id: str = CALLER_ORG_ID,
    copilot_session_id: str | None = None,
) -> WorkflowRun:
    now = datetime.now(UTC)
    return WorkflowRun(
        workflow_run_id=workflow_run_id,
        workflow_id="w_tmpl",
        workflow_permanent_id="wpid_tmpl",
        organization_id=organization_id,
        copilot_session_id=copilot_session_id,
        status=WorkflowRunStatus.running,
        started_at=now,
        created_at=now,
        modified_at=now,
    )


@pytest.fixture
def template_run_db(monkeypatch: pytest.MonkeyPatch) -> tuple[AsyncMock, AsyncMock]:
    get_by_run = AsyncMock(return_value=_make_template_workflow())
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
            tasks=SimpleNamespace(
                get_tasks_by_workflow_run_id=AsyncMock(return_value=[]),
                get_step_counts_by_task_ids=AsyncMock(return_value=(0, 0)),
            ),
            browser_sessions=SimpleNamespace(
                get_persistent_browser_session_by_runnable_id=AsyncMock(return_value=None)
            ),
            workflow_runs=SimpleNamespace(
                get_workflow_run_parameters=AsyncMock(return_value=[]),
                get_workflow_run_block_errors=AsyncMock(return_value=[]),
                get_workflow_run_retried_by=AsyncMock(return_value=None),
            ),
            workflow_run_attempts=make_workflow_run_attempts_fake(),
        ),
    )
    return get_by_run, get_by_wpid


@pytest.mark.asyncio
@pytest.mark.parametrize("copilot_session_id", [None, "wcc_123456"])
async def test_template_run_detail_resolves_via_run_join(
    monkeypatch: pytest.MonkeyPatch,
    template_run_db: tuple[AsyncMock, AsyncMock],
    copilot_session_id: str | None,
) -> None:
    get_by_run, get_by_wpid = template_run_db
    workflow_run = _make_workflow_run(copilot_session_id=copilot_session_id)

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
    assert response.copilot_session_id == copilot_session_id

    # Resolution must go through the run join (scoped by the run's org), not the caller-org
    # permanent-id lookup that returns None for cross-org template workflows.
    get_by_run.assert_awaited_once()
    assert get_by_run.await_args.args[0] == "wr_tmpl"
    assert get_by_run.await_args.kwargs.get("organization_id") == CALLER_ORG_ID
    get_by_wpid.assert_not_awaited()

    monkeypatch.setattr(service, "get_workflow_by_workflow_run_id", AsyncMock(return_value=get_by_run.return_value))
    monkeypatch.setattr(app, "WORKFLOW_SERVICE", service)
    monkeypatch.setattr(app.AGENT_FUNCTION, "calculate_workflow_run_total_cost", AsyncMock(return_value=0.0))
    canonical = next(
        route for route in agent_protocol.base_router.routes if route.path == "/workflows/runs/{workflow_run_id}"
    )
    assert canonical.endpoint is agent_protocol.get_workflow_and_run_from_workflow_run_id
    assert canonical.response_model is WorkflowRunWithWorkflowResponse
    api = FastAPI()
    api.dependency_overrides[org_auth_service.get_current_org] = lambda: SimpleNamespace(organization_id=CALLER_ORG_ID)
    api.include_router(agent_protocol.base_router, prefix="/v1")
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=api), base_url="http://test", trust_env=False
    ) as client:
        serialized = await client.get("/v1/workflows/runs/wr_tmpl")

    assert serialized.status_code == 200
    assert serialized.json()["copilot_session_id"] == copilot_session_id
    assert serialized.json()["workflow"]["organization_id"] == TEMPLATE_ORG_ID


@pytest.mark.asyncio
@pytest.mark.usefixtures("template_run_db")
@pytest.mark.parametrize(("passed_run_id", "passed_org_id"), [("wr_other", CALLER_ORG_ID), ("wr_tmpl", "o_other")])
async def test_builder_rejects_a_passed_run_for_another_run_or_org(passed_run_id: str, passed_org_id: str) -> None:
    with pytest.raises(ValueError, match="does not match"):
        await WorkflowService().build_workflow_run_status_response(
            workflow_permanent_id="wpid_tmpl",
            workflow_run_id="wr_tmpl",
            organization_id=CALLER_ORG_ID,
            workflow_run=_make_workflow_run(passed_run_id, passed_org_id),
        )
