from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy import func, or_, select, update

from skyvern.forge.sdk.db._error_handling import db_operation
from skyvern.forge.sdk.db.base_repository import BaseRepository
from skyvern.forge.sdk.db.models import CredentialFolderModel, CredentialModel
from skyvern.forge.sdk.schemas.credentials import Credential


class CredentialFoldersRepository(BaseRepository):
    """Database operations for credential folder management."""

    @db_operation("create_credential_folder")
    async def create_credential_folder(
        self,
        organization_id: str,
        title: str,
        description: str | None = None,
    ) -> CredentialFolderModel:
        async with self.Session() as session:
            folder = CredentialFolderModel(
                organization_id=organization_id,
                title=title,
                description=description,
            )
            session.add(folder)
            await session.commit()
            await session.refresh(folder)
            return folder

    @db_operation("get_credential_folders")
    async def get_credential_folders(
        self,
        organization_id: str,
        page: int = 1,
        page_size: int = 10,
        search_query: str | None = None,
    ) -> list[CredentialFolderModel]:
        async with self.Session() as session:
            stmt = (
                select(CredentialFolderModel)
                .filter_by(organization_id=organization_id)
                .filter(CredentialFolderModel.deleted_at.is_(None))
            )

            if search_query:
                escaped = search_query.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
                search_pattern = f"%{escaped}%"
                stmt = stmt.filter(
                    or_(
                        CredentialFolderModel.title.ilike(search_pattern, escape="\\"),
                        CredentialFolderModel.description.ilike(search_pattern, escape="\\"),
                    )
                )

            stmt = stmt.order_by(CredentialFolderModel.modified_at.desc())
            stmt = stmt.offset((page - 1) * page_size).limit(page_size)

            result = await session.execute(stmt)
            return list(result.scalars().all())

    @db_operation("get_credential_folder")
    async def get_credential_folder(
        self,
        folder_id: str,
        organization_id: str,
    ) -> CredentialFolderModel | None:
        async with self.Session() as session:
            stmt = (
                select(CredentialFolderModel)
                .filter_by(folder_id=folder_id, organization_id=organization_id)
                .filter(CredentialFolderModel.deleted_at.is_(None))
            )
            result = await session.execute(stmt)
            return result.scalar_one_or_none()

    @db_operation("update_credential_folder")
    async def update_credential_folder(
        self,
        folder_id: str,
        organization_id: str,
        title: str | None = None,
        description: str | None = None,
    ) -> CredentialFolderModel | None:
        async with self.Session() as session:
            stmt = (
                select(CredentialFolderModel)
                .filter_by(folder_id=folder_id, organization_id=organization_id)
                .filter(CredentialFolderModel.deleted_at.is_(None))
            )
            result = await session.execute(stmt)
            folder = result.scalar_one_or_none()
            if not folder:
                return None

            if title is not None:
                folder.title = title
            if description is not None:
                folder.description = description

            folder.modified_at = datetime.now(timezone.utc)
            await session.commit()
            await session.refresh(folder)
            return folder

    @db_operation("soft_delete_credential_folder")
    async def soft_delete_credential_folder(
        self,
        folder_id: str,
        organization_id: str,
    ) -> bool:
        """Soft delete a credential folder and detach its credentials (folder_id -> NULL).

        Credentials are intentionally never deleted here: they hold vault-backed secret
        material whose removal must go through the credential vault service. Deleting a
        folder only unfiles its credentials so no secrets are orphaned.
        """
        async with self.Session() as session:
            folder_stmt = (
                select(CredentialFolderModel)
                .filter_by(folder_id=folder_id, organization_id=organization_id)
                .filter(CredentialFolderModel.deleted_at.is_(None))
            )
            folder_result = await session.execute(folder_stmt)
            folder = folder_result.scalar_one_or_none()
            if not folder:
                return False

            await session.execute(
                update(CredentialModel)
                .where(CredentialModel.folder_id == folder_id)
                .where(CredentialModel.organization_id == organization_id)
                .values(folder_id=None, modified_at=datetime.now(timezone.utc))
            )

            folder.deleted_at = datetime.now(timezone.utc)
            await session.commit()
            return True

    @db_operation("get_credential_folder_credential_count")
    async def get_credential_folder_credential_count(
        self,
        folder_id: str,
        organization_id: str,
    ) -> int:
        async with self.Session() as session:
            stmt = (
                select(func.count(CredentialModel.credential_id))
                .where(CredentialModel.folder_id == folder_id)
                .where(CredentialModel.organization_id == organization_id)
                .where(CredentialModel.deleted_at.is_(None))
            )
            result = await session.execute(stmt)
            return result.scalar_one()

    @db_operation("get_credential_folder_credential_counts_batch")
    async def get_credential_folder_credential_counts_batch(
        self,
        folder_ids: list[str],
        organization_id: str,
    ) -> dict[str, int]:
        async with self.Session() as session:
            stmt = (
                select(
                    CredentialModel.folder_id,
                    func.count(CredentialModel.credential_id).label("count"),
                )
                .where(CredentialModel.folder_id.in_(folder_ids))
                .where(CredentialModel.organization_id == organization_id)
                .where(CredentialModel.deleted_at.is_(None))
                .group_by(CredentialModel.folder_id)
            )
            result = await session.execute(stmt)
            rows = result.all()

            # Unpack positionally: Row.count would resolve to the tuple method, not the labeled column.
            # Folders with no credentials are absent from the result.
            return {folder_id: count for folder_id, count in rows}

    @db_operation("set_credential_folder")
    async def set_credential_folder(
        self,
        credential_id: str,
        organization_id: str,
        folder_id: str | None,
    ) -> Credential | None:
        # Treat an empty folder_id as "remove from folder" (None) so it isn't
        # written as a non-existent "" id and trip the folder foreign key.
        folder_id = folder_id or None
        async with self.Session() as session:
            credential = await session.get(CredentialModel, credential_id)
            if credential is None or credential.organization_id != organization_id or credential.deleted_at is not None:
                return None

            # Validate the target folder in-org and bump its modified_at in a single fetch
            if folder_id:
                folder_model = await session.get(CredentialFolderModel, folder_id)
                if (
                    folder_model is None
                    or folder_model.organization_id != organization_id
                    or folder_model.deleted_at is not None
                ):
                    raise ValueError(f"Credential folder {folder_id} not found")
                folder_model.modified_at = datetime.now(timezone.utc)

            credential.folder_id = folder_id
            credential.modified_at = datetime.now(timezone.utc)

            await session.commit()
            await session.refresh(credential)
            return Credential.model_validate(credential)
