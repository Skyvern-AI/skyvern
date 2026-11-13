from __future__ import annotations

from datetime import datetime, timezone

from skyvern.config import settings
from skyvern.forge.sdk.schemas.files import UploadedFile


def uploaded_file_uri(organization_id: str, filename: str = "screenshot.png") -> str:
    return f"s3://{settings.AWS_S3_BUCKET_UPLOADS}/{settings.ENV}/{organization_id}/2026-08-15/{filename}"


class FakeUploadedFilesRepository:
    """In-memory stand-in with the same org-scoping and soft-delete semantics as the real one."""

    def __init__(self) -> None:
        self.rows: dict[str, UploadedFile] = {}
        self._next_id = 0

    def seed(
        self,
        organization_id: str,
        expires_at: datetime | None = None,
        filename: str = "screenshot.png",
        run_id: str | None = None,
        size_bytes: int | None = None,
    ) -> str:
        self._next_id += 1
        file_id = f"file_{self._next_id}"
        now = datetime.now(timezone.utc)
        self.rows[file_id] = UploadedFile(
            file_id=file_id,
            organization_id=organization_id,
            storage_uri=uploaded_file_uri(organization_id, filename),
            filename=filename,
            size_bytes=size_bytes,
            expires_at=expires_at,
            run_id=run_id,
            created_at=now,
            modified_at=now,
        )
        return file_id

    def live_ids(self) -> set[str]:
        return {file_id for file_id, row in self.rows.items() if row.deleted_at is None}

    async def create_uploaded_file(
        self,
        file_id: str,
        organization_id: str,
        storage_uri: str,
        filename: str,
        size_bytes: int | None = None,
        expires_at: datetime | None = None,
    ) -> UploadedFile:
        now = datetime.now(timezone.utc)
        for row in self.rows.values():
            if row.organization_id == organization_id and row.storage_uri == storage_uri and row.deleted_at is None:
                row.deleted_at = now
        self.rows[file_id] = UploadedFile(
            file_id=file_id,
            organization_id=organization_id,
            storage_uri=storage_uri,
            filename=filename,
            size_bytes=size_bytes,
            expires_at=expires_at,
            created_at=now,
            modified_at=now,
        )
        return self.rows[file_id]

    async def get_uploaded_file(self, file_id: str, organization_id: str) -> UploadedFile | None:
        row = self.rows.get(file_id)
        if row is None or row.organization_id != organization_id or row.deleted_at is not None:
            return None
        return row

    async def claim_uploaded_file_for_deletion(self, file_id: str, organization_id: str) -> UploadedFile | None:
        row = await self.get_uploaded_file(file_id, organization_id)
        if row is None:
            return None
        row.deleted_at = datetime.now(timezone.utc)
        return row

    async def get_expired_uploaded_files(self, before: datetime, limit: int = 500) -> list[UploadedFile]:
        return [
            row
            for row in self.rows.values()
            if row.deleted_at is None and row.expires_at is not None and row.expires_at <= before
        ][:limit]

    async def get_uploaded_files_by_ids(self, file_ids: list[str], organization_id: str) -> list[UploadedFile]:
        return [
            row
            for file_id in file_ids
            if (row := self.rows.get(file_id)) is not None
            and row.organization_id == organization_id
            and row.deleted_at is None
        ]

    async def get_uploaded_files_for_run(self, run_id: str) -> list[UploadedFile]:
        return [row for row in self.rows.values() if row.run_id == run_id and row.deleted_at is None]

    async def attach_uploaded_files_to_run(
        self,
        file_ids: list[str],
        organization_id: str,
        run_id: str,
        expires_at: datetime,
    ) -> list[UploadedFile]:
        attached = []
        for file_id in file_ids:
            row = self.rows.get(file_id)
            if row is None or row.organization_id != organization_id or row.deleted_at is not None:
                continue
            if row.run_id is not None and row.run_id != run_id:
                continue
            row.run_id = run_id
            row.expires_at = min(row.expires_at, expires_at) if row.expires_at else expires_at
            attached.append(row)
        return attached
