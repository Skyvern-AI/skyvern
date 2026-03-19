import sys
from types import ModuleType, SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from workers.run_parameters import RunSkyvernWorkflowParams

from skyvern.forge.sdk.db.enums import WorkflowRunTriggerType
from skyvern.forge.sdk.workflow.models.workflow import WorkflowRunStatus
from tests.unit.worker_activity_import_helpers import import_temporal_v2_worker_activities


@pytest.mark.asyncio
async def test_setup_scheduled_workflow_run_sets_trigger_metadata(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    activities = import_temporal_v2_worker_activities(monkeypatch)
    created_workflow_run = SimpleNamespace(workflow_run_id="wr_sched_123", status=WorkflowRunStatus.created)
    queued_workflow_run = SimpleNamespace(workflow_run_id="wr_sched_123", status=WorkflowRunStatus.queued)
    organization = SimpleNamespace(organization_id="org_123")
    workflow = SimpleNamespace(title="Scheduled Workflow")

    fake_db = SimpleNamespace(
        get_workflow_run=AsyncMock(return_value=None),
        get_organization=AsyncMock(return_value=organization),
        update_workflow_run=AsyncMock(return_value=queued_workflow_run),
        get_run=AsyncMock(return_value=None),
        create_task_run=AsyncMock(),
    )
    fake_workflow_service = SimpleNamespace(
        setup_workflow_run=AsyncMock(return_value=created_workflow_run),
        get_workflow_by_permanent_id=AsyncMock(return_value=workflow),
    )

    monkeypatch.setattr(activities.app, "DATABASE", fake_db)
    monkeypatch.setattr(activities.app, "WORKFLOW_SERVICE", fake_workflow_service)

    result = await activities.setup_scheduled_workflow_run(
        organization_id="org_123",
        workflow_permanent_id="wpid_123",
        workflow_schedule_id="ws_123",
        temporal_workflow_id="temporal_sched_123",
        parameters={"key": "value"},
        workflow_run_id="wr_sched_123",
    )

    assert result == "wr_sched_123"
    assert (
        fake_workflow_service.setup_workflow_run.await_args.kwargs["trigger_type"] == WorkflowRunTriggerType.scheduled
    )
    assert fake_workflow_service.setup_workflow_run.await_args.kwargs["workflow_schedule_id"] == "ws_123"
    fake_db.update_workflow_run.assert_awaited_once_with(
        workflow_run_id="wr_sched_123",
        status=WorkflowRunStatus.queued,
        job_id="temporal_sched_123",
    )
    fake_db.create_task_run.assert_awaited_once()


@pytest.mark.asyncio
async def test_setup_scheduled_workflow_run_reuses_existing_run_without_duplicate_task_run(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    activities = import_temporal_v2_worker_activities(monkeypatch)
    existing_workflow_run = SimpleNamespace(workflow_run_id="wr_sched_123", status=WorkflowRunStatus.queued)
    organization = SimpleNamespace(organization_id="org_123")

    fake_db = SimpleNamespace(
        get_workflow_run=AsyncMock(return_value=existing_workflow_run),
        get_organization=AsyncMock(return_value=organization),
        update_workflow_run=AsyncMock(),
        get_run=AsyncMock(return_value=SimpleNamespace(run_id="wr_sched_123")),
        create_task_run=AsyncMock(),
    )
    fake_workflow_service = SimpleNamespace(
        setup_workflow_run=AsyncMock(),
        get_workflow_by_permanent_id=AsyncMock(return_value=SimpleNamespace(title="Scheduled Workflow")),
    )

    monkeypatch.setattr(activities.app, "DATABASE", fake_db)
    monkeypatch.setattr(activities.app, "WORKFLOW_SERVICE", fake_workflow_service)

    result = await activities.setup_scheduled_workflow_run(
        organization_id="org_123",
        workflow_permanent_id="wpid_123",
        workflow_schedule_id="ws_123",
        temporal_workflow_id="temporal_sched_123",
        parameters={"key": "value"},
        workflow_run_id="wr_sched_123",
    )

    assert result == "wr_sched_123"
    fake_workflow_service.setup_workflow_run.assert_not_awaited()
    fake_db.update_workflow_run.assert_not_awaited()
    fake_db.create_task_run.assert_not_awaited()


@pytest.mark.asyncio
async def test_setup_scheduled_workflow_run_requeues_existing_created_run(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    activities = import_temporal_v2_worker_activities(monkeypatch)
    existing_workflow_run = SimpleNamespace(workflow_run_id="wr_sched_123", status=WorkflowRunStatus.created)
    queued_workflow_run = SimpleNamespace(workflow_run_id="wr_sched_123", status=WorkflowRunStatus.queued)
    organization = SimpleNamespace(organization_id="org_123")

    fake_db = SimpleNamespace(
        get_workflow_run=AsyncMock(return_value=existing_workflow_run),
        get_organization=AsyncMock(return_value=organization),
        update_workflow_run=AsyncMock(return_value=queued_workflow_run),
        get_run=AsyncMock(return_value=None),
        create_task_run=AsyncMock(),
    )
    fake_workflow_service = SimpleNamespace(
        setup_workflow_run=AsyncMock(),
        get_workflow_by_permanent_id=AsyncMock(return_value=SimpleNamespace(title="Scheduled Workflow")),
    )

    monkeypatch.setattr(activities.app, "DATABASE", fake_db)
    monkeypatch.setattr(activities.app, "WORKFLOW_SERVICE", fake_workflow_service)

    result = await activities.setup_scheduled_workflow_run(
        organization_id="org_123",
        workflow_permanent_id="wpid_123",
        workflow_schedule_id="ws_123",
        temporal_workflow_id="temporal_sched_123",
        parameters={"key": "value"},
        workflow_run_id="wr_sched_123",
    )

    assert result == "wr_sched_123"
    fake_workflow_service.setup_workflow_run.assert_not_awaited()
    fake_db.update_workflow_run.assert_awaited_once_with(
        workflow_run_id="wr_sched_123",
        status=WorkflowRunStatus.queued,
        job_id="temporal_sched_123",
    )
    fake_db.create_task_run.assert_awaited_once()


@pytest.mark.asyncio
async def test_run_workflow_activity_rejects_scheduled_runs_without_workflow_permanent_id(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    activities = import_temporal_v2_worker_activities(monkeypatch)
    run_workflow_module = ModuleType("scripts.run_workflow")
    run_workflow_module.execute_workflow = AsyncMock()
    monkeypatch.setitem(sys.modules, "scripts.run_workflow", run_workflow_module)
    monkeypatch.setattr(
        activities.otel_trace,
        "get_current_span",
        lambda: SimpleNamespace(set_attribute=lambda *args, **kwargs: None),
    )
    monkeypatch.setattr(activities.os, "makedirs", lambda *args, **kwargs: None)

    with pytest.raises(RuntimeError, match="workflow_permanent_id"):
        await activities.run_workflow_activity(
            RunSkyvernWorkflowParams(
                organization_id="org_123",
                workflow_run_id="",
                is_scheduled_run=True,
                workflow_schedule_id="ws_123",
            )
        )


@pytest.mark.asyncio
async def test_run_workflow_activity_rejects_scheduled_runs_without_schedule_id(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    activities = import_temporal_v2_worker_activities(monkeypatch)
    run_workflow_module = ModuleType("scripts.run_workflow")
    run_workflow_module.execute_workflow = AsyncMock()
    monkeypatch.setitem(sys.modules, "scripts.run_workflow", run_workflow_module)
    monkeypatch.setattr(
        activities.otel_trace,
        "get_current_span",
        lambda: SimpleNamespace(set_attribute=lambda *args, **kwargs: None),
    )
    monkeypatch.setattr(activities.os, "makedirs", lambda *args, **kwargs: None)

    with pytest.raises(RuntimeError, match="workflow_schedule_id"):
        await activities.run_workflow_activity(
            RunSkyvernWorkflowParams(
                organization_id="org_123",
                workflow_run_id="",
                workflow_permanent_id="wpid_123",
                is_scheduled_run=True,
            )
        )
