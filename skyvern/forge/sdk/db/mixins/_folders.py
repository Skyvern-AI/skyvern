from __future__ import annotations

from datetime import datetime
from typing import TYPE_CHECKING

import structlog
from sqlalchemy import func, or_, select, update
from sqlalchemy.exc import SQLAlchemyError

from skyvern.forge.sdk.db.models import FolderModel, WorkflowModel
from skyvern.forge.sdk.db.utils import convert_to_workflow
from skyvern.forge.sdk.workflow.models.workflow import Workflow

if TYPE_CHECKING:
    from skyvern.forge.sdk.db.base_alchemy_db import BaseAlchemyDB

LOG = structlog.get_logger()


class FoldersMixin:
    """Mixin providing folder database operations.

    Requires: self.Session, self.debug_enabled, self.get_workflow_by_permanent_id (from AgentDB)
    """

    async def create_folder(
        self: BaseAlchemyDB,
        organization_id: str,
        title: str,
        description: str | None = None,
    ) -> FolderModel:
        """Create a new folder."""
        try:
            async with self.Session() as session:
                folder = FolderModel(
                    organization_id=organization_id,
                    title=title,
                    description=description,
                )
                session.add(folder)
                await session.commit()
                await session.refresh(folder)
                return folder
        except SQLAlchemyError:
            LOG.error("SQLAlchemyError in create_folder", exc_info=True)
            raise

    async def get_folders(
        self: BaseAlchemyDB,
        organization_id: str,
        page: int = 1,
        page_size: int = 10,
        search_query: str | None = None,
    ) -> list[FolderModel]:
        """Get all folders for an organization with pagination and optional search."""
        try:
            async with self.Session() as session:
                stmt = (
                    select(FolderModel)
                    .filter_by(organization_id=organization_id)
                    .filter(FolderModel.deleted_at.is_(None))
                )

                if search_query:
                    search_pattern = f"%{search_query}%"
                    stmt = stmt.filter(
                        or_(
                            FolderModel.title.ilike(search_pattern),
                            FolderModel.description.ilike(search_pattern),
                        )
                    )

                stmt = stmt.order_by(FolderModel.modified_at.desc())
                stmt = stmt.offset((page - 1) * page_size).limit(page_size)

                result = await session.execute(stmt)
                return list(result.scalars().all())
        except SQLAlchemyError:
            LOG.error("SQLAlchemyError in get_folders", exc_info=True)
            raise

    async def get_folder(
        self: BaseAlchemyDB,
        folder_id: str,
        organization_id: str,
    ) -> FolderModel | None:
        """Get a folder by ID."""
        try:
            async with self.Session() as session:
                stmt = (
                    select(FolderModel)
                    .filter_by(folder_id=folder_id, organization_id=organization_id)
                    .filter(FolderModel.deleted_at.is_(None))
                )
                result = await session.execute(stmt)
                return result.scalar_one_or_none()
        except SQLAlchemyError:
            LOG.error("SQLAlchemyError in get_folder", exc_info=True)
            raise

    async def update_folder(
        self: BaseAlchemyDB,
        folder_id: str,
        organization_id: str,
        title: str | None = None,
        description: str | None = None,
    ) -> FolderModel | None:
        """Update a folder's title or description."""
        try:
            async with self.Session() as session:
                stmt = (
                    select(FolderModel)
                    .filter_by(folder_id=folder_id, organization_id=organization_id)
                    .filter(FolderModel.deleted_at.is_(None))
                )
                result = await session.execute(stmt)
                folder = result.scalar_one_or_none()
                if not folder:
                    return None

                if title is not None:
                    folder.title = title
                if description is not None:
                    folder.description = description

                folder.modified_at = datetime.utcnow()
                await session.commit()
                await session.refresh(folder)
                return folder
        except SQLAlchemyError:
            LOG.error("SQLAlchemyError in update_folder", exc_info=True)
            raise

    async def get_workflow_permanent_ids_in_folder(
        self: BaseAlchemyDB,
        folder_id: str,
        organization_id: str,
    ) -> list[str]:
        """Get workflow permanent IDs (latest versions only) in a folder."""
        try:
            async with self.Session() as session:
                # Subquery to get the latest version for each workflow
                subquery = (
                    select(
                        WorkflowModel.organization_id,
                        WorkflowModel.workflow_permanent_id,
                        func.max(WorkflowModel.version).label("max_version"),
                    )
                    .where(WorkflowModel.organization_id == organization_id)
                    .where(WorkflowModel.deleted_at.is_(None))
                    .group_by(
                        WorkflowModel.organization_id,
                        WorkflowModel.workflow_permanent_id,
                    )
                    .subquery()
                )

                # Get workflow_permanent_ids where the latest version is in this folder
                stmt = (
                    select(WorkflowModel.workflow_permanent_id)
                    .join(
                        subquery,
                        (WorkflowModel.organization_id == subquery.c.organization_id)
                        & (WorkflowModel.workflow_permanent_id == subquery.c.workflow_permanent_id)
                        & (WorkflowModel.version == subquery.c.max_version),
                    )
                    .where(WorkflowModel.folder_id == folder_id)
                )
                result = await session.execute(stmt)
                return list(result.scalars().all())
        except SQLAlchemyError:
            LOG.error("SQLAlchemyError in get_workflow_permanent_ids_in_folder", exc_info=True)
            raise

    async def soft_delete_folder(
        self: BaseAlchemyDB,
        folder_id: str,
        organization_id: str,
        delete_workflows: bool = False,
    ) -> bool:
        """Soft delete a folder. Optionally delete all workflows in the folder."""
        try:
            async with self.Session() as session:
                # Check if folder exists
                folder_stmt = (
                    select(FolderModel)
                    .filter_by(folder_id=folder_id, organization_id=organization_id)
                    .filter(FolderModel.deleted_at.is_(None))
                )
                folder_result = await session.execute(folder_stmt)
                folder = folder_result.scalar_one_or_none()
                if not folder:
                    return False

                # If delete_workflows is True, delete all workflows in the folder
                if delete_workflows:
                    # Get workflow permanent IDs in the folder (inline logic)
                    subquery = (
                        select(
                            WorkflowModel.organization_id,
                            WorkflowModel.workflow_permanent_id,
                            func.max(WorkflowModel.version).label("max_version"),
                        )
                        .where(WorkflowModel.organization_id == organization_id)
                        .where(WorkflowModel.deleted_at.is_(None))
                        .group_by(
                            WorkflowModel.organization_id,
                            WorkflowModel.workflow_permanent_id,
                        )
                        .subquery()
                    )

                    workflow_permanent_ids_stmt = (
                        select(WorkflowModel.workflow_permanent_id)
                        .join(
                            subquery,
                            (WorkflowModel.organization_id == subquery.c.organization_id)
                            & (WorkflowModel.workflow_permanent_id == subquery.c.workflow_permanent_id)
                            & (WorkflowModel.version == subquery.c.max_version),
                        )
                        .where(WorkflowModel.folder_id == folder_id)
                    )
                    result = await session.execute(workflow_permanent_ids_stmt)
                    workflow_permanent_ids = list(result.scalars().all())

                    # Soft delete all workflows with these permanent IDs in a single bulk update
                    if workflow_permanent_ids:
                        update_workflows_query = (
                            update(WorkflowModel)
                            .where(WorkflowModel.workflow_permanent_id.in_(workflow_permanent_ids))
                            .where(WorkflowModel.organization_id == organization_id)
                            .where(WorkflowModel.deleted_at.is_(None))
                            .values(deleted_at=datetime.utcnow())
                        )
                        await session.execute(update_workflows_query)
                else:
                    # Just remove folder_id from all workflows in this folder
                    update_workflows_query = (
                        update(WorkflowModel)
                        .where(WorkflowModel.folder_id == folder_id)
                        .where(WorkflowModel.organization_id == organization_id)
                        .values(folder_id=None, modified_at=datetime.utcnow())
                    )
                    await session.execute(update_workflows_query)

                # Soft delete the folder
                folder.deleted_at = datetime.utcnow()
                await session.commit()
                return True
        except SQLAlchemyError:
            LOG.error("SQLAlchemyError in soft_delete_folder", exc_info=True)
            raise

    async def get_folder_workflow_count(
        self: BaseAlchemyDB,
        folder_id: str,
        organization_id: str,
    ) -> int:
        """Get the count of workflows (latest versions only) in a folder."""
        try:
            async with self.Session() as session:
                # Subquery to get the latest version for each workflow (same pattern as get_workflows_by_organization_id)
                subquery = (
                    select(
                        WorkflowModel.organization_id,
                        WorkflowModel.workflow_permanent_id,
                        func.max(WorkflowModel.version).label("max_version"),
                    )
                    .where(WorkflowModel.organization_id == organization_id)
                    .where(WorkflowModel.deleted_at.is_(None))
                    .group_by(
                        WorkflowModel.organization_id,
                        WorkflowModel.workflow_permanent_id,
                    )
                    .subquery()
                )

                # Count workflows where the latest version is in this folder
                stmt = (
                    select(func.count(WorkflowModel.workflow_permanent_id))
                    .join(
                        subquery,
                        (WorkflowModel.organization_id == subquery.c.organization_id)
                        & (WorkflowModel.workflow_permanent_id == subquery.c.workflow_permanent_id)
                        & (WorkflowModel.version == subquery.c.max_version),
                    )
                    .where(WorkflowModel.folder_id == folder_id)
                )
                result = await session.execute(stmt)
                return result.scalar_one()
        except SQLAlchemyError:
            LOG.error("SQLAlchemyError in get_folder_workflow_count", exc_info=True)
            raise

    async def get_folder_workflow_counts_batch(
        self: BaseAlchemyDB,
        folder_ids: list[str],
        organization_id: str,
    ) -> dict[str, int]:
        """Get workflow counts for multiple folders in a single query."""
        try:
            async with self.Session() as session:
                # Subquery to get the latest version for each workflow
                subquery = (
                    select(
                        WorkflowModel.organization_id,
                        WorkflowModel.workflow_permanent_id,
                        func.max(WorkflowModel.version).label("max_version"),
                    )
                    .where(WorkflowModel.organization_id == organization_id)
                    .where(WorkflowModel.deleted_at.is_(None))
                    .group_by(
                        WorkflowModel.organization_id,
                        WorkflowModel.workflow_permanent_id,
                    )
                    .subquery()
                )

                # Count workflows grouped by folder_id
                stmt = (
                    select(
                        WorkflowModel.folder_id,
                        func.count(WorkflowModel.workflow_permanent_id).label("count"),
                    )
                    .join(
                        subquery,
                        (WorkflowModel.organization_id == subquery.c.organization_id)
                        & (WorkflowModel.workflow_permanent_id == subquery.c.workflow_permanent_id)
                        & (WorkflowModel.version == subquery.c.max_version),
                    )
                    .where(WorkflowModel.folder_id.in_(folder_ids))
                    .group_by(WorkflowModel.folder_id)
                )
                result = await session.execute(stmt)
                rows = result.all()

                # Convert to dict, defaulting to 0 for folders with no workflows
                return {row.folder_id: row.count for row in rows}
        except SQLAlchemyError:
            LOG.error("SQLAlchemyError in get_folder_workflow_counts_batch", exc_info=True)
            raise

    async def update_workflow_folder(
        self: BaseAlchemyDB,
        workflow_permanent_id: str,
        organization_id: str,
        folder_id: str | None,
    ) -> Workflow | None:
        """Update folder assignment for the latest version of a workflow."""
        try:
            # Get the latest version of the workflow
            latest_workflow = await self.get_workflow_by_permanent_id(
                workflow_permanent_id=workflow_permanent_id,
                organization_id=organization_id,
            )

            if not latest_workflow:
                return None

            async with self.Session() as session:
                # Validate folder exists in-org if folder_id is provided
                if folder_id:
                    stmt = (
                        select(FolderModel.folder_id)
                        .where(FolderModel.folder_id == folder_id)
                        .where(FolderModel.organization_id == organization_id)
                        .where(FolderModel.deleted_at.is_(None))
                    )
                    if (await session.scalar(stmt)) is None:
                        raise ValueError(f"Folder {folder_id} not found")

                workflow_model = await session.get(WorkflowModel, latest_workflow.workflow_id)
                if workflow_model:
                    workflow_model.folder_id = folder_id
                    workflow_model.modified_at = datetime.utcnow()

                    # Update folder's modified_at in the same transaction
                    if folder_id:
                        folder_model = await session.get(FolderModel, folder_id)
                        if folder_model:
                            folder_model.modified_at = datetime.utcnow()

                    await session.commit()
                    await session.refresh(workflow_model)

                    return convert_to_workflow(workflow_model, self.debug_enabled)
                return None
        except SQLAlchemyError:
            LOG.error("SQLAlchemyError in update_workflow_folder", exc_info=True)
            raise
