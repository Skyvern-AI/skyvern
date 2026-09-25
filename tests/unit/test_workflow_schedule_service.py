from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

import skyvern.forge.sdk.workflow.retry_policy as retry_policy_module
import skyvern.services.workflow_schedule_service as schedule_service
from skyvern.forge.sdk.db.enums import WorkflowRunTriggerType
from skyvern.forge.sdk.schemas.workflow_schedules import WorkflowSchedule
from skyvern.forge.sdk.workflow.models.workflow import WorkflowRunStatus


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
    monkeypatch.setattr(schedule_service, "compute_previous_fire_time", lambda *_args, **_kwargs: previous_fire_time)

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
    monkeypatch.setattr(schedule_service, "compute_previous_fire_time", lambda *_args, **_kwargs: previous_fire_time)

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
        status=WorkflowRunStatus.created,
    )
    prepare_workflow = AsyncMock(return_value=fake_workflow_run)
    initialize_state = AsyncMock()
    prepare_llm = AsyncMock()
    execution_started = asyncio.Event()
    execution_release = asyncio.Event()
    execution_completed = asyncio.Event()

    async def execute_workflow_with_retries(**_kwargs: object) -> None:
        execution_started.set()
        await execution_release.wait()
        execution_completed.set()

    execute_workflow = AsyncMock(side_effect=execute_workflow_with_retries)
    fake_app = SimpleNamespace(
        DATABASE=SimpleNamespace(
            schedules=SimpleNamespace(
                get_all_enabled_schedules=AsyncMock(return_value=[schedule]),
                has_schedule_fired_since=AsyncMock(return_value=False),
            ),
            organizations=SimpleNamespace(get_organization=AsyncMock(return_value=fake_org)),
            workflow_runs=SimpleNamespace(queue_initial_dispatch=AsyncMock(return_value=True)),
        ),
        WORKFLOW_SERVICE=SimpleNamespace(execute_workflow_with_retries=execute_workflow),
    )
    monkeypatch.setattr(schedule_service, "app", fake_app)
    monkeypatch.setattr(retry_policy_module, "app", fake_app)
    monkeypatch.setattr(schedule_service, "compute_previous_fire_time", lambda *_args, **_kwargs: previous_fire_time)
    monkeypatch.setattr(schedule_service, "prepare_workflow", prepare_workflow)
    monkeypatch.setattr(schedule_service, "initialize_skyvern_state_file", initialize_state)
    monkeypatch.setattr(schedule_service, "prepare_org_llm_runtime", prepare_llm)

    scheduler = schedule_service.LocalWorkflowScheduleScheduler(poll_interval_seconds=1, max_concurrent_runs=1)
    tasks = await scheduler.dispatch_due_schedules()
    await asyncio.wait_for(execution_started.wait(), timeout=1)
    assert tasks[0].done() is False
    execution_release.set()
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
    prepare_llm.assert_awaited_once_with(fake_app.DATABASE, "org_test", fake_org)
    assert execution_completed.is_set()
    execute_workflow.assert_awaited_once_with(
        workflow_run_id=expected_workflow_run_id,
        api_key=None,
        organization=fake_org,
        browser_session_id=None,
        block_labels=None,
        block_outputs=None,
        need_call_webhook=True,
        claim_initial_attempt=True,
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("has_attempt_row", [False, True], ids=["no-policy", "policy"])
@pytest.mark.parametrize("initializer", ["initialize_skyvern_state_file", "prepare_org_llm_runtime"])
async def test_run_schedule_no_policy_initializer_failure_fails_the_run(
    monkeypatch: pytest.MonkeyPatch, has_attempt_row: bool, initializer: str
) -> None:
    schedule = _schedule()
    previous_fire_time = datetime(2026, 6, 2, 10, 0, tzinfo=UTC)
    run_id = schedule_service.build_scheduled_workflow_run_id(schedule.workflow_schedule_id, previous_fire_time)
    execute = AsyncMock()
    failed_run = SimpleNamespace(workflow_run_id=run_id, status=WorkflowRunStatus.failed)
    fail_run = AsyncMock(return_value=failed_run)
    webhook = AsyncMock()
    get_attempts = AsyncMock(return_value=[SimpleNamespace(attempt_number=1)] if has_attempt_row else [])
    fake_app = SimpleNamespace(
        DATABASE=SimpleNamespace(
            organizations=SimpleNamespace(
                get_organization=AsyncMock(return_value=SimpleNamespace(organization_id="org_test"))
            ),
            workflow_runs=SimpleNamespace(queue_initial_dispatch=AsyncMock(return_value=True)),
            workflow_run_attempts=SimpleNamespace(get_attempts=get_attempts),
        ),
        WORKFLOW_SERVICE=SimpleNamespace(
            execute_workflow_with_retries=execute,
            mark_workflow_run_as_failed_if_not_final=fail_run,
            execute_workflow_webhook=webhook,
        ),
    )
    monkeypatch.setattr(schedule_service, "app", fake_app)
    monkeypatch.setattr(retry_policy_module, "app", fake_app)
    monkeypatch.setattr(
        schedule_service,
        "prepare_workflow",
        AsyncMock(return_value=SimpleNamespace(workflow_run_id=run_id, browser_session_id=None)),
    )
    monkeypatch.setattr(schedule_service, "initialize_skyvern_state_file", AsyncMock())
    monkeypatch.setattr(schedule_service, "prepare_org_llm_runtime", AsyncMock())
    error = RuntimeError(f"{initializer} unavailable")
    monkeypatch.setattr(schedule_service, initializer, AsyncMock(side_effect=error))
    scheduler = schedule_service.LocalWorkflowScheduleScheduler(poll_interval_seconds=1, max_concurrent_runs=1)
    due = schedule_service.DueWorkflowSchedule(schedule=schedule, previous_fire_time=previous_fire_time)
    if has_attempt_row:
        with pytest.raises(RuntimeError, match=f"{initializer} unavailable"):
            await scheduler._run_schedule(due)
        fail_run.assert_not_awaited()
        webhook.assert_not_awaited()
    else:
        await scheduler._run_schedule(due)
        fail_run.assert_awaited_once_with(
            workflow_run_id=run_id,
            failure_reason=f"Workflow run initialization failed before execution: RuntimeError: {error}",
            cascade_children=False,
        )
        webhook.assert_awaited_once_with(failed_run, api_key=None, claim_kind=None)
    get_attempts.assert_awaited_once_with(run_id)
    fake_app.DATABASE.workflow_runs.queue_initial_dispatch.assert_awaited_once_with(run_id, 1)
    execute.assert_not_awaited()
