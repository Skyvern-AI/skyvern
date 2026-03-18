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
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

MODULE = "skyvern.services.script_service"


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
            patch(f"{MODULE}._run_cached_function", new_callable=AsyncMock),
            patch(f"{MODULE}._update_workflow_block", update_block_mock),
            patch(f"{MODULE}._fallback_to_ai_run", fallback_mock),
            patch(f"{MODULE}._clear_cached_block_overrides"),
            patch(f"{MODULE}.get_path_for_workflow_download_directory", return_value=download_dir),
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
        }

    return _setup


def _cleanup(refs):
    for p in refs["patches"]:
        p.stop()


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
        list_files_side_effect=[[], [fake_file], [fake_file]],  # before, local verify, rename
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
        list_files_side_effect=[[], [abs_path], [abs_path]],  # before, local verify, rename
        rename_mock=rename_mock,
    )
    try:
        from skyvern.services.script_service import download

        await download(prompt="Download invoice", download_suffix="invoice", label="test_block")

        rename_mock.assert_called_once_with(abs_path, "invoice.pdf")
    finally:
        _cleanup(refs)


@pytest.mark.asyncio
async def test_verification_raises_when_no_new_file(setup):
    """When no new file appears on local filesystem, the cached path should raise
    and fall back to AI."""
    refs = setup(
        get_side_effect=[[], [], [], []],
        list_files_side_effect=[[], []],  # before=empty, after=still empty (local verification fails)
    )
    try:
        from skyvern.services.script_service import download

        await download(prompt="Download invoice", label="test_block")

        refs["fallback"].assert_called_once()
        error_arg = refs["fallback"].call_args.kwargs.get("error")
        assert "did not produce a new file on the local filesystem" in str(error_arg)
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
        list_files_side_effect=[[], [incomplete, complete], [incomplete, complete]],  # before, local verify, rename
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
        list_files_side_effect=[[], [new_file], [new_file]],  # before, local verify, rename
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
async def test_local_verification_fails_when_no_file_downloaded(setup):
    """SKY-8433: When download_selector() returns None and click silently succeeds
    without downloading anything, the local filesystem check should detect
    no new files and trigger AI fallback instead of silently succeeding."""
    refs = setup(
        get_side_effect=[[]],  # S3 before-check only; local check fails before S3 after-check
        list_files_side_effect=[[], []],  # before=empty, after=still empty
    )
    try:
        from skyvern.services.script_service import download

        await download(prompt="Download invoice", label="test_block")

        refs["fallback"].assert_called_once()
        error_arg = refs["fallback"].call_args.kwargs.get("error")
        assert "did not produce a new file on the local filesystem" in str(error_arg)
        # S3 save should NOT be called since local check fails first
        refs["storage"].save_downloaded_files.assert_not_called()
    finally:
        _cleanup(refs)


@pytest.mark.asyncio
async def test_local_verification_ignores_crdownload_files(setup, tmp_path):
    """Incomplete .crdownload files should not count as successful downloads
    for local filesystem verification."""
    download_dir = tmp_path / "downloads"
    incomplete_file = str(download_dir / "invoice.pdf.crdownload")
    refs = setup(
        get_side_effect=[[]],
        list_files_side_effect=[[], [incomplete_file]],  # only incomplete file appeared
    )
    try:
        from skyvern.services.script_service import download

        await download(prompt="Download invoice", label="test_block")

        refs["fallback"].assert_called_once()
        error_arg = refs["fallback"].call_args.kwargs.get("error")
        assert "did not produce a new file on the local filesystem" in str(error_arg)
    finally:
        _cleanup(refs)


@pytest.mark.asyncio
async def test_local_verification_passes_with_complete_file(setup, tmp_path):
    """When a complete file appears on the local filesystem after the cached
    function runs, the local verification should pass and allow the block
    to proceed to S3 upload and completion."""
    download_dir = tmp_path / "downloads"
    local_file = str(download_dir / "invoice.pdf")
    refs = setup(
        get_side_effect=[[], [local_file]],  # S3 verification also passes
        list_files_side_effect=[[], [local_file]],  # local verification passes
    )
    try:
        from skyvern.services.script_service import download

        await download(prompt="Download invoice", label="test_block")

        refs["fallback"].assert_not_called()
        refs["update_block"].assert_called_once()
        # S3 save should be called since local check passed
        refs["storage"].save_downloaded_files.assert_called_once()
    finally:
        _cleanup(refs)
