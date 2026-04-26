from __future__ import annotations

import datetime
from typing import TYPE_CHECKING, Callable

import structlog
from sqlalchemy import and_, delete, or_, select, update
from sqlalchemy.exc import SQLAlchemyError

from skyvern.forge.sdk.artifact.models import Artifact, ArtifactType
from skyvern.forge.sdk.db._error_handling import db_operation
from skyvern.forge.sdk.db.base_repository import BaseRepository
from skyvern.forge.sdk.db.models import ActionModel, ArtifactModel
from skyvern.forge.sdk.db.protocols import RunReader
from skyvern.forge.sdk.db.utils import convert_to_artifact

if TYPE_CHECKING:
    from skyvern.forge.sdk.db.base_alchemy_db import _SessionFactory

LOG = structlog.get_logger()


class ArtifactsRepository(BaseRepository):
    """Database operations for artifact management."""

    def __init__(
        self,
        session_factory: _SessionFactory,
        debug_enabled: bool = False,
        is_retryable_error_fn: Callable[[SQLAlchemyError], bool] | None = None,
        run_reader: RunReader | None = None,
    ) -> None:
        super().__init__(session_factory, debug_enabled, is_retryable_error_fn)
        self._run_reader = run_reader

    @db_operation("create_artifact")
    async def create_artifact(
        self,
        artifact_id: str,
        artifact_type: str,
        uri: str,
        organization_id: str,
        step_id: str | None = None,
        task_id: str | None = None,
        workflow_run_id: str | None = None,
        workflow_run_block_id: str | None = None,
        task_v2_id: str | None = None,
        run_id: str | None = None,
        thought_id: str | None = None,
        ai_suggestion_id: str | None = None,
        checksum: str | None = None,
        browser_session_id: str | None = None,
    ) -> Artifact:
        async with self.Session() as session:
            new_artifact = ArtifactModel(
                artifact_id=artifact_id,
                artifact_type=artifact_type,
                uri=uri,
                task_id=task_id,
                step_id=step_id,
                workflow_run_id=workflow_run_id,
                workflow_run_block_id=workflow_run_block_id,
                observer_cruise_id=task_v2_id,
                observer_thought_id=thought_id,
                run_id=run_id,
                ai_suggestion_id=ai_suggestion_id,
                organization_id=organization_id,
                checksum=checksum,
                browser_session_id=browser_session_id,
            )
            session.add(new_artifact)
            await session.commit()
            await session.refresh(new_artifact)
            return convert_to_artifact(new_artifact, self.debug_enabled)

    @db_operation("bulk_create_artifacts")
    async def bulk_create_artifacts(
        self,
        artifact_models: list[ArtifactModel],
    ) -> list[Artifact]:
        """
        Bulk create multiple artifacts in a single database transaction.

        Args:
            artifact_models: List of ArtifactModel instances to insert

        Returns:
            List of created Artifact objects
        """
        if not artifact_models:
            return []

        async with self.Session() as session:
            session.add_all(artifact_models)
            await session.commit()

            # Refresh all artifacts to get their created_at and modified_at values
            for artifact in artifact_models:
                await session.refresh(artifact)

            return [convert_to_artifact(artifact, self.debug_enabled) for artifact in artifact_models]

    @db_operation("get_artifacts_for_task_v2")
    async def get_artifacts_for_task_v2(
        self,
        task_v2_id: str,
        organization_id: str | None = None,
        artifact_types: list[ArtifactType] | None = None,
    ) -> list[Artifact]:
        async with self.Session() as session:
            query = (
                select(ArtifactModel)
                .filter_by(observer_cruise_id=task_v2_id)
                .filter_by(organization_id=organization_id)
            )
            if artifact_types:
                query = query.filter(ArtifactModel.artifact_type.in_(artifact_types))

            query = query.order_by(ArtifactModel.created_at)
            if artifacts := (await session.scalars(query)).all():
                return [convert_to_artifact(artifact, self.debug_enabled) for artifact in artifacts]
            else:
                return []

    @db_operation("get_artifacts_for_task_step")
    async def get_artifacts_for_task_step(
        self,
        task_id: str,
        step_id: str,
        organization_id: str | None = None,
    ) -> list[Artifact]:
        async with self.Session() as session:
            if artifacts := (
                await session.scalars(
                    select(ArtifactModel)
                    .filter_by(task_id=task_id)
                    .filter_by(step_id=step_id)
                    .filter_by(organization_id=organization_id)
                    .order_by(ArtifactModel.created_at)
                )
            ).all():
                return [convert_to_artifact(artifact, self.debug_enabled) for artifact in artifacts]
            else:
                return []

    @db_operation("get_artifacts_for_run")
    async def get_artifacts_for_run(
        self,
        run_id: str,
        organization_id: str,
        artifact_types: list[ArtifactType] | None = None,
        group_by_type: bool = False,
        sort_by: str = "created_at",
    ) -> dict[ArtifactType, list[Artifact]] | list[Artifact]:
        """Return artifacts associated with a run.

        Args:
            run_id: The ID of the run to get artifacts for
            organization_id: The ID of the organization that owns the run
            artifact_types: Optional list of artifact types to filter by
            group_by_type: If True, returns a dictionary mapping artifact types to lists of artifacts.
                         If False, returns a flat list of artifacts. Defaults to False.
            sort_by: Field to sort artifacts by. Must be one of: 'created_at', 'step_id', 'task_id'.
                   Defaults to 'created_at'.

        Returns:
            If group_by_type is True, returns a dictionary mapping artifact types to lists of artifacts.
            If group_by_type is False, returns a list of artifacts sorted by the specified field.

        Raises:
            ValueError: If sort_by is not one of the allowed values
        """
        allowed_sort_fields = {"created_at", "step_id", "task_id"}
        if sort_by not in allowed_sort_fields:
            raise ValueError(f"sort_by must be one of {allowed_sort_fields}")
        if self._run_reader is None:
            raise RuntimeError("run_reader dependency not set")
        run = await self._run_reader.get_run(run_id, organization_id=organization_id)

        async with self.Session() as session:
            query = select(ArtifactModel).filter_by(organization_id=organization_id)

            if run:
                # Workflow run — filter by workflow_run_id
                query = query.filter_by(workflow_run_id=run.workflow_run_id)
            elif run_id.startswith("tsk_"):
                # Task run — _run_reader only handles workflow runs,
                # so fall back to filtering by task_id for task-based artifacts
                query = query.filter_by(task_id=run_id)
            else:
                return []

            if artifact_types:
                query = query.filter(ArtifactModel.artifact_type.in_(artifact_types))

            # Apply sorting
            if sort_by == "created_at":
                query = query.order_by(ArtifactModel.created_at)
            elif sort_by == "step_id":
                query = query.order_by(ArtifactModel.step_id, ArtifactModel.created_at)
            elif sort_by == "task_id":
                query = query.order_by(ArtifactModel.task_id, ArtifactModel.created_at)

            # Execute query and convert to Artifact objects
            artifacts = [
                convert_to_artifact(artifact, self.debug_enabled) for artifact in (await session.scalars(query)).all()
            ]

            # Group artifacts by type if requested
            if group_by_type:
                result: dict[ArtifactType, list[Artifact]] = {}
                for artifact in artifacts:
                    if artifact.artifact_type not in result:
                        result[artifact.artifact_type] = []
                    result[artifact.artifact_type].append(artifact)
                return result

            return artifacts

    @db_operation("get_artifact_by_id")
    async def get_artifact_by_id(
        self,
        artifact_id: str,
        organization_id: str,
    ) -> Artifact | None:
        async with self.Session() as session:
            if artifact := (
                await session.scalars(
                    select(ArtifactModel).filter_by(artifact_id=artifact_id).filter_by(organization_id=organization_id)
                )
            ).first():
                return convert_to_artifact(artifact, self.debug_enabled)
            else:
                return None

    async def get_artifact_by_id_no_org(
        self,
        artifact_id: str,
    ) -> Artifact | None:
        """Fetch an artifact by ID without an organization filter.

        Only use this when the caller has already verified authorization through
        an out-of-band mechanism (e.g. a valid HMAC-signed URL).
        """
        async with self.Session() as session:
            if artifact := (await session.scalars(select(ArtifactModel).filter_by(artifact_id=artifact_id))).first():
                return convert_to_artifact(artifact, self.debug_enabled)
            else:
                return None

    @db_operation("get_artifacts_by_ids")
    async def get_artifacts_by_ids(
        self,
        artifact_ids: list[str],
        organization_id: str,
    ) -> list[Artifact]:
        if not artifact_ids:
            return []
        async with self.Session() as session:
            artifacts = (
                await session.scalars(
                    select(ArtifactModel)
                    .filter(ArtifactModel.artifact_id.in_(artifact_ids))
                    .filter_by(organization_id=organization_id)
                )
            ).all()
            return [convert_to_artifact(artifact, self.debug_enabled) for artifact in artifacts]

    @db_operation("get_artifacts_by_entity_id")
    async def get_artifacts_by_entity_id(
        self,
        *,
        organization_id: str | None,
        artifact_type: ArtifactType | None = None,
        task_id: str | None = None,
        step_id: str | None = None,
        workflow_run_id: str | None = None,
        workflow_run_block_id: str | None = None,
        thought_id: str | None = None,
        task_v2_id: str | None = None,
        limit: int | None = None,
    ) -> list[Artifact]:
        async with self.Session() as session:
            # Build base query
            query = select(ArtifactModel)

            if artifact_type is not None:
                query = query.filter_by(artifact_type=artifact_type)
            if task_id is not None:
                query = query.filter_by(task_id=task_id)
            if step_id is not None:
                query = query.filter_by(step_id=step_id)
            if workflow_run_id is not None:
                query = query.filter_by(workflow_run_id=workflow_run_id)
            if workflow_run_block_id is not None:
                query = query.filter_by(workflow_run_block_id=workflow_run_block_id)
            if thought_id is not None:
                query = query.filter_by(observer_thought_id=thought_id)
            if task_v2_id is not None:
                query = query.filter_by(observer_cruise_id=task_v2_id)
            # Handle backward compatibility where old artifact rows were stored with organization_id NULL
            if organization_id is not None:
                query = query.filter(
                    or_(ArtifactModel.organization_id == organization_id, ArtifactModel.organization_id.is_(None))
                )

            query = query.order_by(ArtifactModel.created_at.desc())

            if limit is not None:
                query = query.limit(limit)

            artifacts = (await session.scalars(query)).all()
            LOG.debug("Artifacts fetched", count=len(artifacts))
            return [convert_to_artifact(a, self.debug_enabled) for a in artifacts]

    @db_operation("get_artifact_by_entity_id")
    async def get_artifact_by_entity_id(
        self,
        *,
        artifact_type: ArtifactType,
        organization_id: str,
        task_id: str | None = None,
        step_id: str | None = None,
        workflow_run_id: str | None = None,
        workflow_run_block_id: str | None = None,
        thought_id: str | None = None,
        task_v2_id: str | None = None,
    ) -> Artifact | None:
        artifacts = await self.get_artifacts_by_entity_id(
            organization_id=organization_id,
            artifact_type=artifact_type,
            task_id=task_id,
            step_id=step_id,
            workflow_run_id=workflow_run_id,
            workflow_run_block_id=workflow_run_block_id,
            thought_id=thought_id,
            task_v2_id=task_v2_id,
            limit=1,
        )
        return artifacts[0] if artifacts else None

    @db_operation("get_artifact")
    async def get_artifact(
        self,
        task_id: str,
        step_id: str,
        artifact_type: ArtifactType,
        organization_id: str | None = None,
    ) -> Artifact | None:
        async with self.Session() as session:
            artifact = (
                await session.scalars(
                    select(ArtifactModel)
                    .filter_by(task_id=task_id)
                    .filter_by(step_id=step_id)
                    .filter_by(organization_id=organization_id)
                    .filter_by(artifact_type=artifact_type)
                    .order_by(ArtifactModel.created_at.desc())
                )
            ).first()
            if artifact:
                return convert_to_artifact(artifact, self.debug_enabled)
            return None

    @db_operation("find_download_artifact")
    async def find_download_artifact(
        self,
        organization_id: str,
        run_id: str,
        uri: str,
    ) -> Artifact | None:
        """Return the existing DOWNLOAD artifact for ``(run_id, uri)`` if any.

        Used by :meth:`ArtifactManager.create_download_artifact` to stay
        idempotent: repeated saves of the same file in the same run (e.g.
        within a loop block iteration) must reuse the existing artifact_id
        so downstream URL-based dedup keeps seeing a stable URL.
        """
        async with self.Session() as session:
            artifact = (
                await session.scalars(
                    select(ArtifactModel)
                    .filter(ArtifactModel.run_id == run_id)
                    .filter(ArtifactModel.artifact_type == ArtifactType.DOWNLOAD)
                    .filter(ArtifactModel.organization_id == organization_id)
                    .filter(ArtifactModel.uri == uri)
                    .order_by(ArtifactModel.created_at.desc())
                )
            ).first()
            if artifact:
                return convert_to_artifact(artifact, self.debug_enabled)
            return None

    @db_operation("list_artifacts_for_run_by_type")
    async def list_artifacts_for_run_by_type(
        self,
        run_id: str,
        organization_id: str,
        artifact_type: ArtifactType,
    ) -> list[Artifact]:
        """List all artifacts for a run filtered by type, using the dedicated ``run_id`` column.

        Unlike :meth:`get_artifacts_for_run` this does not consult a ``RunReader`` —
        it filters directly on the partial index ``ix_artifacts_run_id_partial`` and
        returns the rows ordered by creation time.
        """
        async with self.Session() as session:
            artifacts = (
                await session.scalars(
                    select(ArtifactModel)
                    .filter(ArtifactModel.run_id == run_id)
                    .filter(ArtifactModel.artifact_type == artifact_type)
                    .filter(ArtifactModel.organization_id == organization_id)
                    .order_by(ArtifactModel.created_at)
                )
            ).all()
            return [convert_to_artifact(a, self.debug_enabled) for a in artifacts]

    @db_operation("find_artifact_for_browser_session")
    async def find_artifact_for_browser_session(
        self,
        organization_id: str,
        browser_session_id: str,
        uri: str,
        artifact_type: ArtifactType,
    ) -> Artifact | None:
        """Return the existing artifact row for ``(browser_session_id, uri)`` if any.

        Used by :meth:`ArtifactManager.create_browser_session_download_artifact`
        to stay idempotent: the watcher fires repeatedly as a downloaded file
        grows, so we look up the existing row before inserting.
        """
        async with self.Session() as session:
            artifact = (
                await session.scalars(
                    select(ArtifactModel)
                    .filter(ArtifactModel.browser_session_id == browser_session_id)
                    .filter(ArtifactModel.artifact_type == artifact_type)
                    .filter(ArtifactModel.organization_id == organization_id)
                    .filter(ArtifactModel.uri == uri)
                    .order_by(ArtifactModel.created_at.desc())
                )
            ).first()
            if artifact:
                return convert_to_artifact(artifact, self.debug_enabled)
            return None

    @db_operation("list_artifacts_for_browser_session_by_type")
    async def list_artifacts_for_browser_session_by_type(
        self,
        browser_session_id: str,
        organization_id: str,
        artifact_type: ArtifactType,
    ) -> list[Artifact]:
        """List all artifacts for a browser session filtered by type.

        Filters directly on the partial index ``ix_artifacts_browser_session_id_partial``
        and returns the rows ordered by creation time. Used by the
        ``GET /v1/browser_sessions/{id}`` read path.
        """
        async with self.Session() as session:
            artifacts = (
                await session.scalars(
                    select(ArtifactModel)
                    .filter(ArtifactModel.browser_session_id == browser_session_id)
                    .filter(ArtifactModel.artifact_type == artifact_type)
                    .filter(ArtifactModel.organization_id == organization_id)
                    .order_by(ArtifactModel.created_at)
                )
            ).all()
            return [convert_to_artifact(a, self.debug_enabled) for a in artifacts]

    @db_operation("claim_session_download_artifacts_for_run")
    async def claim_session_download_artifacts_for_run(
        self,
        run_id: str,
        browser_session_id: str,
        organization_id: str,
        run_started_at: datetime.datetime,
    ) -> int:
        """Tag session-scoped DOWNLOAD artifacts that landed during this run with ``run_id``.

        Called at run finalization. ``occupy_browser_session`` ensures at
        most one run is active on a session at a time, so the time-window
        match is unambiguous.

        Returns the number of rows updated. Idempotent: re-running picks up
        only ``run_id IS NULL`` rows, so a retry after success is a no-op.
        """
        async with self.Session() as session:
            result = await session.execute(
                update(ArtifactModel)
                .where(ArtifactModel.browser_session_id == browser_session_id)
                .where(ArtifactModel.organization_id == organization_id)
                .where(ArtifactModel.artifact_type == ArtifactType.DOWNLOAD)
                .where(ArtifactModel.run_id.is_(None))
                .where(ArtifactModel.created_at >= run_started_at)
                .values(run_id=run_id)
            )
            await session.commit()
            return result.rowcount or 0

    @db_operation("delete_artifact_for_browser_session")
    async def delete_artifact_for_browser_session(
        self,
        organization_id: str,
        browser_session_id: str,
        uri: str,
        artifact_type: ArtifactType,
    ) -> int:
        """Delete the artifact row for ``(browser_session_id, uri)`` if any.

        Mirror of :meth:`find_artifact_for_browser_session` for the watcher's
        ``Change.deleted`` path: when the user/browser removes a downloaded
        file we drop the row too, otherwise the next API read would return a
        signed URL pointing at a deleted S3 object.

        Returns the number of rows removed (0 or 1). Safe to call when no row
        exists.
        """
        async with self.Session() as session:
            result = await session.execute(
                delete(ArtifactModel)
                .where(ArtifactModel.browser_session_id == browser_session_id)
                .where(ArtifactModel.organization_id == organization_id)
                .where(ArtifactModel.artifact_type == artifact_type)
                .where(ArtifactModel.uri == uri)
            )
            await session.commit()
            return result.rowcount or 0

    @db_operation("get_artifact_for_run")
    async def get_artifact_for_run(
        self,
        run_id: str,
        artifact_type: ArtifactType,
        organization_id: str | None = None,
    ) -> Artifact | None:
        async with self.Session() as session:
            artifact = (
                await session.scalars(
                    select(ArtifactModel)
                    .filter(ArtifactModel.run_id == run_id)
                    .filter(ArtifactModel.artifact_type == artifact_type)
                    .filter(ArtifactModel.organization_id == organization_id)
                    .order_by(ArtifactModel.created_at.desc())
                    .limit(1)
                )
            ).first()
            if artifact:
                return convert_to_artifact(artifact, self.debug_enabled)
            return None

    @db_operation("get_latest_artifact")
    async def get_latest_artifact(
        self,
        task_id: str,
        step_id: str | None = None,
        artifact_types: list[ArtifactType] | None = None,
        organization_id: str | None = None,
    ) -> Artifact | None:
        artifacts = await self.get_latest_n_artifacts(
            task_id=task_id,
            step_id=step_id,
            artifact_types=artifact_types,
            organization_id=organization_id,
            n=1,
        )
        if artifacts:
            return artifacts[0]
        return None

    @db_operation("get_latest_n_artifacts")
    async def get_latest_n_artifacts(
        self,
        task_id: str,
        step_id: str | None = None,
        artifact_types: list[ArtifactType] | None = None,
        organization_id: str | None = None,
        n: int = 1,
    ) -> list[Artifact] | None:
        async with self.Session() as session:
            artifact_query = select(ArtifactModel).filter_by(task_id=task_id)
            if organization_id:
                artifact_query = artifact_query.filter_by(organization_id=organization_id)
            if step_id:
                artifact_query = artifact_query.filter_by(step_id=step_id)
            if artifact_types:
                artifact_query = artifact_query.filter(ArtifactModel.artifact_type.in_(artifact_types))

            artifacts = (await session.scalars(artifact_query.order_by(ArtifactModel.created_at.desc()))).fetchmany(n)
            if artifacts:
                return [convert_to_artifact(artifact, self.debug_enabled) for artifact in artifacts]
            return None

    @db_operation("delete_task_artifacts")
    async def delete_task_artifacts(self, organization_id: str, task_id: str) -> None:
        async with self.Session() as session:
            # delete artifacts by filtering organization_id and task_id
            stmt = delete(ArtifactModel).where(
                and_(
                    ArtifactModel.organization_id == organization_id,
                    ArtifactModel.task_id == task_id,
                )
            )
            await session.execute(stmt)
            await session.commit()

    @db_operation("delete_task_v2_artifacts")
    async def delete_task_v2_artifacts(self, task_v2_id: str, organization_id: str | None = None) -> None:
        async with self.Session() as session:
            stmt = delete(ArtifactModel).where(
                and_(
                    ArtifactModel.observer_cruise_id == task_v2_id,
                    ArtifactModel.organization_id == organization_id,
                )
            )
            await session.execute(stmt)
            await session.commit()

    @db_operation("update_action_screenshot_artifact_id")
    async def update_action_screenshot_artifact_id(
        self, *, organization_id: str, action_id: str, screenshot_artifact_id: str
    ) -> None:
        async with self.Session() as session:
            await session.execute(
                update(ActionModel)
                .where(ActionModel.action_id == action_id, ActionModel.organization_id == organization_id)
                .values(screenshot_artifact_id=screenshot_artifact_id)
            )
            await session.commit()
