"""Workflow-tag write/read path. Named ``tags`` (not ``workflow_tags``) so run-tag events can land here later."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone

import structlog
from sqlalchemy import and_, select, text
from sqlalchemy.dialects import postgresql, sqlite
from sqlalchemy.ext.asyncio import AsyncSession

from skyvern.forge.sdk.db._error_handling import db_operation, register_passthrough_exception
from skyvern.forge.sdk.db.base_repository import BaseRepository
from skyvern.forge.sdk.db.models import TagKeyModel, WorkflowTagEventModel
from skyvern.forge.sdk.workflow.models.tags import TagEventType, TagWriteContext
from skyvern.forge.sdk.workflow.models.validators import RUN_METADATA_MAX_KEYS

LOG = structlog.get_logger()

# Aliased to RUN_METADATA_MAX_KEYS so tag and run_metadata caps stay coupled.
MAX_TAGS_PER_WORKFLOW = RUN_METADATA_MAX_KEYS


class TagCountLimitExceeded(ValueError):
    """Raised when an apply would push a workflow over MAX_TAGS_PER_WORKFLOW."""


# Cap breaches are user input, not infra failures — log as BusinessLogicError (WARN).
register_passthrough_exception(TagCountLimitExceeded)


@dataclass(frozen=True)
class TagChange:
    """One concrete state change derived from a caller's set/delete request."""

    key: str
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
    ) -> list[TagChange]:
        """Atomically apply SET/DELETE events for a workflow's tags. Sets win
        over deletes on same-key collision; same-value SETs are no-ops. Same-key
        concurrent writers race the partial UNIQUE — the loser's IntegrityError
        is not caught here, so it surfaces to the caller as a 5xx."""
        effective_deletes = {k for k in deletes if k not in sets}
        if not sets and not effective_deletes:
            return []

        now = datetime.now(timezone.utc)

        async with self.Session() as session:
            current = await self._get_current_active_set_events(
                session,
                workflow_permanent_id=workflow_permanent_id,
                organization_id=organization_id,
            )

            # Soft cap: concurrent distinct-key writers can each pass; partial
            # UNIQUE catches only same-key races. Cap is a UX rule, acceptable.
            projected_keys = (set(current.keys()) | set(sets.keys())) - effective_deletes
            if len(projected_keys) > MAX_TAGS_PER_WORKFLOW:
                raise TagCountLimitExceeded(
                    f"workflow {workflow_permanent_id} would have {len(projected_keys)} tags; "
                    f"max is {MAX_TAGS_PER_WORKFLOW}"
                )

            changes: list[TagChange] = []

            for key, new_value in sets.items():
                existing = current.get(key)
                if existing is not None and existing.value == new_value:
                    continue
                if existing is not None:
                    existing.superseded_at = now
                event = WorkflowTagEventModel(
                    workflow_permanent_id=workflow_permanent_id,
                    organization_id=organization_id,
                    key=key,
                    value=new_value,
                    event_type=TagEventType.SET.value,
                    set_at=now,
                    set_by=context.caller_id,
                    source=context.source.value,
                    caller_type=context.caller_type.value if context.caller_type else None,
                )
                session.add(event)
                changes.append(
                    TagChange(
                        key=key,
                        new_value=new_value,
                        event_type=TagEventType.SET,
                        superseded_event_id=existing.tag_event_id if existing else None,
                    )
                )

            for key in effective_deletes:
                existing = current.get(key)
                if existing is None:
                    continue
                existing.superseded_at = now
                event = WorkflowTagEventModel(
                    workflow_permanent_id=workflow_permanent_id,
                    organization_id=organization_id,
                    key=key,
                    value=None,
                    event_type=TagEventType.DELETE.value,
                    set_at=now,
                    set_by=context.caller_id,
                    source=context.source.value,
                    caller_type=context.caller_type.value if context.caller_type else None,
                )
                session.add(event)
                changes.append(
                    TagChange(
                        key=key,
                        new_value=None,
                        event_type=TagEventType.DELETE,
                        superseded_event_id=existing.tag_event_id,
                    )
                )

            # Flush UPDATEs-before-INSERTs so the partial UNIQUE sees a
            # consistent state when the new SET row hits it.
            await session.flush()

            # Auto-register TagKeyModel rows only for keys whose SET actually
            # wrote a new event. The partial UNIQUE on (org, key) WHERE
            # deleted_at IS NULL races concurrent first-use writers, so we use
            # an INSERT ... ON CONFLICT DO NOTHING to swallow the race instead
            # of letting it surface as a 5xx. postgresql and sqlite need their
            # own insert() construct to compile ON CONFLICT, but share the
            # on_conflict_do_nothing signature. Both require index_where to
            # match the partial unique index or ON CONFLICT inference fails.
            changed_set_keys = {c.key for c in changes if c.event_type == TagEventType.SET}
            if changed_set_keys:
                rows = [{"organization_id": organization_id, "key": key} for key in changed_set_keys]
                dialect_name = session.bind.dialect.name if session.bind is not None else "postgresql"
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

            await session.commit()
            return changes

    async def _get_current_active_set_events(
        self,
        session: AsyncSession,
        *,
        workflow_permanent_id: str,
        organization_id: str,
    ) -> dict[str, WorkflowTagEventModel]:
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
        return {row.key: row for row in result.scalars().all()}

    @db_operation("get_active_tags_for_workflow")
    async def get_active_tags_for_workflow(
        self,
        workflow_permanent_id: str,
        organization_id: str,
    ) -> dict[str, str]:
        """Return the current {key: value} map for a workflow."""
        async with self.Session() as session:
            rows = await self._get_current_active_set_events(
                session,
                workflow_permanent_id=workflow_permanent_id,
                organization_id=organization_id,
            )
            # SET events always carry a non-null value, but defend against
            # data drift: skip-and-log instead of crashing the request.
            result: dict[str, str] = {}
            for key, row in rows.items():
                if row.value is None:
                    LOG.warning(
                        "active SET tag row has null value; skipping",
                        tag_event_id=row.tag_event_id,
                        organization_id=organization_id,
                        workflow_permanent_id=workflow_permanent_id,
                        key=key,
                    )
                    continue
                result[key] = row.value
            return result

    @db_operation("get_active_tag_events_for_workflow")
    async def get_active_tag_events_for_workflow(
        self,
        workflow_permanent_id: str,
        organization_id: str,
    ) -> list[WorkflowTagEventModel]:
        """Active SET event rows for a workflow. Carries full attribution
        (source/set_at/set_by) so callers can surface per-tag provenance —
        ``get_active_tags_for_workflow`` returns just key→value."""
        async with self.Session() as session:
            rows = await self._get_current_active_set_events(
                session,
                workflow_permanent_id=workflow_permanent_id,
                organization_id=organization_id,
            )
            return list(rows.values())

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
    ) -> dict[str, dict[str, str]]:
        """Batch read of current SET tags for many workflows. Cross-org isolation
        is enforced by the organization_id filter — wpids that don't belong to
        the org are silently absent from the result (no rows match)."""
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

        result: dict[str, dict[str, str]] = {}
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
            result.setdefault(row.workflow_permanent_id, {})[row.key] = row.value
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
