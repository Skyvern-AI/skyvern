"""SKY-9185: run detail endpoint must survive parent workflow soft-delete."""

from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from skyvern.exceptions import WorkflowNotFoundForWorkflowRun
from skyvern.forge.sdk.routes import agent_protocol
from skyvern.forge.sdk.workflow.models.workflow import (
    Workflow,
    WorkflowDefinition,
    WorkflowRunResponseBase,
    WorkflowRunStatus,
    WorkflowStatus,
)


def _make_workflow(*, deleted_at: datetime | None) -> Workflow:
    now = datetime.now(timezone.utc)
    return Workflow(
        workflow_id="w_1",
        organization_id="o_1",
        title="Historical Workflow",
        workflow_permanent_id="wpid_1",
        version=1,
        is_saved_task=False,
        workflow_definition=WorkflowDefinition(parameters=[], blocks=[]),
        created_at=now,
        modified_at=now,
        deleted_at=deleted_at,
        status=WorkflowStatus.published,
    )


def _make_status_response() -> WorkflowRunResponseBase:
    now = datetime.now(timezone.utc)
    # ``build_workflow_run_status_response`` populates ``workflow_id`` with the
    # workflow_permanent_id (not the versioned DB id), so mirror that here.
    return WorkflowRunResponseBase(
        workflow_id="wpid_1",
        workflow_run_id="wr_1",
        status=WorkflowRunStatus.completed,
        created_at=now,
        modified_at=now,
        parameters={},
    )


@pytest.mark.asyncio
async def test_run_detail_returns_run_when_parent_workflow_soft_deleted(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    deleted_at = datetime.now(timezone.utc)
    workflow = _make_workflow(deleted_at=deleted_at)
    status_response = _make_status_response()

    get_workflow_mock = AsyncMock(return_value=workflow)
    build_status_mock = AsyncMock(return_value=status_response)
    get_browser_session_mock = AsyncMock(return_value=None)

    monkeypatch.setattr(
        agent_protocol.app.WORKFLOW_SERVICE,
        "get_workflow_by_workflow_run_id",
        get_workflow_mock,
    )
    monkeypatch.setattr(
        agent_protocol.app.WORKFLOW_SERVICE,
        "build_workflow_run_status_response",
        build_status_mock,
    )
    monkeypatch.setattr(
        agent_protocol.app,
        "DATABASE",
        SimpleNamespace(
            browser_sessions=SimpleNamespace(
                get_persistent_browser_session_by_runnable_id=get_browser_session_mock,
            )
        ),
    )

    response = await agent_protocol.get_workflow_and_run_from_workflow_run_id(
        workflow_run_id="wr_1",
        current_org=SimpleNamespace(organization_id="o_1"),
    )

    # Workflow lookup must include soft-deleted rows so historical runs remain accessible.
    get_workflow_mock.assert_awaited_once()
    get_workflow_call = get_workflow_mock.await_args
    assert get_workflow_call is not None
    assert get_workflow_call.kwargs.get("filter_deleted") is False

    # Status response builder must be told to allow deleted workflows.
    build_status_mock.assert_awaited_once()
    build_status_call = build_status_mock.await_args
    assert build_status_call is not None
    assert build_status_call.kwargs.get("allow_deleted") is True

    assert response.workflow.workflow_permanent_id == "wpid_1"
    assert response.workflow.deleted_at == deleted_at
    assert response.workflow_run_id == "wr_1"


@pytest.mark.asyncio
async def test_run_detail_still_404s_when_run_does_not_exist(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        agent_protocol.app.WORKFLOW_SERVICE,
        "get_workflow_by_workflow_run_id",
        AsyncMock(side_effect=WorkflowNotFoundForWorkflowRun(workflow_run_id="wr_missing")),
    )

    with pytest.raises(WorkflowNotFoundForWorkflowRun) as exc_info:
        await agent_protocol.get_workflow_and_run_from_workflow_run_id(
            workflow_run_id="wr_missing",
            current_org=SimpleNamespace(organization_id="o_1"),
        )

    assert exc_info.value.status_code == 404
