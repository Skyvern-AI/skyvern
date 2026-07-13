"""Unit tests for the global execute_workflow resolve-by-run.workflow_id change.

execute_workflow resolves the exact workflow version stamped on the run (get_workflow by
workflow_id) instead of latest-by-permanent-id, and short-circuits a canceled run before that
resolution.
"""

from types import SimpleNamespace
from typing import Any, cast
from unittest.mock import AsyncMock

import pytest

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
