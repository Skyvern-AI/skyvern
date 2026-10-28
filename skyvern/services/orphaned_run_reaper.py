from datetime import datetime, timedelta
from functools import partial

import structlog

from skyvern.forge import app
from skyvern.forge.failure_classifier import WORKER_CONTAINER_RESTARTED_REASON_CODE, classify_from_failure_reason
from skyvern.forge.final_failure_diagnostic import log_diagnostic_unavailable
from skyvern.forge.sdk.db.datetime_utils import naive_utc_now
from skyvern.forge.sdk.db.repositories.run_execution_leases import RunExecutionLease
from skyvern.forge.sdk.executor.background_task_executor import BackgroundTaskExecutor
from skyvern.forge.sdk.executor.factory import AsyncExecutorFactory
from skyvern.forge.sdk.executor.run_liveness import (
    effective_timeout_minutes,
    is_run_registered,
    owner_id,
    schedule_side_effects,
)
from skyvern.forge.sdk.models import StepStatus
from skyvern.forge.sdk.schemas.task_v2 import TaskV2, TaskV2Status
from skyvern.forge.sdk.schemas.tasks import Task, TaskStatus
from skyvern.forge.sdk.services.org_auth_token_service import get_valid_org_api_token
from skyvern.forge.sdk.workflow.models.workflow import WorkflowRun, WorkflowRunStatus
from skyvern.forge.sdk.workflow.retry_policy import get_recorded_decision
from skyvern.services import task_v2_service, uploaded_file_service

LOG = structlog.get_logger()

EXPIRED_SCAN_LIMIT = 500
# One traversal walks the leases already expired when it started, page by page in (heartbeat_at, run_id) order.
# None for both starts a new traversal from the oldest expired lease.
_scan_cutoff: datetime | None = None
_scan_after: tuple[datetime, str] | None = None


async def _api_key(organization_id: str) -> str | None:
    token = await get_valid_org_api_token(app.DATABASE.organizations, organization_id)
    if token is None:
        LOG.warning("Orphaned run webhook skipped: no API key", organization_id=organization_id)
        return None
    return token.token


async def _workflow_side_effects(run: WorkflowRun, attempt_number: int | None, has_attempts: bool) -> None:
    log_diagnostic_unavailable(run.workflow_run_id, "orphan_reaper")
    if has_attempts:
        decision = await get_recorded_decision(run.workflow_run_id, attempt_number=attempt_number)
        if decision is None:
            LOG.warning("Orphaned run has no recorded retry decision", workflow_run_id=run.workflow_run_id)
            return
        if decision.retry:
            executor = AsyncExecutorFactory.get_executor()
            if isinstance(executor, BackgroundTaskExecutor):
                await executor.schedule_recorded_retry(run.workflow_run_id, decision.attempt_number)
            return
        await app.WORKFLOW_SERVICE._run_terminal_side_effects_with_retries(
            run,
            decision,
            api_key=await _api_key(run.organization_id),
        )
        return
    await uploaded_file_service.delete_files_attached_to_run(run_id=run.workflow_run_id)
    api_key = await _api_key(run.organization_id)
    if api_key:
        await app.WORKFLOW_SERVICE.execute_workflow_webhook(run, api_key, claim_kind=None, finalizer="orphan_reaper")


async def _reload_task_v2(run_id: str, task: TaskV2) -> TaskV2:
    try:
        return await app.DATABASE.observer.get_task_v2(task.observer_cruise_id, task.organization_id) or task
    except Exception:
        LOG.warning("Failed to reload reaped task v2", run_id=run_id, exc_info=True)
        return task


async def _reload_task(task: Task) -> Task:
    try:
        return await app.DATABASE.tasks.get_task(task.task_id, task.organization_id) or task
    except Exception:
        LOG.warning("Failed to reload reaped task", run_id=task.task_id, exc_info=True)
        return task


# The runs list is synced before effects are queued: a saturated or cancelled effect queue must not leave it running.
async def _sync_task_v2_run(run_id: str, organization_id: str, task: TaskV2) -> None:
    task = await _reload_task_v2(run_id, task)
    try:
        await app.DATABASE.tasks.sync_task_run_status(
            organization_id=organization_id,
            run_id=task.observer_cruise_id,
            status=task.status.value,
            started_at=task.started_at,
            finished_at=task.finished_at,
        )
    except Exception:
        LOG.warning("Failed to sync reaped task v2", run_id=run_id, exc_info=True)


async def _sync_task_run(task: Task) -> None:
    task = await _reload_task(task)
    try:
        await app.DATABASE.tasks.sync_task_run_status(
            organization_id=task.organization_id,
            run_id=task.task_id,
            status=task.status.value,
            started_at=task.started_at,
            finished_at=task.finished_at,
        )
    except Exception:
        LOG.warning("Failed to sync reaped task", run_id=task.task_id, exc_info=True)


async def _task_v2_side_effects(run: WorkflowRun, task: TaskV2) -> None:
    log_diagnostic_unavailable(run.workflow_run_id, "orphan_reaper")
    task = await _reload_task_v2(run.workflow_run_id, task)
    await uploaded_file_service.delete_files_attached_to_run(run_id=run.workflow_run_id)
    await task_v2_service.send_task_v2_webhook(task)


async def _task_side_effects(task: Task) -> None:
    log_diagnostic_unavailable(task.task_id, "orphan_reaper")
    task = await _reload_task(task)
    await uploaded_file_service.delete_files_attached_to_run(run_id=task.task_id)
    api_key = await _api_key(task.organization_id)
    if api_key:
        await app.agent.execute_task_webhook(task, api_key)


async def _reap_workflow(lease: RunExecutionLease, reason: str, category: list[dict] | None) -> bool:
    run = await app.DATABASE.workflow_runs.get_workflow_run(lease.run_id)
    if run is None or run.status not in (WorkflowRunStatus.running, WorkflowRunStatus.paused):
        return False
    attempts = await app.DATABASE.workflow_run_attempts.get_attempts(lease.run_id)
    if attempts and max(row.attempt_number for row in attempts) != lease.attempt_number:
        return False
    task_v2 = await app.DATABASE.observer.get_task_v2_by_workflow_run_id(lease.run_id, run.organization_id)
    if task_v2 is not None:
        won = await app.DATABASE.observer.update_task_v2_status_if_not_final(
            task_v2.observer_cruise_id,
            run.organization_id,
            TaskV2Status.failed,
            failure_category=category,
        )
        if not won:
            task_v2 = await app.DATABASE.observer.get_task_v2(task_v2.observer_cruise_id, run.organization_id)
            if task_v2 is None:
                return False
            if not task_v2.status.is_final():
                return False
            finalized = await app.WORKFLOW_SERVICE._update_workflow_run_status_if_not_final(
                lease.run_id,
                WorkflowRunStatus(task_v2.status.value),
                only_from=[WorkflowRunStatus.running, WorkflowRunStatus.paused],
                require_latest_attempt_number=lease.attempt_number,
            )
            if finalized is None:
                return False
            await _sync_task_v2_run(lease.run_id, run.organization_id, task_v2)
        else:
            task_v2 = task_v2.model_copy(
                update={
                    "status": TaskV2Status.failed,
                    "failure_category": category,
                    "finished_at": task_v2.finished_at or naive_utc_now(),
                }
            )
            await _sync_task_v2_run(lease.run_id, run.organization_id, task_v2)
            try:
                finalized = await app.WORKFLOW_SERVICE.mark_workflow_run_as_failed_if_not_final(
                    lease.run_id,
                    reason,
                    category,
                    cascade_children=True,
                    only_from=[WorkflowRunStatus.running, WorkflowRunStatus.paused],
                    require_latest_attempt_number=lease.attempt_number,
                    worker_container_restarted=True,
                )
            except Exception:
                finalized = await app.DATABASE.workflow_runs.get_workflow_run(lease.run_id)
                if (
                    finalized is None
                    or finalized.status != WorkflowRunStatus.failed
                    or finalized.failure_reason != reason
                ):
                    raise
                LOG.warning(
                    "Reaped workflow status committed before finalizer failed", run_id=lease.run_id, exc_info=True
                )
            if finalized is None:
                # The task v2 write already committed `failed`, so its webhook and runs-list sync must still go out.
                finalized = run
        schedule_side_effects(partial(_task_v2_side_effects, finalized, task_v2))
        outcome = "task_v2_webhook"
    else:
        try:
            finalized = await app.WORKFLOW_SERVICE.mark_workflow_run_as_failed_if_not_final(
                lease.run_id,
                reason,
                category,
                cascade_children=True,
                only_from=[WorkflowRunStatus.running, WorkflowRunStatus.paused],
                require_latest_attempt_number=lease.attempt_number,
                worker_container_restarted=True,
            )
        except Exception:
            finalized = await app.DATABASE.workflow_runs.get_workflow_run(lease.run_id)
            if finalized is None or finalized.status != WorkflowRunStatus.failed or finalized.failure_reason != reason:
                raise
            LOG.warning("Reaped workflow status committed before finalizer failed", run_id=lease.run_id, exc_info=True)
        if finalized is None:
            return False
        schedule_side_effects(partial(_workflow_side_effects, finalized, lease.attempt_number, bool(attempts)))
        outcome = "retry_decision" if attempts else "webhook"
    LOG.info(
        "Reaped orphaned workflow run",
        run_id=lease.run_id,
        organization_id=run.organization_id,
        attempt_number=lease.attempt_number,
        outcome=outcome,
    )
    return True


async def _reap_task(lease: RunExecutionLease, reason: str) -> bool:
    task = await app.DATABASE.tasks.get_task(lease.run_id)
    if task is None or task.workflow_run_id is not None or task.status != TaskStatus.running:
        return False
    claimed = await app.DATABASE.tasks.bulk_update_tasks(
        [lease.run_id],
        status=TaskStatus.failed,
        failure_reason=reason,
        only_if_status_in=[TaskStatus.running],
    )
    if not claimed:
        return False
    task = task.model_copy(
        update={
            "status": TaskStatus.failed,
            "failure_reason": reason,
            "finished_at": task.finished_at or naive_utc_now(),
        }
    )
    try:
        await app.DATABASE.tasks.bulk_update_steps_by_task_ids(
            [lease.run_id],
            new_status=StepStatus.failed,
            only_if_status_in=[StepStatus.created, StepStatus.running],
        )
    except Exception:
        LOG.warning("Failed to finalize steps for reaped task", run_id=lease.run_id, exc_info=True)
    await _sync_task_run(task)
    schedule_side_effects(partial(_task_side_effects, task))
    LOG.info(
        "Reaped orphaned task",
        run_id=lease.run_id,
        organization_id=task.organization_id,
        attempt_number=lease.attempt_number,
        outcome="webhook",
    )
    return True


async def reap_orphaned_runs_once() -> None:
    timeout = effective_timeout_minutes()
    reason = (
        "This run stopped because the Skyvern server process that ran it exited or stopped responding "
        f"(no heartbeat for {timeout} minutes). Retry the run if needed."
    )
    category = classify_from_failure_reason(
        reason,
        exception_name=WORKER_CONTAINER_RESTARTED_REASON_CODE,
        fallback_to_unknown=True,
    )
    global _scan_after, _scan_cutoff
    repository = app.DATABASE.run_execution_leases
    if _scan_cutoff is None:
        _scan_cutoff = naive_utc_now() - timedelta(minutes=timeout)
    expired = await repository.get_expired(_scan_cutoff, limit=EXPIRED_SCAN_LIMIT, after=_scan_after)
    # Leases this pass cannot reap (registered here, lost claims, failing reaps) stay expired; resuming after this
    # page keeps them from hiding younger expired leases. The cutoff stays fixed for the traversal, so leases that
    # expire meanwhile wait for the next one instead of keeping every page full; a short page starts a new traversal.
    if len(expired) == EXPIRED_SCAN_LIMIT:
        _scan_after = (expired[-1].heartbeat_at, expired[-1].run_id)
    else:
        _scan_after = _scan_cutoff = None
    reaper_id = owner_id() + ":reaper"
    for lease in expired:
        if is_run_registered(lease.run_id):
            continue
        claimed = False
        try:
            if not await repository.claim(lease.run_id, lease.owner_id, lease.heartbeat_at, reaper_id):
                continue
            claimed = True
            won = False
            if lease.run_type == "workflow_run":
                won = await _reap_workflow(lease, reason, category)
            elif lease.run_type == "task":
                won = await _reap_task(lease, reason)
            await repository.release(reaper_id, [lease.run_id])
            if not won:
                LOG.info(
                    "Released stale run lease",
                    run_id=lease.run_id,
                    run_type=lease.run_type,
                    attempt_number=lease.attempt_number,
                )
        except Exception:
            if claimed:
                try:
                    await repository.unclaim(lease.run_id, reaper_id, lease.heartbeat_at)
                except Exception:
                    LOG.exception("Failed to unclaim orphaned run lease", run_id=lease.run_id)
            LOG.exception("Orphaned run reaper pass failed", run_id=lease.run_id)
