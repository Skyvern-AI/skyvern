from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

import skyvern.services.workflow_schedule_service as schedule_service
from skyvern.forge.sdk.db.enums import WorkflowRunTriggerType
from skyvern.forge.sdk.schemas.workflow_schedules import WorkflowSchedule


def _schedule(*, modified_at: datetime | None = None) -> WorkflowSchedule:
    created_at = datetime(2026, 6, 2, 9, 0, tzinfo=UTC)
    return WorkflowSchedule(
        workflow_schedule_id="wfs_test",
        organization_id="org_test",
        workflow_permanent_id="wpid_test",
        cron_expression="0 * * * *",
        timezone="UTC",
        enabled=True,
        parameters={"city": "Toronto"},
        backend_schedule_id="local-wf-sched-wfs_test",
        created_at=created_at,
        modified_at=modified_at or created_at,
    )


def test_build_scheduled_workflow_run_id_is_deterministic() -> None:
    fire_time = datetime(2026, 6, 2, 10, 0, tzinfo=UTC)

    first = schedule_service.build_scheduled_workflow_run_id("wfs_test", fire_time)
    second = schedule_service.build_scheduled_workflow_run_id("wfs_test", fire_time)

    assert first == second
    assert first.startswith("wr_sched_")


@pytest.mark.asyncio
async def test_get_due_schedule_skips_backfill_after_modified_at(monkeypatch: pytest.MonkeyPatch) -> None:
    previous_fire_time = datetime(2026, 6, 2, 10, 0, tzinfo=UTC)
    schedule = _schedule(modified_at=datetime(2026, 6, 2, 10, 1, tzinfo=UTC))
    has_schedule_fired_since = AsyncMock(return_value=False)
    monkeypatch.setattr(
        schedule_service,
        "app",
        SimpleNamespace(
            DATABASE=SimpleNamespace(schedules=SimpleNamespace(has_schedule_fired_since=has_schedule_fired_since))
        ),
    )
    monkeypatch.setattr(schedule_service, "compute_previous_fire_time", lambda *_args: previous_fire_time)

    scheduler = schedule_service.LocalWorkflowScheduleScheduler(poll_interval_seconds=1, max_concurrent_runs=1)

    assert await scheduler._get_due_schedule(schedule) is None
    has_schedule_fired_since.assert_not_awaited()


@pytest.mark.asyncio
async def test_get_due_schedule_skips_when_fire_already_has_run(monkeypatch: pytest.MonkeyPatch) -> None:
    previous_fire_time = datetime(2026, 6, 2, 10, 0, tzinfo=UTC)
    schedule = _schedule(modified_at=datetime(2026, 6, 2, 9, 30, tzinfo=UTC))
    has_schedule_fired_since = AsyncMock(return_value=True)
    monkeypatch.setattr(
        schedule_service,
        "app",
        SimpleNamespace(
            DATABASE=SimpleNamespace(schedules=SimpleNamespace(has_schedule_fired_since=has_schedule_fired_since))
        ),
    )
    monkeypatch.setattr(schedule_service, "compute_previous_fire_time", lambda *_args: previous_fire_time)

    scheduler = schedule_service.LocalWorkflowScheduleScheduler(poll_interval_seconds=1, max_concurrent_runs=1)

    assert await scheduler._get_due_schedule(schedule) is None
    has_schedule_fired_since.assert_awaited_once_with("wfs_test", previous_fire_time)


@pytest.mark.asyncio
async def test_dispatch_due_schedules_launches_scheduled_workflow(monkeypatch: pytest.MonkeyPatch) -> None:
    previous_fire_time = datetime(2026, 6, 2, 10, 0, tzinfo=UTC)
    schedule = _schedule(modified_at=datetime(2026, 6, 2, 9, 30, tzinfo=UTC))
    expected_workflow_run_id = schedule_service.build_scheduled_workflow_run_id(
        schedule.workflow_schedule_id,
        previous_fire_time,
    )

    fake_org = SimpleNamespace(organization_id="org_test")
    fake_workflow_run = SimpleNamespace(
        workflow_run_id=expected_workflow_run_id,
        workflow_id="w_test",
        workflow_permanent_id="wpid_test",
        browser_session_id=None,
    )
    prepare_workflow = AsyncMock(return_value=fake_workflow_run)
    initialize_state = AsyncMock()
    execute_workflow = AsyncMock()
    fake_app = SimpleNamespace(
        DATABASE=SimpleNamespace(
            schedules=SimpleNamespace(
                get_all_enabled_schedules=AsyncMock(return_value=[schedule]),
                has_schedule_fired_since=AsyncMock(return_value=False),
            ),
            organizations=SimpleNamespace(get_organization=AsyncMock(return_value=fake_org)),
        ),
        WORKFLOW_SERVICE=SimpleNamespace(execute_workflow=execute_workflow),
    )
    monkeypatch.setattr(schedule_service, "app", fake_app)
    monkeypatch.setattr(schedule_service, "compute_previous_fire_time", lambda *_args: previous_fire_time)
    monkeypatch.setattr(schedule_service, "prepare_workflow", prepare_workflow)
    monkeypatch.setattr(schedule_service, "initialize_skyvern_state_file", initialize_state)

    scheduler = schedule_service.LocalWorkflowScheduleScheduler(poll_interval_seconds=1, max_concurrent_runs=1)
    tasks = await scheduler.dispatch_due_schedules()
    await asyncio.gather(*tasks)

    assert len(tasks) == 1
    prepare_workflow.assert_awaited_once()
    prepare_kwargs = prepare_workflow.await_args.kwargs
    assert prepare_kwargs["trigger_type"] == WorkflowRunTriggerType.scheduled
    assert prepare_kwargs["workflow_schedule_id"] == "wfs_test"
    assert prepare_kwargs["workflow_run_id"] == expected_workflow_run_id
    assert prepare_kwargs["workflow_request"].data == {"city": "Toronto"}
    initialize_state.assert_awaited_once_with(
        workflow_run_id=expected_workflow_run_id,
        organization_id="org_test",
    )
    execute_workflow.assert_awaited_once_with(
        workflow_run_id=expected_workflow_run_id,
        api_key=None,
        organization=fake_org,
        browser_session_id=None,
    )
