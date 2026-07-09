from __future__ import annotations

from datetime import datetime, timedelta
from typing import TYPE_CHECKING, Any, Callable
from typing import cast as typing_cast

import structlog
from sqlalchemy import (
    ColumnElement,
    Label,
    Text,
    and_,
    cast,
    exists,
    func,
    literal,
    literal_column,
    or_,
    select,
    update,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.exc import SQLAlchemyError

from skyvern.exceptions import WorkflowParameterNotFound, WorkflowRunNotFound
from skyvern.forge.sdk.db._error_handling import db_operation
from skyvern.forge.sdk.db.base_alchemy_db import read_retry
from skyvern.forge.sdk.db.base_repository import BaseRepository
from skyvern.forge.sdk.db.datetime_utils import naive_utc_now, to_naive_utc
from skyvern.forge.sdk.db.enums import WorkflowRunTriggerType
from skyvern.forge.sdk.db.exceptions import NotFoundError

if TYPE_CHECKING:
    from skyvern.forge.sdk.db.base_alchemy_db import _SessionFactory

from skyvern.forge.sdk.db._sentinels import _UNSET
from skyvern.forge.sdk.db.models import (
    PersistentBrowserSessionModel,
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
    truncate_oversized_jsonb_value,
)
from skyvern.forge.sdk.log_artifacts import save_workflow_run_logs
from skyvern.forge.sdk.schemas.persistent_browser_sessions import FORCED_WORKFLOW_SESSION_RUNNABLE_TYPE
from skyvern.forge.sdk.schemas.tasks import Task
from skyvern.forge.sdk.workflow.models.parameter import WorkflowParameter
from skyvern.forge.sdk.workflow.models.workflow import (
    WorkflowRun,
    WorkflowRunOutputParameter,
    WorkflowRunParameter,
    WorkflowRunStatus,
)
from skyvern.schemas.runs import MAX_SEARCH_FETCH_LIMIT, ProxyLocationInput, RunType

LOG = structlog.get_logger()


def _merge_script_run(
    existing: dict | None,
    ai_fallback_triggered: bool | None,
    script_id: str | None,
    script_revision_id: str | None,
) -> dict:
    """Merge-on-write semantics for `workflow_runs.script_run`.

    Callers update different facets of `script_run` at different points in a
    run's lifecycle — setup time writes script identity, mid-execution fallback
    writes `ai_fallback_triggered=True`. A replace-based update would clobber
    whichever facet the caller didn't touch, so merge preserves the other.

    Pure function for testability (see `tests/unit/db/
    test_workflow_runs_script_run_merge.py`). None-valued params are skipped;
    non-None params overwrite the corresponding key in the merged dict.
    """
    merged = dict(existing or {})
    if ai_fallback_triggered is not None:
        merged["ai_fallback_triggered"] = ai_fallback_triggered
    if script_id is not None:
        merged["script_id"] = script_id
    if script_revision_id is not None:
        merged["script_revision_id"] = script_revision_id
    return merged


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

    @staticmethod
    def _workflow_deleted_expr(
        workflow_permanent_id_col: ColumnElement[str | None],
        organization_id_col: ColumnElement[str],
    ) -> Label[bool]:
        active_workflow_exists = (
            select(1)
            .select_from(WorkflowModel)
            .where(
                and_(
                    WorkflowModel.workflow_permanent_id == workflow_permanent_id_col,
                    WorkflowModel.organization_id == organization_id_col,
                    WorkflowModel.deleted_at.is_(None),
                )
            )
            .correlate_except(WorkflowModel)
            .exists()
        )
        return and_(
            workflow_permanent_id_col.isnot(None),
            ~active_workflow_exists,
        ).label("workflow_deleted")

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
            stale_cutoff = naive_utc_now() - timedelta(hours=stale_threshold_hours)

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
        max_elapsed_time_minutes: int | None = None,
        extra_http_headers: dict[str, str] | None = None,
        cdp_connect_headers: dict[str, str] | None = None,
        browser_address: str | None = None,
        sequential_key: str | None = None,
        run_with: str | None = None,
        debug_session_id: str | None = None,
        ai_fallback: bool | None = None,
        code_gen: bool | None = None,
        workflow_run_id: str | None = None,
        trigger_type: WorkflowRunTriggerType | None = None,
        workflow_schedule_id: str | None = None,
        ignore_inherited_workflow_system_prompt: bool = False,
        copilot_session_id: str | None = None,
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
                max_elapsed_time_minutes=max_elapsed_time_minutes,
                extra_http_headers=extra_http_headers,
                cdp_connect_headers=cdp_connect_headers,
                browser_address=browser_address,
                sequential_key=sequential_key,
                run_with=run_with,
                debug_session_id=debug_session_id,
                ai_fallback=ai_fallback,
                code_gen=code_gen,
                trigger_type=trigger_type.value if trigger_type else None,
                workflow_schedule_id=workflow_schedule_id,
                ignore_inherited_workflow_system_prompt=ignore_inherited_workflow_system_prompt,
                copilot_session_id=copilot_session_id,
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
        script_id: str | None = None,
        script_revision_id: str | None = None,
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
        proxy_location: ProxyLocationInput | object = _UNSET,
        browser_address: str | None = None,
        extra_http_headers: dict[str, str] | None = None,
        cdp_connect_headers: dict[str, str] | None = None,
        failure_category: list[dict[str, Any]] | None = None,
        started_at: datetime | None | object = _UNSET,
        queued_at: datetime | None | object = _UNSET,
        finished_at: datetime | None | object = _UNSET,
    ) -> WorkflowRun:
        async with self.Session() as session:
            workflow_run = (
                await session.scalars(select(WorkflowRunModel).filter_by(workflow_run_id=workflow_run_id))
            ).first()
            if workflow_run:
                if status:
                    workflow_run.status = status
                if status and status == WorkflowRunStatus.queued and workflow_run.queued_at is None:
                    workflow_run.queued_at = naive_utc_now()
                if status and status == WorkflowRunStatus.running and workflow_run.started_at is None:
                    workflow_run.started_at = naive_utc_now()
                if status and status.is_final() and workflow_run.finished_at is None:
                    workflow_run.finished_at = naive_utc_now()
                if failure_reason:
                    workflow_run.failure_reason = failure_reason
                if webhook_failure_reason is not None:
                    workflow_run.webhook_failure_reason = webhook_failure_reason
                if ai_fallback_triggered is not None or script_id is not None or script_revision_id is not None:
                    workflow_run.script_run = _merge_script_run(
                        existing=workflow_run.script_run,
                        ai_fallback_triggered=ai_fallback_triggered,
                        script_id=script_id,
                        script_revision_id=script_revision_id,
                    )
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
                if extra_http_headers is not None:
                    workflow_run.extra_http_headers = extra_http_headers
                if cdp_connect_headers is not None:
                    workflow_run.cdp_connect_headers = cdp_connect_headers
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
                if proxy_location is not _UNSET:
                    workflow_run.proxy_location = serialize_proxy_location(
                        typing_cast(ProxyLocationInput, proxy_location)
                    )
                if failure_category is not None:
                    workflow_run.failure_category = failure_category
                # Explicit timestamp overrides (used when resetting workflow runs)
                if started_at is not _UNSET:
                    workflow_run.started_at = to_naive_utc(typing_cast(datetime | None, started_at))
                if queued_at is not _UNSET:
                    workflow_run.queued_at = to_naive_utc(typing_cast(datetime | None, queued_at))
                if finished_at is not _UNSET:
                    workflow_run.finished_at = to_naive_utc(typing_cast(datetime | None, finished_at))
                await session.commit()
                await save_workflow_run_logs(workflow_run_id)
                await session.refresh(workflow_run)
                return convert_to_workflow_run(workflow_run)
            else:
                raise WorkflowRunNotFound(workflow_run_id)

    @db_operation("increment_workflow_run_credits")
    async def increment_workflow_run_credits(
        self,
        workflow_run_id: str,
        credits: int,
        is_cached: bool = False,
    ) -> None:
        col = WorkflowRunModel.cached_credits_used if is_cached else WorkflowRunModel.credits_used
        async with self.Session() as session:
            result = await session.execute(
                update(WorkflowRunModel)
                .where(WorkflowRunModel.workflow_run_id == workflow_run_id)
                .values({col: func.coalesce(col, 0) + credits})
            )
            if result.rowcount == 0:
                LOG.warning(
                    "increment_workflow_run_credits matched no rows",
                    workflow_run_id=workflow_run_id,
                    credits=credits,
                    is_cached=is_cached,
                )
            await session.commit()

    @db_operation("update_workflow_run_if_not_final")
    async def update_workflow_run_if_not_final(
        self,
        workflow_run_id: str,
        status: WorkflowRunStatus,
        failure_reason: str | None = None,
        run_with: str | None = None,
    ) -> WorkflowRun | None:
        """Transition a workflow run to ``status`` only if it is not already in a
        terminal state. Returns the updated row, or ``None`` when the row was
        already terminal (or missing). Implemented as a single conditional
        ``UPDATE ... WHERE status IN (<non-terminal>)`` so a concurrent
        finalization write cannot be clobbered by a late cancel.

        Mirrors the timestamp side effects of :meth:`update_workflow_run`:
        ``finished_at`` is stamped on terminal transitions and ``started_at``
        is stamped on the first ``running`` transition (preserving any
        existing value via ``COALESCE``).
        """
        non_terminal = [s.value for s in WorkflowRunStatus if not s.is_final()]
        now = naive_utc_now()
        values: dict[str, Any] = {"status": status}
        if status.is_final():
            values["finished_at"] = now
        if status == WorkflowRunStatus.running:
            values["started_at"] = func.coalesce(WorkflowRunModel.started_at, now)
        if failure_reason is not None:
            values["failure_reason"] = failure_reason
        if run_with is not None:
            values["run_with"] = run_with

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
            # save_workflow_run_logs reuses this session and commits, expiring `refreshed`.
            # Refresh before convert_to_workflow_run to avoid a greenlet-less lazy-load (MissingGreenlet).
            await session.refresh(refreshed)
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
                .filter(WorkflowRunModel.copilot_session_id.is_(None))
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
            # task_runs.workflow_permanent_id is unreliable on legacy workflow_run rows; the joined
            # workflow_runs row carries the canonical WPID, so coalesce both before deriving anything.
            effective_wpid = func.coalesce(
                TaskRunModel.workflow_permanent_id,
                WorkflowRunModel.workflow_permanent_id,
            )
            workflow_deleted_expr = self._workflow_deleted_expr(effective_wpid, TaskRunModel.organization_id)
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
                    effective_wpid.label("workflow_permanent_id"),
                    TaskRunModel.script_run.label("script_run"),
                    WorkflowRunModel.trigger_type.label("trigger_type"),
                    TaskRunModel.searchable_text.label("searchable_text"),
                    workflow_deleted_expr,
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
                # Coalesced filter so rows with NULL task_runs.status but a set workflow_runs.status are visible.
                .filter(effective_status.isnot(None))
                .filter(TaskRunModel.parent_workflow_run_id.is_(None))
                .filter(TaskRunModel.debug_session_id.is_(None))
                .filter(WorkflowRunModel.copilot_session_id.is_(None))
            )

            if status:
                query = query.filter(effective_status.in_(status))

            if search_key:
                query = query.filter(
                    or_(
                        TaskRunModel.searchable_text.icontains(search_key, autoescape=True),
                        TaskRunModel.run_id.icontains(search_key, autoescape=True),
                        effective_wpid.icontains(search_key, autoescape=True),
                        # task_runs.searchable_text is only title+url, so agent inputs are matched via
                        # workflow_run_parameters / extra_http_headers correlated on this run's run_id.
                        *self._run_input_search_clauses(
                            search_key,
                            TaskRunModel.run_id,
                            WorkflowRunModel.extra_http_headers,
                        ),
                    )
                )

            offset = (page - 1) * page_size
            # Search merges task_runs and fallback workflow_runs before slicing, so each source fetches enough rows.
            query_limit = min(page * page_size, MAX_SEARCH_FETCH_LIMIT) if search_key else page_size
            query = query.order_by(TaskRunModel.created_at.desc()).limit(query_limit)
            if not search_key:
                query = query.offset(offset)

            result = await session.execute(query)
            rows = [dict(row) for row in result.mappings().all()]

            if search_key:
                task_run_exists = (
                    select(1)
                    .select_from(TaskRunModel)
                    .where(TaskRunModel.organization_id == WorkflowRunModel.organization_id)
                    .where(TaskRunModel.run_id == WorkflowRunModel.workflow_run_id)
                    .correlate_except(TaskRunModel)
                    .exists()
                )
                fallback_query = (
                    select(
                        WorkflowRunModel.workflow_run_id.label("task_run_id"),
                        WorkflowRunModel.workflow_run_id.label("run_id"),
                        literal(RunType.workflow_run.value).label("task_run_type"),
                        WorkflowRunModel.status.label("status"),
                        WorkflowModel.title.label("title"),
                        WorkflowRunModel.started_at.label("started_at"),
                        WorkflowRunModel.finished_at.label("finished_at"),
                        WorkflowRunModel.created_at.label("created_at"),
                        WorkflowRunModel.workflow_permanent_id.label("workflow_permanent_id"),
                        WorkflowRunModel.script_run.label("script_run"),
                        WorkflowRunModel.trigger_type.label("trigger_type"),
                        WorkflowModel.title.label("searchable_text"),
                        self._workflow_deleted_expr(
                            WorkflowRunModel.workflow_permanent_id,
                            WorkflowRunModel.organization_id,
                        ),
                    )
                    .select_from(WorkflowRunModel)
                    .outerjoin(WorkflowModel, WorkflowModel.workflow_id == WorkflowRunModel.workflow_id)
                    .filter(WorkflowRunModel.organization_id == organization_id)
                    .filter(WorkflowRunModel.parent_workflow_run_id.is_(None))
                    .filter(WorkflowRunModel.debug_session_id.is_(None))
                    .filter(WorkflowRunModel.copilot_session_id.is_(None))
                    .filter(~task_run_exists)
                )
                fallback_query = self._apply_workflow_run_search_key_filter(fallback_query, search_key)
                if status:
                    fallback_query = fallback_query.filter(WorkflowRunModel.status.in_(status))
                fallback_query = fallback_query.order_by(WorkflowRunModel.created_at.desc()).limit(query_limit)
                fallback_result = await session.execute(fallback_query)
                rows.extend(dict(row) for row in fallback_result.mappings().all())
                rows.sort(key=lambda row: row["created_at"], reverse=True)
                rows = rows[offset : offset + page_size]

            return rows

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
        include_browser_session_rows: bool = False,
    ) -> WorkflowRun | None:
        async with self.Session() as session:
            query = select(WorkflowRunModel).filter_by(workflow_permanent_id=workflow_permanent_id)
            if not include_browser_session_rows:
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
        include_browser_session_rows: bool = False,
    ) -> WorkflowRun | None:
        async with self.Session() as session:
            query = select(WorkflowRunModel).filter_by(workflow_permanent_id=workflow_permanent_id)
            if not include_browser_session_rows:
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

    @db_operation("get_blocking_sequential_workflow_run")
    async def get_blocking_sequential_workflow_run(self, workflow_run_id: str) -> WorkflowRun | None:
        # Sequential-execution gate: returns the earliest-queued run that shares this run's
        # sequential identity and is still in flight (queued/running/paused) and was queued
        # before it, or None when this run is clear to start. A direct scan over all
        # same-identity runs — not a walk of one depends_on chain — so it holds when the
        # dependency graph fans out (forest) or a queued predecessor is canceled.
        # Ordering uses queued_at, not created_at: it is stamped at enqueue (before Temporal
        # submission) so it reflects submit order. The no-double-start guarantee rests on the
        # strict total order over (queued_at, workflow_run_id) below, not on any lock.
        async with self.Session() as session:
            run = (await session.scalars(select(WorkflowRunModel).filter_by(workflow_run_id=workflow_run_id))).first()
            if run is None:
                return None

            self_forced = False
            if run.browser_session_id and not run.debug_session_id:
                try:
                    persistent_browser_session = (
                        await session.scalars(
                            select(PersistentBrowserSessionModel).filter_by(
                                persistent_browser_session_id=run.browser_session_id,
                                organization_id=run.organization_id,
                            )
                        )
                    ).first()
                    self_forced = (
                        persistent_browser_session is not None
                        and persistent_browser_session.runnable_type == FORCED_WORKFLOW_SESSION_RUNNABLE_TYPE
                    )
                except Exception:
                    LOG.warning(
                        "Failed to fetch persistent browser session for runtime lane selection",
                        workflow_run_id=workflow_run_id,
                        browser_session_id=run.browser_session_id,
                        organization_id=run.organization_id,
                        exc_info=True,
                    )

            # Lane resolution mirrors enqueue priority: browser_session_id > browser_address
            # > sequential_key > whole workflow. Debug and forced-session runs carry a
            # browser_session_id but are excluded from the session lane as they are at enqueue.
            query = select(WorkflowRunModel).filter_by(organization_id=run.organization_id)
            if run.browser_session_id and not run.debug_session_id and not self_forced:
                query = query.filter_by(browser_session_id=run.browser_session_id)
            elif run.browser_address:
                query = query.filter_by(browser_address=run.browser_address)
            elif run.sequential_key:
                query = query.filter_by(
                    workflow_permanent_id=run.workflow_permanent_id,
                    sequential_key=run.sequential_key,
                )
                if not self_forced:
                    query = query.filter(WorkflowRunModel.browser_session_id.is_(None))
            else:
                query = query.filter_by(workflow_permanent_id=run.workflow_permanent_id)
                if not self_forced:
                    query = query.filter(WorkflowRunModel.browser_session_id.is_(None))

            # Sequential runs are stamped queued_at before Temporal submission; the fallback
            # only guards hand-created rows (e.g. tests) from comparing against None.
            self_queued_at = run.queued_at if run.queued_at is not None else run.created_at

            query = query.filter(
                WorkflowRunModel.status.in_(
                    [
                        WorkflowRunStatus.queued,
                        WorkflowRunStatus.running,
                        WorkflowRunStatus.paused,
                    ]
                )
            )
            # Safe because a sequential run is always stamped queued_at (passes through queued)
            # before it can reach running/paused, so this filter never hides a real blocker.
            query = query.filter(WorkflowRunModel.queued_at.isnot(None))
            query = query.filter(
                or_(
                    WorkflowRunModel.queued_at < self_queued_at,
                    and_(
                        WorkflowRunModel.queued_at == self_queued_at,
                        WorkflowRunModel.workflow_run_id < run.workflow_run_id,
                    ),
                )
            )
            query = query.order_by(
                WorkflowRunModel.queued_at.asc(),
                WorkflowRunModel.workflow_run_id.asc(),
            )
            blocker = (await session.scalars(query)).first()
            return convert_to_workflow_run(blocker) if blocker else None

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
    def _run_input_search_clauses(
        search_key: str,
        workflow_run_id_col: ColumnElement[Any],
        extra_http_headers_col: ColumnElement[Any],
    ) -> list[ColumnElement[bool]]:
        """Clauses matching a run's agent inputs: workflow_run_parameters (key/description/value)
        and extra_http_headers, correlated on the given workflow_run_id column. Self-contained
        subqueries, so they OR into either the task_runs or workflow_runs search without adding an
        implicit FROM."""
        # Match parameter key or description (only for non-deleted parameter definitions).
        # Use EXISTS to avoid duplicate rows and to keep pagination correct.
        param_key_desc_exists = exists(
            select(1)
            .select_from(WorkflowRunParameterModel)
            .join(
                WorkflowParameterModel,
                WorkflowParameterModel.workflow_parameter_id == WorkflowRunParameterModel.workflow_parameter_id,
            )
            .where(WorkflowRunParameterModel.workflow_run_id == workflow_run_id_col)
            .where(WorkflowParameterModel.deleted_at.is_(None))
            .where(
                or_(
                    WorkflowParameterModel.key.icontains(search_key, autoescape=True),
                    WorkflowParameterModel.description.icontains(search_key, autoescape=True),
                )
            )
        )
        # Match run parameter value directly (searches all values regardless of parameter definition status).
        param_value_exists = exists(
            select(1)
            .select_from(WorkflowRunParameterModel)
            .where(WorkflowRunParameterModel.workflow_run_id == workflow_run_id_col)
            .where(WorkflowRunParameterModel.value.icontains(search_key, autoescape=True))
        )
        # Match extra HTTP headers (cast JSON to text for search, skip NULLs).
        extra_headers_match = and_(
            extra_http_headers_col.isnot(None),
            func.cast(extra_http_headers_col, Text()).icontains(search_key, autoescape=True),
        )
        return [param_key_desc_exists, param_value_exists, extra_headers_match]

    @staticmethod
    def _apply_workflow_run_search_key_filter(query, search_key: str | None):  # type: ignore[no-untyped-def]
        if not search_key:
            return query
        # Call only on WorkflowRunModel queries that already join WorkflowModel. The TaskRunModel query in
        # get_all_runs_v2 uses its own filter so workflow-title search cannot add an implicit workflows FROM.
        id_matches = WorkflowRunModel.workflow_run_id.icontains(search_key, autoescape=True)
        workflow_title_matches = WorkflowModel.title.icontains(search_key, autoescape=True)
        workflow_permanent_id_matches = WorkflowRunModel.workflow_permanent_id.icontains(
            search_key,
            autoescape=True,
        )
        return query.where(
            or_(
                id_matches,
                workflow_title_matches,
                workflow_permanent_id_matches,
                *WorkflowRunsRepository._run_input_search_clauses(
                    search_key,
                    WorkflowRunModel.workflow_run_id,
                    WorkflowRunModel.extra_http_headers,
                ),
            )
        )

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
                .filter(WorkflowRunModel.copilot_session_id.is_(None))
            )

            query = self._apply_workflow_run_search_key_filter(query, search_key)
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
        exclude_child_runs: bool = False,
        created_at_start: datetime | None = None,
        created_at_end: datetime | None = None,
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
                .filter(WorkflowRunModel.copilot_session_id.is_(None))
            )
            if exclude_child_runs:
                query = query.filter(WorkflowRunModel.parent_workflow_run_id.is_(None))
            query = self._apply_workflow_run_search_key_filter(query, search_key)
            query = self._apply_error_code_filter(query, error_code)
            if status:
                query = query.filter(WorkflowRunModel.status.in_(status))
            if created_at_start is not None:
                query = query.filter(WorkflowRunModel.created_at >= created_at_start)
            if created_at_end is not None:
                query = query.filter(WorkflowRunModel.created_at < created_at_end)
            query = query.order_by(WorkflowRunModel.created_at.desc()).limit(page_size).offset(db_page * page_size)
            workflow_runs_and_titles_tuples = (await session.execute(query)).all()
            workflow_runs = [
                convert_to_workflow_run(run, workflow_title=title, debug_enabled=self.debug_enabled)
                for run, title in workflow_runs_and_titles_tuples
            ]
            return workflow_runs

    @db_operation("get_workflow_runs_for_browser_session")
    async def get_workflow_runs_for_browser_session(
        self,
        browser_session_id: str,
        organization_id: str,
        page: int = 1,
        page_size: int = 10,
    ) -> list[WorkflowRun]:
        async with self.Session() as session:
            db_page = page - 1
            query = (
                select(WorkflowRunModel, WorkflowModel.title)
                .join(WorkflowModel, WorkflowModel.workflow_id == WorkflowRunModel.workflow_id)
                .filter(WorkflowRunModel.browser_session_id == browser_session_id)
                .filter(WorkflowRunModel.organization_id == organization_id)
                .filter(WorkflowRunModel.parent_workflow_run_id.is_(None))
                .filter(WorkflowRunModel.copilot_session_id.is_(None))
                .order_by(WorkflowRunModel.created_at.desc())
                .limit(page_size)
                .offset(db_page * page_size)
            )
            workflow_runs_and_titles_tuples = (await session.execute(query)).all()
            return [
                convert_to_workflow_run(run, workflow_title=title, debug_enabled=self.debug_enabled)
                for run, title in workflow_runs_and_titles_tuples
            ]

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
        value = truncate_oversized_jsonb_value(
            value,
            context={"workflow_run_id": workflow_run_id, "output_parameter_id": output_parameter_id},
        )
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
        value = truncate_oversized_jsonb_value(
            value,
            context={"workflow_run_id": workflow_run_id, "output_parameter_id": output_parameter_id},
        )
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
