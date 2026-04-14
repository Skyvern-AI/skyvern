from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import TYPE_CHECKING, Any, Callable

import structlog
from sqlalchemy import Text, and_, cast, exists, func, literal, literal_column, or_, select, update
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.exc import SQLAlchemyError

from skyvern.exceptions import WorkflowParameterNotFound, WorkflowRunNotFound
from skyvern.forge.sdk.db._error_handling import db_operation
from skyvern.forge.sdk.db.base_alchemy_db import read_retry
from skyvern.forge.sdk.db.base_repository import BaseRepository
from skyvern.forge.sdk.db.enums import WorkflowRunTriggerType
from skyvern.forge.sdk.db.exceptions import NotFoundError

if TYPE_CHECKING:
    from skyvern.forge.sdk.db.base_alchemy_db import _SessionFactory

from skyvern.forge.sdk.db._sentinels import _UNSET
from skyvern.forge.sdk.db.models import (
    TaskModel,
    TaskRunModel,
    WorkflowModel,
    WorkflowParameterModel,
    WorkflowRunBlockModel,
    WorkflowRunModel,
    WorkflowRunOutputParameterModel,
    WorkflowRunParameterModel,
)
from skyvern.forge.sdk.db.protocols import WorkflowParameterReader
from skyvern.forge.sdk.db.utils import (
    convert_to_task,
    convert_to_workflow_run,
    convert_to_workflow_run_output_parameter,
    convert_to_workflow_run_parameter,
    serialize_proxy_location,
)
from skyvern.forge.sdk.log_artifacts import save_workflow_run_logs
from skyvern.forge.sdk.schemas.tasks import Task
from skyvern.forge.sdk.workflow.models.parameter import WorkflowParameter
from skyvern.forge.sdk.workflow.models.workflow import (
    WorkflowRun,
    WorkflowRunOutputParameter,
    WorkflowRunParameter,
    WorkflowRunStatus,
)
from skyvern.schemas.runs import ProxyLocationInput, RunType

LOG = structlog.get_logger()


class WorkflowRunsRepository(BaseRepository):
    """Database operations for workflow runs."""

    def __init__(
        self,
        session_factory: _SessionFactory,
        debug_enabled: bool = False,
        is_retryable_error_fn: Callable[[SQLAlchemyError], bool] | None = None,
        workflow_parameter_reader: WorkflowParameterReader | None = None,
        dialect_name: str = "postgresql",
    ) -> None:
        super().__init__(session_factory, debug_enabled, is_retryable_error_fn)
        self._workflow_parameter_reader = workflow_parameter_reader
        self._dialect_name = dialect_name

    @db_operation("get_running_workflow_runs_info_globally")
    async def get_running_workflow_runs_info_globally(
        self,
        stale_threshold_hours: int = 24,
    ) -> tuple[int, int]:
        """
        Get information about running workflow runs across all organizations.
        Used by cleanup service to determine if cleanup should be skipped.

        Args:
            stale_threshold_hours: Workflow runs not updated for this many hours are considered stale.

        Returns:
            Tuple of (active_workflow_count, stale_workflow_count).
            Active workflows are those updated within the threshold.
            Stale workflows are those not updated within the threshold but still in running status.
        """
        async with self.Session() as session:
            running_statuses = [
                WorkflowRunStatus.created,
                WorkflowRunStatus.queued,
                WorkflowRunStatus.running,
                WorkflowRunStatus.paused,
            ]
            stale_cutoff = datetime.now(timezone.utc) - timedelta(hours=stale_threshold_hours)

            # Count active workflow runs (recently updated)
            active_query = (
                select(func.count())
                .select_from(WorkflowRunModel)
                .filter(WorkflowRunModel.status.in_(running_statuses))
                .filter(WorkflowRunModel.modified_at >= stale_cutoff)
            )
            active_count = (await session.execute(active_query)).scalar_one()

            # Count stale workflow runs (not updated for a long time)
            stale_query = (
                select(func.count())
                .select_from(WorkflowRunModel)
                .filter(WorkflowRunModel.status.in_(running_statuses))
                .filter(WorkflowRunModel.modified_at < stale_cutoff)
            )
            stale_count = (await session.execute(stale_query)).scalar_one()

            return (active_count, stale_count)

    @db_operation("create_workflow_run")
    async def create_workflow_run(
        self,
        workflow_permanent_id: str,
        workflow_id: str,
        organization_id: str,
        browser_session_id: str | None = None,
        browser_profile_id: str | None = None,
        proxy_location: ProxyLocationInput = None,
        webhook_callback_url: str | None = None,
        totp_verification_url: str | None = None,
        totp_identifier: str | None = None,
        parent_workflow_run_id: str | None = None,
        max_screenshot_scrolling_times: int | None = None,
        extra_http_headers: dict[str, str] | None = None,
        browser_address: str | None = None,
        sequential_key: str | None = None,
        run_with: str | None = None,
        debug_session_id: str | None = None,
        ai_fallback: bool | None = None,
        code_gen: bool | None = None,
        workflow_run_id: str | None = None,
        trigger_type: WorkflowRunTriggerType | None = None,
        workflow_schedule_id: str | None = None,
    ) -> WorkflowRun:
        async with self.Session() as session:
            kwargs: dict[str, Any] = {}
            if workflow_run_id is not None:
                kwargs["workflow_run_id"] = workflow_run_id
            workflow_run = WorkflowRunModel(
                workflow_permanent_id=workflow_permanent_id,
                workflow_id=workflow_id,
                organization_id=organization_id,
                browser_session_id=browser_session_id,
                browser_profile_id=browser_profile_id,
                proxy_location=serialize_proxy_location(proxy_location),
                status="created",
                webhook_callback_url=webhook_callback_url,
                totp_verification_url=totp_verification_url,
                totp_identifier=totp_identifier,
                parent_workflow_run_id=parent_workflow_run_id,
                max_screenshot_scrolling_times=max_screenshot_scrolling_times,
                extra_http_headers=extra_http_headers,
                browser_address=browser_address,
                sequential_key=sequential_key,
                run_with=run_with,
                debug_session_id=debug_session_id,
                ai_fallback=ai_fallback,
                code_gen=code_gen,
                trigger_type=trigger_type.value if trigger_type else None,
                workflow_schedule_id=workflow_schedule_id,
                **kwargs,
            )
            session.add(workflow_run)
            await session.commit()
            await session.refresh(workflow_run)
            return convert_to_workflow_run(workflow_run)

    @db_operation("update_workflow_run")
    async def update_workflow_run(
        self,
        workflow_run_id: str,
        status: WorkflowRunStatus | None = None,
        failure_reason: str | None = None,
        webhook_failure_reason: str | None = None,
        ai_fallback_triggered: bool | None = None,
        job_id: str | None = None,
        run_with: str | None = None,
        sequential_key: str | None = None,
        ai_fallback: bool | None = None,
        depends_on_workflow_run_id: str | None = None,
        browser_session_id: str | None = None,
        waiting_for_verification_code: bool | None = None,
        verification_code_identifier: str | None = None,
        verification_code_polling_started_at: datetime | None = None,
        browser_profile_id: str | None | object = _UNSET,
        browser_address: str | None = None,
        extra_http_headers: dict[str, str] | None = None,
        failure_category: list[dict[str, Any]] | None = None,
    ) -> WorkflowRun:
        async with self.Session() as session:
            workflow_run = (
                await session.scalars(select(WorkflowRunModel).filter_by(workflow_run_id=workflow_run_id))
            ).first()
            if workflow_run:
                if status:
                    workflow_run.status = status
                if status and status == WorkflowRunStatus.queued and workflow_run.queued_at is None:
                    workflow_run.queued_at = datetime.now(timezone.utc)
                if status and status == WorkflowRunStatus.running and workflow_run.started_at is None:
                    workflow_run.started_at = datetime.now(timezone.utc)
                if status and status.is_final() and workflow_run.finished_at is None:
                    workflow_run.finished_at = datetime.now(timezone.utc)
                if failure_reason:
                    workflow_run.failure_reason = failure_reason
                if webhook_failure_reason is not None:
                    workflow_run.webhook_failure_reason = webhook_failure_reason
                if ai_fallback_triggered is not None:
                    workflow_run.script_run = {"ai_fallback_triggered": ai_fallback_triggered}
                if job_id:
                    workflow_run.job_id = job_id
                if run_with:
                    workflow_run.run_with = run_with
                if sequential_key:
                    workflow_run.sequential_key = sequential_key
                if ai_fallback is not None:
                    workflow_run.ai_fallback = ai_fallback
                if depends_on_workflow_run_id:
                    workflow_run.depends_on_workflow_run_id = depends_on_workflow_run_id
                if browser_session_id:
                    workflow_run.browser_session_id = browser_session_id
                if browser_address:
                    workflow_run.browser_address = browser_address
                if extra_http_headers:
                    workflow_run.extra_http_headers = extra_http_headers
                # 2FA verification code waiting state updates
                if waiting_for_verification_code is not None:
                    workflow_run.waiting_for_verification_code = waiting_for_verification_code
                if verification_code_identifier is not None:
                    workflow_run.verification_code_identifier = verification_code_identifier
                if verification_code_polling_started_at is not None:
                    workflow_run.verification_code_polling_started_at = verification_code_polling_started_at
                if waiting_for_verification_code is not None and not waiting_for_verification_code:
                    # Clear related fields when waiting is set to False
                    workflow_run.verification_code_identifier = None
                    workflow_run.verification_code_polling_started_at = None
                if browser_profile_id is not _UNSET:
                    workflow_run.browser_profile_id = browser_profile_id
                if failure_category is not None:
                    workflow_run.failure_category = failure_category
                await session.commit()
                await save_workflow_run_logs(workflow_run_id)
                await session.refresh(workflow_run)
                return convert_to_workflow_run(workflow_run)
            else:
                raise WorkflowRunNotFound(workflow_run_id)

    @db_operation("update_workflow_run_if_not_final")
    async def update_workflow_run_if_not_final(
        self,
        workflow_run_id: str,
        status: WorkflowRunStatus,
        failure_reason: str | None = None,
    ) -> WorkflowRun | None:
        """Transition a workflow run to ``status`` only if it is not already in a
        terminal state. Returns the updated row, or ``None`` when the row was
        already terminal (or missing). Implemented as a single conditional
        ``UPDATE ... WHERE status IN (<non-terminal>)`` so a concurrent
        finalization write cannot be clobbered by a late cancel.
        """
        non_terminal = [s.value for s in WorkflowRunStatus if not s.is_final()]
        values: dict[str, Any] = {"status": status}
        if status.is_final():
            values["finished_at"] = datetime.now(timezone.utc)
        if failure_reason is not None:
            values["failure_reason"] = failure_reason

        async with self.Session() as session:
            result = await session.execute(
                update(WorkflowRunModel)
                .where(
                    WorkflowRunModel.workflow_run_id == workflow_run_id,
                    WorkflowRunModel.status.in_(non_terminal),
                )
                .values(**values)
                .returning(WorkflowRunModel.workflow_run_id)
            )
            affected = result.scalar_one_or_none()
            await session.commit()
            if affected is None:
                return None
            refreshed = (
                await session.scalars(select(WorkflowRunModel).filter_by(workflow_run_id=workflow_run_id))
            ).one()
            await save_workflow_run_logs(workflow_run_id)
            return convert_to_workflow_run(refreshed)

    @db_operation("bulk_update_workflow_runs")
    async def bulk_update_workflow_runs(
        self,
        workflow_run_ids: list[str],
        status: WorkflowRunStatus | None = None,
        failure_reason: str | None = None,
    ) -> None:
        """Bulk update workflow runs by their IDs.

        Args:
            workflow_run_ids: List of workflow run IDs to update
            status: Optional status to set for all workflow runs
            failure_reason: Optional failure reason to set for all workflow runs
        """
        if not workflow_run_ids:
            return

        async with self.Session() as session:
            update_values = {}
            if status:
                update_values["status"] = status.value
            if failure_reason:
                update_values["failure_reason"] = failure_reason

            if update_values:
                update_stmt = (
                    update(WorkflowRunModel)
                    .where(WorkflowRunModel.workflow_run_id.in_(workflow_run_ids))
                    .values(**update_values)
                )
                await session.execute(update_stmt)
                await session.commit()

    @db_operation("clear_workflow_run_failure_reason")
    async def clear_workflow_run_failure_reason(self, workflow_run_id: str, organization_id: str) -> WorkflowRun:
        async with self.Session() as session:
            workflow_run = (
                await session.scalars(
                    select(WorkflowRunModel)
                    .filter_by(workflow_run_id=workflow_run_id)
                    .filter_by(organization_id=organization_id)
                )
            ).first()
            if workflow_run:
                workflow_run.failure_reason = None
                await session.commit()
                await session.refresh(workflow_run)
                return convert_to_workflow_run(workflow_run)
            else:
                raise NotFoundError("Workflow run not found")

    @db_operation("get_all_runs")
    async def get_all_runs(
        self,
        organization_id: str,
        page: int = 1,
        page_size: int = 10,
        status: list[WorkflowRunStatus] | None = None,
        include_debugger_runs: bool = False,
        search_key: str | None = None,
    ) -> list[WorkflowRun | Task]:
        async with self.Session() as session:
            # temporary limit to 10 pages
            if page > 10:
                return []

            limit = page * page_size

            workflow_run_query = (
                select(WorkflowRunModel, WorkflowModel.title)
                .join(WorkflowModel, WorkflowModel.workflow_id == WorkflowRunModel.workflow_id)
                .filter(WorkflowRunModel.organization_id == organization_id)
                .filter(WorkflowRunModel.parent_workflow_run_id.is_(None))
            )

            if not include_debugger_runs:
                workflow_run_query = workflow_run_query.filter(WorkflowRunModel.debug_session_id.is_(None))

            if search_key:
                key_like = f"%{search_key}%"
                # Match workflow_run_id directly
                id_matches = WorkflowRunModel.workflow_run_id.ilike(key_like)
                # Match parameter key or description (only for non-deleted parameter definitions)
                param_key_desc_exists = exists(
                    select(1)
                    .select_from(WorkflowRunParameterModel)
                    .join(
                        WorkflowParameterModel,
                        WorkflowParameterModel.workflow_parameter_id == WorkflowRunParameterModel.workflow_parameter_id,
                    )
                    .where(WorkflowRunParameterModel.workflow_run_id == WorkflowRunModel.workflow_run_id)
                    .where(WorkflowParameterModel.deleted_at.is_(None))
                    .where(
                        or_(
                            WorkflowParameterModel.key.ilike(key_like),
                            WorkflowParameterModel.description.ilike(key_like),
                        )
                    )
                )
                # Match run parameter value directly (searches all values regardless of parameter definition status)
                param_value_exists = exists(
                    select(1)
                    .select_from(WorkflowRunParameterModel)
                    .where(WorkflowRunParameterModel.workflow_run_id == WorkflowRunModel.workflow_run_id)
                    .where(WorkflowRunParameterModel.value.ilike(key_like))
                )
                # Match extra HTTP headers (cast JSON to text for search, skip NULLs)
                extra_headers_match = and_(
                    WorkflowRunModel.extra_http_headers.isnot(None),
                    func.cast(WorkflowRunModel.extra_http_headers, Text()).ilike(key_like),
                )
                workflow_run_query = workflow_run_query.where(
                    or_(id_matches, param_key_desc_exists, param_value_exists, extra_headers_match)
                )

            if status:
                workflow_run_query = workflow_run_query.filter(WorkflowRunModel.status.in_(status))
            workflow_run_query = workflow_run_query.order_by(WorkflowRunModel.created_at.desc()).limit(limit)
            workflow_run_query_result = (await session.execute(workflow_run_query)).all()
            workflow_runs = [
                convert_to_workflow_run(run, workflow_title=title, debug_enabled=self.debug_enabled)
                for run, title in workflow_run_query_result
            ]

            task_query = (
                select(TaskModel)
                .filter(TaskModel.organization_id == organization_id)
                .filter(TaskModel.workflow_run_id.is_(None))
            )
            if status:
                task_query = task_query.filter(TaskModel.status.in_(status))
            task_query = task_query.order_by(TaskModel.created_at.desc()).limit(limit)
            task_query_result = (await session.scalars(task_query)).all()
            tasks = [convert_to_task(task, debug_enabled=self.debug_enabled) for task in task_query_result]

            runs = workflow_runs + tasks

            runs.sort(key=lambda x: x.created_at, reverse=True)

            lower = (page - 1) * page_size
            upper = page * page_size

            return runs[lower:upper]

    @read_retry()
    async def get_all_runs_v2(
        self,
        organization_id: str,
        page: int = 1,
        page_size: int = 10,
        status: list[str] | None = None,
        search_key: str | None = None,
    ) -> list[dict[str, Any]]:
        async with self.Session() as session:
            effective_status = func.coalesce(WorkflowRunModel.status, TaskRunModel.status)
            query = (
                select(
                    TaskRunModel.task_run_id.label("task_run_id"),
                    TaskRunModel.run_id.label("run_id"),
                    TaskRunModel.task_run_type.label("task_run_type"),
                    effective_status.label("status"),
                    TaskRunModel.title.label("title"),
                    TaskRunModel.started_at.label("started_at"),
                    TaskRunModel.finished_at.label("finished_at"),
                    TaskRunModel.created_at.label("created_at"),
                    TaskRunModel.workflow_permanent_id.label("workflow_permanent_id"),
                    TaskRunModel.script_run.label("script_run"),
                    TaskRunModel.searchable_text.label("searchable_text"),
                )
                .select_from(TaskRunModel)
                .outerjoin(
                    WorkflowRunModel,
                    and_(
                        TaskRunModel.task_run_type == RunType.workflow_run,
                        WorkflowRunModel.workflow_run_id == TaskRunModel.run_id,
                        WorkflowRunModel.organization_id == TaskRunModel.organization_id,
                    ),
                )
                .filter(TaskRunModel.organization_id == organization_id)
                .filter(TaskRunModel.status.isnot(None))
                .filter(TaskRunModel.parent_workflow_run_id.is_(None))
                .filter(TaskRunModel.debug_session_id.is_(None))
            )

            if status:
                query = query.filter(effective_status.in_(status))

            if search_key:
                query = query.filter(
                    or_(
                        TaskRunModel.searchable_text.icontains(search_key, autoescape=True),
                        TaskRunModel.run_id.icontains(search_key, autoescape=True),
                        TaskRunModel.workflow_permanent_id.icontains(search_key, autoescape=True),
                    )
                )

            offset = (page - 1) * page_size
            query = query.order_by(TaskRunModel.created_at.desc()).offset(offset).limit(page_size)

            result = await session.execute(query)
            return [dict(row) for row in result.mappings().all()]

    @read_retry()
    @db_operation("get_workflow_run", log_errors=False)
    async def get_workflow_run(
        self,
        workflow_run_id: str,
        organization_id: str | None = None,
        job_id: str | None = None,
        status: WorkflowRunStatus | None = None,
    ) -> WorkflowRun | None:
        async with self.Session() as session:
            get_workflow_run_query = select(WorkflowRunModel).filter_by(workflow_run_id=workflow_run_id)
            if organization_id:
                get_workflow_run_query = get_workflow_run_query.filter_by(organization_id=organization_id)
            if job_id:
                get_workflow_run_query = get_workflow_run_query.filter_by(job_id=job_id)
            if status:
                get_workflow_run_query = get_workflow_run_query.filter_by(status=status.value)
            if workflow_run := (await session.scalars(get_workflow_run_query)).first():
                return convert_to_workflow_run(workflow_run)
            return None

    async def get_run(self, run_id: str, organization_id: str | None = None) -> WorkflowRun | None:
        """Alias satisfying the RunReader protocol."""
        return await self.get_workflow_run(run_id, organization_id=organization_id)

    @db_operation("get_last_queued_workflow_run")
    async def get_last_queued_workflow_run(
        self,
        workflow_permanent_id: str,
        organization_id: str | None = None,
        sequential_key: str | None = None,
    ) -> WorkflowRun | None:
        async with self.Session() as session:
            query = select(WorkflowRunModel).filter_by(workflow_permanent_id=workflow_permanent_id)
            query = query.filter(WorkflowRunModel.browser_session_id.is_(None))
            if organization_id:
                query = query.filter_by(organization_id=organization_id)
            query = query.filter_by(status=WorkflowRunStatus.queued)
            if sequential_key:
                query = query.filter_by(sequential_key=sequential_key)
            query = query.order_by(WorkflowRunModel.modified_at.desc())
            workflow_run = (await session.scalars(query)).first()
            return convert_to_workflow_run(workflow_run) if workflow_run else None

    @db_operation("get_workflow_runs_by_ids")
    async def get_workflow_runs_by_ids(
        self,
        workflow_run_ids: list[str],
        workflow_permanent_id: str | None = None,
        organization_id: str | None = None,
    ) -> list[WorkflowRun]:
        async with self.Session() as session:
            query = select(WorkflowRunModel).filter(WorkflowRunModel.workflow_run_id.in_(workflow_run_ids))
            if workflow_permanent_id:
                query = query.filter_by(workflow_permanent_id=workflow_permanent_id)
            if organization_id:
                query = query.filter_by(organization_id=organization_id)
            workflow_runs = (await session.scalars(query)).all()
            return [convert_to_workflow_run(workflow_run) for workflow_run in workflow_runs]

    @db_operation("get_last_running_workflow_run")
    async def get_last_running_workflow_run(
        self,
        workflow_permanent_id: str,
        organization_id: str | None = None,
        sequential_key: str | None = None,
    ) -> WorkflowRun | None:
        async with self.Session() as session:
            query = select(WorkflowRunModel).filter_by(workflow_permanent_id=workflow_permanent_id)
            query = query.filter(WorkflowRunModel.browser_session_id.is_(None))
            if organization_id:
                query = query.filter_by(organization_id=organization_id)
            query = query.filter_by(status=WorkflowRunStatus.running)
            if sequential_key:
                query = query.filter_by(sequential_key=sequential_key)
            query = query.filter(
                WorkflowRunModel.started_at.isnot(None)
            )  # filter out workflow runs that does not have a started_at timestamp
            query = query.order_by(WorkflowRunModel.started_at.desc())
            workflow_run = (await session.scalars(query)).first()
            return convert_to_workflow_run(workflow_run) if workflow_run else None

    async def _get_last_workflow_run_by_filter(
        self,
        organization_id: str | None = None,
        **filters: str,
    ) -> WorkflowRun | None:
        """Get the last queued or running workflow run matching the given column filters.

        Used for browser_session_id and browser_address sequential execution.
        """
        async with self.Session() as session:
            query = select(WorkflowRunModel).filter_by(**filters)
            if organization_id:
                query = query.filter_by(organization_id=organization_id)

            # check if there's a queued run
            queue_query = query.filter_by(status=WorkflowRunStatus.queued)
            queue_query = queue_query.order_by(WorkflowRunModel.modified_at.desc())
            workflow_run = (await session.scalars(queue_query)).first()
            if workflow_run:
                return convert_to_workflow_run(workflow_run)

            # check if there's a running run
            running_query = query.filter_by(status=WorkflowRunStatus.running)
            running_query = running_query.filter(WorkflowRunModel.started_at.isnot(None))
            running_query = running_query.order_by(WorkflowRunModel.started_at.desc())
            workflow_run = (await session.scalars(running_query)).first()
            if workflow_run:
                return convert_to_workflow_run(workflow_run)
            return None

    async def get_last_workflow_run_for_browser_session(
        self,
        browser_session_id: str,
        organization_id: str | None = None,
    ) -> WorkflowRun | None:
        return await self._get_last_workflow_run_by_filter(
            organization_id=organization_id,
            browser_session_id=browser_session_id,
        )

    async def get_last_workflow_run_for_browser_address(
        self,
        browser_address: str,
        organization_id: str | None = None,
    ) -> WorkflowRun | None:
        return await self._get_last_workflow_run_by_filter(
            organization_id=organization_id,
            browser_address=browser_address,
        )

    @db_operation("get_workflows_depending_on")
    async def get_workflows_depending_on(
        self,
        workflow_run_id: str,
    ) -> list[WorkflowRun]:
        """
        Get all workflow runs that depend on the given workflow_run_id.

        Used to find workflows that should be signaled when a workflow completes,
        for sequential workflow dependency handling.

        Args:
            workflow_run_id: The workflow_run_id to find dependents for

        Returns:
            List of WorkflowRun objects that have depends_on_workflow_run_id set to workflow_run_id
        """
        async with self.Session() as session:
            query = select(WorkflowRunModel).filter_by(depends_on_workflow_run_id=workflow_run_id)
            workflow_runs = (await session.scalars(query)).all()
            return [convert_to_workflow_run(workflow_run) for workflow_run in workflow_runs]

    @staticmethod
    def _apply_search_key_filter(query, search_key: str | None):  # type: ignore[no-untyped-def]
        if not search_key:
            return query
        key_like = f"%{search_key}%"
        # Match workflow_run_id directly
        id_matches = WorkflowRunModel.workflow_run_id.ilike(key_like)
        # Match parameter key or description (only for non-deleted parameter definitions)
        # Use EXISTS to avoid duplicate rows and to keep pagination correct
        param_key_desc_exists = exists(
            select(1)
            .select_from(WorkflowRunParameterModel)
            .join(
                WorkflowParameterModel,
                WorkflowParameterModel.workflow_parameter_id == WorkflowRunParameterModel.workflow_parameter_id,
            )
            .where(WorkflowRunParameterModel.workflow_run_id == WorkflowRunModel.workflow_run_id)
            .where(WorkflowParameterModel.deleted_at.is_(None))
            .where(
                or_(
                    WorkflowParameterModel.key.ilike(key_like),
                    WorkflowParameterModel.description.ilike(key_like),
                )
            )
        )
        # Match run parameter value directly (searches all values regardless of parameter definition status)
        param_value_exists = exists(
            select(1)
            .select_from(WorkflowRunParameterModel)
            .where(WorkflowRunParameterModel.workflow_run_id == WorkflowRunModel.workflow_run_id)
            .where(WorkflowRunParameterModel.value.ilike(key_like))
        )
        # Match extra HTTP headers (cast JSON to text for search, skip NULLs)
        extra_headers_match = and_(
            WorkflowRunModel.extra_http_headers.isnot(None),
            func.cast(WorkflowRunModel.extra_http_headers, Text()).ilike(key_like),
        )
        return query.where(or_(id_matches, param_key_desc_exists, param_value_exists, extra_headers_match))

    def _apply_error_code_filter(self, query, error_code: str | None):  # type: ignore[no-untyped-def]
        if not error_code:
            return query

        if self._dialect_name == "sqlite":
            # Task errors: array of objects like [{"error_code": "timeout", ...}]
            # Use json_each to iterate + json_extract to match the error_code field
            error_code_in_tasks = exists(
                select(1)
                .select_from(TaskModel)
                .where(TaskModel.workflow_run_id == WorkflowRunModel.workflow_run_id)
                .where(
                    exists(
                        select(1)
                        .select_from(func.json_each(TaskModel.errors))
                        .where(func.json_extract(literal_column("json_each.value"), "$.error_code") == error_code)
                    )
                )
            )
            # Block errors: flat array of strings like ["timeout", "network_error"]
            error_code_in_blocks = exists(
                select(1)
                .select_from(WorkflowRunBlockModel)
                .where(WorkflowRunBlockModel.workflow_run_id == WorkflowRunModel.workflow_run_id)
                .where(
                    exists(
                        select(1)
                        .select_from(func.json_each(WorkflowRunBlockModel.error_codes))
                        .where(literal_column("json_each.value") == error_code)
                    )
                )
            )
        else:
            # PostgreSQL: native JSONB containment
            error_code_in_tasks = exists(
                select(1)
                .select_from(TaskModel)
                .where(TaskModel.workflow_run_id == WorkflowRunModel.workflow_run_id)
                .where(cast(TaskModel.errors, JSONB).contains(literal([{"error_code": error_code}], type_=JSONB)))
            )
            error_code_in_blocks = exists(
                select(1)
                .select_from(WorkflowRunBlockModel)
                .where(WorkflowRunBlockModel.workflow_run_id == WorkflowRunModel.workflow_run_id)
                .where(cast(WorkflowRunBlockModel.error_codes, JSONB).contains(literal([error_code], type_=JSONB)))
            )
        return query.where(or_(error_code_in_tasks, error_code_in_blocks))

    @db_operation("get_workflow_runs")
    async def get_workflow_runs(
        self,
        organization_id: str,
        page: int = 1,
        page_size: int = 10,
        status: list[WorkflowRunStatus] | None = None,
        ordering: tuple[str, str] | None = None,
        search_key: str | None = None,
        error_code: str | None = None,
    ) -> list[WorkflowRun]:
        async with self.Session() as session:
            db_page = page - 1  # offset logic is 0 based

            query = (
                select(WorkflowRunModel, WorkflowModel.title)
                .join(WorkflowModel, WorkflowModel.workflow_id == WorkflowRunModel.workflow_id)
                .filter(WorkflowRunModel.organization_id == organization_id)
                .filter(WorkflowRunModel.parent_workflow_run_id.is_(None))
            )

            query = self._apply_search_key_filter(query, search_key)
            query = self._apply_error_code_filter(query, error_code)

            if status:
                query = query.filter(WorkflowRunModel.status.in_(status))

            allowed_ordering_fields = {
                "created_at": WorkflowRunModel.created_at,
                "status": WorkflowRunModel.status,
            }

            field, direction = ("created_at", "desc")

            if ordering and isinstance(ordering, tuple) and len(ordering) == 2:
                req_field, req_direction = ordering
                if req_field in allowed_ordering_fields and req_direction in ("asc", "desc"):
                    field, direction = req_field, req_direction

            order_column = allowed_ordering_fields[field]

            if direction == "asc":
                query = query.order_by(order_column.asc())
            else:
                query = query.order_by(order_column.desc())

            query = query.limit(page_size).offset(db_page * page_size)

            workflow_runs = (await session.execute(query)).all()

            return [
                convert_to_workflow_run(run, workflow_title=title, debug_enabled=self.debug_enabled)
                for run, title in workflow_runs
            ]

    @db_operation("get_workflow_runs_count")
    async def get_workflow_runs_count(
        self,
        organization_id: str,
        status: list[WorkflowRunStatus] | None = None,
    ) -> int:
        async with self.Session() as session:
            count_query = (
                select(func.count())
                .select_from(WorkflowRunModel)
                .filter(WorkflowRunModel.organization_id == organization_id)
            )
            if status:
                count_query = count_query.filter(WorkflowRunModel.status.in_(status))
            return (await session.execute(count_query)).scalar_one()

    @db_operation("get_workflow_runs_for_organization_by_status")
    async def get_workflow_runs_for_organization_by_status(
        self,
        organization_id: str,
        status: WorkflowRunStatus,
        limit: int | None = None,
    ) -> list[WorkflowRun]:
        """Return workflow runs for an organization ordered oldest-first."""
        async with self.Session() as session:
            query = (
                select(WorkflowRunModel)
                .filter(WorkflowRunModel.organization_id == organization_id)
                .filter(WorkflowRunModel.status == status.value)
                .order_by(WorkflowRunModel.created_at.asc())
            )
            if limit is not None:
                query = query.limit(limit)
            workflow_runs = (await session.scalars(query)).all()
            return [convert_to_workflow_run(workflow_run) for workflow_run in workflow_runs]

    @db_operation("get_workflow_runs_for_organization_by_statuses")
    async def get_workflow_runs_for_organization_by_statuses(
        self,
        organization_id: str,
        statuses: list[WorkflowRunStatus],
        limit: int | None = None,
    ) -> list[WorkflowRun]:
        """Return workflow runs for an organization filtered by multiple statuses."""
        if not statuses:
            return []

        async with self.Session() as session:
            query = (
                select(WorkflowRunModel)
                .filter(WorkflowRunModel.organization_id == organization_id)
                .filter(WorkflowRunModel.status.in_([status.value for status in statuses]))
                .order_by(WorkflowRunModel.created_at.asc())
            )
            if limit is not None:
                query = query.limit(limit)
            workflow_runs = (await session.scalars(query)).all()
            return [convert_to_workflow_run(workflow_run) for workflow_run in workflow_runs]

    @db_operation("get_workflow_runs_for_workflow_permanent_id")
    async def get_workflow_runs_for_workflow_permanent_id(
        self,
        workflow_permanent_id: str,
        organization_id: str,
        page: int = 1,
        page_size: int = 10,
        status: list[WorkflowRunStatus] | None = None,
        search_key: str | None = None,
        error_code: str | None = None,
    ) -> list[WorkflowRun]:
        """
        Get runs for a workflow, with optional `search_key` on run ID, parameter key/description/value,
        or extra HTTP headers.
        """
        async with self.Session() as session:
            db_page = page - 1  # offset logic is 0 based
            query = (
                select(WorkflowRunModel, WorkflowModel.title)
                .join(WorkflowModel, WorkflowModel.workflow_id == WorkflowRunModel.workflow_id)
                .filter(WorkflowRunModel.workflow_permanent_id == workflow_permanent_id)
                .filter(WorkflowRunModel.organization_id == organization_id)
            )
            query = self._apply_search_key_filter(query, search_key)
            query = self._apply_error_code_filter(query, error_code)
            if status:
                query = query.filter(WorkflowRunModel.status.in_(status))
            query = query.order_by(WorkflowRunModel.created_at.desc()).limit(page_size).offset(db_page * page_size)
            workflow_runs_and_titles_tuples = (await session.execute(query)).all()
            workflow_runs = [
                convert_to_workflow_run(run, workflow_title=title, debug_enabled=self.debug_enabled)
                for run, title in workflow_runs_and_titles_tuples
            ]
            return workflow_runs

    @db_operation("get_workflow_runs_by_parent_workflow_run_id")
    async def get_workflow_runs_by_parent_workflow_run_id(
        self,
        parent_workflow_run_id: str,
        organization_id: str | None = None,
    ) -> list[WorkflowRun]:
        async with self.Session() as session:
            query = select(WorkflowRunModel).filter(WorkflowRunModel.parent_workflow_run_id == parent_workflow_run_id)
            if organization_id is not None:
                query = query.filter(WorkflowRunModel.organization_id == organization_id)
            workflow_runs = (await session.scalars(query)).all()
            return [convert_to_workflow_run(run) for run in workflow_runs]

    @db_operation("get_workflow_run_output_parameters")
    async def get_workflow_run_output_parameters(self, workflow_run_id: str) -> list[WorkflowRunOutputParameter]:
        async with self.Session() as session:
            workflow_run_output_parameters = (
                await session.scalars(
                    select(WorkflowRunOutputParameterModel)
                    .filter_by(workflow_run_id=workflow_run_id)
                    .order_by(WorkflowRunOutputParameterModel.created_at)
                )
            ).all()
            return [
                convert_to_workflow_run_output_parameter(parameter, self.debug_enabled)
                for parameter in workflow_run_output_parameters
            ]

    @db_operation("get_workflow_run_output_parameter_by_id")
    async def get_workflow_run_output_parameter_by_id(
        self, workflow_run_id: str, output_parameter_id: str
    ) -> WorkflowRunOutputParameter | None:
        async with self.Session() as session:
            parameter = (
                await session.scalars(
                    select(WorkflowRunOutputParameterModel)
                    .filter_by(workflow_run_id=workflow_run_id)
                    .filter_by(output_parameter_id=output_parameter_id)
                    .order_by(WorkflowRunOutputParameterModel.created_at)
                )
            ).first()

            if parameter:
                return convert_to_workflow_run_output_parameter(parameter, self.debug_enabled)

            return None

    @db_operation("create_or_update_workflow_run_output_parameter")
    async def create_or_update_workflow_run_output_parameter(
        self,
        workflow_run_id: str,
        output_parameter_id: str,
        value: dict[str, Any] | list | str | None,
    ) -> WorkflowRunOutputParameter:
        async with self.Session() as session:
            # check if the workflow run output parameter already exists
            # if it does, update the value
            if workflow_run_output_parameter := (
                await session.scalars(
                    select(WorkflowRunOutputParameterModel)
                    .filter_by(workflow_run_id=workflow_run_id)
                    .filter_by(output_parameter_id=output_parameter_id)
                )
            ).first():
                LOG.info(
                    "Updating existing workflow run output parameter",
                    workflow_run_id=workflow_run_output_parameter.workflow_run_id,
                    output_parameter_id=workflow_run_output_parameter.output_parameter_id,
                )
                workflow_run_output_parameter.value = value
                await session.commit()
                await session.refresh(workflow_run_output_parameter)
                return convert_to_workflow_run_output_parameter(workflow_run_output_parameter, self.debug_enabled)

            # if it does not exist, create a new one
            workflow_run_output_parameter = WorkflowRunOutputParameterModel(
                workflow_run_id=workflow_run_id,
                output_parameter_id=output_parameter_id,
                value=value,
            )
            session.add(workflow_run_output_parameter)
            await session.commit()
            await session.refresh(workflow_run_output_parameter)
            return convert_to_workflow_run_output_parameter(workflow_run_output_parameter, self.debug_enabled)

    @db_operation("update_workflow_run_output_parameter")
    async def update_workflow_run_output_parameter(
        self,
        workflow_run_id: str,
        output_parameter_id: str,
        value: dict[str, Any] | list | str | None,
    ) -> WorkflowRunOutputParameter:
        async with self.Session() as session:
            workflow_run_output_parameter = (
                await session.scalars(
                    select(WorkflowRunOutputParameterModel)
                    .filter_by(workflow_run_id=workflow_run_id)
                    .filter_by(output_parameter_id=output_parameter_id)
                )
            ).first()
            if not workflow_run_output_parameter:
                raise NotFoundError(
                    f"WorkflowRunOutputParameter not found for {workflow_run_id} and {output_parameter_id}"
                )
            workflow_run_output_parameter.value = value
            await session.commit()
            await session.refresh(workflow_run_output_parameter)
            return convert_to_workflow_run_output_parameter(workflow_run_output_parameter, self.debug_enabled)

    @db_operation("create_workflow_run_parameter")
    async def create_workflow_run_parameter(
        self, workflow_run_id: str, workflow_parameter: WorkflowParameter, value: Any
    ) -> WorkflowRunParameter:
        workflow_parameter_id = workflow_parameter.workflow_parameter_id
        async with self.Session() as session:
            workflow_run_parameter = WorkflowRunParameterModel(
                workflow_run_id=workflow_run_id,
                workflow_parameter_id=workflow_parameter_id,
                value=value,
            )
            session.add(workflow_run_parameter)
            await session.flush()
            converted = convert_to_workflow_run_parameter(
                workflow_run_parameter, workflow_parameter, self.debug_enabled
            )
            await session.commit()
            return converted

    @db_operation("create_workflow_run_parameters")
    async def create_workflow_run_parameters(
        self,
        workflow_run_id: str,
        workflow_parameter_values: list[tuple[WorkflowParameter, Any]],
    ) -> list[WorkflowRunParameter]:
        if not workflow_parameter_values:
            return []

        workflow_run_parameters = [
            WorkflowRunParameterModel(
                workflow_run_id=workflow_run_id,
                workflow_parameter_id=workflow_parameter.workflow_parameter_id,
                value=value,
            )
            for workflow_parameter, value in workflow_parameter_values
        ]

        async with self.Session() as session:
            session.add_all(workflow_run_parameters)
            await session.flush()
            converted = [
                convert_to_workflow_run_parameter(workflow_run_parameter, workflow_parameter, self.debug_enabled)
                for workflow_run_parameter, (workflow_parameter, _) in zip(
                    workflow_run_parameters, workflow_parameter_values, strict=True
                )
            ]
            await session.commit()
            return converted

    @db_operation("get_workflow_run_parameters")
    async def get_workflow_run_parameters(
        self, workflow_run_id: str
    ) -> list[tuple[WorkflowParameter, WorkflowRunParameter]]:
        async with self.Session() as session:
            workflow_run_parameters = (
                await session.scalars(select(WorkflowRunParameterModel).filter_by(workflow_run_id=workflow_run_id))
            ).all()
            results = []
            for workflow_run_parameter in workflow_run_parameters:
                if self._workflow_parameter_reader is None:
                    raise RuntimeError("workflow_parameter_reader dependency not set")
                workflow_parameter = await self._workflow_parameter_reader.get_workflow_parameter(
                    workflow_run_parameter.workflow_parameter_id
                )
                if not workflow_parameter:
                    raise WorkflowParameterNotFound(workflow_parameter_id=workflow_run_parameter.workflow_parameter_id)
                results.append(
                    (
                        workflow_parameter,
                        convert_to_workflow_run_parameter(
                            workflow_run_parameter,
                            workflow_parameter,
                            self.debug_enabled,
                        ),
                    )
                )
            return results

    @db_operation("get_workflow_run_block_errors")
    async def get_workflow_run_block_errors(
        self,
        workflow_run_id: str,
        organization_id: str | None = None,
    ) -> list[tuple[list[str], str | None]]:
        """Return (error_codes, failure_reason) tuples for blocks with non-null error_codes."""
        async with self.Session() as session:
            query = select(WorkflowRunBlockModel.error_codes, WorkflowRunBlockModel.failure_reason).filter_by(
                workflow_run_id=workflow_run_id
            )
            if organization_id is not None:
                query = query.filter_by(organization_id=organization_id)
            query = query.where(WorkflowRunBlockModel.error_codes.isnot(None))
            rows = (await session.execute(query)).all()
            return [(row.error_codes, row.failure_reason) for row in rows]
