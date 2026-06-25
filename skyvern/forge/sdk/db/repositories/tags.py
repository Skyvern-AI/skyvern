"""Workflow-tag write/read path. Named ``tags`` (not ``workflow_tags``) so run-tag events can land here later."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone

import structlog
from sqlalchemy import Exists, and_, exists, func, select, text, update
from sqlalchemy.dialects import postgresql, sqlite
from sqlalchemy.ext.asyncio import AsyncSession

from skyvern.forge.sdk.db._error_handling import db_operation, register_passthrough_exception
from skyvern.forge.sdk.db.base_repository import BaseRepository
from skyvern.forge.sdk.db.models import TagKeyModel, TagValueModel, WorkflowModel, WorkflowTagEventModel
from skyvern.forge.sdk.workflow.models.tags import TagEventType, TagWriteContext
from skyvern.forge.sdk.workflow.models.validators import RUN_METADATA_MAX_KEYS, random_tag_color

LOG = structlog.get_logger()

# Aliased to RUN_METADATA_MAX_KEYS so tag and run_metadata caps stay coupled.
MAX_TAGS_PER_WORKFLOW = RUN_METADATA_MAX_KEYS


class TagCountLimitExceeded(ValueError):
    """Raised when an apply would push a workflow over MAX_TAGS_PER_WORKFLOW."""


class TagValueRenameCollision(ValueError):
    """Raised when renaming ``(key, old)`` to ``(key, new)`` and ``(key, new)``
    already exists active org-wide. v1 rejects rather than merging."""


# Cap breaches and rename collisions are user input, not infra failures — log as
# BusinessLogicError (WARN), not UnexpectedError (ERROR).
register_passthrough_exception(TagCountLimitExceeded)
register_passthrough_exception(TagValueRenameCollision)


@dataclass(frozen=True)
class TagValueRenameResult:
    """Outcome of a successful grouped-label rename: the new ``(key, value)``,
    its carried-over color, and how many workflows were re-tagged."""

    key: str
    value: str
    color: str
    renamed_workflow_count: int


@dataclass(frozen=True)
class TagChange:
    """One concrete state change derived from a caller's set/delete request.
    ``key`` is None for a standalone label (identified by its value)."""

    key: str | None
    new_value: str | None
    event_type: TagEventType
    superseded_event_id: str | None


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
        label_sets = label_sets or []
        label_deletes = label_deletes or []
        colors = colors or {}
        label_set_values = set(label_sets)
        effective_deletes = {k for k in deletes if k not in sets}
        effective_label_deletes = {v for v in label_deletes if v not in label_set_values}
        if not sets and not effective_deletes and not label_set_values and not effective_label_deletes:
            return []

        now = datetime.now(timezone.utc)

        async with self.Session() as session:
            active_rows = await self._get_active_set_rows(
                session,
                workflow_permanent_id=workflow_permanent_id,
                organization_id=organization_id,
            )
            # Grouped tags keyed by their group; standalone labels keyed by value.
            grouped_current = {row.key: row for row in active_rows if row.key is not None}
            label_current = {row.value: row for row in active_rows if row.key is None}

            # Soft cap: concurrent distinct-identity writers can each pass; the
            # partial UNIQUEs catch only same-identity races. Cap is a UX rule.
            projected_grouped = (set(grouped_current.keys()) | set(sets.keys())) - effective_deletes
            projected_labels = (set(label_current.keys()) | label_set_values) - effective_label_deletes
            projected_total = len(projected_grouped) + len(projected_labels)
            if projected_total > MAX_TAGS_PER_WORKFLOW:
                raise TagCountLimitExceeded(
                    f"workflow {workflow_permanent_id} would have {projected_total} tags; "
                    f"max is {MAX_TAGS_PER_WORKFLOW}"
                )

            changes: list[TagChange] = []

            def _add_event(*, key: str | None, value: str | None, event_type: TagEventType) -> None:
                session.add(
                    WorkflowTagEventModel(
                        workflow_permanent_id=workflow_permanent_id,
                        organization_id=organization_id,
                        key=key,
                        value=value,
                        event_type=event_type.value,
                        set_at=now,
                        set_by=context.caller_id,
                        source=context.source.value,
                        caller_type=context.caller_type.value if context.caller_type else None,
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

    async def _get_active_set_rows(
        self,
        session: AsyncSession,
        *,
        workflow_permanent_id: str,
        organization_id: str,
    ) -> list[WorkflowTagEventModel]:
        """Active (non-superseded, non-deleted) SET event rows for a workflow —
        both grouped tags and standalone labels."""
        stmt = select(WorkflowTagEventModel).where(
            and_(
                WorkflowTagEventModel.organization_id == organization_id,
                WorkflowTagEventModel.workflow_permanent_id == workflow_permanent_id,
                WorkflowTagEventModel.superseded_at.is_(None),
                WorkflowTagEventModel.event_type == TagEventType.SET.value,
                WorkflowTagEventModel.deleted_at.is_(None),
            )
        )
        result = await session.execute(stmt)
        return list(result.scalars().all())

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
        async with self.Session() as session:
            stmt = (
                select(WorkflowTagEventModel)
                .where(WorkflowTagEventModel.organization_id == organization_id)
                .where(WorkflowTagEventModel.workflow_permanent_id == workflow_permanent_id)
                .where(WorkflowTagEventModel.deleted_at.is_(None))
            )
            if since is not None:
                stmt = stmt.where(WorkflowTagEventModel.set_at >= since)
            if key is not None:
                stmt = stmt.where(WorkflowTagEventModel.key == key)
            stmt = stmt.order_by(WorkflowTagEventModel.set_at.desc(), WorkflowTagEventModel.tag_event_id.desc()).limit(
                limit
            )
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
        if not workflow_permanent_ids:
            return {}

        async with self.Session() as session:
            stmt = select(WorkflowTagEventModel).where(
                and_(
                    WorkflowTagEventModel.organization_id == organization_id,
                    WorkflowTagEventModel.workflow_permanent_id.in_(workflow_permanent_ids),
                    WorkflowTagEventModel.superseded_at.is_(None),
                    WorkflowTagEventModel.event_type == TagEventType.SET.value,
                    WorkflowTagEventModel.deleted_at.is_(None),
                )
            )
            rows = (await session.execute(stmt)).scalars().all()

        result: dict[str, list[tuple[str | None, str]]] = {}
        for row in rows:
            if row.value is None:
                LOG.warning(
                    "active SET tag row has null value; skipping",
                    tag_event_id=row.tag_event_id,
                    organization_id=organization_id,
                    workflow_permanent_id=row.workflow_permanent_id,
                    key=row.key,
                )
                continue
            result.setdefault(row.workflow_permanent_id, []).append((row.key, row.value))
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
    async def list_tag_values(self, organization_id: str) -> list[TagValueModel]:
        """Active (key, value, color) registry entries for the org, ordered by key
        then value. The frontend joins these onto tags by (key, value) the same way
        it joins descriptions onto keys."""
        async with self.Session() as session:
            stmt = (
                select(TagValueModel)
                .where(TagValueModel.organization_id == organization_id)
                .where(TagValueModel.deleted_at.is_(None))
                .order_by(TagValueModel.key.asc(), TagValueModel.value.asc())
            )
            result = await session.execute(stmt)
            return list(result.scalars().all())

    @db_operation("recolor_tag_value")
    async def recolor_tag_value(
        self,
        organization_id: str,
        key: str,
        value: str,
        color: str,
    ) -> TagValueModel | None:
        """Recolor an existing (key, value) registry row. Returns None when the pair
        is not registered for the org (caller should 404)."""
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

    @db_operation("delete_tag_key")
    async def delete_tag_key(
        self,
        organization_id: str,
        key: str,
        context: TagWriteContext,
    ) -> int | None:
        """Cascade-delete a tag key: write a DELETE event for every workflow that
        currently has it, then soft-delete the key registry row and its value color
        rows (so GET /tag-values stops returning colors for the removed key). Returns the number
        of workflows the tag was removed from, or None when the key is not
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
        INSERT for the DELETE events to avoid the ORM round-trip."""
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

            active_sets = (
                (
                    await session.execute(
                        select(WorkflowTagEventModel).where(
                            and_(
                                WorkflowTagEventModel.organization_id == organization_id,
                                WorkflowTagEventModel.key == key,
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

            for existing in active_sets:
                existing.superseded_at = now
                session.add(
                    WorkflowTagEventModel(
                        workflow_permanent_id=existing.workflow_permanent_id,
                        organization_id=organization_id,
                        key=key,
                        value=None,
                        event_type=TagEventType.DELETE.value,
                        set_at=now,
                        set_by=context.caller_id,
                        source=context.source.value,
                        caller_type=context.caller_type.value if context.caller_type else None,
                    )
                )

            key_row.deleted_at = now
            await self._soft_delete_tag_value_rows(session, organization_id=organization_id, key=key, now=now)
            await session.commit()
            return len(active_sets)

    @db_operation("delete_tag_value")
    async def delete_tag_value(
        self,
        organization_id: str,
        key: str,
        value: str,
        context: TagWriteContext,
    ) -> int | None:
        """Cascade-delete a single grouped label ``(key, value)``, mirroring
        ``delete_tag_key`` at value granularity: write a DELETE event (carrying the
        value, so history records which label was removed) for every workflow with
        an active SET on it, then soft-delete the ``(key, value)`` color row. Returns
        the number of workflows the label was removed from, or None when neither a
        registered color row nor an active SET exists (caller should 404). Idempotent:
        a second call returns None; re-applying via a SET re-registers the label.

        DELETE events don't match the SET-only partial UNIQUE, so superseding the SET
        and inserting the DELETE in one transaction needs no flush ordering (same as
        ``delete_tag_key``). The same accepted delete-vs-SET race applies."""
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

            active_sets = (
                (
                    await session.execute(
                        select(WorkflowTagEventModel).where(
                            and_(
                                WorkflowTagEventModel.organization_id == organization_id,
                                WorkflowTagEventModel.key == key,
                                WorkflowTagEventModel.value == value,
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

            if value_row is None and not active_sets:
                return None

            for existing in active_sets:
                existing.superseded_at = now
                session.add(
                    WorkflowTagEventModel(
                        workflow_permanent_id=existing.workflow_permanent_id,
                        organization_id=organization_id,
                        key=key,
                        value=value,
                        event_type=TagEventType.DELETE.value,
                        set_at=now,
                        set_by=context.caller_id,
                        source=context.source.value,
                        caller_type=context.caller_type.value if context.caller_type else None,
                    )
                )

            await self._soft_delete_tag_value_rows(
                session, organization_id=organization_id, key=key, now=now, value=value
            )
            await session.commit()
            return len(active_sets)

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
        disproportionate for a rare manual admin action."""
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

            if old_row is None and not active_sets:
                return None

            if await self._grouped_value_active(session, organization_id=organization_id, key=key, value=new_value):
                raise TagValueRenameCollision(
                    f"tag value '{key}:{new_value}' already exists; rename would merge labels (not supported)"
                )

            for existing in active_sets:
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
        registered color row or an active SET event in use on some workflow."""
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
        return in_use is not None
