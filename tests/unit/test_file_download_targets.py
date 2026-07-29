from __future__ import annotations

import hashlib
import os
import unicodedata
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch
from urllib.parse import urlparse

import pytest

from skyvern.exceptions import AzureConfigurationError
from skyvern.forge.sdk.api.llm.api_handler_factory import LLMAPIHandlerFactory
from skyvern.forge.sdk.services import google_oauth_service
from skyvern.forge.sdk.workflow.models.block import BaseTaskBlock, FileDownloadBlock
from skyvern.forge.sdk.workflow.models.parameter import OutputParameter
from skyvern.forge.sdk.workflow.workflow_definition_converter import block_yaml_to_block
from skyvern.schemas.workflows import (
    BlockResult,
    BlockStatus,
    FileDownloadBlockYAML,
    FileDownloadTarget,
    FileStorageType,
)


def _output_parameter() -> OutputParameter:
    now = datetime.now(UTC)
    return OutputParameter(
        output_parameter_id="download-output-id",
        workflow_id="workflow-id",
        key="download_output",
        created_at=now,
        modified_at=now,
    )


def _file_download_block(download_target: FileDownloadTarget, **kwargs: object) -> FileDownloadBlock:
    return FileDownloadBlock(
        label="download",
        output_parameter=_output_parameter(),
        navigation_goal="Download the requested files.",
        download_target=download_target,
        **kwargs,
    )


def _browser_result(
    block: FileDownloadBlock,
    *,
    success: bool = True,
    downloaded_filenames: tuple[str, ...] = (),
) -> BlockResult:
    return BlockResult(
        success=success,
        output_parameter=block.output_parameter,
        output_parameter_value={
            "task_id": "task-id",
            "downloaded_files": [
                {"filename": filename, "checksum": f"checksum-{filename}"} for filename in downloaded_filenames
            ],
        },
        status=BlockStatus.completed if success else BlockStatus.failed,
        failure_reason=None if success else "Browser download failed",
    )


def _write_downloads(directory: Path, *names: str) -> list[Path]:
    directory.mkdir(parents=True, exist_ok=True)
    files = [directory / name for name in names]
    for file in files:
        file.write_text(file.name)
    return files


async def _execute_file_download(
    block: FileDownloadBlock,
    browser_result: BlockResult,
    download_dir: Path,
    *,
    selection_result: tuple[list[str], str] | None = None,
    upload_side_effect: BaseException | None = None,
    secret_values: dict[str, str] | None = None,
    downloads_during_execute: tuple[str, ...] = (),
    during_execute: Callable[[], None] | None = None,
) -> SimpleNamespace:
    workflow_run_context = MagicMock()
    workflow_run_context.organization_id = "organization-id"
    if secret_values is None:
        secret_values = {
            value: value
            for value in (
                block.aws_secret_access_key,
                block.azure_storage_account_key,
                block.sftp_password,
                block.sftp_private_key,
                block.sftp_private_key_passphrase,
            )
            if value
        }
    workflow_run_context.get_original_secret_value_or_none.side_effect = secret_values.get
    upload = AsyncMock(return_value="customer://uploaded-file", side_effect=upload_side_effect)
    select_files = AsyncMock()
    if selection_result is not None:
        select_files.return_value = selection_result
    recorded_output = SimpleNamespace(value=None)

    async def record_output_parameter_value(
        _workflow_context: MagicMock,
        _workflow_id: str,
        value: object = None,
    ) -> None:
        recorded_output.value = value

    record_output = AsyncMock(side_effect=record_output_parameter_value)

    async def execute_browser(*args: object, **kwargs: object) -> BlockResult:
        if during_execute is None:
            _write_downloads(download_dir, *downloads_during_execute)
        else:
            during_execute()
        await block.record_output_parameter_value(
            workflow_run_context,
            "workflow-run-id",
            browser_result.output_parameter_value,
        )
        return browser_result

    with (
        patch.object(BaseTaskBlock, "execute", new_callable=AsyncMock, side_effect=execute_browser) as browser_execute,
        patch.object(FileDownloadBlock, "record_output_parameter_value", record_output),
        patch.object(FileDownloadBlock, "get_workflow_run_context", return_value=workflow_run_context),
        patch.object(FileDownloadBlock, "_format_destination_template_parameters", return_value=None),
        patch.object(FileDownloadBlock, "_select_files_to_upload_with_prompt", select_files),
        patch(
            "skyvern.forge.sdk.workflow.models.block.get_path_for_workflow_download_directory",
            return_value=download_dir,
        ),
        patch("skyvern.forge.sdk.workflow.models.block.skyvern_context.current", return_value=None),
        patch("skyvern.forge.sdk.workflow.models.block.app") as mock_app,
    ):
        mock_app.AGENT_FUNCTION.upload_file_to_customer_storage = upload
        mock_app.AGENT_FUNCTION.get_google_workspace_credentials = AsyncMock(
            return_value=SimpleNamespace(token="google-access-token")
        )
        mock_app.DATABASE.observer.update_workflow_run_block = AsyncMock()
        result = await block.execute(
            workflow_run_id="workflow-run-id",
            workflow_run_block_id="workflow-run-block-id",
            organization_id="organization-id",
        )

    return SimpleNamespace(
        result=result,
        browser_execute=browser_execute,
        upload=upload,
        select_files=select_files,
        workflow_run_context=workflow_run_context,
        record_output=record_output,
        recorded_output=recorded_output,
        get_google_workspace_credentials=mock_app.AGENT_FUNCTION.get_google_workspace_credentials,
    )


@pytest.mark.asyncio
async def test_website_target_returns_browser_result_without_dispatch(tmp_path: Path) -> None:
    block = _file_download_block(FileDownloadTarget.WEBSITE)
    browser_result = _browser_result(block)

    execution = await _execute_file_download(block, browser_result, tmp_path / "downloads")

    assert execution.result is browser_result
    execution.browser_execute.assert_awaited_once()
    execution.upload.assert_not_awaited()


@pytest.mark.asyncio
async def test_sftp_target_dispatches_each_download_and_preserves_browser_result(tmp_path: Path) -> None:
    download_dir = tmp_path / "downloads"
    downloaded_files = [download_dir / "invoice.pdf", download_dir / "report.csv"]
    block = _file_download_block(
        FileDownloadTarget.SFTP,
        sftp_host="sftp.example.com",
        sftp_port=2222,
        sftp_username="skyvern",
        sftp_password="password",
        sftp_remote_path="incoming",
    )
    browser_result = _browser_result(block, downloaded_filenames=("invoice.pdf", "report.csv"))

    execution = await _execute_file_download(
        block,
        browser_result,
        download_dir,
        downloads_during_execute=("invoice.pdf", "report.csv"),
    )

    assert execution.result is browser_result
    assert execution.result.output_parameter_value == {
        "task_id": "task-id",
        "downloaded_files": [
            {"filename": "invoice.pdf", "checksum": "checksum-invoice.pdf"},
            {"filename": "report.csv", "checksum": "checksum-report.csv"},
        ],
    }
    assert execution.upload.await_count == 2
    assert {Path(call.kwargs["file_path"]) for call in execution.upload.await_args_list} == set(downloaded_files)
    assert all(
        call.kwargs["destination"].storage_type == FileStorageType.SFTP for call in execution.upload.await_args_list
    )


@pytest.mark.asyncio
async def test_prior_block_downloads_are_not_dispatched(tmp_path: Path) -> None:
    download_dir = tmp_path / "downloads"
    _write_downloads(download_dir, "prior_block.pdf")
    block = _file_download_block(
        FileDownloadTarget.SFTP,
        sftp_host="sftp.example.com",
        sftp_username="skyvern",
        sftp_password="password",
    )
    browser_result = _browser_result(block, downloaded_filenames=("this_block.pdf",))

    execution = await _execute_file_download(
        block,
        browser_result,
        download_dir,
        downloads_during_execute=("this_block.pdf",),
    )

    execution.upload.assert_awaited_once()
    assert Path(execution.upload.await_args.kwargs["file_path"]).name == "this_block.pdf"


@pytest.mark.asyncio
async def test_download_scope_loss_does_not_deliver_prior_block_files(tmp_path: Path) -> None:
    download_dir = tmp_path / "downloads"
    prior_file = _write_downloads(download_dir, "prior_block.pdf")[0]
    current_file = download_dir / "this_block.pdf"
    block = _file_download_block(
        FileDownloadTarget.SFTP,
        sftp_host="sftp.example.com",
        sftp_username="skyvern",
        sftp_password="password",
    )
    browser_result = _browser_result(block)

    execution = await _execute_file_download(
        block,
        browser_result,
        download_dir,
        downloads_during_execute=(current_file.name,),
    )

    execution.upload.assert_awaited_once()
    assert execution.upload.await_args.kwargs["file_path"] == str(current_file)
    assert all(call.kwargs["file_path"] != str(prior_file) for call in execution.upload.await_args_list)


@pytest.mark.asyncio
async def test_new_name_file_delivered_even_if_its_hash_fails(tmp_path: Path) -> None:
    download_dir = tmp_path / "downloads"
    prior_file = _write_downloads(download_dir, "prior_block.pdf")[0]
    current_file = download_dir / "this_block.pdf"
    block = _file_download_block(
        FileDownloadTarget.SFTP,
        sftp_host="sftp.example.com",
        sftp_username="skyvern",
        sftp_password="password",
    )

    def calculate_hash(file_path: str) -> str:
        if file_path == str(current_file):
            raise OSError("current hash failed")
        return hashlib.sha256(Path(file_path).read_bytes()).hexdigest()

    with patch("skyvern.forge.sdk.workflow.models.block.calculate_sha256_for_file", side_effect=calculate_hash):
        execution = await _execute_file_download(
            block,
            _browser_result(block),
            download_dir,
            downloads_during_execute=(current_file.name,),
        )

    execution.upload.assert_awaited_once()
    assert execution.upload.await_args.kwargs["file_path"] == str(current_file)
    assert all(call.kwargs["file_path"] != str(prior_file) for call in execution.upload.await_args_list)


@pytest.mark.asyncio
async def test_baseline_listdir_failure_fails_closed(tmp_path: Path) -> None:
    download_dir = tmp_path / "downloads"
    prior_file = _write_downloads(download_dir, "prior_block.pdf")[0]
    current_file = download_dir / "this_block.pdf"
    block = _file_download_block(
        FileDownloadTarget.SFTP,
        sftp_host="sftp.example.com",
        sftp_username="skyvern",
        sftp_password="password",
    )
    real_listdir = os.listdir
    baseline_list_attempted = False

    def listdir(path: os.PathLike[str] | str) -> list[str]:
        nonlocal baseline_list_attempted
        if not baseline_list_attempted:
            baseline_list_attempted = True
            raise OSError("baseline list failed")
        return real_listdir(path)

    with patch("skyvern.forge.sdk.workflow.models.block.os.listdir", side_effect=listdir):
        execution = await _execute_file_download(
            block,
            _browser_result(block),
            download_dir,
            downloads_during_execute=(current_file.name,),
        )

    assert baseline_list_attempted is True
    assert execution.result.success is False
    assert execution.result.status == BlockStatus.failed
    assert "pre-download baseline" in (execution.result.failure_reason or "")
    assert "sftp" in (execution.result.failure_reason or "").lower()
    execution.upload.assert_not_awaited()
    assert prior_file.exists()
    # Fail fast: a failed baseline scan is rejected before the browser download runs.
    execution.browser_execute.assert_not_awaited()


@pytest.mark.asyncio
async def test_post_download_listdir_failure_fails_closed(tmp_path: Path) -> None:
    download_dir = tmp_path / "downloads"
    _write_downloads(download_dir, "prior_block.pdf")
    current_file = download_dir / "this_block.pdf"
    block = _file_download_block(
        FileDownloadTarget.SFTP,
        sftp_host="sftp.example.com",
        sftp_username="skyvern",
        sftp_password="password",
    )
    real_listdir = os.listdir
    post_download = False

    def during_execute() -> None:
        nonlocal post_download
        _write_downloads(download_dir, current_file.name)
        post_download = True

    def listdir(path: os.PathLike[str] | str) -> list[str]:
        if post_download:
            raise OSError("post-download list failed")
        return real_listdir(path)

    with patch("skyvern.forge.sdk.workflow.models.block.os.listdir", side_effect=listdir):
        execution = await _execute_file_download(
            block, _browser_result(block), download_dir, during_execute=during_execute
        )

    assert post_download is True
    assert execution.result.success is False
    assert execution.result.status == BlockStatus.failed
    execution.upload.assert_not_awaited()
    assert execution.recorded_output.value is None


@pytest.mark.asyncio
async def test_unhashable_prior_file_is_not_delivered(tmp_path: Path) -> None:
    download_dir = tmp_path / "downloads"
    prior_file = _write_downloads(download_dir, "prior_block.pdf")[0]
    current_file = download_dir / "this_block.pdf"
    block = _file_download_block(
        FileDownloadTarget.SFTP,
        sftp_host="sftp.example.com",
        sftp_username="skyvern",
        sftp_password="password",
    )
    baseline_hash_failed = False

    def calculate_hash(file_path: str) -> str:
        nonlocal baseline_hash_failed
        if file_path == str(prior_file) and not baseline_hash_failed:
            baseline_hash_failed = True
            raise OSError("baseline hash failed")
        return hashlib.sha256(Path(file_path).read_bytes()).hexdigest()

    with patch("skyvern.forge.sdk.workflow.models.block.calculate_sha256_for_file", side_effect=calculate_hash):
        execution = await _execute_file_download(
            block,
            _browser_result(block),
            download_dir,
            downloads_during_execute=(current_file.name,),
        )

    execution.upload.assert_awaited_once()
    assert execution.upload.await_args.kwargs["file_path"] == str(current_file)
    assert all(call.kwargs["file_path"] != str(prior_file) for call in execution.upload.await_args_list)


@pytest.mark.asyncio
async def test_attributable_file_delivered_even_if_scope_list_incomplete(tmp_path: Path) -> None:
    download_dir = tmp_path / "downloads"
    current_file = download_dir / "current.pdf"
    block = _file_download_block(
        FileDownloadTarget.SFTP,
        sftp_host="sftp.example.com",
        sftp_username="skyvern",
        sftp_password="password",
    )

    execution = await _execute_file_download(
        block,
        _browser_result(block, downloaded_filenames=("stale.pdf",)),
        download_dir,
        downloads_during_execute=(current_file.name,),
    )

    execution.upload.assert_awaited_once()
    assert execution.upload.await_args.kwargs["file_path"] == str(current_file)


@pytest.mark.asyncio
async def test_no_new_local_files_is_noop_even_if_scoped_list_nonempty(tmp_path: Path) -> None:
    download_dir = tmp_path / "downloads"
    prior_file = _write_downloads(download_dir, "prior_block.pdf")[0]
    block = _file_download_block(
        FileDownloadTarget.SFTP,
        sftp_host="sftp.example.com",
        sftp_username="skyvern",
        sftp_password="password",
    )
    browser_result = _browser_result(block, downloaded_filenames=(prior_file.name,))

    execution = await _execute_file_download(block, browser_result, download_dir)

    assert execution.result is browser_result
    assert execution.result.success is True
    execution.upload.assert_not_awaited()


@pytest.mark.asyncio
async def test_prior_files_do_not_consume_current_block_upload_limit(tmp_path: Path) -> None:
    download_dir = tmp_path / "downloads"
    _write_downloads(download_dir, "prior.pdf")
    block = _file_download_block(
        FileDownloadTarget.SFTP,
        sftp_host="sftp.example.com",
        sftp_username="skyvern",
        sftp_password="password",
    )

    with patch("skyvern.forge.sdk.workflow.models.block.MAX_UPLOAD_FILE_COUNT", 1):
        execution = await _execute_file_download(
            block,
            _browser_result(block, downloaded_filenames=("current.pdf",)),
            download_dir,
            downloads_during_execute=("current.pdf",),
        )

    execution.upload.assert_awaited_once()
    assert Path(execution.upload.await_args.kwargs["file_path"]).name == "current.pdf"


@pytest.mark.asyncio
async def test_same_basename_with_wrong_checksum_never_dispatches_prior_block_file(tmp_path: Path) -> None:
    download_dir = tmp_path / "downloads"
    prior_file = _write_downloads(download_dir, "invoice.pdf")[0]
    prior_file.write_bytes(b"prior block contents")
    block = _file_download_block(
        FileDownloadTarget.SFTP,
        sftp_host="sftp.example.com",
        sftp_username="skyvern",
        sftp_password="password",
    )
    browser_result = _browser_result(block, downloaded_filenames=("invoice.pdf",))
    assert isinstance(browser_result.output_parameter_value, dict)
    browser_result.output_parameter_value["downloaded_files"] = [
        {
            "filename": "invoice.pdf",
            "checksum": hashlib.sha256(b"this block contents").hexdigest(),
        }
    ]

    execution = await _execute_file_download(block, browser_result, download_dir)

    execution.upload.assert_not_awaited()


@pytest.mark.asyncio
async def test_same_name_overwrite_new_content_is_delivered(tmp_path: Path) -> None:
    download_dir = tmp_path / "downloads"
    report_file, unchanged_file = _write_downloads(download_dir, "report.pdf", "unchanged.pdf")
    report_file.write_bytes(b"before-data")
    unchanged_file.write_bytes(b"unchanged-data")
    original_stat = report_file.stat()
    new_contents = b"after-data!"
    assert len(new_contents) == original_stat.st_size

    block = _file_download_block(
        FileDownloadTarget.SFTP,
        sftp_host="sftp.example.com",
        sftp_username="skyvern",
        sftp_password="password",
    )
    browser_result = _browser_result(block)
    assert isinstance(browser_result.output_parameter_value, dict)
    browser_result.output_parameter_value["downloaded_files"] = [
        {"filename": report_file.name, "checksum": hashlib.sha256(new_contents).hexdigest()},
        {"filename": unchanged_file.name, "checksum": hashlib.sha256(unchanged_file.read_bytes()).hexdigest()},
    ]

    def overwrite_report() -> None:
        report_file.write_bytes(new_contents)
        os.utime(report_file, ns=(original_stat.st_atime_ns, original_stat.st_mtime_ns))

    execution = await _execute_file_download(
        block,
        browser_result,
        download_dir,
        during_execute=overwrite_report,
    )

    execution.upload.assert_awaited_once()
    assert execution.upload.await_args.kwargs["file_path"] == str(report_file)
    assert all(call.kwargs["file_path"] != str(unchanged_file) for call in execution.upload.await_args_list)


@pytest.mark.asyncio
async def test_same_name_identical_redownload_with_new_mtime_is_delivered(tmp_path: Path) -> None:
    download_dir = tmp_path / "downloads"
    report_file = _write_downloads(download_dir, "report.pdf")[0]
    report_file.write_bytes(b"same-bytes")
    original_stat = report_file.stat()
    block = _file_download_block(
        FileDownloadTarget.SFTP,
        sftp_host="sftp.example.com",
        sftp_username="skyvern",
        sftp_password="password",
    )

    def overwrite_report() -> None:
        report_file.write_bytes(b"same-bytes")
        os.utime(report_file, ns=(original_stat.st_atime_ns, original_stat.st_mtime_ns + 1_000_000_000))

    execution = await _execute_file_download(
        block,
        _browser_result(block),
        download_dir,
        during_execute=overwrite_report,
    )

    execution.upload.assert_awaited_once()
    assert execution.upload.await_args.kwargs["file_path"] == str(report_file)


@pytest.mark.asyncio
async def test_same_name_identical_content_frozen_mtime_is_not_delivered(tmp_path: Path) -> None:
    download_dir = tmp_path / "downloads"
    report_file = _write_downloads(download_dir, "report.pdf")[0]
    report_file.write_bytes(b"same-bytes")
    original_stat = report_file.stat()
    block = _file_download_block(
        FileDownloadTarget.SFTP,
        sftp_host="sftp.example.com",
        sftp_username="skyvern",
        sftp_password="password",
    )

    def overwrite_report() -> None:
        report_file.write_bytes(b"same-bytes")
        os.utime(report_file, ns=(original_stat.st_atime_ns, original_stat.st_mtime_ns))

    execution = await _execute_file_download(
        block,
        _browser_result(block),
        download_dir,
        during_execute=overwrite_report,
    )

    execution.upload.assert_not_awaited()


@pytest.mark.asyncio
async def test_nonempty_scoped_download_missing_locally_is_noop(tmp_path: Path) -> None:
    block = _file_download_block(
        FileDownloadTarget.SFTP,
        sftp_host="sftp.example.com",
        sftp_username="skyvern",
        sftp_password="password",
    )
    browser_result = _browser_result(block, downloaded_filenames=("missing.pdf",))

    execution = await _execute_file_download(block, browser_result, tmp_path / "downloads")

    assert execution.result is browser_result
    assert execution.result.success is True
    execution.upload.assert_not_awaited()


@pytest.mark.asyncio
async def test_download_suffix_renamed_scoped_file_is_dispatched(tmp_path: Path) -> None:
    download_dir = tmp_path / "downloads"
    renamed_file = download_dir / "quarterly-report.pdf"
    block = _file_download_block(
        FileDownloadTarget.SFTP,
        download_suffix="quarterly-report",
        sftp_host="sftp.example.com",
        sftp_username="skyvern",
        sftp_password="password",
    )
    browser_result = _browser_result(block, downloaded_filenames=("quarterly-report.pdf",))

    execution = await _execute_file_download(
        block,
        browser_result,
        download_dir,
        downloads_during_execute=("quarterly-report.pdf",),
    )

    execution.upload.assert_awaited_once()
    assert execution.upload.await_args.kwargs["file_path"] == str(renamed_file)


@pytest.mark.asyncio
async def test_scope_matching_normalizes_unicode_filename(tmp_path: Path) -> None:
    download_dir = tmp_path / "downloads"
    nfc_name = "r\N{LATIN SMALL LETTER E WITH ACUTE}sume.pdf"
    nfd_name = unicodedata.normalize("NFD", nfc_name)
    assert nfc_name != nfd_name
    local_file = download_dir / nfd_name
    block = _file_download_block(
        FileDownloadTarget.SFTP,
        sftp_host="sftp.example.com",
        sftp_username="skyvern",
        sftp_password="password",
    )
    browser_result = _browser_result(block, downloaded_filenames=(nfc_name,))
    assert isinstance(browser_result.output_parameter_value, dict)
    browser_result.output_parameter_value["downloaded_files"] = [
        {"filename": nfc_name, "checksum": hashlib.sha256(nfd_name.encode()).hexdigest()}
    ]

    execution = await _execute_file_download(
        block,
        browser_result,
        download_dir,
        downloads_during_execute=(nfd_name,),
    )

    execution.upload.assert_awaited_once()
    assert execution.upload.await_args.kwargs["file_path"] == str(local_file)


@pytest.mark.asyncio
async def test_nfc_nfd_identical_leftovers_are_not_leaked_by_mtime(tmp_path: Path) -> None:
    download_dir = tmp_path / "downloads"
    download_dir.mkdir(parents=True, exist_ok=True)
    nfc_name = "r\N{LATIN SMALL LETTER E WITH ACUTE}sum\N{LATIN SMALL LETTER E WITH ACUTE}.pdf"
    nfd_name = unicodedata.normalize("NFD", nfc_name)
    assert nfc_name != nfd_name
    nfc_file = download_dir / nfc_name
    nfd_file = download_dir / nfd_name
    nfc_file.write_bytes(b"identical-leftover")
    nfd_file.write_bytes(b"identical-leftover")
    if len(os.listdir(download_dir)) < 2:
        pytest.skip("filesystem normalizes unicode filenames; NFC/NFD collision is not reproducible here")
    os.utime(nfc_file, ns=(0, 1_000_000_000))
    os.utime(nfd_file, ns=(0, 9_000_000_000))

    block = _file_download_block(
        FileDownloadTarget.SFTP,
        sftp_host="sftp.example.com",
        sftp_username="skyvern",
        sftp_password="password",
    )

    real_listdir = os.listdir

    # Force the adversarial order (nfc last): a reverted normalized-key baseline would then hold
    # nfc's smaller mtime, so nfd's larger mtime would leak. The raw-key fix must skip both.
    def ordered_listdir(path: os.PathLike[str] | str) -> list[str]:
        entries = real_listdir(path)
        if set(entries) == {nfc_name, nfd_name}:
            return [nfd_name, nfc_name]
        return entries

    with patch("skyvern.forge.sdk.workflow.models.block.os.listdir", side_effect=ordered_listdir):
        execution = await _execute_file_download(block, _browser_result(block), download_dir)

    execution.upload.assert_not_awaited()


@pytest.mark.asyncio
async def test_nfc_nfd_different_content_leftovers_are_not_leaked(tmp_path: Path) -> None:
    download_dir = tmp_path / "downloads"
    download_dir.mkdir(parents=True, exist_ok=True)
    nfc_name = "caf\N{LATIN SMALL LETTER E WITH ACUTE}.pdf"
    nfd_name = unicodedata.normalize("NFD", nfc_name)
    assert nfc_name != nfd_name
    (download_dir / nfc_name).write_bytes(b"aws-key-one")
    (download_dir / nfd_name).write_bytes(b"totally-different")
    if len(os.listdir(download_dir)) < 2:
        pytest.skip("filesystem normalizes unicode filenames; NFC/NFD collision is not reproducible here")

    block = _file_download_block(
        FileDownloadTarget.SFTP,
        sftp_host="sftp.example.com",
        sftp_username="skyvern",
        sftp_password="password",
    )

    execution = await _execute_file_download(block, _browser_result(block), download_dir)

    execution.upload.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("malformed_entry", [{}, {"filename": None}, None, "invoice.pdf"])
async def test_malformed_scoped_download_entry_is_ignored_without_hiding_valid_file(
    tmp_path: Path, malformed_entry: object
) -> None:
    download_dir = tmp_path / "downloads"
    _write_downloads(download_dir, "prior.pdf")
    valid_file = download_dir / "valid.pdf"
    block = _file_download_block(
        FileDownloadTarget.SFTP,
        sftp_host="sftp.example.com",
        sftp_username="skyvern",
        sftp_password="password",
    )
    browser_result = _browser_result(block)
    assert isinstance(browser_result.output_parameter_value, dict)
    browser_result.output_parameter_value["downloaded_files"] = [malformed_entry, {"filename": "valid.pdf"}]

    execution = await _execute_file_download(
        block,
        browser_result,
        download_dir,
        downloads_during_execute=("valid.pdf",),
    )

    execution.upload.assert_awaited_once()
    assert execution.upload.await_args.kwargs["file_path"] == str(valid_file)


@pytest.mark.asyncio
async def test_dispatch_failure_returns_failed_result_and_clears_output(tmp_path: Path) -> None:
    download_dir = tmp_path / "downloads"
    block = _file_download_block(
        FileDownloadTarget.SFTP,
        sftp_host="sftp.example.com",
        sftp_username="skyvern",
        sftp_password="password",
    )
    browser_result = _browser_result(block, downloaded_filenames=("invoice.pdf",))

    execution = await _execute_file_download(
        block,
        browser_result,
        download_dir,
        upload_side_effect=RuntimeError("upload failed"),
        downloads_during_execute=("invoice.pdf",),
    )

    assert execution.result.success is False
    assert execution.result.status == BlockStatus.failed
    assert "sftp" in (execution.result.failure_reason or "").lower()
    assert execution.record_output.await_count == 2
    assert execution.record_output.await_args_list[0].args[2] == browser_result.output_parameter_value
    execution.record_output.assert_awaited_with(execution.workflow_run_context, "workflow-run-id", None)
    assert execution.recorded_output.value is None


@pytest.mark.asyncio
async def test_empty_scope_and_empty_download_dir_is_noop(tmp_path: Path) -> None:
    download_dir = tmp_path / "downloads"
    _write_downloads(download_dir)
    block = _file_download_block(
        FileDownloadTarget.SFTP,
        sftp_host="sftp.example.com",
        sftp_username="skyvern",
        sftp_password="password",
    )
    browser_result = _browser_result(block)

    execution = await _execute_file_download(block, browser_result, download_dir)

    assert execution.result is browser_result
    assert execution.result.success is True
    execution.upload.assert_not_awaited()


@pytest.mark.asyncio
async def test_empty_scope_with_only_unchanged_prior_files_is_noop(tmp_path: Path) -> None:
    download_dir = tmp_path / "downloads"
    _write_downloads(download_dir, "prior.pdf")
    block = _file_download_block(
        FileDownloadTarget.SFTP,
        sftp_host="sftp.example.com",
        sftp_username="skyvern",
        sftp_password="password",
        continue_on_empty=False,
    )
    browser_result = _browser_result(block)

    execution = await _execute_file_download(block, browser_result, download_dir)

    assert execution.result is browser_result
    assert execution.result.success is True
    execution.upload.assert_not_awaited()


@pytest.mark.asyncio
async def test_empty_scope_delivers_new_file_from_this_block(tmp_path: Path) -> None:
    download_dir = tmp_path / "downloads"
    prior_file = _write_downloads(download_dir, "prior.pdf")[0]
    current_file = download_dir / "current.pdf"
    block = _file_download_block(
        FileDownloadTarget.SFTP,
        sftp_host="sftp.example.com",
        sftp_username="skyvern",
        sftp_password="password",
        continue_on_empty=False,
    )

    execution = await _execute_file_download(
        block,
        _browser_result(block),
        download_dir,
        downloads_during_execute=(current_file.name,),
    )

    assert execution.result.success is True
    execution.upload.assert_awaited_once()
    assert execution.upload.await_args.kwargs["file_path"] == str(current_file)
    assert all(call.kwargs["file_path"] != str(prior_file) for call in execution.upload.await_args_list)


@pytest.mark.asyncio
async def test_pre_download_snapshot_tolerates_file_deleted_during_stat(tmp_path: Path) -> None:
    download_dir = tmp_path / "downloads"
    vanishing_file = _write_downloads(download_dir, "vanishing.pdf")[0]
    current_file = download_dir / "current.pdf"
    block = _file_download_block(
        FileDownloadTarget.SFTP,
        sftp_host="sftp.example.com",
        sftp_username="skyvern",
        sftp_password="password",
    )
    real_stat = os.stat
    deleted = False

    def delete_during_stat(path: os.PathLike[str] | str, *args: object, **kwargs: object) -> os.stat_result:
        nonlocal deleted
        if not deleted and os.fspath(path) == str(vanishing_file):
            deleted = True
            vanishing_file.unlink()
            raise FileNotFoundError(str(vanishing_file))
        return real_stat(path, *args, **kwargs)

    with patch("skyvern.forge.sdk.workflow.models.block.os.stat", side_effect=delete_during_stat):
        execution = await _execute_file_download(
            block,
            _browser_result(block, downloaded_filenames=(current_file.name,)),
            download_dir,
            downloads_during_execute=(current_file.name,),
        )

    assert execution.result.success is True
    execution.upload.assert_awaited_once()
    assert execution.upload.await_args.kwargs["file_path"] == str(current_file)


@pytest.mark.asyncio
async def test_empty_scope_with_local_files_and_continue_on_empty_is_noop(tmp_path: Path) -> None:
    download_dir = tmp_path / "downloads"
    _write_downloads(download_dir, "prior.pdf")
    block = _file_download_block(
        FileDownloadTarget.SFTP,
        sftp_host="sftp.example.com",
        sftp_username="skyvern",
        sftp_password="password",
        continue_on_empty=True,
    )
    browser_result = _browser_result(block)

    execution = await _execute_file_download(block, browser_result, download_dir)

    assert execution.result is browser_result
    assert execution.result.success is True
    execution.upload.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("download_target", "destination_fields", "expected_storage_type"),
    [
        (
            FileDownloadTarget.S3,
            {
                "s3_bucket": "bucket",
                "aws_access_key_id": "access-key",
                "aws_secret_access_key": "secret-key",
            },
            FileStorageType.S3,
        ),
        (
            FileDownloadTarget.AZURE,
            {
                "azure_storage_account_name": "account",
                "azure_storage_account_key": "account-key",
                "azure_blob_container_name": "container",
            },
            FileStorageType.AZURE,
        ),
        (
            FileDownloadTarget.GOOGLE_DRIVE,
            {
                "google_credential_id": "google-credential-id",
                "google_drive_folder_id": "https://drive.google.com/drive/folders/folder-id",
            },
            FileStorageType.GOOGLE_DRIVE,
        ),
    ],
    ids=["s3", "azure", "google-drive"],
)
async def test_external_target_dispatches_download_file(
    tmp_path: Path,
    download_target: FileDownloadTarget,
    destination_fields: dict[str, object],
    expected_storage_type: FileStorageType,
) -> None:
    download_dir = tmp_path / "downloads"
    downloaded_file = download_dir / "statement.pdf"
    block = _file_download_block(download_target, **destination_fields)
    browser_result = _browser_result(block, downloaded_filenames=("statement.pdf",))

    execution = await _execute_file_download(
        block,
        browser_result,
        download_dir,
        downloads_during_execute=("statement.pdf",),
    )

    assert execution.result is browser_result
    execution.upload.assert_awaited_once()
    assert execution.upload.await_args.kwargs["file_path"] == str(downloaded_file)
    assert execution.upload.await_args.kwargs["destination"].storage_type == expected_storage_type
    if download_target == FileDownloadTarget.GOOGLE_DRIVE:
        execution.get_google_workspace_credentials.assert_awaited_once()


@pytest.mark.asyncio
async def test_s3_destination_unwraps_secret_values(tmp_path: Path) -> None:
    download_dir = tmp_path / "downloads"
    block = _file_download_block(
        FileDownloadTarget.S3,
        s3_bucket="bucket",
        aws_access_key_id="{{aws_key}}",
        aws_secret_access_key="{{aws_secret}}",
    )
    execution = await _execute_file_download(
        block,
        _browser_result(block, downloaded_filenames=("statement.pdf",)),
        download_dir,
        secret_values={"{{aws_key}}": "actual-key", "{{aws_secret}}": "actual-secret"},
        downloads_during_execute=("statement.pdf",),
    )

    destination = execution.upload.await_args.kwargs["destination"]
    assert destination.storage_type == FileStorageType.S3
    assert destination.aws_access_key_id == "actual-key"
    assert destination.aws_secret_access_key == "actual-secret"


@pytest.mark.asyncio
async def test_s3_dispatch_rejects_empty_resolved_credentials_before_upload(tmp_path: Path) -> None:
    download_dir = tmp_path / "downloads"
    resolved_credential = "\t"
    block = _file_download_block(
        FileDownloadTarget.S3,
        s3_bucket="bucket",
        aws_access_key_id="{{aws_key}}",
        aws_secret_access_key="{{aws_secret}}",
    )
    execution = await _execute_file_download(
        block,
        _browser_result(block, downloaded_filenames=("statement.pdf",)),
        download_dir,
        secret_values={
            "{{aws_key}}": resolved_credential,
            "{{aws_secret}}": resolved_credential,
        },
        downloads_during_execute=("statement.pdf",),
    )

    assert execution.result.success is False
    assert "not configured" in execution.result.failure_reason
    assert resolved_credential not in execution.result.failure_reason
    assert execution.recorded_output.value is None
    execution.upload.assert_not_awaited()


@pytest.mark.asyncio
async def test_azure_destination_unwraps_secret_values(tmp_path: Path) -> None:
    download_dir = tmp_path / "downloads"
    block = _file_download_block(
        FileDownloadTarget.AZURE,
        azure_storage_account_name="{{azure_account}}",
        azure_storage_account_key="{{azure_key}}",
        azure_blob_container_name="container",
    )
    execution = await _execute_file_download(
        block,
        _browser_result(block, downloaded_filenames=("statement.pdf",)),
        download_dir,
        secret_values={"{{azure_account}}": "actualaccount", "{{azure_key}}": "actual-key"},
        downloads_during_execute=("statement.pdf",),
    )

    destination = execution.upload.await_args.kwargs["destination"]
    assert destination.storage_type == FileStorageType.AZURE
    assert destination.azure_storage_account_name == "actualaccount"
    assert destination.azure_storage_account_key == "actual-key"
    parsed_customer_uri = urlparse(destination.customer_uri)
    assert parsed_customer_uri.scheme == "https"
    assert parsed_customer_uri.hostname == "actualaccount.blob.core.windows.net"
    assert parsed_customer_uri.path.startswith("/container/")


@pytest.mark.asyncio
async def test_azure_dispatch_rejects_invalid_resolved_account_name_before_building_uri(tmp_path: Path) -> None:
    download_dir = tmp_path / "downloads"
    block = _file_download_block(
        FileDownloadTarget.AZURE,
        azure_storage_account_name="{{azure_account}}",
        azure_storage_account_key="{{azure_key}}",
        azure_blob_container_name="container",
    )

    with patch.object(block, "_get_azure_blob_uri", wraps=block._get_azure_blob_uri) as get_azure_blob_uri:
        execution = await _execute_file_download(
            block,
            _browser_result(block, downloaded_filenames=("statement.pdf",)),
            download_dir,
            secret_values={"{{azure_account}}": "127.0.0.1#", "{{azure_key}}": "actual-key"},
            downloads_during_execute=("statement.pdf",),
        )

    assert execution.result.success is False
    assert "account name" in execution.result.failure_reason
    get_azure_blob_uri.assert_not_called()
    execution.upload.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("resolved_account_name", "resolved_account_key"),
    [
        ("", "actual-key"),
        ("actualaccount", ""),
        ("  ", "actual-key"),
        ("actualaccount", "\t"),
    ],
    ids=["empty-account-name", "empty-account-key", "whitespace-account-name", "whitespace-account-key"],
)
async def test_azure_dispatch_rejects_empty_resolved_credentials_before_upload(
    tmp_path: Path,
    resolved_account_name: str,
    resolved_account_key: str,
) -> None:
    download_dir = tmp_path / "downloads"
    block = _file_download_block(
        FileDownloadTarget.AZURE,
        azure_storage_account_name="{{azure_account}}",
        azure_storage_account_key="{{azure_key}}",
        azure_blob_container_name="container",
    )
    execution = await _execute_file_download(
        block,
        _browser_result(block, downloaded_filenames=("statement.pdf",)),
        download_dir,
        secret_values={
            "{{azure_account}}": resolved_account_name,
            "{{azure_key}}": resolved_account_key,
        },
        downloads_during_execute=("statement.pdf",),
    )

    assert execution.result.success is False
    assert "not configured" in execution.result.failure_reason
    execution.upload.assert_not_awaited()


@pytest.mark.asyncio
async def test_azure_dispatch_without_resolved_configuration_raises_before_upload(tmp_path: Path) -> None:
    local_file = tmp_path / "statement.pdf"
    local_file.write_bytes(b"statement")
    block = FileDownloadBlock.model_construct(
        label="download",
        output_parameter=None,
        download_target=FileDownloadTarget.AZURE,
        azure_storage_account_name=None,
        azure_storage_account_key=None,
        azure_blob_container_name="container",
        path=None,
    )
    workflow_run_context = MagicMock()
    workflow_run_context.get_original_secret_value_or_none.return_value = None

    with patch("skyvern.forge.sdk.workflow.models.block.app") as mock_app:
        mock_app.AGENT_FUNCTION.upload_file_to_customer_storage = AsyncMock()
        with pytest.raises(AzureConfigurationError, match="not configured"):
            await block._dispatch_files_to_storage(
                storage_type=FileStorageType.AZURE,
                files_to_upload=[str(local_file)],
                workflow_run_id="workflow-run-id",
                workflow_run_block_id="workflow-run-block-id",
                organization_id="organization-id",
                workflow_run_context=workflow_run_context,
            )

    mock_app.AGENT_FUNCTION.upload_file_to_customer_storage.assert_not_awaited()


@pytest.mark.asyncio
async def test_google_drive_unwraps_credential_id_and_requests_drive_scopes(tmp_path: Path) -> None:
    download_dir = tmp_path / "downloads"
    block = _file_download_block(
        FileDownloadTarget.GOOGLE_DRIVE,
        google_credential_id="{{google_credential}}",
        google_drive_folder_id="https://drive.google.com/drive/folders/folder-id",
    )
    execution = await _execute_file_download(
        block,
        _browser_result(block, downloaded_filenames=("statement.pdf",)),
        download_dir,
        secret_values={"{{google_credential}}": "actual-google-credential"},
        downloads_during_execute=("statement.pdf",),
    )

    execution.get_google_workspace_credentials.assert_awaited_once_with(
        organization_id="organization-id",
        credential_id="actual-google-credential",
        required_scopes=list(google_oauth_service.GOOGLE_DRIVE_SCOPES),
    )
    destination = execution.upload.await_args.kwargs["destination"]
    assert destination.storage_type == FileStorageType.GOOGLE_DRIVE
    assert destination.google_access_token == "google-access-token"
    assert destination.google_drive_folder_id == "folder-id"


@pytest.mark.asyncio
async def test_sftp_unwraps_secrets_and_defaults_port_to_22(tmp_path: Path) -> None:
    download_dir = tmp_path / "downloads"
    block = _file_download_block(
        FileDownloadTarget.SFTP,
        sftp_host="sftp.example.com",
        sftp_username="{{sftp_user}}",
        sftp_password="{{sftp_password}}",
        sftp_private_key="{{sftp_key}}",
        sftp_private_key_passphrase="{{sftp_passphrase}}",
    )
    execution = await _execute_file_download(
        block,
        _browser_result(block, downloaded_filenames=("statement.pdf",)),
        download_dir,
        secret_values={
            "{{sftp_user}}": "actual-user",
            "{{sftp_password}}": "actual-password",
            "{{sftp_key}}": "actual-private-key",
            "{{sftp_passphrase}}": "actual-passphrase",
        },
        downloads_during_execute=("statement.pdf",),
    )

    destination = execution.upload.await_args.kwargs["destination"]
    assert destination.storage_type == FileStorageType.SFTP
    assert destination.sftp_port == 22
    assert destination.sftp_username == "actual-user"
    assert destination.sftp_password == "actual-password"
    assert destination.sftp_private_key == "actual-private-key"
    assert destination.sftp_private_key_passphrase == "actual-passphrase"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("download_target", "destination_fields", "sensitive_field", "literal_secret"),
    [
        (
            FileDownloadTarget.SFTP,
            {
                "sftp_host": "sftp.example.com",
                "sftp_username": "skyvern",
                "sftp_password": "literal-sftp-password",
            },
            "sftp_password",
            "literal-sftp-password",
        ),
        (
            FileDownloadTarget.S3,
            {
                "s3_bucket": "bucket",
                "aws_access_key_id": "access-key-id",
                "aws_secret_access_key": "literal-aws-secret-access-key",
            },
            "aws_secret_access_key",
            "literal-aws-secret-access-key",
        ),
        (
            FileDownloadTarget.AZURE,
            {
                "azure_storage_account_name": "storageaccount",
                "azure_storage_account_key": "literal-azure-storage-account-key",
                "azure_blob_container_name": "container",
            },
            "azure_storage_account_key",
            "literal-azure-storage-account-key",
        ),
    ],
    ids=["sftp-password", "aws-secret-access-key", "azure-storage-account-key"],
)
async def test_literal_destination_secret_dispatches_unchanged_when_encryption_is_disabled(
    tmp_path: Path,
    download_target: FileDownloadTarget,
    destination_fields: dict[str, object],
    sensitive_field: str,
    literal_secret: str,
) -> None:
    download_dir = tmp_path / "downloads"
    block = _file_download_block(download_target, **destination_fields)

    execution = await _execute_file_download(
        block,
        _browser_result(block, downloaded_filenames=("statement.pdf",)),
        download_dir,
        secret_values={},
        downloads_during_execute=("statement.pdf",),
    )

    assert execution.result.success is True
    destination = execution.upload.await_args.kwargs["destination"]
    assert getattr(destination, sensitive_field) == literal_secret


@pytest.mark.asyncio
async def test_unset_optional_sftp_secret_fields_do_not_block_dispatch(tmp_path: Path) -> None:
    download_dir = tmp_path / "downloads"
    block = _file_download_block(
        FileDownloadTarget.SFTP,
        sftp_host="sftp.example.com",
        sftp_username="skyvern",
        sftp_password="{{sftp_password}}",
    )

    execution = await _execute_file_download(
        block,
        _browser_result(block, downloaded_filenames=("statement.pdf",)),
        download_dir,
        secret_values={"{{sftp_password}}": "resolved-sftp-password"},
        downloads_during_execute=("statement.pdf",),
    )

    destination = execution.upload.await_args.kwargs["destination"]
    assert destination.sftp_password == "resolved-sftp-password"
    assert destination.sftp_private_key is None
    assert destination.sftp_private_key_passphrase is None
    resolver_calls = execution.workflow_run_context.get_original_secret_value_or_none.call_args_list
    assert all(call.args[0] is not None for call in resolver_calls)


@pytest.mark.asyncio
async def test_missing_sftp_host_fails_without_dispatch(tmp_path: Path) -> None:
    block = _file_download_block(
        FileDownloadTarget.SFTP,
        sftp_username="skyvern",
        sftp_password="password",
    )
    browser_result = _browser_result(block)

    execution = await _execute_file_download(block, browser_result, tmp_path / "downloads")

    assert execution.result.success is False
    assert execution.result.status == BlockStatus.failed
    assert "sftp_host" in (execution.result.failure_reason or "")
    execution.upload.assert_not_awaited()
    # Fail fast: a misconfigured destination is rejected before the browser download runs.
    execution.browser_execute.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("download_target", "destination_fields", "missing_field"),
    [
        (FileDownloadTarget.S3, {"aws_access_key_id": "key", "aws_secret_access_key": "secret"}, "s3_bucket"),
        (
            FileDownloadTarget.AZURE,
            {"azure_storage_account_name": "account", "azure_storage_account_key": "key"},
            "azure_blob_container_name",
        ),
        (
            FileDownloadTarget.GOOGLE_DRIVE,
            {"google_credential_id": "credential"},
            "google_drive_folder_id",
        ),
        (FileDownloadTarget.SFTP, {"sftp_host": "host", "sftp_username": "user"}, "sftp_password"),
    ],
    ids=["s3", "azure", "google-drive", "sftp"],
)
async def test_missing_required_destination_field_fails_without_dispatch(
    tmp_path: Path,
    download_target: FileDownloadTarget,
    destination_fields: dict[str, object],
    missing_field: str,
) -> None:
    block = _file_download_block(download_target, **destination_fields)

    execution = await _execute_file_download(block, _browser_result(block), tmp_path / "downloads")

    assert execution.result.success is False
    assert execution.result.status == BlockStatus.failed
    assert missing_field in (execution.result.failure_reason or "")
    execution.upload.assert_not_awaited()


@pytest.mark.asyncio
async def test_prompt_dispatches_only_selected_downloads(tmp_path: Path) -> None:
    download_dir = tmp_path / "downloads"
    downloaded_files = [download_dir / "invoice-1.pdf", download_dir / "invoice-2.pdf", download_dir / "report.csv"]
    selected_files = [str(downloaded_files[0]), str(downloaded_files[1])]
    block = _file_download_block(
        FileDownloadTarget.SFTP,
        sftp_host="sftp.example.com",
        sftp_username="skyvern",
        sftp_password="password",
        prompt="only the invoices",
    )
    browser_result = _browser_result(
        block,
        downloaded_filenames=("invoice-1.pdf", "invoice-2.pdf", "report.csv"),
    )

    execution = await _execute_file_download(
        block,
        browser_result,
        download_dir,
        selection_result=(selected_files, "The invoice files match."),
        downloads_during_execute=("invoice-1.pdf", "invoice-2.pdf", "report.csv"),
    )

    assert execution.result is browser_result
    execution.select_files.assert_awaited_once()
    assert set(execution.select_files.await_args.kwargs["files_to_upload"]) == {str(file) for file in downloaded_files}
    assert execution.upload.await_count == 2
    assert {call.kwargs["file_path"] for call in execution.upload.await_args_list} == set(selected_files)


@pytest.mark.asyncio
async def test_prompt_selector_only_receives_current_block_scope(tmp_path: Path) -> None:
    download_dir = tmp_path / "downloads"
    prior_file = _write_downloads(download_dir, "prior.pdf")[0]
    current_file = download_dir / "current.pdf"
    block = _file_download_block(
        FileDownloadTarget.SFTP,
        sftp_host="sftp.example.com",
        sftp_username="skyvern",
        sftp_password="password",
        prompt="upload the requested file",
    )

    execution = await _execute_file_download(
        block,
        _browser_result(block, downloaded_filenames=("current.pdf",)),
        download_dir,
        selection_result=([str(current_file)], "Current file matches."),
        downloads_during_execute=("current.pdf",),
    )

    execution.select_files.assert_awaited_once()
    assert execution.select_files.await_args.kwargs["files_to_upload"] == [str(current_file)]
    assert str(prior_file) not in execution.select_files.await_args.kwargs["files_to_upload"]
    assert execution.upload.await_args.kwargs["file_path"] == str(current_file)


@pytest.mark.asyncio
async def test_prompt_selector_rejects_prior_block_filename_not_in_candidates(tmp_path: Path) -> None:
    download_dir = tmp_path / "downloads"
    prior_file, current_file = _write_downloads(download_dir, "prior.pdf", "current.pdf")
    block = _file_download_block(
        FileDownloadTarget.SFTP,
        sftp_host="sftp.example.com",
        sftp_username="skyvern",
        sftp_password="password",
        prompt="upload prior.pdf",
    )
    handler = AsyncMock(
        return_value={"reasoning": "The user requested the prior file.", "files_to_upload": [prior_file.name]}
    )

    with (
        patch.object(LLMAPIHandlerFactory, "get_override_llm_api_handler", return_value=handler),
        patch("skyvern.forge.sdk.workflow.models.block.app") as mock_app,
    ):
        mock_app.DATABASE.observer.get_workflow_run_block = AsyncMock(return_value=None)
        with pytest.raises(ValueError, match="not candidate files"):
            await block._select_files_to_upload_with_prompt(
                prompt=block.prompt or "",
                files_to_upload=[str(current_file)],
                workflow_run_block_id="workflow-run-block-id",
                organization_id="organization-id",
            )


@pytest.mark.asyncio
async def test_prompt_selecting_no_downloads_is_successful_noop(tmp_path: Path) -> None:
    download_dir = tmp_path / "downloads"
    block = _file_download_block(
        FileDownloadTarget.SFTP,
        sftp_host="sftp.example.com",
        sftp_username="skyvern",
        sftp_password="password",
        prompt="only the invoices",
    )
    browser_result = _browser_result(
        block,
        downloaded_filenames=("invoice.pdf", "report.csv", "notes.txt"),
    )

    execution = await _execute_file_download(
        block,
        browser_result,
        download_dir,
        selection_result=([], "No files match."),
        downloads_during_execute=("invoice.pdf", "report.csv", "notes.txt"),
    )

    assert execution.result is browser_result
    assert execution.result.success is True
    execution.select_files.assert_awaited_once()
    execution.upload.assert_not_awaited()


@pytest.mark.asyncio
async def test_failed_browser_download_returns_without_dispatch(tmp_path: Path) -> None:
    block = _file_download_block(
        FileDownloadTarget.SFTP,
        sftp_host="sftp.example.com",
        sftp_username="skyvern",
        sftp_password="password",
    )
    browser_result = _browser_result(block, success=False)

    execution = await _execute_file_download(block, browser_result, tmp_path / "downloads")

    assert execution.result is browser_result
    execution.upload.assert_not_awaited()


def test_file_download_yaml_conversion_preserves_destination_fields() -> None:
    yaml_block = FileDownloadBlockYAML(
        label="download",
        navigation_goal="Download the requested files.",
        download_target=FileDownloadTarget.SFTP,
        sftp_host="sftp.example.com",
        sftp_port=2222,
        sftp_remote_path="incoming/reports",
    )

    block = block_yaml_to_block(yaml_block, {"download_output": _output_parameter()})

    assert isinstance(block, FileDownloadBlock)
    assert block.download_target == FileDownloadTarget.SFTP
    assert block.sftp_host == "sftp.example.com"
    assert block.sftp_port == 2222
    assert block.sftp_remote_path == "incoming/reports"


@pytest.mark.asyncio
async def test_non_cached_script_download_threads_destination_into_single_block_execution() -> None:
    validation_output = SimpleNamespace(
        label="download",
        output_parameter=_output_parameter(),
        workflow_run_id="workflow-run-id",
        organization_id="organization-id",
        browser_session_id="browser-session-id",
        context=SimpleNamespace(parent_workflow_run_block_id="parent-block-id"),
    )

    with (
        patch("skyvern.services.script_service.script_run_context_manager.get_cached_fn", return_value=None),
        patch(
            "skyvern.services.script_service._validate_and_get_output_parameter",
            new_callable=AsyncMock,
            return_value=validation_output,
        ),
        patch.object(FileDownloadBlock, "execute_safe", autospec=True) as execute_safe,
    ):
        from skyvern.services.script_service import download

        await download(
            prompt="Only upload invoices.",
            navigation_goal="Download all reports.",
            label="download",
            download_target="sftp",
            sftp_host="sftp.example.com",
            sftp_username="user",
            sftp_password="password",
        )

    execute_safe.assert_awaited_once()
    block = execute_safe.await_args.args[0]
    assert block.download_target == FileDownloadTarget.SFTP
    assert block.navigation_goal == "Download all reports."
    assert block.prompt == "Only upload invoices."
    assert block.sftp_host == "sftp.example.com"
    assert block.sftp_username == "user"
    assert block.sftp_password == "password"
    assert execute_safe.await_args.kwargs == {
        "workflow_run_id": "workflow-run-id",
        "parent_workflow_run_block_id": "parent-block-id",
        "organization_id": "organization-id",
        "browser_session_id": "browser-session-id",
    }
