import os
from datetime import UTC, datetime
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from skyvern.config import settings
from skyvern.constants import AZURE_BLOB_STORAGE_MAX_UPLOAD_FILE_COUNT, MAX_UPLOAD_FILE_COUNT
from skyvern.forge.sdk.workflow.models.block import FileUploadBlock, UploadToS3Block


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


def test_subdirectory_does_not_count_toward_upload_limit(tmp_path: Path) -> None:
    for index in range(300):
        (tmp_path / f"file_{index}.txt").touch()
    (tmp_path / "nested").mkdir()

    files_to_upload = FileUploadBlock.model_construct()._get_files_to_upload_from_download_dir(
        download_files_path=str(tmp_path),
        max_file_count=MAX_UPLOAD_FILE_COUNT,
    )

    assert len(files_to_upload) == 300


@pytest.mark.asyncio
async def test_upload_to_s3_counts_files_not_subdirectories(tmp_path: Path) -> None:
    run_dir = tmp_path / "wr_1"
    run_dir.mkdir()
    for index in range(300):
        (run_dir / f"file_{index}.txt").touch()
    (run_dir / "nested").mkdir()
    client = MagicMock(upload_file_from_path=AsyncMock())
    workflow_run_context = MagicMock()
    workflow_run_context.has_parameter.return_value = False

    async def execute() -> None:
        await UploadToS3Block.model_construct(label="upload", path=str(run_dir)).execute(
            workflow_run_id="wr_1", workflow_run_block_id="", organization_id="o_1"
        )

    with (
        patch.object(UploadToS3Block, "get_workflow_run_context", return_value=workflow_run_context),
        patch.object(UploadToS3Block, "format_potential_template_parameters", return_value=None),
        patch.object(UploadToS3Block, "record_output_parameter_value", AsyncMock()),
        patch.object(UploadToS3Block, "build_block_result", AsyncMock()),
        patch.object(UploadToS3Block, "get_async_aws_client", return_value=client),
        patch("skyvern.forge.sdk.workflow.models.block.get_download_retry_started_at", AsyncMock(return_value=None)),
        patch("skyvern.forge.sdk.workflow.models.block.skyvern_context.current", return_value=None),
        patch.object(settings, "DOWNLOAD_PATH", str(tmp_path)),
    ):
        await execute()
        assert client.upload_file_from_path.await_count == 300

        (run_dir / "file_300.txt").touch()
        client.upload_file_from_path.reset_mock()
        with pytest.raises(ValueError, match="Too many files"):
            await execute()
        client.upload_file_from_path.assert_not_awaited()
