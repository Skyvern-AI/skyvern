"""Tests for the cached download path in script_service.download().

Validates that the cached download flow:
1. Uploads files to remote storage (save_downloaded_files) so verification works
2. Renames files with download_suffix BEFORE the S3 upload
3. Verifies the download produced new files via get_downloaded_files
4. Falls back to AI on verification failure
5. Handles timeouts gracefully
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from structlog.testing import capture_logs

from tests.unit._fingerprint_expectations import expected_fingerprint

MODULE = "skyvern.services.script_service"


@pytest.fixture(autouse=True)
def _keyed_fingerprint(fingerprint_secret_key: str) -> str:
    return fingerprint_secret_key


def _make_mock_app(storage):
    """Create a mock that replaces the `app` module-level reference in script_service."""
    mock_app = MagicMock()
    mock_app.STORAGE = storage
    return mock_app


def _make_storage(get_side_effect=None):
    """Create a mock storage with async save/get methods."""
    s = MagicMock()
    s.save_downloaded_files = AsyncMock()
    s.get_downloaded_files = AsyncMock(side_effect=get_side_effect or [[], ["file.pdf"]])
    return s


@pytest.fixture()
def mock_context():
    ctx = MagicMock()
    ctx.organization_id = "o_test_org"
    ctx.workflow_run_id = "wr_test_run"
    ctx.browser_session_id = None
    ctx.run_id = ctx.workflow_run_id
    ctx.prompt = None
    return ctx


@pytest.fixture()
def setup(mock_context, tmp_path):
    """Provide a helper that configures all mocks for the download() function.

    Returns a callable that accepts optional overrides for storage/rename/list_files.
    """
    download_dir = tmp_path / "downloads"
    download_dir.mkdir()

    def _setup(
        get_side_effect=None,
        save_side_effect=None,
        list_files_side_effect=None,
        rename_mock=None,
    ):
        storage = _make_storage(get_side_effect)
        if save_side_effect is not None:
            storage.save_downloaded_files = AsyncMock(side_effect=save_side_effect)
        mock_app = _make_mock_app(storage)

        list_side = list_files_side_effect if list_files_side_effect is not None else [[]]
        rename = rename_mock or MagicMock()

        fallback_mock = AsyncMock()
        update_block_mock = AsyncMock()
        run_cached_mock = AsyncMock()
        wait_downloads_mock = AsyncMock()
        get_download_dir_mock = MagicMock(return_value=download_dir)

        all_patches = [
            patch(f"{MODULE}.app", mock_app),
            patch(f"{MODULE}.script_run_context_manager.get_cached_fn", return_value=AsyncMock()),
            patch(
                f"{MODULE}._create_workflow_block_run_and_task",
                new_callable=AsyncMock,
                return_value=("wrb_1", "tsk_1", "stp_1"),
            ),
            patch(f"{MODULE}._render_template_with_label", side_effect=lambda p, _: p),
            patch(f"{MODULE}.skyvern_context.ensure_context", return_value=mock_context),
            patch(f"{MODULE}._prepare_cached_block_inputs", new_callable=AsyncMock),
            patch(f"{MODULE}._run_cached_function", new=run_cached_mock),
            patch(f"{MODULE}._update_workflow_block", update_block_mock),
            patch(f"{MODULE}._fallback_to_ai_run", fallback_mock),
            patch(f"{MODULE}._clear_cached_block_overrides"),
            patch(f"{MODULE}.check_downloading_files_and_wait_for_download_to_complete", new=wait_downloads_mock),
            patch(f"{MODULE}.get_path_for_workflow_download_directory", new=get_download_dir_mock),
            patch(f"{MODULE}.list_files_in_directory", side_effect=list_side),
            patch(f"{MODULE}.rename_file", rename),
        ]

        for p in all_patches:
            p.start()

        return {
            "storage": storage,
            "app": mock_app,
            "patches": all_patches,
            "download_dir": download_dir,
            "fallback": fallback_mock,
            "update_block": update_block_mock,
            "rename": rename,
            "run_cached": run_cached_mock,
            "wait_downloads": wait_downloads_mock,
            "get_download_dir": get_download_dir_mock,
        }

    return _setup


def _cleanup(refs):
    for p in refs["patches"]:
        p.stop()


def test_render_template_with_label_injects_workflow_run_id():
    context = SimpleNamespace(workflow_run_id="wr_cached_run", script_run_parameters={}, loop_metadata=None)
    workflow_run_context = SimpleNamespace(
        values={},
        workflow_title="Cached workflow",
        workflow_id="wf_cached",
        workflow_permanent_id="wpid_cached",
        workflow_run_id="wr_cached_run",
        browser_session_id=None,
    )
    mock_app = MagicMock()
    mock_app.WORKFLOW_CONTEXT_MANAGER.get_workflow_run_context.return_value = workflow_run_context

    with (
        patch(f"{MODULE}.app", mock_app),
        patch(f"{MODULE}.skyvern_context.current", return_value=context),
    ):
        from skyvern.services.script_service import _render_template_with_label

        rendered = _render_template_with_label("exports/{{ workflow_run_id }}")

    assert rendered == "exports/wr_cached_run"


@pytest.mark.asyncio
async def test_cached_download_calls_save_downloaded_files(setup, tmp_path):
    """save_downloaded_files must be called so get_downloaded_files can find the file."""
    download_dir = tmp_path / "downloads"
    local_file = str(download_dir / "file1.pdf")
    refs = setup(
        get_side_effect=[[], ["file1.pdf"]],
        list_files_side_effect=[[], [local_file]],  # before=empty, after=has file (local verification)
    )
    try:
        from skyvern.services.script_service import download

        await download(prompt="Download invoice", label="test_block")

        refs["storage"].save_downloaded_files.assert_called_once_with(
            organization_id="o_test_org",
            run_id="wr_test_run",
        )
    finally:
        _cleanup(refs)


@pytest.mark.asyncio
async def test_cached_download_dispatches_only_new_files_to_destination(setup, tmp_path):
    download_dir = tmp_path / "downloads"
    existing_file = str(download_dir / "existing.pdf")
    downloaded_file = str(download_dir / "downloaded.pdf")
    refs = setup(
        get_side_effect=[[], ["downloaded.pdf"]],
        list_files_side_effect=[[existing_file], [existing_file, downloaded_file]],
    )
    try:
        from skyvern.services.script_service import FileDownloadBlock, download

        with patch.object(FileDownloadBlock, "_dispatch_files_to_storage", autospec=True) as dispatch_files:
            await download(
                prompt="Download invoice",
                label="test_block",
                download_target="s3",
                s3_bucket="bucket",
                aws_access_key_id="access-key",
                aws_secret_access_key="secret-key",
                region_name="us-east-1",
            )

        file_download_block = dispatch_files.await_args.args[0]
        assert file_download_block.download_target == "s3"
        assert file_download_block.s3_bucket == "bucket"
        assert file_download_block.aws_access_key_id == "access-key"
        assert file_download_block.aws_secret_access_key == "secret-key"
        assert file_download_block.region_name == "us-east-1"
        dispatch_files.assert_awaited_once_with(
            file_download_block,
            storage_type="s3",
            files_to_upload=[downloaded_file],
            workflow_run_id="wr_test_run",
            workflow_run_block_id="wrb_1",
            organization_id="o_test_org",
            workflow_run_context=refs["app"].WORKFLOW_CONTEXT_MANAGER.get_workflow_run_context.return_value,
        )
    finally:
        _cleanup(refs)


@pytest.mark.asyncio
async def test_cached_download_awaits_in_flight_downloads_before_snapshot(setup, tmp_path):
    download_dir = tmp_path / "downloads"
    downloaded_file = str(download_dir / "downloaded.pdf")
    refs = setup(
        get_side_effect=[[], ["downloaded.pdf"]],
        list_files_side_effect=[[], [downloaded_file]],
    )
    try:
        from skyvern.services.script_service import FileDownloadBlock, download

        async def assert_waited_before_dispatch(*_args, **_kwargs):
            refs["wait_downloads"].assert_awaited_once_with(
                download_dir=download_dir,
                organization_id="o_test_org",
                browser_session_id=None,
            )

        with patch.object(
            FileDownloadBlock,
            "_dispatch_files_to_storage",
            autospec=True,
            side_effect=assert_waited_before_dispatch,
        ) as dispatch_files:
            await download(
                prompt="Download invoice",
                label="test_block",
                download_target="s3",
                s3_bucket="bucket",
                aws_access_key_id="access-key",
                aws_secret_access_key="secret-key",
            )

        refs["wait_downloads"].assert_awaited_once_with(
            download_dir=download_dir,
            organization_id="o_test_org",
            browser_session_id=None,
        )
        dispatch_files.assert_awaited_once()
    finally:
        _cleanup(refs)


@pytest.mark.asyncio
async def test_cached_download_uses_parent_run_id_for_download_dir(setup, mock_context, tmp_path):
    mock_context.run_id = "wr_parent_run"
    mock_context.workflow_run_id = "wr_child_run"
    download_dir = tmp_path / "downloads"
    downloaded_file = str(download_dir / "downloaded.pdf")
    refs = setup(
        get_side_effect=[[], ["downloaded.pdf"]],
        list_files_side_effect=[[], [downloaded_file]],
    )
    try:
        from skyvern.services.script_service import download

        await download(prompt="Download invoice", label="test_block")

        refs["get_download_dir"].assert_called_once_with("wr_parent_run")
    finally:
        _cleanup(refs)


@pytest.mark.asyncio
async def test_cached_download_dispatch_failure_does_not_fallback_to_ai(setup, tmp_path):
    download_dir = tmp_path / "downloads"
    downloaded_file = str(download_dir / "downloaded.pdf")
    refs = setup(
        get_side_effect=[[], ["downloaded.pdf"]],
        list_files_side_effect=[[], [downloaded_file]],
    )
    try:
        from skyvern.services.script_service import FileDownloadBlock, download

        with (
            patch.object(
                FileDownloadBlock,
                "_dispatch_files_to_storage",
                autospec=True,
                side_effect=RuntimeError("destination unavailable"),
            ) as dispatch_files,
            pytest.raises(RuntimeError, match="destination unavailable"),
        ):
            await download(
                prompt="Download invoice",
                label="test_block",
                download_target="s3",
                s3_bucket="bucket",
                aws_access_key_id="access-key",
                aws_secret_access_key="secret-key",
            )

        dispatch_files.assert_awaited_once()
        refs["fallback"].assert_not_awaited()
        assert refs["update_block"].await_args.args[1].value == "failed"
        assert refs["update_block"].await_args.kwargs["failure_reason"] == "destination unavailable"
    finally:
        _cleanup(refs)


@pytest.mark.asyncio
async def test_cached_download_ai_fallback_still_dispatches_to_destination(setup, tmp_path):
    download_dir = tmp_path / "downloads"
    fallback_file = str(download_dir / "fallback.pdf")
    refs = setup(
        get_side_effect=[[]],
        list_files_side_effect=[[], [], [fallback_file]],
    )
    refs["run_cached"].side_effect = RuntimeError("cached download failed")

    async def create_fallback_download(**kwargs):
        Path(fallback_file).write_bytes(b"fallback contents")

    refs["fallback"].side_effect = create_fallback_download
    try:
        from skyvern.services.script_service import FileDownloadBlock, download

        with patch.object(FileDownloadBlock, "_dispatch_files_to_storage", autospec=True) as dispatch_files:
            await download(
                prompt="Download invoice",
                label="test_block",
                download_target="s3",
                s3_bucket="bucket",
                aws_access_key_id="access-key",
                aws_secret_access_key="secret-key",
            )

        refs["fallback"].assert_awaited_once()
        dispatch_files.assert_awaited_once()
        assert dispatch_files.await_args.kwargs["files_to_upload"] == [fallback_file]
    finally:
        _cleanup(refs)


@pytest.mark.asyncio
async def test_cached_website_download_never_dispatches_external_storage(setup, tmp_path):
    download_dir = tmp_path / "downloads"
    downloaded_file = str(download_dir / "downloaded.pdf")
    refs = setup(
        get_side_effect=[[], ["downloaded.pdf"]],
        list_files_side_effect=[[], [downloaded_file]],
    )
    try:
        from skyvern.services.script_service import FileDownloadBlock, download

        with patch.object(FileDownloadBlock, "_dispatch_files_to_storage", autospec=True) as dispatch_files:
            await download(
                prompt="Download invoice",
                label="test_block",
                download_target="website",
                s3_bucket="stale-bucket",
                aws_access_key_id="stale-key",
                aws_secret_access_key="stale-secret",
            )

        dispatch_files.assert_not_awaited()
    finally:
        _cleanup(refs)


@pytest.mark.asyncio
async def test_cached_download_applies_destination_prompt_before_dispatch(setup, tmp_path):
    download_dir = tmp_path / "downloads"
    invoice_file = str(download_dir / "invoice.pdf")
    report_file = str(download_dir / "report.csv")
    refs = setup(
        get_side_effect=[[], ["invoice.pdf", "report.csv"]],
        list_files_side_effect=[[], [invoice_file, report_file]],
    )
    try:
        from skyvern.services.script_service import FileDownloadBlock, download

        selected_files = AsyncMock(return_value=([invoice_file], "Only the invoice matches."))
        with (
            patch.object(FileDownloadBlock, "_select_files_to_upload_with_prompt", selected_files),
            patch.object(FileDownloadBlock, "_dispatch_files_to_storage", autospec=True) as dispatch_files,
        ):
            await download(
                prompt="Only upload invoice PDFs.",
                navigation_goal="Download the invoice and report.",
                label="test_block",
                download_target="s3",
                s3_bucket="bucket",
                aws_access_key_id="access-key",
                aws_secret_access_key="secret-key",
            )

        file_download_block = dispatch_files.await_args.args[0]
        selected_files.assert_awaited_once_with(
            file_download_block,
            prompt="Only upload invoice PDFs.",
            files_to_upload=[invoice_file, report_file],
            workflow_run_block_id="wrb_1",
            organization_id="o_test_org",
        )
        assert dispatch_files.await_args.kwargs["files_to_upload"] == [invoice_file]
    finally:
        _cleanup(refs)


@pytest.mark.asyncio
async def test_cached_download_detects_same_path_content_replacement(setup, tmp_path):
    download_dir = tmp_path / "downloads"
    replaced_file = download_dir / "invoice.pdf"
    replaced_file.write_bytes(b"prior block contents")
    refs = setup(
        get_side_effect=[[], ["invoice.pdf"]],
        list_files_side_effect=[[str(replaced_file)], [str(replaced_file)]],
    )
    refs["run_cached"].side_effect = lambda _: replaced_file.write_bytes(b"this block contents")
    try:
        from skyvern.services.script_service import FileDownloadBlock, download

        with (
            patch(f"{MODULE}.CACHED_DOWNLOAD_NO_FILE_GRACE_SECONDS", 0),
            patch.object(FileDownloadBlock, "_dispatch_files_to_storage", autospec=True) as dispatch_files,
        ):
            await download(
                prompt="Download invoice",
                label="test_block",
                download_target="s3",
                s3_bucket="bucket",
                aws_access_key_id="access-key",
                aws_secret_access_key="secret-key",
            )

        dispatch_files.assert_awaited_once()
        assert dispatch_files.await_args.kwargs["files_to_upload"] == [str(replaced_file)]
        refs["fallback"].assert_not_awaited()
    finally:
        _cleanup(refs)


@pytest.mark.asyncio
async def test_cached_missing_destination_halts_without_fallback(setup, tmp_path):
    download_dir = tmp_path / "downloads"
    downloaded_file = str(download_dir / "invoice.pdf")
    refs = setup(
        get_side_effect=[[], ["invoice.pdf"]],
        list_files_side_effect=[[], [downloaded_file]],
    )
    try:
        from skyvern.exceptions import ScriptTerminationException
        from skyvern.services.script_service import FileDownloadBlock, download

        with patch.object(FileDownloadBlock, "_dispatch_files_to_storage", autospec=True) as dispatch_files:
            # A misconfigured cached destination must halt the script (raise), not just return a
            # failed status, so later generated statements do not run; and it must not download or fall back.
            with pytest.raises(ScriptTerminationException):
                await download(
                    prompt="Download invoice",
                    label="test_block",
                    download_target="s3",
                    aws_access_key_id="access-key",
                    aws_secret_access_key="secret-key",
                )

        dispatch_files.assert_not_awaited()
        refs["fallback"].assert_not_awaited()
    finally:
        _cleanup(refs)


@pytest.mark.asyncio
async def test_rename_happens_before_s3_upload(setup, tmp_path):
    """download_suffix rename must happen BEFORE save_downloaded_files.

    This ensures remote storage receives the correctly-named file and subsequent
    blocks get the right URLs. Matches the agent path ordering in agent.py.
    """
    download_dir = tmp_path / "downloads"
    call_order: list[str] = []

    rename_mock = MagicMock(side_effect=lambda path, name: call_order.append("rename"))

    async def track_save(**kwargs):
        call_order.append("save")

    fake_file = str(download_dir / "uuid-random.pdf")

    refs = setup(
        get_side_effect=[[], ["invoice.pdf"]],
        save_side_effect=track_save,
        # before, local verify, rename local_files_after, existing_names dedup lookup
        list_files_side_effect=[[], [fake_file], [fake_file], [fake_file]],
        rename_mock=rename_mock,
    )
    try:
        from skyvern.services.script_service import download

        await download(prompt="Download invoice", download_suffix="invoice", label="test_block")

        assert call_order == ["rename", "save"], f"Expected rename before save, got: {call_order}"
    finally:
        _cleanup(refs)


@pytest.mark.asyncio
async def test_download_suffix_rename_uses_file_path_directly(setup, tmp_path):
    """rename_file should receive the absolute path from list_files_in_directory,
    not a reconstructed path via Path joining."""
    download_dir = tmp_path / "downloads"
    abs_path = str(download_dir / "abc123.pdf")

    rename_mock = MagicMock(return_value=str(download_dir / "invoice.pdf"))
    refs = setup(
        get_side_effect=[[], ["invoice.pdf"]],
        # before, local verify, rename local_files_after, existing_names dedup lookup
        list_files_side_effect=[[], [abs_path], [abs_path], [abs_path]],
        rename_mock=rename_mock,
    )
    try:
        from skyvern.services.script_service import download

        await download(prompt="Download invoice", download_suffix="invoice", label="test_block")

        rename_mock.assert_called_once_with(abs_path, "invoice.pdf")
    finally:
        _cleanup(refs)


@pytest.mark.asyncio
async def test_cached_download_rename_emits_finalize_lineage(setup, tmp_path):
    """The cached-script (run_with=code) rename path must emit the same download_suffix_finalize_rename
    lineage as the agent path, tagged execution_path=cached_script, so a code-mode freeze is attributable."""
    download_dir = tmp_path / "downloads"
    abs_path = str(download_dir / "abc123.pdf")

    rename_mock = MagicMock(return_value=str(download_dir / "invoice.pdf"))
    refs = setup(
        get_side_effect=[[], ["invoice.pdf"]],
        list_files_side_effect=[[], [abs_path], [abs_path], [abs_path]],
        rename_mock=rename_mock,
    )
    try:
        from skyvern.services.script_service import download

        with capture_logs() as cap:
            await download(
                prompt="Download invoice",
                download_suffix="invoice",
                label="test_block",
                cache_key="bill_usage_download",
            )

        events = [e for e in cap if e.get("event") == "download_suffix_finalize_rename"]
        assert len(events) == 1, "cached rename path did not emit download_suffix_finalize_rename"
        event = events[0]
        assert event["execution_path"] == "cached_script"
        assert event["finalize_workflow_run_id"] == "wr_test_run"
        assert event["finalize_task_id"] == "tsk_1"  # per-download-task attribution in cached mode
        assert event["block_label"] == "bill_usage_download"  # persisted block label (= cache_key)
        assert event["passed_download_suffix_fp"] == expected_fingerprint("invoice")
        assert event["pre_rename_filename_fp"] == expected_fingerprint("abc123.pdf")
        assert event["desired_name_fp"] == expected_fingerprint("invoice.pdf")
        assert event["will_rename"] is True
    finally:
        _cleanup(refs)


@pytest.mark.asyncio
async def test_verification_raises_when_no_new_file(setup):
    """When no new file appears on local filesystem, the poll loop should exhaust
    the grace period and fall back to AI."""
    refs = setup(
        get_side_effect=[[], [], [], []],
        list_files_side_effect=[[] for _ in range(10)],  # before + polls all empty
    )
    try:
        from skyvern.services.script_service import download

        call_count = 0

        def advancing_time():
            nonlocal call_count
            call_count += 1
            return call_count * 30.0

        with (
            patch(f"{MODULE}.asyncio.sleep", new_callable=AsyncMock),
            patch(f"{MODULE}.asyncio.get_running_loop") as mock_loop,
        ):
            mock_loop.return_value.time = advancing_time
            await download(prompt="Download invoice", label="test_block")

        refs["fallback"].assert_called_once()
        error_arg = refs["fallback"].call_args.kwargs.get("error")
        assert "no file produced" in str(error_arg).lower() or "did not produce" in str(error_arg).lower()
    finally:
        _cleanup(refs)


@pytest.mark.asyncio
async def test_verification_retries_before_failing(setup, tmp_path):
    """get_downloaded_files retries up to 3 times before declaring failure."""
    download_dir = tmp_path / "downloads"
    local_file = str(download_dir / "file.pdf")
    # Before: 0 files. After: [], [], ["file.pdf"] (succeeds on 3rd attempt)
    refs = setup(
        get_side_effect=[[], [], [], ["file.pdf"]],
        list_files_side_effect=[[], [local_file]],  # local verification passes
    )
    try:
        from skyvern.services.script_service import download

        with patch(f"{MODULE}.asyncio.sleep", new_callable=AsyncMock):
            await download(prompt="Download invoice", label="test_block")

        refs["fallback"].assert_not_called()
        assert refs["storage"].get_downloaded_files.call_count == 4  # 1 before + 3 retries
    finally:
        _cleanup(refs)


@pytest.mark.asyncio
async def test_save_timeout_skips_verification(setup, tmp_path):
    """TimeoutError on save_downloaded_files should skip S3 verification entirely.
    No point retrying get_downloaded_files when we know S3 is degraded.
    Local file verification still passes since a file was downloaded."""
    download_dir = tmp_path / "downloads"
    local_file = str(download_dir / "file.pdf")
    refs = setup(
        save_side_effect=asyncio.TimeoutError(),
        get_side_effect=[[]],  # only the before-check runs; after-check is skipped
        list_files_side_effect=[[], [local_file]],  # local verification passes
    )
    try:
        from skyvern.services.script_service import download

        await download(prompt="Download invoice", label="test_block")

        # S3 verification skipped, but local verification passed → block completes
        refs["fallback"].assert_not_called()
        refs["update_block"].assert_called_once()
        # get_downloaded_files called only once (before-check), not 3 more times for after
        assert refs["storage"].get_downloaded_files.call_count == 1
    finally:
        _cleanup(refs)


@pytest.mark.asyncio
async def test_save_generic_exception_skips_verification(setup, tmp_path):
    """Non-timeout S3 failure (e.g., permission error) should also skip S3 verification.
    Matches agent.py which catches both TimeoutError and generic Exception.
    Local file verification still passes since a file was downloaded."""
    download_dir = tmp_path / "downloads"
    local_file = str(download_dir / "file.pdf")
    refs = setup(
        save_side_effect=RuntimeError("S3 permission denied"),
        get_side_effect=[[]],  # only the before-check runs; after-check is skipped
        list_files_side_effect=[[], [local_file]],  # local verification passes
    )
    try:
        from skyvern.services.script_service import download

        await download(prompt="Download invoice", label="test_block")

        # S3 verification skipped, but local verification passed → block completes
        refs["fallback"].assert_not_called()
        refs["update_block"].assert_called_once()
        # get_downloaded_files called only once (before-check), not 3 more times for after
        assert refs["storage"].get_downloaded_files.call_count == 1
    finally:
        _cleanup(refs)


@pytest.mark.asyncio
async def test_get_before_timeout_skips_verification(setup, tmp_path):
    """If the before-check times out, S3 verification should be skipped entirely
    to avoid spurious AI fallbacks under degraded storage.
    Local file verification still passes since a file was downloaded."""
    download_dir = tmp_path / "downloads"
    local_file = str(download_dir / "file.pdf")
    refs = setup(
        get_side_effect=asyncio.TimeoutError(),
        list_files_side_effect=[[], [local_file]],  # local verification passes
    )
    try:
        from skyvern.services.script_service import download

        with patch(f"{MODULE}.asyncio.sleep", new_callable=AsyncMock):
            await download(prompt="Download invoice", label="test_block")

        # Should NOT fall back — local verification passed, S3 verification skipped
        refs["fallback"].assert_not_called()
    finally:
        _cleanup(refs)


@pytest.mark.asyncio
async def test_get_after_timeout_skips_verification(setup, tmp_path):
    """If the after-check times out, S3 verification should be skipped.
    Local file verification still passes since a file was downloaded."""
    download_dir = tmp_path / "downloads"
    local_file = str(download_dir / "file.pdf")
    call_count = {"n": 0}

    async def get_side_effect(**kwargs):
        call_count["n"] += 1
        if call_count["n"] == 1:
            return []  # before-check succeeds
        raise asyncio.TimeoutError()

    refs = setup(
        get_side_effect=get_side_effect,
        list_files_side_effect=[[], [local_file]],  # local verification passes
    )
    try:
        from skyvern.services.script_service import download

        with patch(f"{MODULE}.asyncio.sleep", new_callable=AsyncMock):
            await download(prompt="Download invoice", label="test_block")

        refs["fallback"].assert_not_called()
    finally:
        _cleanup(refs)


@pytest.mark.asyncio
async def test_no_rename_without_download_suffix(setup, tmp_path):
    """When download_suffix is not provided, rename should not be called."""
    download_dir = tmp_path / "downloads"
    local_file = str(download_dir / "file.pdf")
    rename_mock = MagicMock()
    refs = setup(
        get_side_effect=[[], ["file.pdf"]],
        rename_mock=rename_mock,
        list_files_side_effect=[[], [local_file]],  # local verification passes
    )
    try:
        from skyvern.services.script_service import download

        await download(prompt="Download invoice", label="test_block")

        rename_mock.assert_not_called()
    finally:
        _cleanup(refs)


@pytest.mark.asyncio
async def test_rename_skips_crdownload_files(setup, tmp_path):
    """Files with .crdownload extension (incomplete downloads) should be skipped."""
    download_dir = tmp_path / "downloads"
    incomplete = str(download_dir / "file.crdownload")
    complete = str(download_dir / "invoice.pdf")

    rename_mock = MagicMock(return_value=str(download_dir / "renamed.pdf"))
    refs = setup(
        get_side_effect=[[], ["invoice.pdf"]],
        # before, local verify, rename local_files_after, existing_names dedup lookup
        list_files_side_effect=[
            [incomplete],
            [incomplete, complete],
            [incomplete, complete],
            [incomplete, complete],
        ],
        rename_mock=rename_mock,
    )
    try:
        from skyvern.services.script_service import download

        await download(prompt="Download invoice", download_suffix="renamed", label="test_block")

        rename_mock.assert_called_once_with(complete, "renamed.pdf")
    finally:
        _cleanup(refs)


@pytest.mark.asyncio
async def test_rename_handles_name_collision(setup, tmp_path):
    """When target filename already exists, a counter suffix should be added."""
    download_dir = tmp_path / "downloads"
    download_dir.mkdir(exist_ok=True)
    new_file = str(download_dir / "uuid.pdf")
    # Create collision: "invoice.pdf" already exists
    (download_dir / "invoice.pdf").touch()

    rename_mock = MagicMock(return_value=str(download_dir / "invoice_1.pdf"))
    refs = setup(
        get_side_effect=[[], ["invoice_1.pdf"]],
        # before, local verify, rename local_files_after, existing_names dedup lookup (already has invoice.pdf)
        list_files_side_effect=[[], [new_file], [new_file], [new_file, str(download_dir / "invoice.pdf")]],
        rename_mock=rename_mock,
    )
    try:
        from skyvern.services.script_service import download

        await download(prompt="Download invoice", download_suffix="invoice", label="test_block")

        rename_mock.assert_called_once_with(new_file, "invoice_1.pdf")
    finally:
        _cleanup(refs)


@pytest.mark.asyncio
async def test_block_marked_completed_on_success(setup, tmp_path):
    """On successful download + verification, block should be marked completed."""
    download_dir = tmp_path / "downloads"
    local_file = str(download_dir / "file.pdf")
    refs = setup(
        get_side_effect=[[], ["file.pdf"]],
        list_files_side_effect=[[], [local_file]],  # local verification passes
    )
    try:
        from skyvern.services.script_service import download

        await download(prompt="Download invoice", label="test_block")

        refs["update_block"].assert_called_once()
        refs["fallback"].assert_not_called()
    finally:
        _cleanup(refs)


@pytest.mark.asyncio
async def test_poll_fails_when_no_file_downloaded(setup):
    """SKY-8433: When download_selector() returns None and click silently succeeds
    without downloading anything, the poll loop should detect no new files
    after the grace period and trigger AI fallback."""
    refs = setup(
        get_side_effect=[[]],
        list_files_side_effect=[[] for _ in range(10)],
    )
    try:
        from skyvern.services.script_service import download

        # Mock time to advance past grace period, mock sleep to not wait
        call_count = 0

        def advancing_time():
            nonlocal call_count
            call_count += 1
            return call_count * 30.0

        with (
            patch(f"{MODULE}.asyncio.sleep", new_callable=AsyncMock),
            patch(f"{MODULE}.asyncio.get_running_loop") as mock_loop,
        ):
            mock_loop.return_value.time = advancing_time
            await download(prompt="Download invoice", label="test_block")

        refs["fallback"].assert_called_once()
        error_arg = refs["fallback"].call_args.kwargs.get("error")
        assert "no file produced" in str(error_arg).lower() or "did not produce" in str(error_arg).lower()
        refs["storage"].save_downloaded_files.assert_not_called()
    finally:
        _cleanup(refs)


@pytest.mark.asyncio
async def test_poll_waits_for_crdownload_to_complete(setup, tmp_path):
    """When a .crdownload file appears (browser-native download in progress),
    the poll loop should keep waiting until the complete file appears."""
    download_dir = tmp_path / "downloads"
    incomplete_file = str(download_dir / "invoice.pdf.crdownload")
    complete_file = str(download_dir / "invoice.pdf")
    refs = setup(
        get_side_effect=[[], [complete_file]],
        list_files_side_effect=[
            [],  # before
            [incomplete_file],  # poll 1: .crdownload detected, keep waiting
            [incomplete_file],  # poll 2: still downloading
            [complete_file],  # poll 3: download finished, .crdownload renamed
        ],
    )
    try:
        from skyvern.services.script_service import download

        with patch(f"{MODULE}.asyncio.sleep", new_callable=AsyncMock):
            await download(prompt="Download invoice", label="test_block")

        refs["fallback"].assert_not_called()
        refs["update_block"].assert_called_once()
        refs["storage"].save_downloaded_files.assert_called_once()
    finally:
        _cleanup(refs)


@pytest.mark.asyncio
async def test_cached_download_waits_for_all_files_before_dispatch(setup, tmp_path):
    download_dir = tmp_path / "downloads"
    first_complete = str(download_dir / "a.pdf")
    second_incomplete = str(download_dir / "b.pdf.crdownload")
    second_complete = str(download_dir / "b.pdf")
    refs = setup(
        get_side_effect=[[], [first_complete, second_complete]],
        list_files_side_effect=[
            [],
            [first_complete, second_incomplete],
            [first_complete, second_complete],
        ],
    )
    try:
        from skyvern.services.script_service import FileDownloadBlock, download

        async def assert_partial_state_not_settled(_delay):
            refs["storage"].save_downloaded_files.assert_not_awaited()
            dispatch_files.assert_not_awaited()
            refs["update_block"].assert_not_awaited()

        with (
            patch.object(FileDownloadBlock, "_dispatch_files_to_storage", autospec=True) as dispatch_files,
            patch(
                f"{MODULE}.asyncio.sleep",
                new_callable=AsyncMock,
                side_effect=assert_partial_state_not_settled,
            ) as sleep,
        ):
            await download(
                prompt="Download reports",
                label="test_block",
                download_target="s3",
                s3_bucket="bucket",
                aws_access_key_id="access-key",
                aws_secret_access_key="secret-key",
            )

        sleep.assert_awaited_once()
        dispatch_files.assert_awaited_once()
        assert dispatch_files.await_args.kwargs["files_to_upload"] == [first_complete, second_complete]
        assert refs["update_block"].await_args.args[1].value == "completed"
    finally:
        _cleanup(refs)


@pytest.mark.asyncio
async def test_poll_passes_immediately_with_complete_file(setup, tmp_path):
    """When a complete file appears immediately after the cached function
    (CDP atomic write), the poll should pass on the first check with no waiting."""
    download_dir = tmp_path / "downloads"
    local_file = str(download_dir / "invoice.pdf")
    refs = setup(
        get_side_effect=[[], [local_file]],
        list_files_side_effect=[[], [local_file]],  # before=empty, first poll=file present
    )
    try:
        from skyvern.services.script_service import download

        sleep_mock = AsyncMock()

        # Patching `script_service.asyncio.sleep` directly mutates the shared
        # asyncio module, so a stray sleep from any in-process code lands on
        # the mock and breaks `assert_not_called`. Swap script_service's
        # asyncio reference for a proxy that intercepts only `sleep`.
        class _AsyncioProxy:
            def __init__(self, sleep_attr):
                self.sleep = sleep_attr

            def __getattr__(self, name):
                return getattr(asyncio, name)

        with patch(f"{MODULE}.asyncio", _AsyncioProxy(sleep_mock)):
            await download(prompt="Download invoice", label="test_block")

        refs["fallback"].assert_not_called()
        refs["update_block"].assert_called_once()
        refs["storage"].save_downloaded_files.assert_called_once()
        # Should not have slept — file was there immediately
        sleep_mock.assert_not_called()
    finally:
        _cleanup(refs)


@pytest.mark.asyncio
async def test_poll_grace_period_only_crdownload_fails(setup, tmp_path):
    """If only .crdownload files appear and never complete within the timeout,
    the poll should eventually fail and trigger AI fallback."""
    download_dir = tmp_path / "downloads"
    incomplete_file = str(download_dir / "invoice.pdf.crdownload")
    refs = setup(
        get_side_effect=[[]],
        # Keep returning only .crdownload forever — simulate stalled download
        list_files_side_effect=[[], *[[incomplete_file]] * 200],
    )
    try:
        from skyvern.services.script_service import download

        # Mock sleep to not actually wait, and mock time to advance past timeout
        call_count = 0
        real_time = asyncio.get_running_loop().time

        def advancing_time():
            nonlocal call_count
            call_count += 1
            # Each call advances 10s, so after ~30 calls we exceed _DOWNLOAD_TIMEOUT (300s)
            return real_time() + (call_count * 10)

        with (
            patch(f"{MODULE}.asyncio.sleep", new_callable=AsyncMock),
            patch(f"{MODULE}.asyncio.get_running_loop") as mock_loop,
        ):
            mock_loop.return_value.time = advancing_time
            await download(prompt="Download invoice", label="test_block")

        refs["fallback"].assert_called_once()
        error_arg = refs["fallback"].call_args.kwargs.get("error")
        assert "never completed" in str(error_arg).lower() or "timed out" in str(error_arg).lower()
    finally:
        _cleanup(refs)


@pytest.mark.asyncio
async def test_poll_crdownload_disappears_without_complete_file(setup, tmp_path):
    """When a .crdownload file appears then disappears (download cancelled/failed)
    without a complete file replacing it, the loop should eventually time out
    instead of looping forever."""
    download_dir = tmp_path / "downloads"
    incomplete_file = str(download_dir / "invoice.pdf.crdownload")
    refs = setup(
        get_side_effect=[[]],
        list_files_side_effect=[
            [],  # before
            [incomplete_file],  # poll 1: .crdownload appears
            [],  # poll 2: .crdownload disappeared, no complete file
            [],  # poll 3+: still nothing
            *[[] for _ in range(50)],
        ],
    )
    try:
        from skyvern.services.script_service import download

        call_count = 0

        def advancing_time():
            nonlocal call_count
            call_count += 1
            return call_count * 10.0

        with (
            patch(f"{MODULE}.asyncio.sleep", new_callable=AsyncMock),
            patch(f"{MODULE}.asyncio.get_running_loop") as mock_loop,
        ):
            mock_loop.return_value.time = advancing_time
            await download(prompt="Download invoice", label="test_block")

        refs["fallback"].assert_called_once()
        error_arg = refs["fallback"].call_args.kwargs.get("error")
        assert "disappeared" in str(error_arg).lower() or "did not produce" in str(error_arg).lower()
    finally:
        _cleanup(refs)


@pytest.mark.asyncio
async def test_poll_succeeds_with_late_start_and_long_running_crdownload(setup, tmp_path):
    """SKY-9431: a download that starts late within the new 60s grace and then
    runs long enough that elapsed-from-poll-start crosses 300s should still
    succeed, because the in-progress timeout is anchored at first detection."""
    download_dir = tmp_path / "downloads"
    incomplete_file = str(download_dir / "invoice.pdf.crdownload")
    complete_file = str(download_dir / "invoice.pdf")
    refs = setup(
        get_side_effect=[[], [complete_file]],
        list_files_side_effect=[
            [],  # idx 0: pre-loop snapshot
            *[[] for _ in range(10)],  # idx 1..10: iters 1..10, elapsed 5..50
            *[[incomplete_file] for _ in range(51)],  # idx 11..61: iters 11..61
            [complete_file],  # idx 62: iter 62, complete file appears
        ],
    )
    try:
        from skyvern.services.script_service import download

        call_count = 0

        def advancing_time():
            nonlocal call_count
            call_count += 1
            return call_count * 5.0

        with (
            patch(f"{MODULE}.asyncio.sleep", new_callable=AsyncMock),
            patch(f"{MODULE}.asyncio.get_running_loop") as mock_loop,
        ):
            mock_loop.return_value.time = advancing_time
            await download(prompt="Download invoice", label="test_block")

        refs["fallback"].assert_not_called()
        refs["update_block"].assert_called_once()
        refs["storage"].save_downloaded_files.assert_called_once()
    finally:
        _cleanup(refs)
