from __future__ import annotations

from datetime import datetime, timedelta, timezone

import structlog
from sqlalchemy import and_, delete, distinct, func, or_, select, update
from sqlalchemy.dialects.postgresql import insert

from skyvern.forge.sdk.db._error_handling import db_operation
from skyvern.forge.sdk.db.base_repository import BaseRepository
from skyvern.forge.sdk.db.exceptions import NotFoundError
from skyvern.forge.sdk.db.models import (
    ScriptBlockModel,
    ScriptBranchHitModel,
    ScriptFallbackEpisodeModel,
    ScriptFileModel,
    ScriptModel,
    WorkflowRunModel,
    WorkflowScriptModel,
)
from skyvern.forge.sdk.db.utils import (
    convert_to_script,
    convert_to_script_block,
    convert_to_script_file,
)
from skyvern.forge.sdk.utils.sanitization import sanitize_postgres_text
from skyvern.schemas.scripts import (
    Script,
    ScriptBlock,
    ScriptBranchHit,
    ScriptFallbackEpisode,
    ScriptFile,
    ScriptStatus,
    WorkflowScript,
)

LOG = structlog.get_logger()


class ScriptsRepository(BaseRepository):
    """Database operations for scripts, script files, script blocks, workflow scripts, and fallback episodes."""

    @db_operation("create_script")
    async def create_script(
        self,
        organization_id: str,
        run_id: str | None = None,
        script_id: str | None = None,
        version: int | None = None,
    ) -> Script:
        async with self.Session() as session:
            script = ScriptModel(
                organization_id=organization_id,
                run_id=run_id,
            )
            if script_id:
                script.script_id = script_id
            if version:
                script.version = version
            session.add(script)
            await session.commit()
            await session.refresh(script)
            return convert_to_script(script)

    @db_operation("get_scripts")
    async def get_scripts(
        self,
        organization_id: str,
        page: int = 1,
        page_size: int = 10,
    ) -> list[Script]:
        async with self.Session() as session:
            # Calculate offset for pagination
            offset = (page - 1) * page_size

            # Subquery to get the latest version of each script
            latest_versions_subquery = (
                select(ScriptModel.script_id, func.max(ScriptModel.version).label("latest_version"))
                .filter_by(organization_id=organization_id)
                .filter(ScriptModel.deleted_at.is_(None))
                .group_by(ScriptModel.script_id)
                .subquery()
            )

            # Main query to get scripts with their latest versions
            get_scripts_query = (
                select(ScriptModel)
                .join(
                    latest_versions_subquery,
                    and_(
                        ScriptModel.script_id == latest_versions_subquery.c.script_id,
                        ScriptModel.version == latest_versions_subquery.c.latest_version,
                    ),
                )
                .filter_by(organization_id=organization_id)
                .filter(ScriptModel.deleted_at.is_(None))
                .order_by(ScriptModel.created_at.desc())
                .limit(page_size)
                .offset(offset)
            )
            scripts = (await session.scalars(get_scripts_query)).all()
            return [convert_to_script(script) for script in scripts]

    @db_operation("get_script")
    async def get_script(
        self,
        script_id: str,
        organization_id: str,
        version: int | None = None,
    ) -> Script | None:
        """Get a specific script by ID and optionally by version."""
        async with self.Session() as session:
            get_script_query = (
                select(ScriptModel)
                .filter_by(script_id=script_id)
                .filter_by(organization_id=organization_id)
                .filter(ScriptModel.deleted_at.is_(None))
            )

            if version is not None:
                get_script_query = get_script_query.filter_by(version=version)
            else:
                # Get the latest version
                get_script_query = get_script_query.order_by(ScriptModel.version.desc()).limit(1)

            if script := (await session.scalars(get_script_query)).first():
                return convert_to_script(script)
            return None

    @db_operation("get_script_revision")
    async def get_script_revision(self, script_revision_id: str, organization_id: str) -> Script | None:
        async with self.Session() as session:
            script = (
                await session.scalars(
                    select(ScriptModel)
                    .filter_by(script_revision_id=script_revision_id)
                    .filter_by(organization_id=organization_id)
                )
            ).first()
            return convert_to_script(script) if script else None

    @db_operation("get_latest_script_version")
    async def get_latest_script_version(self, script_id: str, organization_id: str) -> Script | None:
        """Get the latest version of a script by script_id."""
        async with self.Session() as session:
            script = (
                await session.scalars(
                    select(ScriptModel)
                    .filter_by(script_id=script_id, organization_id=organization_id)
                    .filter(ScriptModel.deleted_at.is_(None))
                    .order_by(ScriptModel.version.desc())
                    .limit(1)
                )
            ).first()
            return convert_to_script(script) if script else None

    @db_operation("get_script_versions")
    async def get_script_versions(
        self,
        script_id: str,
        organization_id: str,
    ) -> list[Script]:
        """Get all versions of a script, ordered by version DESC."""
        async with self.Session() as session:
            query = (
                select(ScriptModel)
                .filter(
                    ScriptModel.script_id == script_id,
                    ScriptModel.organization_id == organization_id,
                    ScriptModel.deleted_at.is_(None),
                )
                .order_by(ScriptModel.version.desc())
            )
            result = await session.scalars(query)
            return [convert_to_script(row) for row in result.all()]

    @db_operation("get_script_version_stats")
    async def get_script_version_stats(
        self,
        organization_id: str,
        script_ids: list[str],
    ) -> dict[str, tuple[int, int]]:
        """Return {script_id: (latest_version, version_count)} for the given script IDs."""
        if not script_ids:
            return {}
        async with self.Session() as session:
            query = (
                select(
                    ScriptModel.script_id,
                    # max(version) must include soft-deleted rows so next-version
                    # assignment doesn't collide with the unique constraint.
                    func.max(ScriptModel.version),
                    # version_count only counts live rows (for display).
                    func.count(ScriptModel.script_revision_id).filter(
                        ScriptModel.deleted_at.is_(None),
                    ),
                )
                .filter(
                    ScriptModel.organization_id == organization_id,
                    ScriptModel.script_id.in_(script_ids),
                )
                .group_by(ScriptModel.script_id)
            )
            rows = (await session.execute(query)).all()
            return {row[0]: (row[1], row[2]) for row in rows}

    @db_operation("soft_delete_script_by_revision")
    async def soft_delete_script_by_revision(self, script_revision_id: str, organization_id: str) -> None:
        async with self.Session() as session:
            await session.execute(
                update(ScriptModel)
                .filter_by(script_revision_id=script_revision_id)
                .filter_by(organization_id=organization_id)
                .values(deleted_at=datetime.now(timezone.utc))
            )
            await session.commit()

    @db_operation("create_script_file")
    async def create_script_file(
        self,
        script_revision_id: str,
        script_id: str,
        organization_id: str,
        file_path: str,
        file_name: str,
        file_type: str,
        content_hash: str | None = None,
        file_size: int | None = None,
        mime_type: str | None = None,
        encoding: str = "utf-8",
        artifact_id: str | None = None,
    ) -> ScriptFile:
        """Create a script file."""
        async with self.Session() as session:
            script_file = ScriptFileModel(
                script_revision_id=script_revision_id,
                script_id=script_id,
                organization_id=organization_id,
                file_path=file_path,
                file_name=file_name,
                file_type=file_type,
                content_hash=content_hash,
                file_size=file_size,
                mime_type=mime_type,
                encoding=encoding,
                artifact_id=artifact_id,
            )
            session.add(script_file)
            await session.commit()
            await session.refresh(script_file)
            return convert_to_script_file(script_file)

    @db_operation("create_script_block")
    async def create_script_block(
        self,
        script_revision_id: str,
        script_id: str,
        organization_id: str,
        script_block_label: str,
        script_file_id: str | None = None,
        run_signature: str | None = None,
        workflow_run_id: str | None = None,
        workflow_run_block_id: str | None = None,
        input_fields: list[str] | None = None,
        requires_agent: bool = False,
    ) -> ScriptBlock:
        """Create a script block."""
        async with self.Session() as session:
            script_block = ScriptBlockModel(
                script_revision_id=script_revision_id,
                script_id=script_id,
                organization_id=organization_id,
                script_block_label=script_block_label,
                script_file_id=script_file_id,
                run_signature=run_signature,
                workflow_run_id=workflow_run_id,
                workflow_run_block_id=workflow_run_block_id,
                input_fields=input_fields,
                requires_agent=requires_agent,
            )
            session.add(script_block)
            await session.commit()
            await session.refresh(script_block)
            return convert_to_script_block(script_block)

    @db_operation("update_script_block")
    async def update_script_block(
        self,
        script_block_id: str,
        organization_id: str,
        script_file_id: str | None = None,
        run_signature: str | None = None,
        workflow_run_id: str | None = None,
        workflow_run_block_id: str | None = None,
        clear_run_signature: bool = False,
        input_fields: list[str] | None = None,
        requires_agent: bool | None = None,
    ) -> ScriptBlock:
        async with self.Session() as session:
            script_block = (
                await session.scalars(
                    select(ScriptBlockModel)
                    .filter_by(script_block_id=script_block_id)
                    .filter_by(organization_id=organization_id)
                )
            ).first()
            if script_block:
                if script_file_id is not None:
                    script_block.script_file_id = script_file_id
                if clear_run_signature:
                    script_block.run_signature = None
                elif run_signature is not None:
                    script_block.run_signature = run_signature
                if workflow_run_id is not None:
                    script_block.workflow_run_id = workflow_run_id
                if workflow_run_block_id is not None:
                    script_block.workflow_run_block_id = workflow_run_block_id
                if input_fields is not None:
                    script_block.input_fields = input_fields
                if requires_agent is not None:
                    script_block.requires_agent = requires_agent
                await session.commit()
                await session.refresh(script_block)
                return convert_to_script_block(script_block)
            else:
                raise NotFoundError("Script block not found")

    @db_operation("get_script_files")
    async def get_script_files(self, script_revision_id: str, organization_id: str) -> list[ScriptFile]:
        async with self.Session() as session:
            script_files = (
                await session.scalars(
                    select(ScriptFileModel)
                    .filter_by(script_revision_id=script_revision_id)
                    .filter_by(organization_id=organization_id)
                )
            ).all()
            return [convert_to_script_file(script_file) for script_file in script_files]

    @db_operation("get_script_file_by_id")
    async def get_script_file_by_id(
        self,
        script_revision_id: str,
        file_id: str,
        organization_id: str,
    ) -> ScriptFile | None:
        async with self.Session() as session:
            script_file = (
                await session.scalars(
                    select(ScriptFileModel)
                    .filter_by(script_revision_id=script_revision_id)
                    .filter_by(file_id=file_id)
                    .filter_by(organization_id=organization_id)
                )
            ).first()

            return convert_to_script_file(script_file) if script_file else None

    @db_operation("get_script_file_by_path")
    async def get_script_file_by_path(
        self,
        script_revision_id: str,
        file_path: str,
        organization_id: str,
    ) -> ScriptFile | None:
        async with self.Session() as session:
            script_file = (
                await session.scalars(
                    select(ScriptFileModel)
                    .filter_by(script_revision_id=script_revision_id)
                    .filter_by(file_path=file_path)
                    .filter_by(organization_id=organization_id)
                )
            ).first()
            return convert_to_script_file(script_file) if script_file else None

    @db_operation("get_script_file_by_content_hash")
    async def get_script_file_by_content_hash(
        self,
        script_id: str,
        organization_id: str,
        content_hash: str,
    ) -> ScriptFile | None:
        """Find the most recent ScriptFile with a matching content_hash across all revisions of a script."""
        async with self.Session() as session:
            script_file = (
                await session.scalars(
                    select(ScriptFileModel)
                    .filter_by(script_id=script_id)
                    .filter_by(organization_id=organization_id)
                    .filter_by(content_hash=content_hash)
                    .order_by(ScriptFileModel.created_at.desc())
                    .limit(1)
                )
            ).first()
            return convert_to_script_file(script_file) if script_file else None

    @db_operation("update_script_file")
    async def update_script_file(
        self,
        script_file_id: str,
        organization_id: str,
        artifact_id: str | None = None,
        content_hash: str | None = None,
    ) -> ScriptFile:
        async with self.Session() as session:
            script_file = (
                await session.scalars(
                    select(ScriptFileModel).filter_by(file_id=script_file_id).filter_by(organization_id=organization_id)
                )
            ).first()
            if script_file:
                if artifact_id:
                    script_file.artifact_id = artifact_id
                if content_hash is not None:
                    script_file.content_hash = content_hash
                await session.commit()
                await session.refresh(script_file)
                return convert_to_script_file(script_file)
            else:
                raise NotFoundError("Script file not found")

    @db_operation("get_script_block")
    async def get_script_block(
        self,
        script_block_id: str,
        organization_id: str,
    ) -> ScriptBlock | None:
        async with self.Session() as session:
            record = (
                await session.scalars(
                    select(ScriptBlockModel)
                    .filter_by(script_block_id=script_block_id)
                    .filter_by(organization_id=organization_id)
                )
            ).first()
            return convert_to_script_block(record) if record else None

    @db_operation("get_script_block_by_label")
    async def get_script_block_by_label(
        self,
        organization_id: str,
        script_revision_id: str,
        script_block_label: str,
    ) -> ScriptBlock | None:
        async with self.Session() as session:
            record = (
                await session.scalars(
                    select(ScriptBlockModel)
                    .filter_by(script_revision_id=script_revision_id)
                    .filter_by(script_block_label=script_block_label)
                    .filter_by(organization_id=organization_id)
                )
            ).first()
            return convert_to_script_block(record) if record else None

    @db_operation("get_script_blocks_by_script_revision_id")
    async def get_script_blocks_by_script_revision_id(
        self,
        script_revision_id: str,
        organization_id: str,
    ) -> list[ScriptBlock]:
        async with self.Session() as session:
            records = (
                await session.scalars(
                    select(ScriptBlockModel)
                    .filter_by(script_revision_id=script_revision_id)
                    .filter_by(organization_id=organization_id)
                    .order_by(ScriptBlockModel.created_at.asc())
                )
            ).all()
            return [convert_to_script_block(record) for record in records]

    @db_operation("create_workflow_script")
    async def create_workflow_script(
        self,
        *,
        organization_id: str,
        script_id: str,
        workflow_permanent_id: str,
        cache_key: str,
        cache_key_value: str,
        workflow_id: str | None = None,
        workflow_run_id: str | None = None,
        status: ScriptStatus = ScriptStatus.published,
        is_pinned: bool = False,
    ) -> None:
        """Create a workflow->script cache mapping entry."""
        async with self.Session() as session:
            record = WorkflowScriptModel(
                organization_id=organization_id,
                script_id=script_id,
                workflow_permanent_id=workflow_permanent_id,
                workflow_id=workflow_id,
                workflow_run_id=workflow_run_id,
                cache_key=cache_key,
                cache_key_value=cache_key_value,
                status=status,
                is_pinned=is_pinned,
            )
            session.add(record)
            await session.commit()

    @db_operation("get_workflow_script")
    async def get_workflow_script(
        self,
        organization_id: str,
        workflow_permanent_id: str,
        workflow_run_id: str,
        statuses: list[ScriptStatus] | None = None,
    ) -> WorkflowScript | None:
        async with self.Session() as session:
            query = (
                select(WorkflowScriptModel)
                .filter_by(organization_id=organization_id)
                .filter_by(workflow_permanent_id=workflow_permanent_id)
                .filter_by(workflow_run_id=workflow_run_id)
            )
            if statuses:
                query = query.filter(WorkflowScriptModel.status.in_(statuses))
            workflow_script_model = (await session.scalars(query)).first()
            return WorkflowScript.model_validate(workflow_script_model) if workflow_script_model else None

    @db_operation("get_workflow_script_source_workflow_id")
    async def get_workflow_script_source_workflow_id(
        self,
        *,
        organization_id: str,
        workflow_permanent_id: str,
        script_id: str,
        cache_key_value: str,
    ) -> str | None:
        """Return the workflow version (w_*) that produced a given cached script row.

        Used to detect when the workflow definition has changed since the cached
        script was generated (SKY-9254).
        """
        async with self.Session() as session:
            query = (
                select(WorkflowScriptModel.workflow_id)
                .where(
                    WorkflowScriptModel.organization_id == organization_id,
                    WorkflowScriptModel.workflow_permanent_id == workflow_permanent_id,
                    WorkflowScriptModel.script_id == script_id,
                    WorkflowScriptModel.cache_key_value == cache_key_value,
                    WorkflowScriptModel.deleted_at.is_(None),
                )
                .order_by(WorkflowScriptModel.created_at.desc())
                .limit(1)
            )
            return (await session.scalars(query)).first()

    @db_operation("get_workflow_script_by_cache_key_value")
    async def get_workflow_script_by_cache_key_value(
        self,
        *,
        organization_id: str,
        workflow_permanent_id: str,
        cache_key_value: str,
        workflow_run_id: str | None = None,
        cache_key: str | None = None,
        statuses: list[ScriptStatus] | None = None,
    ) -> tuple[Script | None, bool]:
        """Get latest script version linked to a workflow by a specific cache_key_value.

        Returns:
            A tuple of (script, is_pinned). The repository implementation does not
            support pinned queries, so is_pinned is always False.
        """
        async with self.Session() as session:
            # Build the query: join workflow_scripts with scripts
            # Join on both script_id and organization_id to leverage uc_org_script_version index
            query = (
                select(ScriptModel)
                .join(
                    WorkflowScriptModel,
                    and_(
                        ScriptModel.organization_id == WorkflowScriptModel.organization_id,
                        ScriptModel.script_id == WorkflowScriptModel.script_id,
                    ),
                )
                .where(
                    WorkflowScriptModel.organization_id == organization_id,
                    WorkflowScriptModel.workflow_permanent_id == workflow_permanent_id,
                    WorkflowScriptModel.cache_key_value == cache_key_value,
                    WorkflowScriptModel.deleted_at.is_(None),
                    # Exclude soft-deleted Script revisions so an empty/failed revision
                    # left behind by a crashed regeneration cannot be returned as the
                    # "latest" version for this cache key. Without this filter, runs
                    # observe has_script=True with script_block_count=0 (empty_blocks_detected
                    # regression tracked under SKY-8757).
                    ScriptModel.deleted_at.is_(None),
                )
            )

            if workflow_run_id:
                query = query.where(WorkflowScriptModel.workflow_run_id == workflow_run_id)

            if cache_key is not None:
                query = query.where(WorkflowScriptModel.cache_key == cache_key)

            if statuses is not None and len(statuses) > 0:
                query = query.where(WorkflowScriptModel.status.in_(statuses))

            query = query.order_by(ScriptModel.created_at.desc(), ScriptModel.version.desc()).limit(1)

            script = (await session.scalars(query)).first()
            return (convert_to_script(script), False) if script else (None, False)

    @db_operation("get_workflow_cache_key_count")
    async def get_workflow_cache_key_count(
        self,
        organization_id: str,
        workflow_permanent_id: str,
        cache_key: str,
        filter: str | None = None,
    ) -> int:
        async with self.Session() as session:
            query = (
                select(func.count())
                .select_from(WorkflowScriptModel)
                .filter_by(organization_id=organization_id)
                .filter_by(workflow_permanent_id=workflow_permanent_id)
                .filter_by(cache_key=cache_key)
                .filter_by(deleted_at=None)
                .filter_by(status="published")
            )

            if filter:
                query = query.filter(WorkflowScriptModel.cache_key_value.contains(filter))

            return (await session.execute(query)).scalar_one()

    @db_operation("get_workflow_cache_key_values")
    async def get_workflow_cache_key_values(
        self,
        organization_id: str,
        workflow_permanent_id: str,
        cache_key: str,
        page: int = 1,
        page_size: int = 100,
        filter: str | None = None,
    ) -> list[str]:
        async with self.Session() as session:
            query = (
                select(WorkflowScriptModel.cache_key_value)
                .order_by(WorkflowScriptModel.cache_key_value.asc())
                .filter_by(organization_id=organization_id)
                .filter_by(workflow_permanent_id=workflow_permanent_id)
                .filter_by(cache_key=cache_key)
                .filter_by(deleted_at=None)
                .filter_by(status="published")
                .offset((page - 1) * page_size)
                .limit(page_size)
            )

            if filter:
                query = query.filter(WorkflowScriptModel.cache_key_value.contains(filter))

            return (await session.scalars(query)).all()

    @db_operation("delete_workflow_cache_key_value")
    async def delete_workflow_cache_key_value(
        self,
        organization_id: str,
        workflow_permanent_id: str,
        cache_key_value: str,
    ) -> bool:
        """
        Soft delete workflow cache key values by setting deleted_at timestamp.

        Returns True if any records were deleted, False otherwise.
        """
        async with self.Session() as session:
            stmt = (
                update(WorkflowScriptModel)
                .where(
                    and_(
                        WorkflowScriptModel.organization_id == organization_id,
                        WorkflowScriptModel.workflow_permanent_id == workflow_permanent_id,
                        WorkflowScriptModel.cache_key_value == cache_key_value,
                        WorkflowScriptModel.deleted_at.is_(None),
                    )
                )
                .values(deleted_at=datetime.now(timezone.utc))
            )

            result = await session.execute(stmt)
            await session.commit()

            return result.rowcount > 0

    @db_operation("delete_workflow_scripts_by_permanent_id")
    async def delete_workflow_scripts_by_permanent_id(
        self,
        organization_id: str,
        workflow_permanent_id: str,
        statuses: list[ScriptStatus] | None = None,
        script_ids: list[str] | None = None,
    ) -> int:
        """
        Soft delete all published workflow scripts for a workflow permanent id by setting deleted_at timestamp.

        Returns True if any records were deleted, False otherwise.
        """
        async with self.Session() as session:
            stmt = (
                update(WorkflowScriptModel)
                .where(
                    and_(
                        WorkflowScriptModel.organization_id == organization_id,
                        WorkflowScriptModel.workflow_permanent_id == workflow_permanent_id,
                        WorkflowScriptModel.deleted_at.is_(None),
                    )
                )
                .values(deleted_at=datetime.now(timezone.utc))
            )

            if statuses:
                stmt = stmt.where(WorkflowScriptModel.status.in_([s.value for s in statuses]))

            if script_ids:
                stmt = stmt.where(WorkflowScriptModel.script_id.in_(script_ids))

            result = await session.execute(stmt)
            await session.commit()

            return result.rowcount

    @db_operation("get_workflow_scripts_by_permanent_id")
    async def get_workflow_scripts_by_permanent_id(
        self,
        organization_id: str,
        workflow_permanent_id: str,
        statuses: list[ScriptStatus] | None = None,
    ) -> list[WorkflowScriptModel]:
        async with self.Session() as session:
            query = (
                select(WorkflowScriptModel)
                .filter_by(organization_id=organization_id)
                .filter_by(workflow_permanent_id=workflow_permanent_id)
                .filter_by(deleted_at=None)
            )

            if statuses:
                query = query.filter(WorkflowScriptModel.status.in_([s.value for s in statuses]))

            query = query.order_by(WorkflowScriptModel.modified_at.desc())
            return (await session.scalars(query)).all()

    # -- Script Run / Stats ------------------------------------------------

    @db_operation("get_workflow_runs_for_script")
    async def get_workflow_runs_for_script(
        self,
        organization_id: str,
        script_id: str,
        page_size: int = 50,
        created_after: datetime | None = None,
        created_before: datetime | None = None,
    ) -> tuple[list[WorkflowRunModel], int, dict[str, int], float | None]:
        """Get workflow runs associated with a script, with total count, status counts,
        and average AI fallbacks per run.

        Includes actual script runs (run_with='code', 'code_v2', or NULL), excluding
        explicit agent runs. run_with is NULL when the workflow ran in auto mode and
        should_run_script() resolved to code via fallback (e.g. code_version >= 1).

        Returns (runs, total_count, status_counts, avg_fallbacks_per_run) where runs
        is limited by page_size, total_count is derived from the status_counts GROUP BY,
        status_counts is a GROUP BY aggregation of statuses across all runs, and
        avg_fallbacks_per_run is the average number of fallback episodes per run.

        If created_after/created_before are provided, filters by the workflow_script
        entry's created_at (not the run's created_at), scoping to the version that
        was active in that time window.
        """
        async with self.Session() as session:
            # Subquery: distinct run IDs for this script
            run_ids_subquery = (
                select(distinct(WorkflowScriptModel.workflow_run_id))
                .filter_by(organization_id=organization_id, script_id=script_id)
                .filter(WorkflowScriptModel.deleted_at.is_(None))
                .filter(WorkflowScriptModel.workflow_run_id.isnot(None))
            )

            # Time-window filters scope by workflow_script creation time,
            # which aligns with the script version that was created/used.
            if created_after is not None:
                run_ids_subquery = run_ids_subquery.filter(
                    WorkflowScriptModel.created_at >= created_after,
                )
            if created_before is not None:
                run_ids_subquery = run_ids_subquery.filter(
                    WorkflowScriptModel.created_at < created_before,
                )

            # Base filter for workflow runs - only include actual script runs.
            # run_with may be NULL when the workflow ran in auto mode and
            # should_run_script() resolved to code mode via fallback (e.g.
            # code_version >= 1 or adaptive_caching). NULL is therefore
            # treated as a code run here; explicit "agent" runs are excluded
            # by not appearing in the workflow_scripts join above.
            base_filters = [
                WorkflowRunModel.workflow_run_id.in_(run_ids_subquery),
                WorkflowRunModel.organization_id == organization_id,
                or_(
                    WorkflowRunModel.run_with.in_(["code", "code_v2"]),
                    WorkflowRunModel.run_with.is_(None),
                ),
            ]

            # Count statuses via GROUP BY (also gives us total_count)
            status_query = (
                select(WorkflowRunModel.status, func.count()).filter(*base_filters).group_by(WorkflowRunModel.status)
            )
            status_counts = {(s or "unknown"): c for s, c in (await session.execute(status_query)).all()}
            total_count = sum(status_counts.values())

            if total_count == 0:
                return [], 0, {}, None

            # Get the actual workflow runs (paginated)
            runs_query = (
                select(WorkflowRunModel)
                .filter(*base_filters)
                .order_by(WorkflowRunModel.created_at.desc())
                .limit(page_size)
            )
            runs = list((await session.scalars(runs_query)).all())

            # Compute average AI fallbacks per run over the last 20 runs.
            max_fallback_sample = 20
            recent_run_ids = (
                select(WorkflowRunModel.workflow_run_id)
                .filter(*base_filters)
                .order_by(WorkflowRunModel.created_at.desc())
                .limit(max_fallback_sample)
            )
            total_fallbacks_result = await session.execute(
                select(func.count())
                .select_from(ScriptFallbackEpisodeModel)
                .filter(
                    ScriptFallbackEpisodeModel.workflow_run_id.in_(recent_run_ids),
                    ScriptFallbackEpisodeModel.organization_id == organization_id,
                )
            )
            total_fallbacks = total_fallbacks_result.scalar() or 0
            sample_size = min(total_count, max_fallback_sample)
            avg_fallbacks_per_run = round(total_fallbacks / sample_size, 2)

            return runs, total_count, status_counts, avg_fallbacks_per_run

    @db_operation("get_script_run_stats")
    async def get_script_run_stats(
        self,
        organization_id: str,
        script_ids: list[str],
    ) -> dict[str, tuple[float | None, int]]:
        """Get success rate and total run count for each script_id.

        Both metrics are computed from the same population (workflow_scripts joined
        to workflow_runs), so they are always consistent.

        Returns a dict mapping script_id -> (success_rate, total_runs) where
        success_rate is 0.0-1.0 or None if no runs.
        """
        if not script_ids:
            return {}
        async with self.Session() as session:
            # Join workflow_scripts -> workflow_runs, group by script_id and status
            query = (
                select(
                    WorkflowScriptModel.script_id,
                    WorkflowRunModel.status,
                    func.count(distinct(WorkflowRunModel.workflow_run_id)),
                )
                .join(
                    WorkflowRunModel,
                    WorkflowScriptModel.workflow_run_id == WorkflowRunModel.workflow_run_id,
                )
                .filter(
                    WorkflowScriptModel.organization_id == organization_id,
                    WorkflowScriptModel.script_id.in_(script_ids),
                    WorkflowScriptModel.deleted_at.is_(None),
                    WorkflowScriptModel.workflow_run_id.isnot(None),
                    WorkflowRunModel.organization_id == organization_id,
                )
                .group_by(WorkflowScriptModel.script_id, WorkflowRunModel.status)
            )
            rows = (await session.execute(query)).all()

            # Aggregate per script_id
            totals: dict[str, int] = {}
            completed: dict[str, int] = {}
            for sid, status, count in rows:
                totals[sid] = totals.get(sid, 0) + count
                if status == "completed":
                    completed[sid] = completed.get(sid, 0) + count

            return {
                sid: (
                    (completed.get(sid, 0) / totals[sid]) if totals.get(sid) else None,
                    totals.get(sid, 0),
                )
                for sid in script_ids
            }

    # -- Script Pinning ----------------------------------------------------

    @db_operation("is_script_pinned")
    async def is_script_pinned(
        self,
        organization_id: str,
        script_id: str,
    ) -> bool:
        """Check if any active workflow_script row for this script_id is pinned."""
        async with self.Session() as session:
            query = (
                select(WorkflowScriptModel.is_pinned)
                .where(
                    WorkflowScriptModel.organization_id == organization_id,
                    WorkflowScriptModel.script_id == script_id,
                    WorkflowScriptModel.is_pinned.is_(True),
                    WorkflowScriptModel.deleted_at.is_(None),
                )
                .limit(1)
            )
            result = await session.scalars(query)
            return result.first() is not None

    @db_operation("pin_workflow_script")
    async def pin_workflow_script(
        self,
        organization_id: str,
        workflow_permanent_id: str,
        cache_key_value: str,
        pinned_by: str | None = None,
    ) -> WorkflowScriptModel | None:
        """Pin all workflow scripts for a given cache key value."""
        async with self.Session() as session:
            stmt = (
                update(WorkflowScriptModel)
                .where(
                    WorkflowScriptModel.organization_id == organization_id,
                    WorkflowScriptModel.workflow_permanent_id == workflow_permanent_id,
                    WorkflowScriptModel.cache_key_value == cache_key_value,
                    WorkflowScriptModel.deleted_at.is_(None),
                )
                .values(
                    is_pinned=True,
                    pinned_at=datetime.now(timezone.utc),
                    pinned_by=pinned_by,
                )
            )
            await session.execute(stmt)
            await session.commit()

            # Return the first updated model for the response
            query = (
                select(WorkflowScriptModel)
                .filter_by(organization_id=organization_id)
                .filter_by(workflow_permanent_id=workflow_permanent_id)
                .filter_by(cache_key_value=cache_key_value)
                .filter_by(deleted_at=None)
                .limit(1)
            )
            result = await session.scalars(query)
            return result.first()

    @db_operation("unpin_workflow_script")
    async def unpin_workflow_script(
        self,
        organization_id: str,
        workflow_permanent_id: str,
        cache_key_value: str,
    ) -> WorkflowScriptModel | None:
        """Unpin workflow scripts for a given cache key value."""
        async with self.Session() as session:
            stmt = (
                update(WorkflowScriptModel)
                .where(
                    WorkflowScriptModel.organization_id == organization_id,
                    WorkflowScriptModel.workflow_permanent_id == workflow_permanent_id,
                    WorkflowScriptModel.cache_key_value == cache_key_value,
                    WorkflowScriptModel.deleted_at.is_(None),
                )
                .values(
                    is_pinned=False,
                    pinned_at=None,
                    pinned_by=None,
                )
            )
            await session.execute(stmt)
            await session.commit()

            # Return the first updated model for the response
            query = (
                select(WorkflowScriptModel)
                .filter_by(organization_id=organization_id)
                .filter_by(workflow_permanent_id=workflow_permanent_id)
                .filter_by(cache_key_value=cache_key_value)
                .filter_by(deleted_at=None)
                .limit(1)
            )
            result = await session.scalars(query)
            return result.first()

    # -- Script Fallback Episode CRUD --------------------------------------

    @db_operation("create_fallback_episode")
    async def create_fallback_episode(
        self,
        organization_id: str,
        workflow_permanent_id: str,
        workflow_run_id: str,
        block_label: str,
        fallback_type: str,
        script_revision_id: str | None = None,
        error_message: str | None = None,
        classify_result: str | None = None,
        agent_actions: list | dict | None = None,
        page_url: str | None = None,
        page_text_snapshot: str | None = None,
    ) -> ScriptFallbackEpisode:
        async with self.Session() as session:
            episode = ScriptFallbackEpisodeModel(
                organization_id=organization_id,
                workflow_permanent_id=workflow_permanent_id,
                workflow_run_id=workflow_run_id,
                block_label=block_label,
                fallback_type=fallback_type,
                script_revision_id=script_revision_id,
                error_message=sanitize_postgres_text(error_message) if error_message else None,
                classify_result=sanitize_postgres_text(classify_result) if classify_result else None,
                agent_actions=agent_actions,
                page_url=sanitize_postgres_text(page_url) if page_url else None,
                page_text_snapshot=sanitize_postgres_text(page_text_snapshot) if page_text_snapshot else None,
            )
            session.add(episode)
            await session.commit()
            await session.refresh(episode)
            return ScriptFallbackEpisode.model_validate(episode)

    @db_operation("get_unreviewed_episodes")
    async def get_unreviewed_episodes(
        self,
        workflow_permanent_id: str,
        organization_id: str,
        limit: int = 100,
        script_revision_id: str | None = None,
    ) -> list[ScriptFallbackEpisode]:
        async with self.Session() as session:
            query = (
                select(ScriptFallbackEpisodeModel)
                .filter_by(
                    organization_id=organization_id,
                    workflow_permanent_id=workflow_permanent_id,
                    reviewed=False,
                )
                .order_by(ScriptFallbackEpisodeModel.created_at.asc())
                .limit(limit)
            )
            if script_revision_id:
                query = query.filter_by(script_revision_id=script_revision_id)
            episodes = (await session.scalars(query)).all()
            return [ScriptFallbackEpisode.model_validate(e) for e in episodes]

    @db_operation("update_fallback_episode")
    async def update_fallback_episode(
        self,
        episode_id: str,
        organization_id: str,
        agent_actions: list | dict | None = None,
        fallback_succeeded: bool | None = None,
    ) -> None:
        values: dict = {}
        if agent_actions is not None:
            values["agent_actions"] = agent_actions
        if fallback_succeeded is not None:
            values["fallback_succeeded"] = fallback_succeeded
        if not values:
            return
        values["modified_at"] = datetime.now(timezone.utc)
        async with self.Session() as session:
            await session.execute(
                update(ScriptFallbackEpisodeModel)
                .where(ScriptFallbackEpisodeModel.episode_id == episode_id)
                .where(ScriptFallbackEpisodeModel.organization_id == organization_id)
                .values(**values)
            )
            await session.commit()

    @db_operation("delete_fallback_episode")
    async def delete_fallback_episode(
        self,
        episode_id: str,
        organization_id: str,
    ) -> None:
        async with self.Session() as session:
            await session.execute(
                delete(ScriptFallbackEpisodeModel)
                .where(ScriptFallbackEpisodeModel.episode_id == episode_id)
                .where(ScriptFallbackEpisodeModel.organization_id == organization_id)
            )
            await session.commit()

    @db_operation("get_fallback_episodes")
    async def get_fallback_episodes(
        self,
        organization_id: str,
        workflow_permanent_id: str,
        page: int = 1,
        page_size: int = 20,
        workflow_run_id: str | None = None,
        block_label: str | None = None,
        reviewed: bool | None = None,
        fallback_type: str | None = None,
    ) -> list[ScriptFallbackEpisode]:
        async with self.Session() as session:
            query = select(ScriptFallbackEpisodeModel).filter(
                ScriptFallbackEpisodeModel.organization_id == organization_id,
                ScriptFallbackEpisodeModel.workflow_permanent_id == workflow_permanent_id,
            )
            if workflow_run_id is not None:
                query = query.filter(ScriptFallbackEpisodeModel.workflow_run_id == workflow_run_id)
            if block_label is not None:
                query = query.filter(ScriptFallbackEpisodeModel.block_label == block_label)
            if reviewed is not None:
                query = query.filter(ScriptFallbackEpisodeModel.reviewed == reviewed)
            if fallback_type is not None:
                query = query.filter(ScriptFallbackEpisodeModel.fallback_type == fallback_type)

            offset = (page - 1) * page_size
            query = query.order_by(ScriptFallbackEpisodeModel.created_at.desc()).limit(page_size).offset(offset)

            result = await session.scalars(query)
            return [ScriptFallbackEpisode.model_validate(row) for row in result.all()]

    @db_operation("get_fallback_episodes_count")
    async def get_fallback_episodes_count(
        self,
        organization_id: str,
        workflow_permanent_id: str | None = None,
        workflow_run_id: str | None = None,
        block_label: str | None = None,
        reviewed: bool | None = None,
        fallback_type: str | None = None,
        script_revision_id: str | None = None,
    ) -> int:
        """Count fallback episodes matching the given filters.

        At least one scoping filter (workflow_permanent_id, workflow_run_id,
        or script_revision_id) should be provided. Without any, this returns
        the total count for the entire organization which is rarely intended.
        """
        if workflow_permanent_id is None and workflow_run_id is None and script_revision_id is None:
            LOG.warning(
                "get_fallback_episodes_count called without any scoping filter",
                organization_id=organization_id,
            )
        async with self.Session() as session:
            query = (
                select(func.count())
                .select_from(ScriptFallbackEpisodeModel)
                .filter(
                    ScriptFallbackEpisodeModel.organization_id == organization_id,
                )
            )
            if workflow_permanent_id is not None:
                query = query.filter(ScriptFallbackEpisodeModel.workflow_permanent_id == workflow_permanent_id)
            if workflow_run_id is not None:
                query = query.filter(ScriptFallbackEpisodeModel.workflow_run_id == workflow_run_id)
            if block_label is not None:
                query = query.filter(ScriptFallbackEpisodeModel.block_label == block_label)
            if reviewed is not None:
                query = query.filter(ScriptFallbackEpisodeModel.reviewed == reviewed)
            if fallback_type is not None:
                query = query.filter(ScriptFallbackEpisodeModel.fallback_type == fallback_type)
            if script_revision_id is not None:
                query = query.filter(ScriptFallbackEpisodeModel.script_revision_id == script_revision_id)

            result = await session.scalar(query)
            return result or 0

    @db_operation("get_fallback_episode")
    async def get_fallback_episode(
        self,
        episode_id: str,
        organization_id: str,
    ) -> ScriptFallbackEpisode | None:
        async with self.Session() as session:
            query = select(ScriptFallbackEpisodeModel).filter(
                ScriptFallbackEpisodeModel.episode_id == episode_id,
                ScriptFallbackEpisodeModel.organization_id == organization_id,
            )
            result = await session.scalar(query)
            if result:
                return ScriptFallbackEpisode.model_validate(result)
            return None

    @db_operation("mark_episode_reviewed")
    async def mark_episode_reviewed(
        self,
        episode_id: str,
        organization_id: str,
        reviewer_output: str | None = None,
        new_script_revision_id: str | None = None,
    ) -> None:
        async with self.Session() as session:
            await session.execute(
                update(ScriptFallbackEpisodeModel)
                .where(ScriptFallbackEpisodeModel.episode_id == episode_id)
                .where(ScriptFallbackEpisodeModel.organization_id == organization_id)
                .values(
                    reviewed=True,
                    reviewer_output=sanitize_postgres_text(reviewer_output) if reviewer_output else None,
                    new_script_revision_id=new_script_revision_id,
                    modified_at=datetime.now(timezone.utc),
                )
            )
            await session.commit()

    @db_operation("get_recent_reviewed_episodes")
    async def get_recent_reviewed_episodes(
        self,
        workflow_permanent_id: str,
        organization_id: str,
        limit: int = 20,
    ) -> list[ScriptFallbackEpisode]:
        """Return recently reviewed episodes for cross-run historical context.

        These give the reviewer visibility into past failures and fixes so it can
        avoid repeating the same mistakes.
        """
        async with self.Session() as session:
            query = (
                select(ScriptFallbackEpisodeModel)
                .filter_by(
                    organization_id=organization_id,
                    workflow_permanent_id=workflow_permanent_id,
                    reviewed=True,
                )
                .order_by(ScriptFallbackEpisodeModel.created_at.desc())
                .limit(limit)
            )
            episodes = (await session.scalars(query)).all()
            return [ScriptFallbackEpisode.model_validate(e) for e in episodes]

    @db_operation("record_branch_hit")
    async def record_branch_hit(
        self,
        organization_id: str,
        workflow_permanent_id: str,
        block_label: str,
        branch_key: str,
    ) -> None:
        """Record a classify branch hit, upserting the hit count and last_hit_at."""
        now = datetime.now(timezone.utc)
        async with self.Session() as session:
            stmt = insert(ScriptBranchHitModel).values(
                organization_id=organization_id,
                workflow_permanent_id=workflow_permanent_id,
                block_label=block_label,
                branch_key=branch_key,
                hit_count=1,
                first_hit_at=now,
                last_hit_at=now,
            )
            stmt = stmt.on_conflict_do_update(
                index_elements=[
                    "organization_id",
                    "workflow_permanent_id",
                    "block_label",
                    "branch_key",
                ],
                set_={
                    "hit_count": ScriptBranchHitModel.hit_count + 1,
                    "last_hit_at": now,
                },
            )
            await session.execute(stmt)
            await session.commit()

    @db_operation("get_stale_branches")
    async def get_stale_branches(
        self,
        organization_id: str,
        workflow_permanent_id: str,
        stale_days: int = 90,
        limit: int = 200,
    ) -> list[ScriptBranchHit]:
        """Get branches that haven't been accessed in stale_days days."""
        cutoff = datetime.now(timezone.utc) - timedelta(days=stale_days)
        async with self.Session() as session:
            query = (
                select(ScriptBranchHitModel)
                .filter_by(
                    organization_id=organization_id,
                    workflow_permanent_id=workflow_permanent_id,
                )
                .filter(ScriptBranchHitModel.last_hit_at < cutoff)
                .order_by(ScriptBranchHitModel.last_hit_at.asc())
                .limit(limit)
            )
            results = (await session.scalars(query)).all()
            return [ScriptBranchHit.model_validate(r) for r in results]
