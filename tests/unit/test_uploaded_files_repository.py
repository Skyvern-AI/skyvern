from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from skyvern.forge.sdk.db.datetime_utils import to_naive_utc
from skyvern.forge.sdk.db.models import Base, UploadedFileModel
from skyvern.forge.sdk.db.repositories.uploaded_files import UploadedFilesRepository


@pytest.mark.asyncio
async def test_attach_expiry_is_sqlite_portable_and_only_moves_earlier() -> None:
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all, tables=[UploadedFileModel.__table__])
    sessions = async_sessionmaker(engine, expire_on_commit=False)
    requested_expiry = to_naive_utc(datetime(2026, 10, 14, tzinfo=UTC))
    assert requested_expiry is not None
    earlier_expiry = requested_expiry - timedelta(days=1)
    async with sessions() as session:
        session.add_all(
            [
                UploadedFileModel(
                    file_id="file_no_expiry",
                    organization_id="org_test",
                    storage_uri="s3://bucket/file_no_expiry",
                    filename="file_no_expiry",
                ),
                UploadedFileModel(
                    file_id="file_earlier_expiry",
                    organization_id="org_test",
                    storage_uri="s3://bucket/file_earlier_expiry",
                    filename="file_earlier_expiry",
                    expires_at=earlier_expiry,
                ),
                UploadedFileModel(
                    file_id="file_later_expiry",
                    organization_id="org_test",
                    storage_uri="s3://bucket/file_later_expiry",
                    filename="file_later_expiry",
                    expires_at=requested_expiry + timedelta(days=1),
                ),
            ]
        )
        await session.commit()

    attached = await UploadedFilesRepository(session_factory=sessions).attach_uploaded_files_to_run(
        ["file_no_expiry", "file_earlier_expiry", "file_later_expiry"],
        organization_id="org_test",
        run_id="run_test",
        expires_at=requested_expiry,
    )

    assert {file.file_id: file.expires_at for file in attached} == {
        "file_no_expiry": requested_expiry,
        "file_earlier_expiry": earlier_expiry,
        "file_later_expiry": requested_expiry,
    }
