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


class FileNotAttachable(ValueError):
    """One or more file ids cannot be attached to the run."""

    def __init__(self, file_ids: list[str]) -> None:
        self.file_ids = file_ids
        super().__init__(
            "These file ids are not available to attach — each must name a file uploaded by this "
            f"organization that is not deleted and not already attached to another run: {', '.join(file_ids)}"
        )


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


async def assert_files_attachable(*, file_ids: list[str], organization_id: str) -> None:
    """Reject unknown, deleted, or already-attached file ids before a run is created.

    Attaching happens after the run exists, which is too late to answer 4xx. Checking first
    means a bad file id costs the caller nothing instead of leaving a started run holding an
    attachment it never got.
    """
    requested = list(dict.fromkeys(file_ids))
    if not requested:
        return
    live = await app.DATABASE.uploaded_files.get_uploaded_files_by_ids(
        file_ids=requested, organization_id=organization_id
    )
    attachable = {uploaded_file.file_id for uploaded_file in live if uploaded_file.run_id is None}
    unattachable = [file_id for file_id in requested if file_id not in attachable]
    if unattachable:
        raise FileNotAttachable(unattachable)


async def attach_files_to_run(*, file_ids: list[str], organization_id: str, run_id: str) -> list[UploadedFile]:
    """Bind uploaded files to a run so the run's terminal handler deletes them.

    Every attachment also gets a backstop expiry: the terminal handler is the fast path, and
    the existing expiry sweep is what guarantees the bytes go away even if a run never reaches
    it. Without the backstop, a lost worker would strand exactly the sensitive data this
    feature exists to bound.
    """
    if not file_ids:
        return []

    backstop = datetime.now(timezone.utc) + timedelta(hours=settings.RUN_ATTACHED_FILE_BACKSTOP_HOURS)
    attached = await app.DATABASE.uploaded_files.attach_uploaded_files_to_run(
        file_ids=list(dict.fromkeys(file_ids)),
        organization_id=organization_id,
        run_id=run_id,
        expires_at=backstop,
    )
    attached_ids = {uploaded_file.file_id for uploaded_file in attached}
    missed = [file_id for file_id in dict.fromkeys(file_ids) if file_id not in attached_ids]
    if missed:
        # Lost a race with a concurrent delete or another run's attach between the pre-check
        # and here. The run is already created, so this is logged rather than raised: the run
        # proceeds and simply has no attachment to delete for those ids.
        LOG.warning(
            "Some files could not be attached to the run",
            organization_id=organization_id,
            run_id=run_id,
            file_ids=missed,
        )
    if attached:
        LOG.info(
            "Attached uploaded files to run",
            organization_id=organization_id,
            run_id=run_id,
            file_ids=sorted(attached_ids),
            backstop_expires_at=backstop,
        )
    return attached


async def delete_files_attached_to_run(*, run_id: str) -> int:
    """Delete every file attached to a run. Safe to call for runs that have no attachments.

    Never raises. This runs inside run teardown, where an exception would cost the run its
    webhook and artifact persistence — a far worse outcome than a late delete. Anything that
    fails here is picked up by the expiry sweep via the backstop expiry set at attach time.
    """
    try:
        attached = await app.DATABASE.uploaded_files.get_uploaded_files_for_run(run_id=run_id)
    except Exception:
        LOG.exception("Failed to look up files attached to run", run_id=run_id)
        return 0
    if not attached:
        return 0

    deleted = 0
    for uploaded_file in attached:
        try:
            await _delete_stored_object(uploaded_file)
        except Exception:
            LOG.exception(
                "Failed to delete file attached to run",
                organization_id=uploaded_file.organization_id,
                run_id=run_id,
                file_id=uploaded_file.file_id,
            )
            continue
        try:
            await app.DATABASE.uploaded_files.claim_uploaded_file_for_deletion(
                file_id=uploaded_file.file_id, organization_id=uploaded_file.organization_id
            )
        except Exception:
            LOG.exception(
                "Deleted the bytes of a run attachment but failed to retire its row",
                organization_id=uploaded_file.organization_id,
                run_id=run_id,
                file_id=uploaded_file.file_id,
            )
            continue
        deleted += 1

    LOG.info(
        "Deleted files attached to run",
        run_id=run_id,
        attached=len(attached),
        deleted=deleted,
    )
    return deleted


async def resolve_file_reference(*, file_id: str, organization_id: str) -> str | None:
    """Return the storage URI behind a file id, or None when the org has no such live file.

    This is what lets a caller hand the agent a file id instead of a presigned URL: the URI is
    read from the row rather than taken from input, and the storage layer re-checks it against
    the organization's prefix before any bytes are read.
    """
    uploaded_file = await app.DATABASE.uploaded_files.get_uploaded_file(
        file_id=file_id, organization_id=organization_id
    )
    return uploaded_file.storage_uri if uploaded_file else None


async def purge_expired_files(*, limit: int = 500) -> dict[str, int]:
    """Delete files whose retention period has elapsed.

    Only rows that carry an ``expires_at`` are eligible — an expiry the caller asked for at
    upload, or one set by the organization's data-retention policy. Deleting an object that
    is already gone is a no-op in every storage backend, so a row whose object went first
    still converges.
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
