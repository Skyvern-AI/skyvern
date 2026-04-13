from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone
from typing import Any, Sequence

import structlog
from sqlalchemy import and_, delete, distinct, func, select, tuple_, update

from skyvern.forge.sdk.db._error_handling import db_operation
from skyvern.forge.sdk.db.base_alchemy_db import read_retry
from skyvern.forge.sdk.db.base_repository import BaseRepository
from skyvern.forge.sdk.db.enums import TaskType
from skyvern.forge.sdk.db.exceptions import NotFoundError
from skyvern.forge.sdk.db.models import (
    ActionModel,
    StepModel,
    TaskModel,
    TaskRunModel,
    WorkflowRunModel,
)
from skyvern.forge.sdk.db.utils import convert_to_step, convert_to_task, hydrate_action, serialize_proxy_location
from skyvern.forge.sdk.models import Step, StepStatus
from skyvern.forge.sdk.schemas.runs import Run
from skyvern.forge.sdk.schemas.tasks import OrderBy, SortDirection, Task, TaskStatus
from skyvern.forge.sdk.utils.sanitization import sanitize_postgres_text
from skyvern.schemas.runs import ProxyLocationInput, RunStatus, RunType
from skyvern.schemas.steps import AgentStepOutput
from skyvern.webeye.actions.actions import Action

LOG = structlog.get_logger()


class TasksRepository(BaseRepository):
    _background_tasks: set[asyncio.Task] = set()  # noqa: RUF012

    @db_operation("create_task")
    async def create_task(
        self,
        url: str,
        title: str | None,
        navigation_goal: str | None,
        data_extraction_goal: str | None,
        navigation_payload: dict[str, Any] | list | str | None,
        status: str = "created",
        complete_criterion: str | None = None,
        terminate_criterion: str | None = None,
        webhook_callback_url: str | None = None,
        totp_verification_url: str | None = None,
        totp_identifier: str | None = None,
        organization_id: str | None = None,
        proxy_location: ProxyLocationInput = None,
        extracted_information_schema: dict[str, Any] | list | str | None = None,
        workflow_run_id: str | None = None,
        order: int | None = None,
        retry: int | None = None,
        max_steps_per_run: int | None = None,
        error_code_mapping: dict[str, str] | None = None,
        task_type: str = TaskType.general,
        application: str | None = None,
        include_action_history_in_verification: bool | None = None,
        model: dict[str, Any] | None = None,
        max_screenshot_scrolling_times: int | None = None,
        extra_http_headers: dict[str, str] | None = None,
        browser_session_id: str | None = None,
        browser_address: str | None = None,
        download_timeout: float | None = None,
    ) -> Task:
        # Sanitize text fields to remove NUL bytes and control characters
        # that PostgreSQL cannot store in text columns
        def _sanitize(v: str | None) -> str | None:
            return sanitize_postgres_text(v) if isinstance(v, str) else v

        navigation_goal = _sanitize(navigation_goal)
        data_extraction_goal = _sanitize(data_extraction_goal)
        title = _sanitize(title)
        url = sanitize_postgres_text(url)
        complete_criterion = _sanitize(complete_criterion)
        terminate_criterion = _sanitize(terminate_criterion)

        async with self.Session() as session:
            new_task = TaskModel(
                status=status,
                task_type=task_type,
                url=url,
                title=title,
                webhook_callback_url=webhook_callback_url,
                totp_verification_url=totp_verification_url,
                totp_identifier=totp_identifier,
                navigation_goal=navigation_goal,
                complete_criterion=complete_criterion,
                terminate_criterion=terminate_criterion,
                data_extraction_goal=data_extraction_goal,
                navigation_payload=navigation_payload,
                organization_id=organization_id,
                proxy_location=serialize_proxy_location(proxy_location),
                extracted_information_schema=extracted_information_schema,
                workflow_run_id=workflow_run_id,
                order=order,
                retry=retry,
                max_steps_per_run=max_steps_per_run,
                error_code_mapping=error_code_mapping,
                application=application,
                include_action_history_in_verification=include_action_history_in_verification,
                model=model,
                max_screenshot_scrolling_times=max_screenshot_scrolling_times,
                extra_http_headers=extra_http_headers,
                browser_session_id=browser_session_id,
                browser_address=browser_address,
                download_timeout=download_timeout,
            )
            session.add(new_task)
            await session.commit()
            await session.refresh(new_task)
            return convert_to_task(new_task, self.debug_enabled)

    @db_operation("create_step")
    async def create_step(
        self,
        task_id: str,
        order: int,
        retry_index: int,
        organization_id: str | None = None,
        status: StepStatus = StepStatus.created,
        created_by: str | None = None,
    ) -> Step:
        async with self.Session() as session:
            new_step = StepModel(
                task_id=task_id,
                order=order,
                retry_index=retry_index,
                status=status,
                organization_id=organization_id,
                created_by=created_by,
            )
            session.add(new_step)
            await session.commit()
            await session.refresh(new_step)
            return convert_to_step(new_step, debug_enabled=self.debug_enabled)

    @read_retry()
    @db_operation("get_task", log_errors=False)
    async def get_task(self, task_id: str, organization_id: str | None = None) -> Task | None:
        """Get a task by its id"""
        async with self.Session() as session:
            query = select(TaskModel).filter_by(task_id=task_id)
            if organization_id is not None:
                query = query.filter_by(organization_id=organization_id)
            if task_obj := (await session.scalars(query)).first():
                return convert_to_task(task_obj, self.debug_enabled)
            else:
                LOG.info(
                    "Task not found",
                    task_id=task_id,
                    organization_id=organization_id,
                )
                return None

    @db_operation("get_tasks_by_ids")
    async def get_tasks_by_ids(
        self,
        task_ids: list[str],
        organization_id: str,
    ) -> list[Task]:
        async with self.Session() as session:
            tasks = (
                await session.scalars(
                    select(TaskModel).filter(TaskModel.task_id.in_(task_ids)).filter_by(organization_id=organization_id)
                )
            ).all()
            return [convert_to_task(task, debug_enabled=self.debug_enabled) for task in tasks]

    @db_operation("get_step")
    async def get_step(self, step_id: str, organization_id: str | None = None) -> Step | None:
        async with self.Session() as session:
            if step := (
                await session.scalars(
                    select(StepModel).filter_by(step_id=step_id).filter_by(organization_id=organization_id)
                )
            ).first():
                return convert_to_step(step, debug_enabled=self.debug_enabled)

            else:
                return None

    @db_operation("get_task_steps")
    async def get_task_steps(self, task_id: str, organization_id: str) -> list[Step]:
        async with self.Session() as session:
            if steps := (
                await session.scalars(
                    select(StepModel)
                    .filter_by(task_id=task_id)
                    .filter_by(organization_id=organization_id)
                    .order_by(StepModel.order)
                    .order_by(StepModel.retry_index)
                )
            ).all():
                return [convert_to_step(step, debug_enabled=self.debug_enabled) for step in steps]
            else:
                return []

    @db_operation("get_steps_by_task_ids")
    async def get_steps_by_task_ids(self, task_ids: list[str], organization_id: str | None = None) -> list[Step]:
        async with self.Session() as session:
            steps = (
                await session.scalars(
                    select(StepModel).filter(StepModel.task_id.in_(task_ids)).filter_by(organization_id=organization_id)
                )
            ).all()
            return [convert_to_step(step, debug_enabled=self.debug_enabled) for step in steps]

    @db_operation("get_step_counts_by_task_ids")
    async def get_step_counts_by_task_ids(
        self, task_ids: list[str], organization_id: str | None = None
    ) -> tuple[int, int]:
        """Return (total_steps, completed_steps) counts without fetching full step objects."""
        async with self.Session() as session:
            query = (
                select(
                    func.count().label("total"),
                    func.count().filter(StepModel.status == StepStatus.completed).label("completed"),
                )
                .where(StepModel.task_id.in_(task_ids))
                .where(StepModel.organization_id == organization_id)
            )
            row = (await session.execute(query)).one()
            return row.total, row.completed

    @db_operation("get_total_unique_step_order_count_by_task_ids")
    async def get_total_unique_step_order_count_by_task_ids(
        self,
        *,
        task_ids: list[str],
        organization_id: str,
    ) -> int:
        """
        Get the total count of unique (step.task_id, step.order) pairs of StepModel for the given task ids
        Basically translate this sql query into a SQLAlchemy query: select count(distinct(s.task_id, s.order)) from steps s
        where s.task_id in task_ids
        """
        async with self.Session() as session:
            query = (
                select(func.count(distinct(tuple_(StepModel.task_id, StepModel.order))))
                .where(StepModel.task_id.in_(task_ids))
                .where(StepModel.organization_id == organization_id)
            )
            return (await session.execute(query)).scalar()

    @db_operation("get_task_step_models")
    async def get_task_step_models(self, task_id: str, organization_id: str | None = None) -> Sequence[StepModel]:
        async with self.Session() as session:
            return (
                await session.scalars(
                    select(StepModel)
                    .filter_by(task_id=task_id)
                    .filter_by(organization_id=organization_id)
                    .order_by(StepModel.order)
                    .order_by(StepModel.retry_index)
                )
            ).all()

    @db_operation("get_task_step_count")
    async def get_task_step_count(self, task_id: str, organization_id: str | None = None) -> int:
        async with self.Session() as session:
            result = await session.scalar(
                select(func.count(StepModel.step_id))
                .filter_by(task_id=task_id)
                .filter_by(organization_id=organization_id)
            )
            return result or 0

    @db_operation("get_task_actions")
    async def get_task_actions(self, task_id: str, organization_id: str | None = None) -> list[Action]:
        async with self.Session() as session:
            query = (
                select(ActionModel)
                .filter(ActionModel.organization_id == organization_id)
                .filter(ActionModel.task_id == task_id)
                .order_by(ActionModel.created_at)
            )

            actions = (await session.scalars(query)).all()
            return [Action.model_validate(action) for action in actions]

    @db_operation("get_task_actions_hydrated")
    async def get_task_actions_hydrated(self, task_id: str, organization_id: str | None = None) -> list[Action]:
        async with self.Session() as session:
            query = (
                select(ActionModel)
                .filter(ActionModel.organization_id == organization_id)
                .filter(ActionModel.task_id == task_id)
                .order_by(ActionModel.created_at)
            )

            actions = (await session.scalars(query)).all()
            return [hydrate_action(action) for action in actions]

    @db_operation("get_tasks_actions")
    async def get_tasks_actions(self, task_ids: list[str], organization_id: str | None = None) -> list[Action]:
        async with self.Session() as session:
            query = (
                select(ActionModel)
                .filter(ActionModel.organization_id == organization_id)
                .filter(ActionModel.task_id.in_(task_ids))
                .order_by(ActionModel.created_at.desc())
            )
            actions = (await session.scalars(query)).all()
            return [hydrate_action(action) for action in actions]

    @db_operation("get_action_count_for_step")
    async def get_action_count_for_step(self, step_id: str, task_id: str, organization_id: str) -> int:
        """Get count of actions for a step. Uses composite index for efficiency."""
        async with self.Session() as session:
            query = (
                select(func.count())
                .select_from(ActionModel)
                .where(ActionModel.organization_id == organization_id)
                .where(ActionModel.task_id == task_id)
                .where(ActionModel.step_id == step_id)
            )
            result = await session.scalar(query)
            return result or 0

    @db_operation("get_first_step")
    async def get_first_step(self, task_id: str, organization_id: str | None = None) -> Step | None:
        async with self.Session() as session:
            if step := (
                await session.scalars(
                    select(StepModel)
                    .filter_by(task_id=task_id)
                    .filter_by(organization_id=organization_id)
                    .order_by(StepModel.order.asc())
                    .order_by(StepModel.retry_index.asc())
                )
            ).first():
                return convert_to_step(step, debug_enabled=self.debug_enabled)
            else:
                LOG.info(
                    "Latest step not found",
                    task_id=task_id,
                    organization_id=organization_id,
                )
                return None

    @db_operation("get_latest_step")
    async def get_latest_step(self, task_id: str, organization_id: str | None = None) -> Step | None:
        async with self.Session() as session:
            if step := (
                await session.scalars(
                    select(StepModel)
                    .filter_by(task_id=task_id)
                    .filter_by(organization_id=organization_id)
                    .filter(StepModel.status != StepStatus.canceled)
                    .order_by(StepModel.order.desc())
                    .order_by(StepModel.retry_index.desc())
                )
            ).first():
                return convert_to_step(step, debug_enabled=self.debug_enabled)
            else:
                LOG.info(
                    "Latest step not found",
                    task_id=task_id,
                    organization_id=organization_id,
                )
                return None

    @db_operation("update_step")
    async def update_step(
        self,
        task_id: str,
        step_id: str,
        status: StepStatus | None = None,
        output: AgentStepOutput | None = None,
        is_last: bool | None = None,
        retry_index: int | None = None,
        organization_id: str | None = None,
        incremental_cost: float | None = None,
        incremental_input_tokens: int | None = None,
        incremental_output_tokens: int | None = None,
        incremental_reasoning_tokens: int | None = None,
        incremental_cached_tokens: int | None = None,
        created_by: str | None = None,
    ) -> Step:
        async with self.Session() as session:
            if step := (
                await session.scalars(
                    select(StepModel)
                    .filter_by(task_id=task_id)
                    .filter_by(step_id=step_id)
                    .filter_by(organization_id=organization_id)
                )
            ).first():
                if status is not None:
                    step.status = status

                    if status.is_terminal() and step.finished_at is None:
                        step.finished_at = datetime.now(timezone.utc)
                if output is not None:
                    step.output = output.model_dump(exclude_none=True)
                if is_last is not None:
                    step.is_last = is_last
                if retry_index is not None:
                    step.retry_index = retry_index
                if incremental_cost is not None:
                    step.step_cost = incremental_cost + float(step.step_cost or 0)
                if incremental_input_tokens is not None:
                    step.input_token_count = incremental_input_tokens + (step.input_token_count or 0)
                if incremental_output_tokens is not None:
                    step.output_token_count = incremental_output_tokens + (step.output_token_count or 0)
                if incremental_reasoning_tokens is not None:
                    step.reasoning_token_count = incremental_reasoning_tokens + (step.reasoning_token_count or 0)
                if incremental_cached_tokens is not None:
                    step.cached_token_count = incremental_cached_tokens + (step.cached_token_count or 0)
                if created_by is not None:
                    step.created_by = created_by

                await session.commit()
                updated_step = await self.get_step(step_id, organization_id)
                if not updated_step:
                    raise NotFoundError("Step not found")
                return updated_step
            else:
                raise NotFoundError("Step not found")

    @db_operation("clear_task_failure_reason")
    async def clear_task_failure_reason(self, organization_id: str, task_id: str) -> Task:
        async with self.Session() as session:
            if task := (
                await session.scalars(
                    select(TaskModel).filter_by(task_id=task_id).filter_by(organization_id=organization_id)
                )
            ).first():
                task.failure_reason = None
                await session.commit()
                await session.refresh(task)
                return convert_to_task(task, debug_enabled=self.debug_enabled)
            else:
                raise NotFoundError("Task not found")

    @db_operation("update_task")
    async def update_task(
        self,
        task_id: str,
        status: TaskStatus | None = None,
        extracted_information: dict[str, Any] | list | str | None = None,
        webhook_failure_reason: str | None = None,
        failure_reason: str | None = None,
        errors: list[dict[str, Any]] | None = None,
        max_steps_per_run: int | None = None,
        organization_id: str | None = None,
        failure_category: list[dict[str, Any]] | None = None,
    ) -> Task:
        if (
            status is None
            and extracted_information is None
            and failure_reason is None
            and errors is None
            and max_steps_per_run is None
            and webhook_failure_reason is None
            and failure_category is None
        ):
            raise ValueError(
                "At least one of status, extracted_information, or failure_reason must be provided to update the task"
            )
        async with self.Session() as session:
            if task := (
                await session.scalars(
                    select(TaskModel).filter_by(task_id=task_id).filter_by(organization_id=organization_id)
                )
            ).first():
                if status is not None:
                    task.status = status
                    if status == TaskStatus.queued and task.queued_at is None:
                        task.queued_at = datetime.now(timezone.utc)
                    if status == TaskStatus.running and task.started_at is None:
                        task.started_at = datetime.now(timezone.utc)
                    if status.is_final() and task.finished_at is None:
                        task.finished_at = datetime.now(timezone.utc)
                if extracted_information is not None:
                    task.extracted_information = extracted_information
                if failure_reason is not None:
                    task.failure_reason = failure_reason
                if errors is not None:
                    task.errors = (task.errors or []) + errors
                if max_steps_per_run is not None:
                    task.max_steps_per_run = max_steps_per_run
                if webhook_failure_reason is not None:
                    task.webhook_failure_reason = webhook_failure_reason
                if failure_category is not None:
                    task.failure_category = failure_category
                await session.commit()
                updated_task = await self.get_task(task_id, organization_id=organization_id)
                if not updated_task:
                    raise NotFoundError("Task not found")

                # Best-effort fire-and-forget write-through to task_runs.
                # Mirrors the WorkflowService pattern — cron catches any missed syncs.
                if status is not None:
                    bg = asyncio.create_task(
                        self.sync_task_run_status(
                            organization_id=updated_task.organization_id or "",
                            run_id=updated_task.task_id,
                            status=status.value,
                            started_at=updated_task.started_at,
                            finished_at=updated_task.finished_at,
                        ),
                    )
                    self._background_tasks.add(bg)
                    bg.add_done_callback(self._background_tasks.discard)

                return updated_task
            else:
                raise NotFoundError("Task not found")

    @db_operation("update_task_2fa_state")
    async def update_task_2fa_state(
        self,
        task_id: str,
        organization_id: str,
        waiting_for_verification_code: bool,
        verification_code_identifier: str | None = None,
        verification_code_polling_started_at: datetime | None = None,
    ) -> Task:
        """Update task 2FA verification code waiting state."""
        async with self.Session() as session:
            if task := (
                await session.scalars(
                    select(TaskModel).filter_by(task_id=task_id).filter_by(organization_id=organization_id)
                )
            ).first():
                task.waiting_for_verification_code = waiting_for_verification_code
                if verification_code_identifier is not None:
                    task.verification_code_identifier = verification_code_identifier
                if verification_code_polling_started_at is not None:
                    task.verification_code_polling_started_at = verification_code_polling_started_at
                if not waiting_for_verification_code:
                    # Clear identifiers when no longer waiting
                    task.verification_code_identifier = None
                    task.verification_code_polling_started_at = None
                await session.commit()
                updated_task = await self.get_task(task_id, organization_id=organization_id)
                if not updated_task:
                    raise NotFoundError("Task not found")
                return updated_task
            else:
                raise NotFoundError("Task not found")

    @db_operation("bulk_update_tasks")
    async def bulk_update_tasks(
        self,
        task_ids: list[str],
        status: TaskStatus | None = None,
        failure_reason: str | None = None,
    ) -> None:
        """Bulk update tasks by their IDs.

        Args:
            task_ids: List of task IDs to update
            status: Optional status to set for all tasks
            failure_reason: Optional failure reason to set for all tasks
        """
        if not task_ids:
            return

        async with self.Session() as session:
            update_values = {}
            if status:
                update_values["status"] = status.value
            if failure_reason:
                update_values["failure_reason"] = failure_reason

            if update_values:
                update_stmt = update(TaskModel).where(TaskModel.task_id.in_(task_ids)).values(**update_values)
                await session.execute(update_stmt)
                await session.commit()

    @db_operation("get_tasks")
    async def get_tasks(
        self,
        page: int = 1,
        page_size: int = 10,
        task_status: list[TaskStatus] | None = None,
        workflow_run_id: str | None = None,
        organization_id: str | None = None,
        only_standalone_tasks: bool = False,
        application: str | None = None,
        order_by_column: OrderBy = OrderBy.created_at,
        order: SortDirection = SortDirection.desc,
    ) -> list[Task]:
        """
        Get all tasks.
        :param page: Starts at 1
        :param page_size:
        :param task_status:
        :param workflow_run_id:
        :param only_standalone_tasks:
        :param order_by_column:
        :param order:
        :return:
        """
        if page < 1:
            raise ValueError(f"Page must be greater than 0, got {page}")

        async with self.Session() as session:
            db_page = page - 1  # offset logic is 0 based
            query = (
                select(TaskModel, WorkflowRunModel.workflow_permanent_id)
                .join(WorkflowRunModel, TaskModel.workflow_run_id == WorkflowRunModel.workflow_run_id, isouter=True)
                .filter(TaskModel.organization_id == organization_id)
            )
            if task_status:
                query = query.filter(TaskModel.status.in_(task_status))
            if workflow_run_id:
                query = query.filter(TaskModel.workflow_run_id == workflow_run_id)
            if only_standalone_tasks:
                query = query.filter(TaskModel.workflow_run_id.is_(None))
            if application:
                query = query.filter(TaskModel.application == application)
            order_by_col = getattr(TaskModel, order_by_column)
            query = (
                query.order_by(order_by_col.desc() if order == SortDirection.desc else order_by_col.asc())
                .limit(page_size)
                .offset(db_page * page_size)
            )

            results = (await session.execute(query)).all()

            return [
                convert_to_task(task, debug_enabled=self.debug_enabled, workflow_permanent_id=workflow_permanent_id)
                for task, workflow_permanent_id in results
            ]

    @db_operation("get_tasks_count")
    async def get_tasks_count(
        self,
        organization_id: str,
        task_status: list[TaskStatus] | None = None,
        workflow_run_id: str | None = None,
        only_standalone_tasks: bool = False,
        application: str | None = None,
    ) -> int:
        async with self.Session() as session:
            count_query = (
                select(func.count()).select_from(TaskModel).filter(TaskModel.organization_id == organization_id)
            )
            if task_status:
                count_query = count_query.filter(TaskModel.status.in_(task_status))
            if workflow_run_id:
                count_query = count_query.filter(TaskModel.workflow_run_id == workflow_run_id)
            if only_standalone_tasks:
                count_query = count_query.filter(TaskModel.workflow_run_id.is_(None))
            if application:
                count_query = count_query.filter(TaskModel.application == application)
            return (await session.execute(count_query)).scalar_one()

    @db_operation("get_running_tasks_info_globally")
    async def get_running_tasks_info_globally(
        self,
        stale_threshold_hours: int = 24,
    ) -> tuple[int, int]:
        """
        Get information about running tasks across all organizations.
        Used by cleanup service to determine if cleanup should be skipped.

        Args:
            stale_threshold_hours: Tasks not updated for this many hours are considered stale.

        Returns:
            Tuple of (active_task_count, stale_task_count).
            Active tasks are those updated within the threshold.
            Stale tasks are those not updated within the threshold but still in running status.
        """
        async with self.Session() as session:
            running_statuses = [TaskStatus.created, TaskStatus.queued, TaskStatus.running]
            stale_cutoff = datetime.now(timezone.utc) - timedelta(hours=stale_threshold_hours)

            # Count active tasks (recently updated)
            active_query = (
                select(func.count())
                .select_from(TaskModel)
                .filter(TaskModel.status.in_(running_statuses))
                .filter(TaskModel.modified_at >= stale_cutoff)
            )
            active_count = (await session.execute(active_query)).scalar_one()

            # Count stale tasks (not updated for a long time)
            stale_query = (
                select(func.count())
                .select_from(TaskModel)
                .filter(TaskModel.status.in_(running_statuses))
                .filter(TaskModel.modified_at < stale_cutoff)
            )
            stale_count = (await session.execute(stale_query)).scalar_one()

            return (active_count, stale_count)

    @db_operation("get_latest_task_by_workflow_id")
    async def get_latest_task_by_workflow_id(
        self,
        organization_id: str,
        workflow_id: str,
        before: datetime | None = None,
    ) -> Task | None:
        async with self.Session() as session:
            query = select(TaskModel).filter_by(organization_id=organization_id).filter_by(workflow_id=workflow_id)
            if before:
                query = query.filter(TaskModel.created_at < before)
            task = (await session.scalars(query.order_by(TaskModel.created_at.desc()))).first()
            if task:
                return convert_to_task(task, debug_enabled=self.debug_enabled)
            return None

    @db_operation("get_last_task_for_workflow_run")
    async def get_last_task_for_workflow_run(self, workflow_run_id: str) -> Task | None:
        async with self.Session() as session:
            if task := (
                await session.scalars(
                    select(TaskModel).filter_by(workflow_run_id=workflow_run_id).order_by(TaskModel.created_at.desc())
                )
            ).first():
                return convert_to_task(task, debug_enabled=self.debug_enabled)
            return None

    @db_operation("get_tasks_by_workflow_run_id")
    async def get_tasks_by_workflow_run_id(self, workflow_run_id: str) -> list[Task]:
        async with self.Session() as session:
            tasks = (
                await session.scalars(
                    select(TaskModel).filter_by(workflow_run_id=workflow_run_id).order_by(TaskModel.created_at)
                )
            ).all()
            return [convert_to_task(task, debug_enabled=self.debug_enabled) for task in tasks]

    @db_operation("delete_task_steps")
    async def delete_task_steps(self, organization_id: str, task_id: str) -> None:
        async with self.Session() as session:
            # delete artifacts by filtering organization_id and task_id
            stmt = delete(StepModel).where(
                and_(
                    StepModel.organization_id == organization_id,
                    StepModel.task_id == task_id,
                )
            )
            await session.execute(stmt)
            await session.commit()

    @db_operation("get_previous_actions_for_task")
    async def get_previous_actions_for_task(self, task_id: str) -> list[Action]:
        async with self.Session() as session:
            query = (
                select(ActionModel)
                .filter_by(task_id=task_id)
                .order_by(ActionModel.step_order, ActionModel.action_order, ActionModel.created_at)
            )
            actions = (await session.scalars(query)).all()
            return [Action.model_validate(action) for action in actions]

    @db_operation("delete_task_actions")
    async def delete_task_actions(self, organization_id: str, task_id: str) -> None:
        async with self.Session() as session:
            # delete actions by filtering organization_id and task_id
            stmt = delete(ActionModel).where(
                and_(
                    ActionModel.organization_id == organization_id,
                    ActionModel.task_id == task_id,
                )
            )
            await session.execute(stmt)
            await session.commit()

    async def sync_task_run_status(
        self,
        organization_id: str,
        run_id: str,
        status: str,
        started_at: datetime | None = None,
        finished_at: datetime | None = None,
    ) -> None:
        """Best-effort write-through: propagate status from source table to task_runs.

        Does NOT raise if the task_runs row is missing (race at creation time).
        """
        try:
            async with self.Session() as session:
                vals: dict[str, Any] = {"status": status}
                if started_at is not None:
                    vals["started_at"] = started_at
                if finished_at is not None:
                    vals["finished_at"] = finished_at
                stmt = (
                    update(TaskRunModel)
                    .where(TaskRunModel.run_id == run_id)
                    .where(TaskRunModel.organization_id == organization_id)
                    .values(**vals)
                )
                await session.execute(stmt)
                await session.commit()
        except Exception:
            LOG.warning(
                "Best-effort task_run status sync failed",
                run_id=run_id,
                organization_id=organization_id,
                status=status,
                exc_info=True,
            )

    @db_operation("create_task_run")
    async def create_task_run(
        self,
        task_run_type: RunType,
        organization_id: str,
        run_id: str,
        title: str | None = None,
        url: str | None = None,
        url_hash: str | None = None,
        status: RunStatus | None = None,
        workflow_permanent_id: str | None = None,
        parent_workflow_run_id: str | None = None,
        debug_session_id: str | None = None,
        # script_run, started_at, finished_at are intentionally omitted here —
        # they are set via update_task_run() after the run starts/finishes (PRs 2-5).
    ) -> Run:
        searchable_text = " ".join(filter(None, [title, url]))
        async with self.Session() as session:
            task_run = TaskRunModel(
                task_run_type=task_run_type,
                organization_id=organization_id,
                run_id=run_id,
                title=title,
                url=url,
                url_hash=url_hash,
                status=status,
                workflow_permanent_id=workflow_permanent_id,
                parent_workflow_run_id=parent_workflow_run_id,
                debug_session_id=debug_session_id,
                searchable_text=searchable_text or None,
            )
            session.add(task_run)
            await session.commit()
            await session.refresh(task_run)
            return Run.model_validate(task_run)

    @db_operation("update_task_run")
    async def update_task_run(
        self,
        organization_id: str,
        run_id: str,
        title: str | None = None,
        url: str | None = None,
        url_hash: str | None = None,
        status: str | None = None,
        started_at: datetime | None = None,
        finished_at: datetime | None = None,
    ) -> None:
        async with self.Session() as session:
            task_run = (
                await session.scalars(
                    select(TaskRunModel).filter_by(run_id=run_id).filter_by(organization_id=organization_id)
                )
            ).first()
            if not task_run:
                raise NotFoundError(f"TaskRun {run_id} not found")

            if title is not None:
                task_run.title = title
            if url is not None:
                task_run.url = url
            if url_hash is not None:
                task_run.url_hash = url_hash
            if status is not None:
                task_run.status = status
            if started_at is not None:
                task_run.started_at = started_at
            if finished_at is not None:
                task_run.finished_at = finished_at

            # Recompute searchable_text when title or url changes
            if title is not None or url is not None:
                task_run.searchable_text = " ".join(filter(None, [task_run.title, task_run.url])) or None

            await session.commit()

    @db_operation("update_job_run_compute_cost")
    async def update_job_run_compute_cost(
        self,
        organization_id: str,
        run_id: str,
        instance_type: str | None = None,
        vcpu_millicores: int | None = None,
        memory_mb: int | None = None,
        duration_ms: int | None = None,
        compute_cost: float | None = None,
    ) -> None:
        """Update compute cost metrics for a job run."""
        async with self.Session() as session:
            task_run = (
                await session.scalars(
                    select(TaskRunModel).filter_by(run_id=run_id).filter_by(organization_id=organization_id)
                )
            ).first()
            if not task_run:
                LOG.warning(
                    "TaskRun not found for compute cost update",
                    run_id=run_id,
                    organization_id=organization_id,
                )
                return

            if instance_type is not None:
                task_run.instance_type = instance_type
            if vcpu_millicores is not None:
                task_run.vcpu_millicores = vcpu_millicores
            if memory_mb is not None:
                task_run.memory_mb = memory_mb
            if duration_ms is not None:
                task_run.duration_ms = duration_ms
            if compute_cost is not None:
                task_run.compute_cost = compute_cost
            await session.commit()

    @db_operation("cache_task_run")
    async def cache_task_run(self, run_id: str, organization_id: str | None = None) -> Run:
        async with self.Session() as session:
            task_run = (
                await session.scalars(
                    select(TaskRunModel).filter_by(organization_id=organization_id).filter_by(run_id=run_id)
                )
            ).first()
            if task_run:
                task_run.cached = True
                await session.commit()
                await session.refresh(task_run)
                return Run.model_validate(task_run)
            raise NotFoundError(f"Run {run_id} not found")

    @db_operation("get_cached_task_run")
    async def get_cached_task_run(
        self, task_run_type: RunType, url_hash: str | None = None, organization_id: str | None = None
    ) -> Run | None:
        async with self.Session() as session:
            query = select(TaskRunModel)
            if task_run_type:
                query = query.filter_by(task_run_type=task_run_type)
            if url_hash:
                query = query.filter_by(url_hash=url_hash)
            if organization_id:
                query = query.filter_by(organization_id=organization_id)
            query = query.filter_by(cached=True).order_by(TaskRunModel.created_at.desc())
            task_run = (await session.scalars(query)).first()
            return Run.model_validate(task_run) if task_run else None

    @db_operation("get_run")
    async def get_run(
        self,
        run_id: str,
        organization_id: str | None = None,
    ) -> Run | None:
        async with self.Session() as session:
            query = select(TaskRunModel).filter_by(run_id=run_id)
            if organization_id:
                query = query.filter_by(organization_id=organization_id)
            task_run = (await session.scalars(query)).first()
            return Run.model_validate(task_run) if task_run else None
