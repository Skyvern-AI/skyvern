"""Workflow-tag write/read path. Named ``tags`` (not ``workflow_tags``) so run-tag events can land here later."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

import structlog
from sqlalchemy import Exists, and_, exists, func, or_, select, text, update
from sqlalchemy.dialects import postgresql, sqlite
from sqlalchemy.ext.asyncio import AsyncSession

from skyvern.forge.sdk.db._error_handling import db_operation, register_passthrough_exception
from skyvern.forge.sdk.db.base_repository import BaseRepository
from skyvern.forge.sdk.db.models import (
    TagKeyModel,
    TagValueModel,
    WorkflowModel,
    WorkflowRunModel,
    WorkflowRunTagEventModel,
    WorkflowTagEventModel,
)
from skyvern.forge.sdk.workflow.models.tags import CallerType, TagEventType, TagSource, TagWriteContext
from skyvern.forge.sdk.workflow.models.validators import (
    RUN_METADATA_MAX_KEYS,
    is_reserved_tag_key,
    normalize_optional_system_tag_key,
    normalize_tag_value,
    random_tag_color,
)

LOG = structlog.get_logger()

# Aliased to RUN_METADATA_MAX_KEYS so tag and run_metadata caps stay coupled.
MAX_TAGS_PER_WORKFLOW = RUN_METADATA_MAX_KEYS
MAX_TAGS_PER_RUN = RUN_METADATA_MAX_KEYS
MAX_SYSTEM_TAGS_PER_WORKFLOW = RUN_METADATA_MAX_KEYS
MAX_SYSTEM_TAGS_PER_RUN = RUN_METADATA_MAX_KEYS
SYSTEM_TAG_CALLER_ID = "system"

# Cap on distinct values surfaced per grouped key in get_run_tag_suggestions, so a
# single high-cardinality key (one distinct value per run) can't fill the whole limit
# and starve keys that sort after it. Standalone labels (key is None) stay uncapped.
SUGGESTIONS_VALUES_PER_KEY = 3


class TagCountLimitExceeded(ValueError):
    """Raised when an apply would push a workflow over MAX_TAGS_PER_WORKFLOW."""


class TagValueRenameCollision(ValueError):
    """Raised when renaming ``(key, old)`` to ``(key, new)`` and ``(key, new)``
    already exists active org-wide. v1 rejects rather than merging."""


class TagValueAlreadyExists(ValueError):
    """Raised when registering a ``(key, value)`` that is already registered active."""


class RunTagWorkflowRunMismatch(ValueError):
    """Raised when a run-tag write targets a workflow run outside the supplied org."""


# Cap breaches and rename collisions are user input, not infra failures — log as
# BusinessLogicError (WARN), not UnexpectedError (ERROR).
register_passthrough_exception(TagCountLimitExceeded)
register_passthrough_exception(TagValueRenameCollision)
register_passthrough_exception(TagValueAlreadyExists)
register_passthrough_exception(RunTagWorkflowRunMismatch)


@dataclass(frozen=True)
class TagValueRenameResult:
    """Outcome of a successful grouped-label rename: the new ``(key, value)``,
    its carried-over color, and how many workflows were re-tagged."""

    key: str
    value: str
    color: str
    renamed_workflow_count: int


@dataclass(frozen=True)
class TagDeleteCascadeResult:
    """Exact rows superseded by a registry delete cascade."""

    removed_from_workflow_count: int
    removed_from_run_count: int

    @property
    def removed_count(self) -> int:
        return self.removed_from_workflow_count + self.removed_from_run_count


@dataclass(frozen=True)
class TagChange:
    """One concrete state change derived from a caller's set/delete request.
    ``key`` is None for a standalone label (identified by its value)."""

    key: str | None
    new_value: str | None
    event_type: TagEventType
    superseded_event_id: str | None


def _normalize_required_system_tag_key(key: object) -> str:
    normalized = normalize_optional_system_tag_key(key)
    if normalized is None:
        raise ValueError("system tag keys must be non-empty")
    return normalized


def _normalize_system_tag_sets(sets: dict[str, str]) -> dict[str, str]:
    normalized: dict[str, str] = {}
    for key, value in sets.items():
        normalized_key = _normalize_required_system_tag_key(key)
        normalized_value = normalize_tag_value(value)
        if normalized_value == "*":
            raise ValueError("system tag values must not be exactly '*'")
        normalized[normalized_key] = normalized_value
    return normalized


def _normalize_system_tag_deletes(deletes: set[str]) -> set[str]:
    return {_normalize_required_system_tag_key(key) for key in deletes}


class TagsRepository(BaseRepository):
    """Database operations for workflow tag events and the tag-key registry."""

    @db_operation("apply_tag_changes")
    async def apply_tag_changes(
        self,
        workflow_permanent_id: str,
        organization_id: str,
        sets: dict[str, str],
        deletes: set[str],
        context: TagWriteContext,
        label_sets: list[str] | None = None,
        label_deletes: list[str] | None = None,
        colors: dict[str, str] | None = None,
    ) -> list[TagChange]:
        """Atomically apply SET/DELETE tag events. Grouped tags via ``sets``/``deletes``
        (keys), standalone labels via ``label_sets``/``label_deletes``; set wins over delete.
        ``colors`` maps a grouped tag's key to a palette color for the value being set;
        a SET key absent from ``colors`` keeps its existing color or gets a random one."""
        return await self._apply_tag_changes(
            event_model=WorkflowTagEventModel,
            entity_id_name="workflow_permanent_id",
            entity_id=workflow_permanent_id,
            entity_label="workflow",
            organization_id=organization_id,
            sets=sets,
            deletes=deletes,
            context=context,
            label_sets=label_sets,
            label_deletes=label_deletes,
            colors=colors,
            max_tags=MAX_TAGS_PER_WORKFLOW,
            max_system_tags=MAX_SYSTEM_TAGS_PER_WORKFLOW,
        )

    @db_operation("apply_run_tag_changes")
    async def apply_run_tag_changes(
        self,
        workflow_run_id: str,
        organization_id: str,
        sets: dict[str, str],
        deletes: set[str],
        context: TagWriteContext,
        label_sets: list[str] | None = None,
        label_deletes: list[str] | None = None,
        colors: dict[str, str] | None = None,
    ) -> list[TagChange]:
        """Atomically apply SET/DELETE tag events to a workflow run."""
        return await self._apply_tag_changes(
            event_model=WorkflowRunTagEventModel,
            entity_id_name="workflow_run_id",
            entity_id=workflow_run_id,
            entity_label="workflow run",
            organization_id=organization_id,
            sets=sets,
            deletes=deletes,
            context=context,
            label_sets=label_sets,
            label_deletes=label_deletes,
            colors=colors,
            max_tags=MAX_TAGS_PER_RUN,
            max_system_tags=MAX_SYSTEM_TAGS_PER_RUN,
        )

    @db_operation("apply_system_run_tag_changes")
    async def apply_system_run_tag_changes(
        self,
        workflow_run_id: str,
        organization_id: str,
        sets: dict[str, str],
        caller_id: str = SYSTEM_TAG_CALLER_ID,
        deletes: set[str] | None = None,
        set_at: datetime | None = None,
    ) -> list[TagChange]:
        """Write Skyvern-owned run tags in the reserved namespace.

        Public API schemas reject ``skyvern.*`` keys. Internal writers should use
        this helper so provenance is always ``source=system`` and
        ``caller_type=system``.
        """
        normalized_sets = _normalize_system_tag_sets(sets)
        normalized_deletes = _normalize_system_tag_deletes(deletes or set())
        context = TagWriteContext(
            caller_id=caller_id,
            source=TagSource.SYSTEM,
            caller_type=CallerType.SYSTEM,
            set_at=set_at,
        )
        return await self._apply_tag_changes(
            event_model=WorkflowRunTagEventModel,
            entity_id_name="workflow_run_id",
            entity_id=workflow_run_id,
            entity_label="workflow run",
            organization_id=organization_id,
            sets=normalized_sets,
            deletes=normalized_deletes,
            context=context,
            label_sets=None,
            label_deletes=None,
            colors=None,
            max_tags=MAX_TAGS_PER_RUN,
            max_system_tags=MAX_SYSTEM_TAGS_PER_RUN,
        )

    async def _apply_tag_changes(
        self,
        *,
        event_model: type[WorkflowTagEventModel] | type[WorkflowRunTagEventModel],
        entity_id_name: str,
        entity_id: str,
        entity_label: str,
        organization_id: str,
        sets: dict[str, str],
        deletes: set[str],
        context: TagWriteContext,
        label_sets: list[str] | None,
        label_deletes: list[str] | None,
        colors: dict[str, str] | None,
        max_tags: int,
        max_system_tags: int,
    ) -> list[TagChange]:
        label_sets = label_sets or []
        label_deletes = label_deletes or []
        colors = colors or {}
        label_set_values = set(label_sets)
        effective_deletes = {k for k in deletes if k not in sets}
        effective_label_deletes = {v for v in label_deletes if v not in label_set_values}
        has_changes = bool(sets or effective_deletes or label_set_values or effective_label_deletes)
        reserved_keys = {key for key in set(sets.keys()) | effective_deletes if is_reserved_tag_key(key)}
        if reserved_keys and context.source != TagSource.SYSTEM:
            raise ValueError("reserved skyvern.* tag keys can only be written with system provenance")
        if event_model is not WorkflowRunTagEventModel and not has_changes:
            return []

        async with self.Session() as session:
            if event_model is WorkflowRunTagEventModel:
                await self._validate_workflow_run_org(
                    session,
                    workflow_run_id=entity_id,
                    organization_id=organization_id,
                )
            if not has_changes:
                return []

            now = context.set_at or datetime.now(timezone.utc)

            active_rows = await self._get_active_set_rows_for_entity(
                session,
                event_model=event_model,
                entity_id_name=entity_id_name,
                entity_id=entity_id,
                organization_id=organization_id,
            )
            # Grouped tags keyed by their group; standalone labels keyed by value.
            grouped_current = {row.key: row for row in active_rows if row.key is not None}
            label_current = {row.value: row for row in active_rows if row.key is None}

            # Soft cap: concurrent distinct-identity writers can each pass; the
            # partial UNIQUEs catch only same-identity races. Cap is a UX rule.
            projected_grouped = (set(grouped_current.keys()) | set(sets.keys())) - effective_deletes
            projected_labels = (set(label_current.keys()) | label_set_values) - effective_label_deletes
            projected_system_grouped = {key for key in projected_grouped if is_reserved_tag_key(key)}
            projected_user_grouped = projected_grouped - projected_system_grouped
            projected_user_total = len(projected_user_grouped) + len(projected_labels)
            if projected_user_total > max_tags:
                raise TagCountLimitExceeded(
                    f"{entity_label} {entity_id} would have {projected_user_total} user-writable tags; "
                    f"max is {max_tags}"
                )
            if len(projected_system_grouped) > max_system_tags:
                raise TagCountLimitExceeded(
                    f"{entity_label} {entity_id} would have {len(projected_system_grouped)} system tags; "
                    f"max is {max_system_tags}"
                )

            changes: list[TagChange] = []

            def _add_event(*, key: str | None, value: str | None, event_type: TagEventType) -> None:
                session.add(
                    event_model(
                        **{
                            entity_id_name: entity_id,
                            "organization_id": organization_id,
                            "key": key,
                            "value": value,
                            "event_type": event_type.value,
                            "set_at": now,
                            "set_by": context.caller_id,
                            "source": context.source.value,
                            "caller_type": context.caller_type.value if context.caller_type else None,
                        }
                    )
                )

            for key, new_value in sets.items():
                existing = grouped_current.get(key)
                if existing is not None and existing.value == new_value:
                    continue
                if existing is not None:
                    existing.superseded_at = now
                _add_event(key=key, value=new_value, event_type=TagEventType.SET)
                changes.append(
                    TagChange(
                        key=key,
                        new_value=new_value,
                        event_type=TagEventType.SET,
                        superseded_event_id=existing.tag_event_id if existing else None,
                    )
                )

            # A standalone label's value IS its identity, so an already-present
            # label is a no-op; otherwise it's a fresh insert (never an overwrite).
            for value in label_set_values:
                if value in label_current:
                    continue
                _add_event(key=None, value=value, event_type=TagEventType.SET)
                changes.append(
                    TagChange(key=None, new_value=value, event_type=TagEventType.SET, superseded_event_id=None)
                )

            for key in effective_deletes:
                existing = grouped_current.get(key)
                if existing is None:
                    continue
                existing.superseded_at = now
                _add_event(key=key, value=None, event_type=TagEventType.DELETE)
                changes.append(
                    TagChange(
                        key=key,
                        new_value=None,
                        event_type=TagEventType.DELETE,
                        superseded_event_id=existing.tag_event_id,
                    )
                )

            # Standalone-label DELETE rows carry the value so history records
            # which label was removed (a group-less delete has no key to identify it).
            for value in effective_label_deletes:
                existing = label_current.get(value)
                if existing is None:
                    continue
                existing.superseded_at = now
                _add_event(key=None, value=value, event_type=TagEventType.DELETE)
                changes.append(
                    TagChange(
                        key=None,
                        new_value=None,
                        event_type=TagEventType.DELETE,
                        superseded_event_id=existing.tag_event_id,
                    )
                )

            # Flush UPDATEs-before-INSERTs so the partial UNIQUE sees a
            # consistent state when the new SET row hits it.
            await session.flush()

            # Register a TagKeyModel only for grouped keys with a new SET (standalone
            # labels have no key). ON CONFLICT DO NOTHING swallows the first-use race.
            changed_set_keys = {c.key for c in changes if c.event_type == TagEventType.SET and c.key is not None}
            if changed_set_keys:
                rows = [{"organization_id": organization_id, "key": key} for key in changed_set_keys]
                dialect_name = session.bind.dialect.name if session.bind is not None else "postgresql"
                # postgres/sqlite each need their own insert() for ON CONFLICT; index_where
                # must match the partial unique index or ON CONFLICT inference fails.
                insert = sqlite.insert if dialect_name == "sqlite" else postgresql.insert
                insert_stmt = (
                    insert(TagKeyModel.__table__)
                    .values(rows)
                    .on_conflict_do_nothing(
                        index_elements=["organization_id", "key"],
                        index_where=text("deleted_at IS NULL"),
                    )
                )
                await session.execute(insert_stmt)

            await self._register_tag_value_colors(
                session,
                organization_id=organization_id,
                sets=sets,
                colors=colors,
                now=now,
            )

            await session.commit()
            return changes

    async def _validate_workflow_run_org(
        self,
        session: AsyncSession,
        *,
        workflow_run_id: str,
        organization_id: str,
    ) -> None:
        row = (
            await session.execute(
                select(WorkflowRunModel.workflow_run_id)
                .where(WorkflowRunModel.workflow_run_id == workflow_run_id)
                .where(WorkflowRunModel.organization_id == organization_id)
                .limit(1)
            )
        ).first()
        if row is None:
            raise RunTagWorkflowRunMismatch(
                f"workflow run {workflow_run_id} does not exist in organization {organization_id}"
            )

    async def _register_tag_value_colors(
        self,
        session: AsyncSession,
        *,
        organization_id: str,
        sets: dict[str, str],
        colors: dict[str, str],
        now: datetime,
    ) -> None:
        """Upsert a color for every grouped (key, value) in this SET request, even when the
        tag event is idempotent: an explicit color overrides (DO UPDATE), an unspecified one
        registers only on first use (DO NOTHING keeps the existing). Standalone labels are skipped."""
        if not sets:
            return

        dialect_name = session.bind.dialect.name if session.bind is not None else "postgresql"
        insert = sqlite.insert if dialect_name == "sqlite" else postgresql.insert

        explicit_rows: list[dict[str, str]] = []
        random_rows: list[dict[str, str]] = []
        for key, value in sets.items():
            provided = colors.get(key)
            if provided is not None:
                explicit_rows.append(
                    {"organization_id": organization_id, "key": key, "value": value, "color": provided}
                )
            else:
                random_rows.append(
                    {"organization_id": organization_id, "key": key, "value": value, "color": random_tag_color()}
                )

        if explicit_rows:
            stmt = insert(TagValueModel.__table__).values(explicit_rows)
            stmt = stmt.on_conflict_do_update(
                index_elements=["organization_id", "key", "value"],
                index_where=text("deleted_at IS NULL"),
                set_={"color": stmt.excluded.color, "modified_at": now},
            )
            await session.execute(stmt)

        if random_rows:
            stmt = (
                insert(TagValueModel.__table__)
                .values(random_rows)
                .on_conflict_do_nothing(
                    index_elements=["organization_id", "key", "value"],
                    index_where=text("deleted_at IS NULL"),
                )
            )
            await session.execute(stmt)

    async def _get_active_set_rows_for_entity(
        self,
        session: AsyncSession,
        *,
        event_model: type[WorkflowTagEventModel] | type[WorkflowRunTagEventModel],
        entity_id_name: str,
        entity_id: str,
        organization_id: str,
    ) -> list[WorkflowTagEventModel] | list[WorkflowRunTagEventModel]:
        """Active SET event rows for a workflow or workflow run, including
        grouped tags and standalone labels."""
        filters = [
            event_model.organization_id == organization_id,
            getattr(event_model, entity_id_name) == entity_id,
            event_model.superseded_at.is_(None),
            event_model.event_type == TagEventType.SET.value,
        ]
        deleted_at = getattr(event_model, "deleted_at", None)
        if deleted_at is not None:
            filters.append(deleted_at.is_(None))

        stmt = select(event_model).where(and_(*filters))
        result = await session.execute(stmt)
        return list(result.scalars().all())

    async def _get_active_set_rows(
        self,
        session: AsyncSession,
        *,
        workflow_permanent_id: str,
        organization_id: str,
    ) -> list[WorkflowTagEventModel]:
        rows = await self._get_active_set_rows_for_entity(
            session,
            event_model=WorkflowTagEventModel,
            entity_id_name="workflow_permanent_id",
            entity_id=workflow_permanent_id,
            organization_id=organization_id,
        )
        return list(rows)

    async def _get_active_set_rows_for_run(
        self,
        session: AsyncSession,
        *,
        workflow_run_id: str,
        organization_id: str,
    ) -> list[WorkflowRunTagEventModel]:
        rows = await self._get_active_set_rows_for_entity(
            session,
            event_model=WorkflowRunTagEventModel,
            entity_id_name="workflow_run_id",
            entity_id=workflow_run_id,
            organization_id=organization_id,
        )
        return list(rows)

    @db_operation("get_active_grouped_tags_for_workflow")
    async def get_active_grouped_tags_for_workflow(
        self,
        workflow_permanent_id: str,
        organization_id: str,
    ) -> dict[str, str]:
        """Current {key: value} map of a workflow's grouped tags. Standalone labels
        (no key) are excluded; use ``get_active_tag_events_for_workflow`` for all tags."""
        async with self.Session() as session:
            rows = await self._get_active_set_rows(
                session,
                workflow_permanent_id=workflow_permanent_id,
                organization_id=organization_id,
            )
            # SET events always carry a non-null value, but defend against
            # data drift: skip-and-log instead of crashing the request.
            result: dict[str, str] = {}
            for row in rows:
                if row.key is None:
                    continue
                if row.value is None:
                    LOG.warning(
                        "active SET tag row has null value; skipping",
                        tag_event_id=row.tag_event_id,
                        organization_id=organization_id,
                        workflow_permanent_id=workflow_permanent_id,
                        key=row.key,
                    )
                    continue
                result[row.key] = row.value
            return result

    @db_operation("get_active_grouped_tags_for_run")
    async def get_active_grouped_tags_for_run(
        self,
        workflow_run_id: str,
        organization_id: str,
    ) -> dict[str, str]:
        """Current {key: value} map of a workflow run's grouped tags."""
        async with self.Session() as session:
            rows = await self._get_active_set_rows_for_run(
                session,
                workflow_run_id=workflow_run_id,
                organization_id=organization_id,
            )
            result: dict[str, str] = {}
            for row in rows:
                if row.key is None:
                    continue
                if row.value is None:
                    LOG.warning(
                        "active SET run tag row has null value; skipping",
                        tag_event_id=row.tag_event_id,
                        organization_id=organization_id,
                        workflow_run_id=workflow_run_id,
                        key=row.key,
                    )
                    continue
                result[row.key] = row.value
            return result

    @db_operation("get_active_tag_events_for_workflow")
    async def get_active_tag_events_for_workflow(
        self,
        workflow_permanent_id: str,
        organization_id: str,
    ) -> list[WorkflowTagEventModel]:
        """Active SET rows (grouped + standalone) with full attribution
        (source/set_at/set_by) for per-tag provenance."""
        async with self.Session() as session:
            return await self._get_active_set_rows(
                session,
                workflow_permanent_id=workflow_permanent_id,
                organization_id=organization_id,
            )

    @db_operation("get_active_tag_events_for_run")
    async def get_active_tag_events_for_run(
        self,
        workflow_run_id: str,
        organization_id: str,
    ) -> list[WorkflowRunTagEventModel]:
        """Active SET rows (grouped + standalone) with full run-tag attribution."""
        async with self.Session() as session:
            return await self._get_active_set_rows_for_run(
                session,
                workflow_run_id=workflow_run_id,
                organization_id=organization_id,
            )

    @db_operation("get_tag_event_history")
    async def get_tag_event_history(
        self,
        workflow_permanent_id: str,
        organization_id: str,
        limit: int = 100,
        since: datetime | None = None,
        key: str | None = None,
    ) -> list[WorkflowTagEventModel]:
        """Return tag events newest-first for a workflow. Includes DELETE and superseded SET rows."""
        rows = await self._get_tag_event_history_for_entity(
            event_model=WorkflowTagEventModel,
            entity_id_name="workflow_permanent_id",
            entity_id=workflow_permanent_id,
            organization_id=organization_id,
            limit=limit,
            since=since,
            key=key,
        )
        return list(rows)

    @db_operation("get_run_tag_event_history")
    async def get_run_tag_event_history(
        self,
        workflow_run_id: str,
        organization_id: str,
        limit: int = 100,
        since: datetime | None = None,
        key: str | None = None,
    ) -> list[WorkflowRunTagEventModel]:
        """Return tag events newest-first for a workflow run, with optional key filter."""
        rows = await self._get_tag_event_history_for_entity(
            event_model=WorkflowRunTagEventModel,
            entity_id_name="workflow_run_id",
            entity_id=workflow_run_id,
            organization_id=organization_id,
            limit=limit,
            since=since,
            key=key,
        )
        return list(rows)

    async def _get_tag_event_history_for_entity(
        self,
        *,
        event_model: type[WorkflowTagEventModel] | type[WorkflowRunTagEventModel],
        entity_id_name: str,
        entity_id: str,
        organization_id: str,
        limit: int,
        since: datetime | None,
        key: str | None,
    ) -> list[WorkflowTagEventModel] | list[WorkflowRunTagEventModel]:
        async with self.Session() as session:
            stmt = (
                select(event_model)
                .where(event_model.organization_id == organization_id)
                .where(getattr(event_model, entity_id_name) == entity_id)
            )
            deleted_at = getattr(event_model, "deleted_at", None)
            if deleted_at is not None:
                stmt = stmt.where(deleted_at.is_(None))
            if since is not None:
                stmt = stmt.where(event_model.set_at >= since)
            if key is not None:
                stmt = stmt.where(event_model.key == key)
            stmt = stmt.order_by(event_model.set_at.desc(), event_model.tag_event_id.desc()).limit(limit)
            result = await session.execute(stmt)
            return list(result.scalars().all())

    @db_operation("get_active_tags_for_workflows")
    async def get_active_tags_for_workflows(
        self,
        workflow_permanent_ids: list[str],
        organization_id: str,
    ) -> dict[str, list[tuple[str | None, str]]]:
        """Batch read of current SET tags (grouped + standalone) as a list of
        (key, value) per workflow. The org filter silently drops foreign wpids."""
        return await self._get_active_tags_for_entities(
            event_model=WorkflowTagEventModel,
            entity_id_name="workflow_permanent_id",
            entity_ids=workflow_permanent_ids,
            organization_id=organization_id,
            log_entity_name="workflow_permanent_id",
        )

    @db_operation("get_active_tags_for_runs")
    async def get_active_tags_for_runs(
        self,
        workflow_run_ids: list[str],
        organization_id: str,
    ) -> dict[str, list[tuple[str | None, str]]]:
        """Batch read of current SET tags (grouped + standalone) per workflow run."""
        return await self._get_active_tags_for_entities(
            event_model=WorkflowRunTagEventModel,
            entity_id_name="workflow_run_id",
            entity_ids=workflow_run_ids,
            organization_id=organization_id,
            log_entity_name="workflow_run_id",
        )

    async def _get_active_tags_for_entities(
        self,
        *,
        event_model: type[WorkflowTagEventModel] | type[WorkflowRunTagEventModel],
        entity_id_name: str,
        entity_ids: list[str],
        organization_id: str,
        log_entity_name: str,
    ) -> dict[str, list[tuple[str | None, str]]]:
        if not entity_ids:
            return {}

        filters = [
            event_model.organization_id == organization_id,
            getattr(event_model, entity_id_name).in_(entity_ids),
            event_model.superseded_at.is_(None),
            event_model.event_type == TagEventType.SET.value,
        ]
        deleted_at = getattr(event_model, "deleted_at", None)
        if deleted_at is not None:
            filters.append(deleted_at.is_(None))

        async with self.Session() as session:
            stmt = select(event_model).where(and_(*filters))
            rows = (await session.execute(stmt)).scalars().all()

        result: dict[str, list[tuple[str | None, str]]] = {}
        for row in rows:
            entity_id = getattr(row, entity_id_name)
            if row.value is None:
                LOG.warning(
                    "active SET tag row has null value; skipping",
                    tag_event_id=row.tag_event_id,
                    organization_id=organization_id,
                    **{log_entity_name: entity_id, "key": row.key},
                )
                continue
            result.setdefault(entity_id, []).append((row.key, row.value))
        return result

    @db_operation("list_tag_keys")
    async def list_tag_keys(self, organization_id: str) -> list[TagKeyModel]:
        """Active tag-key registry entries for the org, ordered by key for stable
        autocomplete output."""
        async with self.Session() as session:
            stmt = (
                select(TagKeyModel)
                .where(TagKeyModel.organization_id == organization_id)
                .where(TagKeyModel.deleted_at.is_(None))
                .order_by(TagKeyModel.key.asc())
            )
            result = await session.execute(stmt)
            return list(result.scalars().all())

    @db_operation("list_tag_values")
    async def list_tag_values(self, organization_id: str, key: str | None = None) -> list[TagValueModel]:
        """Active (key, value, color) registry entries for the org, ordered by key
        then value. The frontend joins these onto tags by (key, value) the same way
        it joins descriptions onto keys."""
        async with self.Session() as session:
            stmt = (
                select(TagValueModel)
                .where(TagValueModel.organization_id == organization_id)
                .where(TagValueModel.deleted_at.is_(None))
            )
            if key is not None:
                stmt = stmt.where(TagValueModel.key == key)
            stmt = stmt.order_by(TagValueModel.key.asc(), TagValueModel.value.asc())
            result = await session.execute(stmt)
            return list(result.scalars().all())

    @db_operation("get_run_tag_suggestions")
    async def get_run_tag_suggestions(
        self,
        organization_id: str,
        limit: int = 1000,
        key_prefix: str | None = None,
    ) -> list[tuple[str | None, str | None]]:
        """Distinct (key, value) pairs from active run-tag SET events for the org,
        including reserved ``skyvern.*`` system keys (the registry-backed
        ``list_tag_keys``/``list_tag_values`` never see these, so this is their only
        surfacing path for pickers). Each grouped key contributes at most
        ``SUGGESTIONS_VALUES_PER_KEY`` values so no single high-cardinality key can
        consume the whole ``limit`` and starve keys that sort after it. When
        ``key_prefix`` is provided, filtering happens before ranking and limiting."""
        async with self.Session() as session:
            distinct_pairs_stmt = (
                select(WorkflowRunTagEventModel.key, WorkflowRunTagEventModel.value)
                .where(WorkflowRunTagEventModel.organization_id == organization_id)
                .where(WorkflowRunTagEventModel.superseded_at.is_(None))
                .where(WorkflowRunTagEventModel.event_type == TagEventType.SET.value)
            )
            if key_prefix is not None:
                distinct_pairs_stmt = distinct_pairs_stmt.where(WorkflowRunTagEventModel.key.startswith(key_prefix))
            distinct_pairs = distinct_pairs_stmt.distinct().subquery()
            ranked = select(
                distinct_pairs.c.key,
                distinct_pairs.c.value,
                func.row_number()
                .over(partition_by=distinct_pairs.c.key, order_by=distinct_pairs.c.value.asc())
                .label("rn"),
            ).subquery()
            stmt = (
                select(ranked.c.key, ranked.c.value)
                .where(or_(ranked.c.key.is_(None), ranked.c.rn <= SUGGESTIONS_VALUES_PER_KEY))
                .order_by(ranked.c.key.asc(), ranked.c.value.asc())
                .limit(limit)
            )
            result = await session.execute(stmt)
            return [(key, value) for key, value in result]

    @db_operation("recolor_tag_value")
    async def recolor_tag_value(
        self,
        organization_id: str,
        key: str,
        value: str,
        color: str,
    ) -> TagValueModel | None:
        """Recolor an existing (key, value) registry row. Returns None when the pair
        is not registered for the org (caller should 404). Reserved ``skyvern.*``
        keys are system-managed and always rejected (no system recolor path exists)."""
        if is_reserved_tag_key(key):
            raise ValueError("reserved skyvern.* tag values are system-managed and cannot be recolored")
        async with self.Session() as session:
            stmt = (
                select(TagValueModel)
                .where(TagValueModel.organization_id == organization_id)
                .where(TagValueModel.key == key)
                .where(TagValueModel.value == value)
                .where(TagValueModel.deleted_at.is_(None))
            )
            row = (await session.execute(stmt)).scalar_one_or_none()
            if row is None:
                return None
            row.color = color
            await session.commit()
            await session.refresh(row)
            return row

    @db_operation("register_tag_value")
    async def register_tag_value(
        self,
        organization_id: str,
        key: str,
        value: str,
        color: str | None = None,
    ) -> TagValueModel:
        """Register a grouped label ``(key, value)`` before any workflow uses it
        (the settings-surface create path; applying tags registers implicitly).
        Also registers the key row so the group shows up in pickers. Writes no
        tag events — the label attaches to nothing yet. Raises
        ``TagValueAlreadyExists`` when the pair is already registered active;
        reserved ``skyvern.*`` keys are always rejected (system-managed)."""
        if is_reserved_tag_key(key):
            raise ValueError("reserved skyvern.* tag values are system-managed and cannot be created manually")
        async with self.Session() as session:
            existing = (
                await session.execute(
                    select(TagValueModel).where(
                        and_(
                            TagValueModel.organization_id == organization_id,
                            TagValueModel.key == key,
                            TagValueModel.value == value,
                            TagValueModel.deleted_at.is_(None),
                        )
                    )
                )
            ).scalar_one_or_none()
            if existing is not None:
                raise TagValueAlreadyExists(f"tag value '{key}:{value}' already exists")

            dialect_name = session.bind.dialect.name if session.bind is not None else "postgresql"
            insert = sqlite.insert if dialect_name == "sqlite" else postgresql.insert
            # ON CONFLICT DO NOTHING keeps a concurrent create/apply race benign;
            # the read-back below returns whichever row won.
            await session.execute(
                insert(TagValueModel.__table__)
                .values(organization_id=organization_id, key=key, value=value, color=color or random_tag_color())
                .on_conflict_do_nothing(
                    index_elements=["organization_id", "key", "value"],
                    index_where=text("deleted_at IS NULL"),
                )
            )
            await session.execute(
                insert(TagKeyModel.__table__)
                .values(organization_id=organization_id, key=key)
                .on_conflict_do_nothing(
                    index_elements=["organization_id", "key"],
                    index_where=text("deleted_at IS NULL"),
                )
            )
            await session.commit()

            return (
                await session.execute(
                    select(TagValueModel).where(
                        and_(
                            TagValueModel.organization_id == organization_id,
                            TagValueModel.key == key,
                            TagValueModel.value == value,
                            TagValueModel.deleted_at.is_(None),
                        )
                    )
                )
            ).scalar_one()

    @db_operation("get_tag_key")
    async def get_tag_key(self, organization_id: str, key: str) -> TagKeyModel | None:
        async with self.Session() as session:
            stmt = (
                select(TagKeyModel)
                .where(TagKeyModel.organization_id == organization_id)
                .where(TagKeyModel.key == key)
                .where(TagKeyModel.deleted_at.is_(None))
            )
            return (await session.execute(stmt)).scalar_one_or_none()

    @db_operation("update_tag_key_description")
    async def update_tag_key_description(
        self,
        organization_id: str,
        key: str,
        description: str | None,
    ) -> TagKeyModel | None:
        """Update description on an existing tag-key row. Returns None when the
        key is not registered for the org (caller should 404)."""
        async with self.Session() as session:
            stmt = (
                select(TagKeyModel)
                .where(TagKeyModel.organization_id == organization_id)
                .where(TagKeyModel.key == key)
                .where(TagKeyModel.deleted_at.is_(None))
            )
            row = (await session.execute(stmt)).scalar_one_or_none()
            if row is None:
                return None
            row.description = description
            await session.commit()
            await session.refresh(row)
            return row

    @db_operation("count_active_workflows_per_key")
    async def count_active_workflows_per_key(self, organization_id: str) -> dict[str, int]:
        """Map of tag key -> number of workflows carrying it (one active SET per
        (workflow, key), so a row count suffices). Standalone labels are excluded."""
        async with self.Session() as session:
            stmt = (
                select(WorkflowTagEventModel.key, func.count(WorkflowTagEventModel.tag_event_id))
                .where(
                    and_(
                        WorkflowTagEventModel.organization_id == organization_id,
                        WorkflowTagEventModel.superseded_at.is_(None),
                        WorkflowTagEventModel.event_type == TagEventType.SET.value,
                        WorkflowTagEventModel.deleted_at.is_(None),
                        WorkflowTagEventModel.key.isnot(None),
                    )
                )
                .group_by(WorkflowTagEventModel.key)
            )
            rows = await session.execute(stmt)
            return {key: count for key, count in rows.all()}

    @db_operation("count_active_runs_per_key")
    async def count_active_runs_per_key(self, organization_id: str) -> dict[str, int]:
        """Map of tag key -> number of workflow runs carrying it. Standalone labels are excluded."""
        async with self.Session() as session:
            stmt = (
                select(WorkflowRunTagEventModel.key, func.count(WorkflowRunTagEventModel.tag_event_id))
                .where(
                    and_(
                        WorkflowRunTagEventModel.organization_id == organization_id,
                        WorkflowRunTagEventModel.superseded_at.is_(None),
                        WorkflowRunTagEventModel.event_type == TagEventType.SET.value,
                        WorkflowRunTagEventModel.key.isnot(None),
                    )
                )
                .group_by(WorkflowRunTagEventModel.key)
            )
            rows = await session.execute(stmt)
            return {key: count for key, count in rows.all()}

    @staticmethod
    def _non_deleted_workflow_exists(organization_id: str) -> Exists:
        """Correlated EXISTS: the tag event's workflow still has a live version.
        Soft-deleting a workflow sets ``deleted_at`` on every version row, but its
        tag events linger; without this filter a label attached only to deleted
        workflows would inflate the workflow counts."""
        return exists().where(
            and_(
                WorkflowModel.workflow_permanent_id == WorkflowTagEventModel.workflow_permanent_id,
                WorkflowModel.organization_id == organization_id,
                WorkflowModel.deleted_at.is_(None),
            )
        )

    @db_operation("count_active_workflows_per_value")
    async def count_active_workflows_per_value(self, organization_id: str) -> dict[tuple[str, str], int]:
        """Map of grouped ``(key, value)`` -> number of non-deleted workflows carrying
        it. Powers the per-label delete blast-radius warning. Standalone labels (no
        key) are excluded; covered by the existing org/key/value active partial index."""
        async with self.Session() as session:
            stmt = (
                select(
                    WorkflowTagEventModel.key,
                    WorkflowTagEventModel.value,
                    func.count(func.distinct(WorkflowTagEventModel.workflow_permanent_id)),
                )
                .where(
                    and_(
                        WorkflowTagEventModel.organization_id == organization_id,
                        WorkflowTagEventModel.superseded_at.is_(None),
                        WorkflowTagEventModel.event_type == TagEventType.SET.value,
                        WorkflowTagEventModel.deleted_at.is_(None),
                        WorkflowTagEventModel.key.isnot(None),
                        self._non_deleted_workflow_exists(organization_id),
                    )
                )
                .group_by(WorkflowTagEventModel.key, WorkflowTagEventModel.value)
            )
            rows = await session.execute(stmt)
            return {(key, value): count for key, value, count in rows.all()}

    @db_operation("count_active_runs_per_value")
    async def count_active_runs_per_value(self, organization_id: str) -> dict[tuple[str, str], int]:
        """Map of grouped ``(key, value)`` -> number of workflow runs carrying it."""
        async with self.Session() as session:
            stmt = (
                select(
                    WorkflowRunTagEventModel.key,
                    WorkflowRunTagEventModel.value,
                    func.count(func.distinct(WorkflowRunTagEventModel.workflow_run_id)),
                )
                .where(
                    and_(
                        WorkflowRunTagEventModel.organization_id == organization_id,
                        WorkflowRunTagEventModel.superseded_at.is_(None),
                        WorkflowRunTagEventModel.event_type == TagEventType.SET.value,
                        WorkflowRunTagEventModel.key.isnot(None),
                    )
                )
                .group_by(WorkflowRunTagEventModel.key, WorkflowRunTagEventModel.value)
            )
            rows = await session.execute(stmt)
            return {(key, value): count for key, value, count in rows.all()}

    @db_operation("count_active_workflows_for_value")
    async def count_active_workflows_for_value(self, organization_id: str, key: str, value: str) -> int:
        """Number of non-deleted workflows with an active SET on grouped ``(key, value)``.
        Targeted single-pair count for the recolor/rename response, avoiding the full-org
        GROUP BY that ``count_active_workflows_per_value`` runs for the list endpoint."""
        async with self.Session() as session:
            stmt = select(func.count(func.distinct(WorkflowTagEventModel.workflow_permanent_id))).where(
                and_(
                    WorkflowTagEventModel.organization_id == organization_id,
                    WorkflowTagEventModel.key == key,
                    WorkflowTagEventModel.value == value,
                    WorkflowTagEventModel.superseded_at.is_(None),
                    WorkflowTagEventModel.event_type == TagEventType.SET.value,
                    WorkflowTagEventModel.deleted_at.is_(None),
                    self._non_deleted_workflow_exists(organization_id),
                )
            )
            return (await session.execute(stmt)).scalar_one()

    @db_operation("count_active_workflows_for_key")
    async def count_active_workflows_for_key(self, organization_id: str, key: str) -> int:
        """Number of non-deleted workflows with an active SET on grouped ``key``."""
        async with self.Session() as session:
            stmt = select(func.count(func.distinct(WorkflowTagEventModel.workflow_permanent_id))).where(
                and_(
                    WorkflowTagEventModel.organization_id == organization_id,
                    WorkflowTagEventModel.key == key,
                    WorkflowTagEventModel.superseded_at.is_(None),
                    WorkflowTagEventModel.event_type == TagEventType.SET.value,
                    WorkflowTagEventModel.deleted_at.is_(None),
                    self._non_deleted_workflow_exists(organization_id),
                )
            )
            return (await session.execute(stmt)).scalar_one()

    @db_operation("count_active_runs_for_key")
    async def count_active_runs_for_key(self, organization_id: str, key: str) -> int:
        """Number of workflow runs with an active SET on grouped ``key``."""
        async with self.Session() as session:
            stmt = select(func.count(func.distinct(WorkflowRunTagEventModel.workflow_run_id))).where(
                and_(
                    WorkflowRunTagEventModel.organization_id == organization_id,
                    WorkflowRunTagEventModel.key == key,
                    WorkflowRunTagEventModel.superseded_at.is_(None),
                    WorkflowRunTagEventModel.event_type == TagEventType.SET.value,
                )
            )
            return (await session.execute(stmt)).scalar_one()

    @db_operation("count_active_runs_for_value")
    async def count_active_runs_for_value(self, organization_id: str, key: str, value: str) -> int:
        """Number of workflow runs with an active SET on grouped ``(key, value)``."""
        async with self.Session() as session:
            stmt = select(func.count(func.distinct(WorkflowRunTagEventModel.workflow_run_id))).where(
                and_(
                    WorkflowRunTagEventModel.organization_id == organization_id,
                    WorkflowRunTagEventModel.key == key,
                    WorkflowRunTagEventModel.value == value,
                    WorkflowRunTagEventModel.superseded_at.is_(None),
                    WorkflowRunTagEventModel.event_type == TagEventType.SET.value,
                )
            )
            return (await session.execute(stmt)).scalar_one()

    async def _soft_delete_tag_value_rows(
        self,
        session: AsyncSession,
        *,
        organization_id: str,
        key: str,
        now: datetime,
        value: str | None = None,
    ) -> None:
        """Soft-delete the active ``tag_values`` color rows for a key (all values
        when ``value`` is None, or a single ``(key, value)`` pair otherwise) so
        GET /tag-values stops returning colors for removed labels. Shared by
        ``delete_tag_key``, ``delete_tag_value``, and ``rename_tag_value`` (which
        retires the old value's row) to keep the cascade consistent."""
        stmt = update(TagValueModel).where(
            TagValueModel.organization_id == organization_id,
            TagValueModel.key == key,
            TagValueModel.deleted_at.is_(None),
        )
        if value is not None:
            stmt = stmt.where(TagValueModel.value == value)
        await session.execute(stmt.values(deleted_at=now))

    async def _get_active_grouped_set_rows(
        self,
        session: AsyncSession,
        *,
        event_model: type[WorkflowTagEventModel] | type[WorkflowRunTagEventModel],
        organization_id: str,
        key: str,
        value: str | None = None,
        soft_delete_column: Any | None = None,
    ) -> list[WorkflowTagEventModel] | list[WorkflowRunTagEventModel]:
        filters = [
            event_model.organization_id == organization_id,
            event_model.key == key,
            event_model.superseded_at.is_(None),
            event_model.event_type == TagEventType.SET.value,
        ]
        if value is not None:
            filters.append(event_model.value == value)
        if soft_delete_column is not None:
            filters.append(soft_delete_column.is_(None))

        result = await session.execute(select(event_model).where(and_(*filters)))
        return list(result.scalars().all())

    @staticmethod
    def _supersede_and_add_delete_event(
        session: AsyncSession,
        *,
        event_model: type[WorkflowTagEventModel] | type[WorkflowRunTagEventModel],
        entity_id_name: str,
        existing: WorkflowTagEventModel | WorkflowRunTagEventModel,
        organization_id: str,
        key: str,
        value: str | None,
        context: TagWriteContext,
        now: datetime,
    ) -> None:
        existing.superseded_at = now
        session.add(
            event_model(
                **{
                    entity_id_name: getattr(existing, entity_id_name),
                    "organization_id": organization_id,
                    "key": key,
                    "value": value,
                    "event_type": TagEventType.DELETE.value,
                    "set_at": now,
                    "set_by": context.caller_id,
                    "source": context.source.value,
                    "caller_type": context.caller_type.value if context.caller_type else None,
                }
            )
        )

    @db_operation("delete_tag_key")
    async def delete_tag_key(
        self,
        organization_id: str,
        key: str,
        context: TagWriteContext,
    ) -> TagDeleteCascadeResult | None:
        """Cascade-delete a tag key: write a DELETE event for every workflow that
        currently has it, then soft-delete the key registry row and its value color
        rows (so GET /tag-values stops returning colors for the removed key). Returns exact workflow/run
        counts removed by the transaction, or None when the key is not
        registered (caller should 404). Idempotent: a second call returns None.

        DELETE events don't match the SET-only partial UNIQUE, so superseding the
        SET and inserting the DELETE in one transaction needs no flush ordering.

        Accepted race (mirrors the soft-cap tolerance in apply_tag_changes): a
        concurrent apply of the same key can insert a fresh active SET after this
        method reads the active set, leaving that tag active while the registry
        row is soft-deleted — i.e. a tag present on a workflow but absent from
        /tag-keys. It is not data loss (the tag still resolves on the workflow)
        and self-heals on the next apply (which re-registers the key via the
        deleted_at IS NULL partial unique). Serializing delete-vs-SET would need
        dialect-specific row/advisory locking, which is disproportionate for a
        rare manual admin action.

        Scale note: this loads the key's active SET rows into the session and
        iterates in Python. Fine for realistic key fan-out (a key on at most a
        few hundred workflows) and a one-off admin action; if a key ever spans
        thousands of workflows, switch to a bulk UPDATE (supersede) + batched
        INSERT for the DELETE events to avoid the ORM round-trip.

        Reserved ``skyvern.*`` keys are rejected unless the write carries system
        provenance, so no user-provenance DELETE can land in the system-tag event log."""
        if is_reserved_tag_key(key) and context.source != TagSource.SYSTEM:
            raise ValueError("reserved skyvern.* tag keys can only be modified with system provenance")
        now = datetime.now(timezone.utc)
        async with self.Session() as session:
            key_row = (
                await session.execute(
                    select(TagKeyModel).where(
                        and_(
                            TagKeyModel.organization_id == organization_id,
                            TagKeyModel.key == key,
                            TagKeyModel.deleted_at.is_(None),
                        )
                    )
                )
            ).scalar_one_or_none()
            if key_row is None:
                return None

            active_sets = await self._get_active_grouped_set_rows(
                session,
                event_model=WorkflowTagEventModel,
                organization_id=organization_id,
                key=key,
                soft_delete_column=WorkflowTagEventModel.deleted_at,
            )
            active_run_sets = await self._get_active_grouped_set_rows(
                session,
                event_model=WorkflowRunTagEventModel,
                organization_id=organization_id,
                key=key,
            )

            for existing in active_sets:
                self._supersede_and_add_delete_event(
                    session,
                    event_model=WorkflowTagEventModel,
                    entity_id_name="workflow_permanent_id",
                    existing=existing,
                    organization_id=organization_id,
                    key=key,
                    value=None,
                    context=context,
                    now=now,
                )
            for existing in active_run_sets:
                self._supersede_and_add_delete_event(
                    session,
                    event_model=WorkflowRunTagEventModel,
                    entity_id_name="workflow_run_id",
                    existing=existing,
                    organization_id=organization_id,
                    key=key,
                    value=None,
                    context=context,
                    now=now,
                )

            key_row.deleted_at = now
            await self._soft_delete_tag_value_rows(session, organization_id=organization_id, key=key, now=now)
            await session.commit()
            return TagDeleteCascadeResult(
                removed_from_workflow_count=len(active_sets),
                removed_from_run_count=len(active_run_sets),
            )

    @db_operation("delete_tag_value")
    async def delete_tag_value(
        self,
        organization_id: str,
        key: str,
        value: str,
        context: TagWriteContext,
    ) -> TagDeleteCascadeResult | None:
        """Cascade-delete a single grouped label ``(key, value)``, mirroring
        ``delete_tag_key`` at value granularity: write a DELETE event (carrying the
        value, so history records which label was removed) for every workflow with
        an active SET on it, then soft-delete the ``(key, value)`` color row. Returns exact
        workflow/run counts removed by the transaction, or None when neither a
        registered color row nor an active SET exists (caller should 404). Idempotent:
        a second call returns None; re-applying via a SET re-registers the label.

        DELETE events don't match the SET-only partial UNIQUE, so superseding the SET
        and inserting the DELETE in one transaction needs no flush ordering (same as
        ``delete_tag_key``). The same accepted delete-vs-SET race applies.

        Reserved ``skyvern.*`` keys are rejected unless the write carries system
        provenance, so no user-provenance DELETE can land in the system-tag event log."""
        if is_reserved_tag_key(key) and context.source != TagSource.SYSTEM:
            raise ValueError("reserved skyvern.* tag values can only be modified with system provenance")
        now = datetime.now(timezone.utc)
        async with self.Session() as session:
            value_row = (
                await session.execute(
                    select(TagValueModel).where(
                        and_(
                            TagValueModel.organization_id == organization_id,
                            TagValueModel.key == key,
                            TagValueModel.value == value,
                            TagValueModel.deleted_at.is_(None),
                        )
                    )
                )
            ).scalar_one_or_none()

            active_sets = await self._get_active_grouped_set_rows(
                session,
                event_model=WorkflowTagEventModel,
                organization_id=organization_id,
                key=key,
                value=value,
                soft_delete_column=WorkflowTagEventModel.deleted_at,
            )
            active_run_sets = await self._get_active_grouped_set_rows(
                session,
                event_model=WorkflowRunTagEventModel,
                organization_id=organization_id,
                key=key,
                value=value,
            )

            if value_row is None and not active_sets and not active_run_sets:
                return None

            for existing in active_sets:
                self._supersede_and_add_delete_event(
                    session,
                    event_model=WorkflowTagEventModel,
                    entity_id_name="workflow_permanent_id",
                    existing=existing,
                    organization_id=organization_id,
                    key=key,
                    value=value,
                    context=context,
                    now=now,
                )
            for existing in active_run_sets:
                self._supersede_and_add_delete_event(
                    session,
                    event_model=WorkflowRunTagEventModel,
                    entity_id_name="workflow_run_id",
                    existing=existing,
                    organization_id=organization_id,
                    key=key,
                    value=value,
                    context=context,
                    now=now,
                )

            await self._soft_delete_tag_value_rows(
                session, organization_id=organization_id, key=key, now=now, value=value
            )
            await session.commit()
            return TagDeleteCascadeResult(
                removed_from_workflow_count=len(active_sets),
                removed_from_run_count=len(active_run_sets),
            )

    @db_operation("rename_tag_value")
    async def rename_tag_value(
        self,
        organization_id: str,
        key: str,
        old_value: str,
        new_value: str,
        context: TagWriteContext,
    ) -> TagValueRenameResult | None:
        """Rename a grouped label ``(key, old_value)`` -> ``(key, new_value)`` by
        cascading through the append-only event log (mirrors ``delete_tag_key`` but
        inserts a SET, not a DELETE): for every workflow with an active SET on
        ``(key, old_value)``, supersede it and insert a new SET on ``(key, new_value)``.
        The new color row inherits the old row's color. Historical events keep their
        point-in-time value (append-only invariant preserved). Returns the rename
        result, or None when ``(key, old_value)`` is not present (caller should 404).

        Raises ``TagValueRenameCollision`` when ``(key, new_value)`` already exists
        active org-wide (registered or in use) — v1 rejects rather than merging.

        The new SET shares the ``(org, wpid, key)`` active-SET partial UNIQUE with the
        superseded row, so the supersede UPDATEs are flushed before the new SET INSERTs
        (same ordering ``apply_tag_changes`` relies on).

        Accepted race (mirrors ``delete_tag_key``): the active-set read, the collision
        check, and the cascade are not serialized against a concurrent ``apply_tag_changes``.
        A SET on ``(key, old_value)`` landing after the read is missed (left on the old
        value), and a SET on ``(key, new_value)`` landing after the collision check can
        coexist with the rename — surfacing as a transient IntegrityError on the partial
        UNIQUE (the caller's one retry covers it) or two active SETs that the next apply
        reconciles. Serializing would need dialect-specific row/advisory locking,
        disproportionate for a rare manual admin action.

        Reserved ``skyvern.*`` keys are rejected unless the write carries system
        provenance, so no user-provenance SET can land in the system-tag event log."""
        if is_reserved_tag_key(key) and context.source != TagSource.SYSTEM:
            raise ValueError("reserved skyvern.* tag values can only be modified with system provenance")
        now = datetime.now(timezone.utc)
        async with self.Session() as session:
            old_row = (
                await session.execute(
                    select(TagValueModel).where(
                        and_(
                            TagValueModel.organization_id == organization_id,
                            TagValueModel.key == key,
                            TagValueModel.value == old_value,
                            TagValueModel.deleted_at.is_(None),
                        )
                    )
                )
            ).scalar_one_or_none()

            active_sets = (
                (
                    await session.execute(
                        select(WorkflowTagEventModel).where(
                            and_(
                                WorkflowTagEventModel.organization_id == organization_id,
                                WorkflowTagEventModel.key == key,
                                WorkflowTagEventModel.value == old_value,
                                WorkflowTagEventModel.superseded_at.is_(None),
                                WorkflowTagEventModel.event_type == TagEventType.SET.value,
                                WorkflowTagEventModel.deleted_at.is_(None),
                            )
                        )
                    )
                )
                .scalars()
                .all()
            )
            active_run_sets = await self._get_active_grouped_set_rows(
                session,
                event_model=WorkflowRunTagEventModel,
                organization_id=organization_id,
                key=key,
                value=old_value,
            )

            if old_row is None and not active_sets and not active_run_sets:
                return None

            if await self._grouped_value_active(session, organization_id=organization_id, key=key, value=new_value):
                raise TagValueRenameCollision(
                    f"tag value '{key}:{new_value}' already exists; rename would merge labels (not supported)"
                )

            for existing in active_sets:
                existing.superseded_at = now
            for existing in active_run_sets:
                existing.superseded_at = now
            # Flush supersede UPDATEs before the new SET INSERTs so the active-SET
            # partial UNIQUE on (org, wpid, key) sees a consistent state.
            await session.flush()
            for existing in active_sets:
                session.add(
                    WorkflowTagEventModel(
                        workflow_permanent_id=existing.workflow_permanent_id,
                        organization_id=organization_id,
                        key=key,
                        value=new_value,
                        event_type=TagEventType.SET.value,
                        set_at=now,
                        set_by=context.caller_id,
                        source=context.source.value,
                        caller_type=context.caller_type.value if context.caller_type else None,
                    )
                )
            for existing in active_run_sets:
                session.add(
                    WorkflowRunTagEventModel(
                        workflow_run_id=existing.workflow_run_id,
                        organization_id=organization_id,
                        key=key,
                        value=new_value,
                        event_type=TagEventType.SET.value,
                        set_at=now,
                        set_by=context.caller_id,
                        source=context.source.value,
                        caller_type=context.caller_type.value if context.caller_type else None,
                    )
                )

            carried_color = old_row.color if old_row is not None else random_tag_color()
            dialect_name = session.bind.dialect.name if session.bind is not None else "postgresql"
            insert = sqlite.insert if dialect_name == "sqlite" else postgresql.insert
            upsert = (
                insert(TagValueModel.__table__)
                .values(organization_id=organization_id, key=key, value=new_value, color=carried_color)
                .on_conflict_do_update(
                    index_elements=["organization_id", "key", "value"],
                    index_where=text("deleted_at IS NULL"),
                    set_={"color": carried_color, "modified_at": now},
                )
            )
            await session.execute(upsert)
            await self._soft_delete_tag_value_rows(
                session, organization_id=organization_id, key=key, now=now, value=old_value
            )
            await session.commit()
            return TagValueRenameResult(
                key=key,
                value=new_value,
                color=carried_color,
                renamed_workflow_count=len(active_sets),
            )

    async def _grouped_value_active(
        self,
        session: AsyncSession,
        *,
        organization_id: str,
        key: str,
        value: str,
    ) -> bool:
        """True when grouped label ``(key, value)`` exists active org-wide — either a
        registered color row or an active SET event in use on some workflow/run."""
        registered = (
            await session.execute(
                select(TagValueModel.tag_value_id)
                .where(
                    and_(
                        TagValueModel.organization_id == organization_id,
                        TagValueModel.key == key,
                        TagValueModel.value == value,
                        TagValueModel.deleted_at.is_(None),
                    )
                )
                .limit(1)
            )
        ).first()
        if registered is not None:
            return True
        in_use = (
            await session.execute(
                select(WorkflowTagEventModel.tag_event_id)
                .where(
                    and_(
                        WorkflowTagEventModel.organization_id == organization_id,
                        WorkflowTagEventModel.key == key,
                        WorkflowTagEventModel.value == value,
                        WorkflowTagEventModel.superseded_at.is_(None),
                        WorkflowTagEventModel.event_type == TagEventType.SET.value,
                        WorkflowTagEventModel.deleted_at.is_(None),
                    )
                )
                .limit(1)
            )
        ).first()
        if in_use is not None:
            return True
        run_in_use = (
            await session.execute(
                select(WorkflowRunTagEventModel.tag_event_id)
                .where(
                    and_(
                        WorkflowRunTagEventModel.organization_id == organization_id,
                        WorkflowRunTagEventModel.key == key,
                        WorkflowRunTagEventModel.value == value,
                        WorkflowRunTagEventModel.superseded_at.is_(None),
                        WorkflowRunTagEventModel.event_type == TagEventType.SET.value,
                    )
                )
                .limit(1)
            )
        ).first()
        return run_in_use is not None
