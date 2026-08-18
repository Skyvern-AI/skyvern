"""Retention and deletion for files uploaded through ``POST /v1/upload_file``.

The security-relevant invariant of this module: a caller names a file only by its opaque
``file_id``. The storage URI that actually gets deleted is always read back from the
``uploaded_files`` row, never taken from the request, and is re-checked against the
caller's organization prefix by the storage layer before the object is removed.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import structlog

from skyvern.config import settings
from skyvern.forge import app
from skyvern.forge.sdk.db.id import generate_uploaded_file_id
from skyvern.forge.sdk.schemas.files import UploadedFile

LOG = structlog.get_logger()


class InvalidRetentionPeriod(ValueError):
    pass


def generate_upload_id() -> str:
    """Mint a file id before the bytes are written, so it can be embedded in storage_uri.

    Two uploads of the same filename on the same day would otherwise compute the same
    storage key and silently overwrite each other's object; embedding the id keeps every
    row's URI unique so a delete can never dereference a URI another live row owns.
    """
    return generate_uploaded_file_id()


def resolve_expires_at(retention_days: int | None, *, now: datetime | None = None) -> datetime | None:
    """Turn a caller-supplied retention period into an absolute expiry.

    ``None`` means the caller asked for no expiry of its own — today's behavior, where the
    file lives until the organization's data-retention policy removes it.
    """
    if retention_days is None:
        return None
    if retention_days < 1 or retention_days > settings.MAX_UPLOADED_FILE_RETENTION_DAYS:
        raise InvalidRetentionPeriod(
            f"retention_days must be between 1 and {settings.MAX_UPLOADED_FILE_RETENTION_DAYS}"
        )
    return (now or datetime.now(timezone.utc)) + timedelta(days=retention_days)


async def record_upload(
    *,
    file_id: str,
    organization_id: str,
    storage_uri: str,
    filename: str,
    size_bytes: int | None,
    retention_days: int | None,
) -> UploadedFile:
    expires_at = resolve_expires_at(retention_days)
    uploaded_file = await app.DATABASE.uploaded_files.create_uploaded_file(
        file_id=file_id,
        organization_id=organization_id,
        storage_uri=storage_uri,
        filename=filename,
        size_bytes=size_bytes,
        expires_at=expires_at,
    )
    LOG.info(
        "Recorded uploaded file",
        organization_id=organization_id,
        file_id=uploaded_file.file_id,
        expires_at=expires_at,
        retention_days=retention_days,
    )
    return uploaded_file


async def delete_uploaded_file(*, file_id: str, organization_id: str) -> bool:
    """Delete an uploaded file's bytes and retire its row. False when there is no such live file.

    The object is removed before the row is retired, so a storage failure surfaces as an
    error against a file that is still listed rather than as a row the caller believes is
    deleted while the bytes remain. The caller can retry; deleting an already-absent object
    is a no-op, so the retry converges.
    """
    uploaded_file = await app.DATABASE.uploaded_files.get_uploaded_file(
        file_id=file_id, organization_id=organization_id
    )
    if uploaded_file is None:
        return False

    await _delete_stored_object(uploaded_file)
    await app.DATABASE.uploaded_files.claim_uploaded_file_for_deletion(file_id=file_id, organization_id=organization_id)
    LOG.info(
        "Deleted uploaded file",
        organization_id=organization_id,
        file_id=uploaded_file.file_id,
    )
    return True


async def purge_expired_files(*, limit: int = 500) -> dict[str, int]:
    """Delete files whose caller-specified retention period has elapsed.

    Only rows that carry an ``expires_at`` are eligible: a file uploaded without a
    retention period is never touched here, so this job can only ever remove data a
    caller explicitly asked to have removed.
    """
    now = datetime.now(timezone.utc)
    expired = await app.DATABASE.uploaded_files.get_expired_uploaded_files(before=now, limit=limit)
    if not expired:
        return {"examined": 0, "deleted": 0, "failed": 0}

    deleted = 0
    failed = 0
    for candidate in expired:
        try:
            # Same ordering as the API delete: a row left live after a storage failure is
            # still expired, so the next tick retries it. Retiring it first would strand
            # the object with nothing left to find it.
            await _delete_stored_object(candidate)
        except Exception:
            failed += 1
            LOG.exception(
                "Failed to delete expired uploaded file",
                organization_id=candidate.organization_id,
                file_id=candidate.file_id,
            )
            continue
        await app.DATABASE.uploaded_files.claim_uploaded_file_for_deletion(
            file_id=candidate.file_id, organization_id=candidate.organization_id
        )
        deleted += 1

    LOG.info("Purged expired uploaded files", examined=len(expired), deleted=deleted, failed=failed)
    return {"examined": len(expired), "deleted": deleted, "failed": failed}


async def _delete_stored_object(uploaded_file: UploadedFile) -> None:
    await app.STORAGE.delete_legacy_file(
        organization_id=uploaded_file.organization_id,
        uri=uploaded_file.storage_uri,
    )
