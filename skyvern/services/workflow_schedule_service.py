"""Local OSS workflow scheduler.

Cloud registers schedules with Temporal. OSS runs this lightweight scanner in
the API process and launches due workflow runs directly through the shared
workflow service.
"""

from __future__ import annotations

import asyncio
import hashlib
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime

import structlog
from sqlalchemy.exc import IntegrityError, SQLAlchemyError

from skyvern.config import settings
from skyvern.forge import app
from skyvern.forge.sdk.db.enums import WorkflowRunTriggerType
from skyvern.forge.sdk.schemas.workflow_schedules import WorkflowSchedule
from skyvern.forge.sdk.workflow.models.workflow import WorkflowRequestBody
from skyvern.forge.sdk.workflow.schedules import compute_previous_fire_time
from skyvern.services.workflow_service import prepare_workflow
from skyvern.utils.files import initialize_skyvern_state_file

LOG = structlog.get_logger(__name__)

FireKey = tuple[str, datetime]


@dataclass(frozen=True)
class DueWorkflowSchedule:
    schedule: WorkflowSchedule
    previous_fire_time: datetime


def build_scheduled_workflow_run_id(workflow_schedule_id: str, fire_time: datetime) -> str:
    normalized_fire_time = _as_utc(fire_time)
    digest = hashlib.sha256(f"{workflow_schedule_id}:{normalized_fire_time.isoformat()}".encode()).hexdigest()[:32]
    return f"wr_sched_{digest}"


def _as_utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)


class LocalWorkflowScheduleScheduler:
    def __init__(self, *, poll_interval_seconds: float, max_concurrent_runs: int) -> None:
        self.poll_interval_seconds = poll_interval_seconds
        self.max_concurrent_runs = max(1, max_concurrent_runs)
        self._dispatch_lock = asyncio.Lock()
        self._running_tasks: set[asyncio.Task[None]] = set()
        self._pending_fire_keys: set[FireKey] = set()

    async def run_forever(self) -> None:
        LOG.info(
            "Workflow schedule scheduler started",
            poll_interval_seconds=self.poll_interval_seconds,
            max_concurrent_runs=self.max_concurrent_runs,
        )

        while True:
            try:
                await self.dispatch_due_schedules()
                await asyncio.sleep(self.poll_interval_seconds)
            except asyncio.CancelledError:
                LOG.info("Workflow schedule scheduler cancelled")
                break
            except Exception:
                LOG.exception("Error in workflow schedule scheduler")
                await asyncio.sleep(self.poll_interval_seconds)

    async def dispatch_due_schedules(self) -> list[asyncio.Task[None]]:
        if self._dispatch_lock.locked():
            LOG.debug("Workflow schedule dispatch already in progress")
            return []

        async with self._dispatch_lock:
            self._reap_finished_tasks()
            open_slots = self.max_concurrent_runs - len(self._running_tasks)
            if open_slots <= 0:
                LOG.debug(
                    "Workflow schedule dispatch skipped; concurrency limit reached",
                    running=len(self._running_tasks),
                    max_concurrent_runs=self.max_concurrent_runs,
                )
                return []

            schedules = await app.DATABASE.schedules.get_all_enabled_schedules()
            dispatched: list[asyncio.Task[None]] = []
            for schedule in schedules:
                if len(dispatched) >= open_slots:
                    break

                due = await self._get_due_schedule(schedule)
                if due is None:
                    continue

                fire_key = (schedule.workflow_schedule_id, due.previous_fire_time)
                if fire_key in self._pending_fire_keys:
                    continue

                self._pending_fire_keys.add(fire_key)
                task = asyncio.create_task(
                    self._run_schedule(due),
                    name=f"workflow-schedule-{schedule.workflow_schedule_id}",
                )
                self._running_tasks.add(task)
                task.add_done_callback(self._release_callback(fire_key))
                dispatched.append(task)

            return dispatched

    async def shutdown(self) -> None:
        for task in list(self._running_tasks):
            task.cancel()
        if self._running_tasks:
            await asyncio.gather(*self._running_tasks, return_exceptions=True)
        self._running_tasks.clear()
        self._pending_fire_keys.clear()

    def _reap_finished_tasks(self) -> None:
        for task in list(self._running_tasks):
            if task.done():
                self._running_tasks.discard(task)

    def _release_callback(self, fire_key: FireKey) -> Callable[[asyncio.Task[None]], None]:
        def _callback(task: asyncio.Task[None]) -> None:
            self._release_task(task, fire_key)

        return _callback

    def _release_task(self, task: asyncio.Task[None], fire_key: FireKey) -> None:
        self._running_tasks.discard(task)
        self._pending_fire_keys.discard(fire_key)
        try:
            task.result()
        except asyncio.CancelledError:
            pass
        except Exception:
            LOG.exception("Scheduled workflow task failed")

    async def _get_due_schedule(self, schedule: WorkflowSchedule) -> DueWorkflowSchedule | None:
        try:
            previous_fire_time = _as_utc(compute_previous_fire_time(schedule.cron_expression, schedule.timezone))
        except Exception:
            LOG.warning(
                "Failed to compute previous fire time for workflow schedule",
                workflow_schedule_id=schedule.workflow_schedule_id,
                cron_expression=schedule.cron_expression,
                timezone=schedule.timezone,
                exc_info=True,
            )
            return None

        modified_at = _as_utc(schedule.modified_at)
        if previous_fire_time < modified_at:
            return None

        if await app.DATABASE.schedules.has_schedule_fired_since(
            schedule.workflow_schedule_id,
            previous_fire_time,
        ):
            return None

        return DueWorkflowSchedule(schedule=schedule, previous_fire_time=previous_fire_time)

    async def _run_schedule(self, due: DueWorkflowSchedule) -> None:
        schedule = due.schedule
        workflow_run_id = build_scheduled_workflow_run_id(schedule.workflow_schedule_id, due.previous_fire_time)
        LOG.info(
            "Dispatching scheduled workflow",
            workflow_schedule_id=schedule.workflow_schedule_id,
            workflow_permanent_id=schedule.workflow_permanent_id,
            organization_id=schedule.organization_id,
            previous_fire_time=due.previous_fire_time.isoformat(),
            workflow_run_id=workflow_run_id,
        )

        organization = await app.DATABASE.organizations.get_organization(schedule.organization_id)
        if organization is None:
            LOG.warning(
                "Skipping workflow schedule with missing organization",
                workflow_schedule_id=schedule.workflow_schedule_id,
                organization_id=schedule.organization_id,
            )
            return

        try:
            workflow_run = await prepare_workflow(
                workflow_id=schedule.workflow_permanent_id,
                organization=organization,
                workflow_request=WorkflowRequestBody(data=schedule.parameters),
                request_id=f"schedule:{schedule.workflow_schedule_id}:{due.previous_fire_time.isoformat()}",
                trigger_type=WorkflowRunTriggerType.scheduled,
                workflow_schedule_id=schedule.workflow_schedule_id,
                workflow_run_id=workflow_run_id,
            )
        except IntegrityError:
            LOG.info(
                "Scheduled workflow run already exists; skipping duplicate fire",
                workflow_schedule_id=schedule.workflow_schedule_id,
                workflow_run_id=workflow_run_id,
            )
            return
        except SQLAlchemyError:
            if await app.DATABASE.schedules.has_schedule_fired_since(
                schedule.workflow_schedule_id,
                due.previous_fire_time,
            ):
                LOG.info(
                    "Scheduled workflow run already persisted; skipping duplicate fire",
                    workflow_schedule_id=schedule.workflow_schedule_id,
                    workflow_run_id=workflow_run_id,
                )
                return
            raise

        await initialize_skyvern_state_file(
            workflow_run_id=workflow_run.workflow_run_id,
            organization_id=organization.organization_id,
        )
        await app.WORKFLOW_SERVICE.execute_workflow(
            workflow_run_id=workflow_run.workflow_run_id,
            api_key=None,
            organization=organization,
            browser_session_id=workflow_run.browser_session_id,
        )


_workflow_schedule_scheduler: LocalWorkflowScheduleScheduler | None = None
_workflow_schedule_task: asyncio.Task[None] | None = None


def start_workflow_schedule_scheduler() -> asyncio.Task[None] | None:
    global _workflow_schedule_scheduler, _workflow_schedule_task

    if not app.AGENT_FUNCTION.workflow_schedules_enabled:
        LOG.debug("Workflow schedules are disabled")
        return None

    if not app.AGENT_FUNCTION.workflow_schedules_use_local_scheduler:
        LOG.debug("Local workflow schedule scheduler is disabled for this backend")
        return None

    if settings.WORKFLOW_SCHEDULE_POLL_INTERVAL_SECONDS <= 0:
        LOG.warning(
            "Workflow schedule scheduler disabled because WORKFLOW_SCHEDULE_POLL_INTERVAL_SECONDS is not positive",
            poll_interval_seconds=settings.WORKFLOW_SCHEDULE_POLL_INTERVAL_SECONDS,
        )
        return None

    if _workflow_schedule_task is not None and not _workflow_schedule_task.done():
        LOG.warning("Workflow schedule scheduler is already running")
        return _workflow_schedule_task

    _workflow_schedule_scheduler = LocalWorkflowScheduleScheduler(
        poll_interval_seconds=settings.WORKFLOW_SCHEDULE_POLL_INTERVAL_SECONDS,
        max_concurrent_runs=settings.WORKFLOW_SCHEDULE_MAX_CONCURRENT_RUNS,
    )
    _workflow_schedule_task = asyncio.create_task(_workflow_schedule_scheduler.run_forever())
    return _workflow_schedule_task


async def stop_workflow_schedule_scheduler() -> None:
    global _workflow_schedule_scheduler, _workflow_schedule_task

    was_running = _workflow_schedule_scheduler is not None or _workflow_schedule_task is not None

    if _workflow_schedule_task is not None and not _workflow_schedule_task.done():
        _workflow_schedule_task.cancel()
        try:
            await _workflow_schedule_task
        except asyncio.CancelledError:
            pass

    if _workflow_schedule_scheduler is not None:
        await _workflow_schedule_scheduler.shutdown()

    _workflow_schedule_scheduler = None
    _workflow_schedule_task = None
    if was_running:
        LOG.info("Workflow schedule scheduler stopped")
