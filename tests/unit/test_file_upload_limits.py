import os
from datetime import UTC, datetime
from pathlib import Path

import pytest

from skyvern.constants import AZURE_BLOB_STORAGE_MAX_UPLOAD_FILE_COUNT, MAX_UPLOAD_FILE_COUNT
from skyvern.forge.sdk.workflow.models.block import FileUploadBlock


@pytest.mark.parametrize(
    "max_file_count",
    [MAX_UPLOAD_FILE_COUNT, AZURE_BLOB_STORAGE_MAX_UPLOAD_FILE_COUNT],
    ids=["s3", "azure"],
)
def test_cloud_storage_block_allows_300_files(tmp_path: Path, max_file_count: int) -> None:
    for index in range(300):
        (tmp_path / f"file_{index}.txt").touch()

    files_to_upload = FileUploadBlock.model_construct()._get_files_to_upload_from_download_dir(
        download_files_path=str(tmp_path),
        max_file_count=max_file_count,
    )

    assert len(files_to_upload) == 300


@pytest.mark.parametrize(
    "max_file_count",
    [MAX_UPLOAD_FILE_COUNT, AZURE_BLOB_STORAGE_MAX_UPLOAD_FILE_COUNT],
    ids=["s3", "azure"],
)
def test_cloud_storage_block_rejects_more_than_300_files(tmp_path: Path, max_file_count: int) -> None:
    for index in range(301):
        (tmp_path / f"file_{index}.txt").touch()

    with pytest.raises(ValueError, match="Max: 300"):
        FileUploadBlock.model_construct()._get_files_to_upload_from_download_dir(
            download_files_path=str(tmp_path),
            max_file_count=max_file_count,
        )


def test_stale_downloads_do_not_count_toward_upload_limit(tmp_path: Path) -> None:
    started_at = datetime(2026, 1, 1, tzinfo=UTC)
    stale = tmp_path / "stale.pdf"
    fresh = tmp_path / "fresh.pdf"
    stale.write_bytes(b"old")
    fresh.write_bytes(b"new")
    os.utime(stale, (started_at.timestamp() - 1,) * 2)
    os.utime(fresh, (started_at.timestamp(),) * 2)

    files = FileUploadBlock.model_construct()._get_files_to_upload_from_download_dir(
        download_files_path=str(tmp_path), max_file_count=1, attempt_started_at=started_at
    )

    assert files == [str(fresh)]
