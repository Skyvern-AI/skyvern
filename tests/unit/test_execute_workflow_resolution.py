"""Unit tests for execute_workflow's early resolution and terminal short-circuits."""

from types import SimpleNamespace
from typing import Any, cast
from unittest.mock import AsyncMock

import pytest

from skyvern.forge.sdk.workflow import service as service_module
from skyvern.forge.sdk.workflow.models.workflow import WorkflowRunStatus
from skyvern.forge.sdk.workflow.service import WorkflowService


class _StopForTest(Exception):
    """Sentinel to abort execute_workflow right after workflow resolution."""


@pytest.mark.asyncio
async def test_execute_workflow_resolves_by_run_workflow_id(monkeypatch: pytest.MonkeyPatch) -> None:
    captured: dict[str, Any] = {}

    async def _capture_resolution(**kwargs: Any) -> Any:
        captured.update(kwargs)
        raise _StopForTest

    workflow_run = SimpleNamespace(
        workflow_permanent_id="wpid_1",
        workflow_id="w_v7",
        status=WorkflowRunStatus.queued,
    )
    service = WorkflowService()
    monkeypatch.setattr(service, "get_workflow_run", AsyncMock(return_value=workflow_run))
    # Latest-by-permanent-id must NOT be used for execution resolution anymore.
    monkeypatch.setattr(
        service,
        "get_workflow_by_permanent_id",
        AsyncMock(side_effect=AssertionError("execution must resolve by run.workflow_id")),
    )
    monkeypatch.setattr(service, "get_workflow", _capture_resolution)

    organization = SimpleNamespace(organization_id="o_1")
    with pytest.raises(_StopForTest):
        await service.execute_workflow(
            workflow_run_id="wr_1",
            api_key="k",
            organization=cast(Any, organization),
        )

    # The exact version stamped on the run executes, not latest-by-permanent-id.
    assert captured["workflow_id"] == "w_v7"


@pytest.mark.asyncio
async def test_execute_workflow_canceled_run_skips_resolution(monkeypatch: pytest.MonkeyPatch) -> None:
    """A run canceled while queued short-circuits BEFORE workflow resolution, so a run whose
    stamped version was deleted after cancellation does not raise WorkflowNotFound."""
    workflow_run = SimpleNamespace(
        workflow_permanent_id="wpid_1",
        workflow_id="w_deleted",
        status=WorkflowRunStatus.canceled,
    )
    service = WorkflowService()
    monkeypatch.setattr(service, "get_workflow_run", AsyncMock(return_value=workflow_run))
    get_workflow = AsyncMock(side_effect=AssertionError("must not resolve a canceled run's workflow"))
    monkeypatch.setattr(service, "get_workflow", get_workflow)

    organization = SimpleNamespace(organization_id="o_1")
    result = await service.execute_workflow(
        workflow_run_id="wr_1",
        api_key="k",
        organization=cast(Any, organization),
    )

    assert result is workflow_run
    get_workflow.assert_not_awaited()


@pytest.mark.asyncio
async def test_execute_workflow_fails_empty_definition_before_marking_running(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    workflow = SimpleNamespace(
        workflow_id="wf_empty",
        workflow_permanent_id="wpid_empty",
        workflow_definition=SimpleNamespace(blocks=[]),
    )
    workflow_run = SimpleNamespace(
        workflow_run_id="wr_empty",
        workflow_id=workflow.workflow_id,
        workflow_permanent_id=workflow.workflow_permanent_id,
        browser_profile_id=None,
        browser_session_id=None,
        browser_address=None,
        run_with="agent",
        status=WorkflowRunStatus.created,
    )
    failed_workflow_run = SimpleNamespace(
        workflow_run_id=workflow_run.workflow_run_id,
        workflow_permanent_id=workflow.workflow_permanent_id,
        status=WorkflowRunStatus.failed,
    )

    service = WorkflowService()
    monkeypatch.setattr(service, "get_workflow_run", AsyncMock(return_value=workflow_run))
    monkeypatch.setattr(service, "get_workflow", AsyncMock(return_value=workflow))
    monkeypatch.setattr(service_module.workflow_script_service, "workflow_has_conditionals", lambda _workflow: False)
    monkeypatch.setattr(service, "bind_browser_action_policy", AsyncMock())
    mark_workflow_run_as_running = AsyncMock(
        side_effect=AssertionError("empty workflow should stop before mark_workflow_run_as_running")
    )
    monkeypatch.setattr(service, "mark_workflow_run_as_running", mark_workflow_run_as_running)
    mark_workflow_run_as_failed = AsyncMock(return_value=failed_workflow_run)
    clean_up_workflow = AsyncMock()
    monkeypatch.setattr(service, "mark_workflow_run_as_failed", mark_workflow_run_as_failed)
    monkeypatch.setattr(service, "clean_up_workflow", clean_up_workflow)

    result = await service.execute_workflow(
        workflow_run_id=workflow_run.workflow_run_id,
        api_key="api_key",
        organization=cast(Any, SimpleNamespace(organization_id="o_test")),
    )

    assert result is failed_workflow_run
    mark_workflow_run_as_failed.assert_awaited_once_with(
        workflow_run_id=workflow_run.workflow_run_id,
        failure_reason="Workflow has no executable blocks.",
    )
    clean_up_workflow.assert_awaited_once_with(
        workflow=workflow,
        workflow_run=failed_workflow_run,
        api_key="api_key",
        browser_session_id=None,
        close_browser_on_completion=True,
        need_call_webhook=True,
    )
    mark_workflow_run_as_running.assert_not_awaited()
