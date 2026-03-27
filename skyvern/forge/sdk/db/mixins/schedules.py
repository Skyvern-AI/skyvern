from __future__ import annotations

import asyncio
from datetime import datetime
from typing import TYPE_CHECKING, Any

import structlog
from sqlalchemy import func, or_, select, text, update

from skyvern.forge.sdk.db._error_handling import db_operation, register_passthrough_exception
from skyvern.forge.sdk.db.models import (
    WorkflowModel,
    WorkflowRunModel,
    WorkflowScheduleModel,
)
from skyvern.forge.sdk.db.utils import convert_to_workflow_schedule
from skyvern.forge.sdk.schemas.workflow_schedules import OrganizationScheduleItem, WorkflowSchedule
from skyvern.forge.sdk.workflow.schedules import compute_next_run

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncEngine

    from skyvern.forge.sdk.db.base_alchemy_db import _SessionFactory

LOG = structlog.get_logger()
_UNSET = object()


class ScheduleLimitExceededError(Exception):
    """Raised when attempting to create a schedule that would exceed the per-workflow limit."""

    def __init__(self, organization_id: str, workflow_permanent_id: str, current_count: int, max_allowed: int):
        self.organization_id = organization_id
        self.workflow_permanent_id = workflow_permanent_id
        self.current_count = current_count
        self.max_allowed = max_allowed
        super().__init__(f"Schedule limit {max_allowed} reached (current: {current_count})")


register_passthrough_exception(ScheduleLimitExceededError)


class SchedulesMixin:
    """Database operations for workflow schedules."""

    Session: _SessionFactory
    engine: AsyncEngine
    debug_enabled: bool
    _sqlite_schedule_lock: asyncio.Lock | None

    @db_operation("create_workflow_schedule")
    async def create_workflow_schedule(
        self,
        organization_id: str,
        workflow_permanent_id: str,
        cron_expression: str,
        timezone: str,
        enabled: bool,
        parameters: dict[str, Any] | None = None,
        temporal_schedule_id: str | None = None,
        name: str | None = None,
        description: str | None = None,
    ) -> WorkflowSchedule:
        async with self.Session() as session:
            workflow_schedule = WorkflowScheduleModel(
                organization_id=organization_id,
                workflow_permanent_id=workflow_permanent_id,
                cron_expression=cron_expression,
                timezone=timezone,
                enabled=enabled,
                parameters=parameters,
                temporal_schedule_id=temporal_schedule_id,
                name=name,
                description=description,
            )
            session.add(workflow_schedule)
            await session.commit()
            await session.refresh(workflow_schedule)
            return convert_to_workflow_schedule(workflow_schedule, self.debug_enabled)

    @db_operation("create_workflow_schedule_with_limit")
    async def create_workflow_schedule_with_limit(
        self,
        organization_id: str,
        workflow_permanent_id: str,
        max_schedules: int | None,
        cron_expression: str,
        timezone: str,
        enabled: bool,
        parameters: dict[str, Any] | None = None,
        name: str | None = None,
        description: str | None = None,
    ) -> tuple[WorkflowSchedule, int]:
        """Create a schedule atomically with limit enforcement.

        On PostgreSQL, uses an advisory lock to serialize concurrent creates for
        the same workflow, preventing TOCTOU races on the schedule count.

        On SQLite, uses an asyncio.Lock (set on AgentDB.__init__) since SQLite
        is single-writer and has no advisory lock support.

        Returns (created_schedule, count_before_insert).
        Raises ScheduleLimitExceededError if count >= max_schedules.
        """
        # SQLite: serialize via Python lock (no advisory locks available).
        # The lock is held across the count-check + insert to prevent TOCTOU.
        sqlite_lock = getattr(self, "_sqlite_schedule_lock", None)
        if sqlite_lock is not None:
            async with sqlite_lock:
                return await self._create_schedule_with_limit_inner(
                    organization_id,
                    workflow_permanent_id,
                    max_schedules,
                    cron_expression,
                    timezone,
                    enabled,
                    parameters,
                    name,
                    description,
                    use_advisory_lock=False,
                )
        return await self._create_schedule_with_limit_inner(
            organization_id,
            workflow_permanent_id,
            max_schedules,
            cron_expression,
            timezone,
            enabled,
            parameters,
            name,
            description,
            use_advisory_lock=True,
        )

    # Intentionally not decorated with @db_operation — errors are caught by the
    # outer create_workflow_schedule_with_limit which owns the operation name.
    async def _create_schedule_with_limit_inner(
        self,
        organization_id: str,
        workflow_permanent_id: str,
        max_schedules: int | None,
        cron_expression: str,
        timezone: str,
        enabled: bool,
        parameters: dict[str, Any] | None,
        name: str | None,
        description: str | None,
        *,
        use_advisory_lock: bool,
    ) -> tuple[WorkflowSchedule, int]:
        async with self.Session() as session:
            if use_advisory_lock:
                lock_key = f"schedule:{organization_id}:{workflow_permanent_id}"
                await session.execute(
                    text("SELECT pg_advisory_xact_lock(hashtext(:key))"),
                    {"key": lock_key},
                )

            count = (
                await session.execute(
                    select(func.count()).where(
                        WorkflowScheduleModel.organization_id == organization_id,
                        WorkflowScheduleModel.workflow_permanent_id == workflow_permanent_id,
                        WorkflowScheduleModel.deleted_at.is_(None),
                    )
                )
            ).scalar_one()

            if max_schedules is not None and count >= max_schedules:
                raise ScheduleLimitExceededError(
                    organization_id=organization_id,
                    workflow_permanent_id=workflow_permanent_id,
                    current_count=count,
                    max_allowed=max_schedules,
                )

            workflow_schedule = WorkflowScheduleModel(
                organization_id=organization_id,
                workflow_permanent_id=workflow_permanent_id,
                cron_expression=cron_expression,
                timezone=timezone,
                enabled=enabled,
                parameters=parameters,
                name=name,
                description=description,
            )
            session.add(workflow_schedule)
            await session.commit()
            await session.refresh(workflow_schedule)
            return convert_to_workflow_schedule(workflow_schedule, self.debug_enabled), count

    @db_operation("set_temporal_schedule_id")
    async def set_temporal_schedule_id(
        self,
        workflow_schedule_id: str,
        organization_id: str,
        temporal_schedule_id: str,
    ) -> WorkflowSchedule | None:
        async with self.Session() as session:
            workflow_schedule = (
                await session.scalars(
                    select(WorkflowScheduleModel).filter_by(
                        workflow_schedule_id=workflow_schedule_id,
                        organization_id=organization_id,
                        deleted_at=None,
                    )
                )
            ).first()

            if not workflow_schedule:
                return None

            workflow_schedule.temporal_schedule_id = temporal_schedule_id
            workflow_schedule.modified_at = datetime.utcnow()
            await session.commit()
            await session.refresh(workflow_schedule)
            return convert_to_workflow_schedule(workflow_schedule, self.debug_enabled)

    @db_operation("update_workflow_schedule")
    async def update_workflow_schedule(
        self,
        workflow_schedule_id: str,
        organization_id: str,
        cron_expression: str,
        timezone: str,
        enabled: bool,
        parameters: dict[str, Any] | None = None,
        temporal_schedule_id: str | None | object = _UNSET,
        name: str | None | object = _UNSET,
        description: str | None | object = _UNSET,
    ) -> WorkflowSchedule | None:
        async with self.Session() as session:
            workflow_schedule = (
                await session.scalars(
                    select(WorkflowScheduleModel).filter_by(
                        workflow_schedule_id=workflow_schedule_id,
                        organization_id=organization_id,
                        deleted_at=None,
                    )
                )
            ).first()

            if not workflow_schedule:
                return None

            workflow_schedule.cron_expression = cron_expression
            workflow_schedule.timezone = timezone
            workflow_schedule.enabled = enabled
            workflow_schedule.parameters = parameters
            if temporal_schedule_id is not _UNSET:
                workflow_schedule.temporal_schedule_id = temporal_schedule_id
            if name is not _UNSET:
                workflow_schedule.name = name
            if description is not _UNSET:
                workflow_schedule.description = description
            workflow_schedule.modified_at = datetime.utcnow()
            await session.commit()
            await session.refresh(workflow_schedule)
            return convert_to_workflow_schedule(workflow_schedule, self.debug_enabled)

    @db_operation("get_workflow_schedule_by_id")
    async def get_workflow_schedule_by_id(
        self,
        workflow_schedule_id: str,
        organization_id: str,
    ) -> WorkflowSchedule | None:
        async with self.Session() as session:
            workflow_schedule = (
                await session.scalars(
                    select(WorkflowScheduleModel).filter_by(
                        workflow_schedule_id=workflow_schedule_id,
                        organization_id=organization_id,
                        deleted_at=None,
                    )
                )
            ).first()
            if not workflow_schedule:
                return None
            return convert_to_workflow_schedule(workflow_schedule, self.debug_enabled)

    @db_operation("get_workflow_schedules")
    async def get_workflow_schedules(
        self,
        workflow_permanent_id: str,
        organization_id: str,
    ) -> list[WorkflowSchedule]:
        async with self.Session() as session:
            rows = (
                await session.scalars(
                    select(WorkflowScheduleModel).filter_by(
                        workflow_permanent_id=workflow_permanent_id,
                        organization_id=organization_id,
                        deleted_at=None,
                    )
                )
            ).all()
            return [convert_to_workflow_schedule(r, self.debug_enabled) for r in rows]

    @db_operation("get_all_enabled_schedules")
    async def get_all_enabled_schedules(
        self,
        organization_id: str | None = None,
    ) -> list[WorkflowSchedule]:
        """Fetch all enabled, non-deleted schedules, optionally filtered by org."""
        async with self.Session() as session:
            stmt = select(WorkflowScheduleModel).where(
                WorkflowScheduleModel.enabled.is_(True),
                WorkflowScheduleModel.deleted_at.is_(None),
            )
            if organization_id:
                stmt = stmt.where(WorkflowScheduleModel.organization_id == organization_id)
            rows = (await session.scalars(stmt)).all()
            return [convert_to_workflow_schedule(r, self.debug_enabled) for r in rows]

    @db_operation("has_schedule_fired_since")
    async def has_schedule_fired_since(
        self,
        workflow_schedule_id: str,
        since: datetime,
    ) -> bool:
        """Check if a workflow_run exists for the given schedule since a timestamp."""
        from sqlalchemy import exists as sa_exists

        async with self.Session() as session:
            row = (
                await session.execute(
                    select(
                        sa_exists().where(
                            WorkflowRunModel.workflow_schedule_id == workflow_schedule_id,
                            WorkflowRunModel.created_at >= since,
                        )
                    )
                )
            ).scalar()
            return bool(row)

    @db_operation("update_workflow_schedule_enabled")
    async def update_workflow_schedule_enabled(
        self,
        workflow_schedule_id: str,
        organization_id: str,
        enabled: bool,
    ) -> WorkflowSchedule | None:
        async with self.Session() as session:
            workflow_schedule = (
                await session.scalars(
                    select(WorkflowScheduleModel).filter_by(
                        workflow_schedule_id=workflow_schedule_id,
                        organization_id=organization_id,
                        deleted_at=None,
                    )
                )
            ).first()
            if not workflow_schedule:
                return None
            workflow_schedule.enabled = enabled
            workflow_schedule.modified_at = datetime.utcnow()
            await session.commit()
            await session.refresh(workflow_schedule)
            return convert_to_workflow_schedule(workflow_schedule, self.debug_enabled)

    @db_operation("delete_workflow_schedule")
    async def delete_workflow_schedule(
        self,
        workflow_schedule_id: str,
        organization_id: str,
    ) -> WorkflowSchedule | None:
        async with self.Session() as session:
            workflow_schedule = (
                await session.scalars(
                    select(WorkflowScheduleModel).filter_by(
                        workflow_schedule_id=workflow_schedule_id,
                        organization_id=organization_id,
                        deleted_at=None,
                    )
                )
            ).first()
            if not workflow_schedule:
                return None

            workflow_schedule.deleted_at = datetime.utcnow()
            workflow_schedule.modified_at = datetime.utcnow()
            await session.commit()
            await session.refresh(workflow_schedule)
            return convert_to_workflow_schedule(workflow_schedule, self.debug_enabled)

    @db_operation("restore_workflow_schedule")
    async def restore_workflow_schedule(
        self,
        workflow_schedule_id: str,
        organization_id: str,
    ) -> WorkflowSchedule | None:
        async with self.Session() as session:
            workflow_schedule = (
                await session.scalars(
                    select(WorkflowScheduleModel)
                    .filter_by(
                        workflow_schedule_id=workflow_schedule_id,
                        organization_id=organization_id,
                    )
                    .filter(WorkflowScheduleModel.deleted_at.isnot(None))
                )
            ).first()
            if not workflow_schedule:
                return None

            workflow_schedule.deleted_at = None
            workflow_schedule.modified_at = datetime.utcnow()
            await session.commit()
            await session.refresh(workflow_schedule)
            return convert_to_workflow_schedule(workflow_schedule, self.debug_enabled)

    @db_operation("count_workflow_schedules")
    async def count_workflow_schedules(
        self,
        organization_id: str,
        workflow_permanent_id: str,
    ) -> int:
        async with self.Session() as session:
            result = await session.execute(
                select(func.count()).where(
                    WorkflowScheduleModel.organization_id == organization_id,
                    WorkflowScheduleModel.workflow_permanent_id == workflow_permanent_id,
                    WorkflowScheduleModel.deleted_at.is_(None),
                )
            )
            return result.scalar_one()

    @db_operation("list_organization_schedules")
    async def list_organization_schedules(
        self,
        organization_id: str,
        page: int = 1,
        page_size: int = 10,
        enabled_filter: bool | None = None,
        search: str | None = None,
    ) -> tuple[list[OrganizationScheduleItem], int]:
        """
        List all schedules for an organization, joined with workflow titles.
        Returns (schedules, total_count).
        """
        if page < 1:
            raise ValueError(f"Page must be greater than 0, got {page}")
        db_page = page - 1
        async with self.Session() as session:
            # Subquery to get the latest version title per workflow_permanent_id
            latest_version_sq = (
                select(
                    WorkflowModel.workflow_permanent_id,
                    func.max(WorkflowModel.version).label("max_version"),
                )
                .where(WorkflowModel.organization_id == organization_id)
                .where(WorkflowModel.deleted_at.is_(None))
                .group_by(WorkflowModel.workflow_permanent_id)
                .subquery()
            )

            workflow_title_sq = (
                select(
                    WorkflowModel.workflow_permanent_id,
                    WorkflowModel.title,
                )
                .join(
                    latest_version_sq,
                    (WorkflowModel.workflow_permanent_id == latest_version_sq.c.workflow_permanent_id)
                    & (WorkflowModel.version == latest_version_sq.c.max_version),
                )
                .subquery()
            )

            # Base query: schedules joined with workflow titles
            base_filter = (
                select(WorkflowScheduleModel, workflow_title_sq.c.title.label("workflow_title"))
                .outerjoin(
                    workflow_title_sq,
                    WorkflowScheduleModel.workflow_permanent_id == workflow_title_sq.c.workflow_permanent_id,
                )
                .where(WorkflowScheduleModel.organization_id == organization_id)
                .where(WorkflowScheduleModel.deleted_at.is_(None))
            )

            if enabled_filter is not None:
                base_filter = base_filter.where(WorkflowScheduleModel.enabled == enabled_filter)

            if search:
                base_filter = base_filter.where(
                    or_(
                        workflow_title_sq.c.title.icontains(search, autoescape=True),
                        WorkflowScheduleModel.name.icontains(search, autoescape=True),
                    )
                )

            # Count query
            count_query = select(func.count()).select_from(base_filter.subquery())
            total_count = (await session.execute(count_query)).scalar_one()

            # Data query with pagination
            data_query = (
                base_filter.order_by(WorkflowScheduleModel.created_at.desc())
                .limit(page_size)
                .offset(db_page * page_size)
            )
            rows = (await session.execute(data_query)).all()

            # Materialize row data while session is open
            raw_schedules = []
            for row in rows:
                schedule_model = row[0]
                raw_schedules.append(
                    (
                        schedule_model.workflow_schedule_id,
                        schedule_model.organization_id,
                        schedule_model.workflow_permanent_id,
                        row[1] or "Untitled Workflow",
                        schedule_model.cron_expression,
                        schedule_model.timezone,
                        schedule_model.enabled,
                        schedule_model.parameters,
                        schedule_model.name,
                        schedule_model.description,
                        schedule_model.created_at,
                        schedule_model.modified_at,
                    )
                )

        # Compute next_run outside session scope (pure CPU, no DB needed)
        schedules: list[OrganizationScheduleItem] = []
        for (
            ws_id,
            org_id,
            wpid,
            title,
            cron_expr,
            tz,
            enabled,
            params,
            name,
            description,
            created,
            modified,
        ) in raw_schedules:
            next_run = None
            if enabled:
                try:
                    next_run = compute_next_run(cron_expr, tz)
                except Exception:
                    LOG.warning(
                        "Failed to compute next_run for schedule",
                        workflow_schedule_id=ws_id,
                        exc_info=True,
                    )

            schedules.append(
                OrganizationScheduleItem(
                    workflow_schedule_id=ws_id,
                    organization_id=org_id,
                    workflow_permanent_id=wpid,
                    workflow_title=title,
                    cron_expression=cron_expr,
                    timezone=tz,
                    enabled=enabled,
                    parameters=params,
                    name=name,
                    description=description,
                    next_run=next_run,
                    created_at=created,
                    modified_at=modified,
                )
            )

        return schedules, total_count

    @db_operation("soft_delete_workflow_and_schedules_by_permanent_id")
    async def soft_delete_workflow_and_schedules_by_permanent_id(
        self,
        workflow_permanent_id: str,
        organization_id: str | None = None,
    ) -> list[str]:
        """Soft-delete a workflow and its active schedules in a single DB transaction."""
        async with self.Session() as session:
            select_query = (
                select(WorkflowScheduleModel.workflow_schedule_id)
                .where(WorkflowScheduleModel.workflow_permanent_id == workflow_permanent_id)
                .where(WorkflowScheduleModel.deleted_at.is_(None))
            )
            if organization_id is not None:
                select_query = select_query.where(WorkflowScheduleModel.organization_id == organization_id)
            result = await session.execute(select_query)
            schedule_ids = list(result.scalars().all())

            deleted_at = datetime.utcnow()
            if schedule_ids:
                update_schedules_query = (
                    update(WorkflowScheduleModel)
                    .where(WorkflowScheduleModel.workflow_schedule_id.in_(schedule_ids))
                    .values(deleted_at=deleted_at)
                )
                await session.execute(update_schedules_query)

            update_workflow_query = (
                update(WorkflowModel)
                .where(WorkflowModel.workflow_permanent_id == workflow_permanent_id)
                .where(WorkflowModel.deleted_at.is_(None))
            )
            if organization_id is not None:
                update_workflow_query = update_workflow_query.filter_by(organization_id=organization_id)
            await session.execute(update_workflow_query.values(deleted_at=deleted_at))
            await session.commit()
            return schedule_ids

    @db_operation("soft_delete_orphaned_schedules")
    async def soft_delete_orphaned_schedules(self, limit: int = 500) -> list[tuple[str, str]]:
        """Soft-delete orphaned schedules and return their identities.

        Uses a single UPDATE ... RETURNING statement so orphan detection and
        soft-deletion happen atomically in one DB round-trip.
        """
        async with self.Session() as session:
            active_workflow_exists = (
                select(WorkflowModel.workflow_permanent_id)
                .where(WorkflowModel.workflow_permanent_id == WorkflowScheduleModel.workflow_permanent_id)
                .where(WorkflowModel.deleted_at.is_(None))
                .correlate(WorkflowScheduleModel)
                .exists()
            )
            orphaned_schedules = (
                select(
                    WorkflowScheduleModel.workflow_schedule_id.label("workflow_schedule_id"),
                    WorkflowScheduleModel.workflow_permanent_id.label("workflow_permanent_id"),
                )
                .where(WorkflowScheduleModel.deleted_at.is_(None))
                .where(~active_workflow_exists)
                .limit(limit)
                .cte("orphaned_schedules")
            )
            update_query = (
                update(WorkflowScheduleModel)
                .where(
                    WorkflowScheduleModel.workflow_schedule_id.in_(select(orphaned_schedules.c.workflow_schedule_id))
                )
                .where(WorkflowScheduleModel.deleted_at.is_(None))
                .values(deleted_at=datetime.utcnow())
                .returning(
                    WorkflowScheduleModel.workflow_schedule_id,
                    WorkflowScheduleModel.workflow_permanent_id,
                )
            )
            result = await session.execute(update_query)
            await session.commit()
            return [(row[0], row[1]) for row in result.all()]
