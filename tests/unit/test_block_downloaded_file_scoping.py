"""
Test that downloaded files are correctly scoped to each task block.

When two task blocks run sequentially in the same workflow run, each block's
downloaded_file_urls should contain only files it downloaded, not files from
previous blocks.
"""

import asyncio
from unittest.mock import AsyncMock, patch

import pytest

from skyvern.forge.sdk.core.skyvern_context import SkyvernContext
from skyvern.forge.sdk.schemas.files import FileInfo
from skyvern.forge.sdk.workflow.loop_download_filter import (
    DOWNLOADED_FILE_SIGS_KEY,
    filter_downloaded_files_for_current_iteration,
    to_downloaded_file_signature,
)
from skyvern.forge.sdk.workflow.models.block import (
    BLOCK_BASELINE_MARKER,
    capture_block_download_baseline,
)


def _make_file_info(url: str, filename: str, checksum: str | None = None) -> FileInfo:
    return FileInfo(url=url, filename=filename, checksum=checksum)


def test_second_block_excludes_first_blocks_files():
    """
    Simulate two sequential task blocks that each download a file.
    After capturing a baseline before block 2, the filter should
    exclude files that existed before block 2 ran.
    """
    file1 = _make_file_info("s3://bucket/file1.pdf", "file1.pdf", "checksum1")
    file2 = _make_file_info("s3://bucket/file2.pdf", "file2.pdf", "checksum2")

    # Before block 2 runs, capture baseline (file1 already exists)
    baseline_sigs = [to_downloaded_file_signature(file1)]
    loop_internal_state = {
        DOWNLOADED_FILE_SIGS_KEY: baseline_sigs,
    }

    # After block 2 runs, get_downloaded_files returns both files
    all_files = [file1, file2]

    # Filter should return only file2 (new since baseline)
    filtered = filter_downloaded_files_for_current_iteration(all_files, loop_internal_state)
    assert len(filtered) == 1
    assert filtered[0].url == "s3://bucket/file2.pdf"


def test_first_block_gets_all_its_files_with_empty_baseline():
    """
    The first task block has an empty baseline, so it should get all files it downloaded.
    """
    file1 = _make_file_info("s3://bucket/file1.pdf", "file1.pdf", "checksum1")

    loop_internal_state = {
        DOWNLOADED_FILE_SIGS_KEY: [],
    }

    filtered = filter_downloaded_files_for_current_iteration([file1], loop_internal_state)
    assert len(filtered) == 1
    assert filtered[0].url == "s3://bucket/file1.pdf"


def test_no_baseline_returns_all_files():
    """
    Without loop_internal_state (legacy behavior), all files are returned.
    This is the behavior that caused the bug — the second block would see
    all files including those from the first block.
    """
    file1 = _make_file_info("s3://bucket/file1.pdf", "file1.pdf", "checksum1")
    file2 = _make_file_info("s3://bucket/file2.pdf", "file2.pdf", "checksum2")

    # No baseline → no filtering
    filtered = filter_downloaded_files_for_current_iteration([file1, file2], None)
    assert len(filtered) == 2


def test_duplicate_files_handled_correctly():
    """
    If the same file exists in the baseline, only one copy is subtracted.
    All three FileInfo objects are intentionally identical (same URL, name,
    checksum) to verify that the counter-based deduplication removes exactly
    one match per baseline entry.
    """
    file_a = _make_file_info("s3://bucket/report.pdf", "report.pdf", "checksum_a")
    file_a_dup = _make_file_info("s3://bucket/report.pdf", "report.pdf", "checksum_a")
    file_b = _make_file_info("s3://bucket/report.pdf", "report.pdf", "checksum_a")

    # Baseline has one copy
    baseline_sigs = [to_downloaded_file_signature(file_a)]
    loop_internal_state = {
        DOWNLOADED_FILE_SIGS_KEY: baseline_sigs,
    }

    # Current run has two copies of the same file
    all_files = [file_a_dup, file_b]

    # One is subtracted by the baseline, one remains
    filtered = filter_downloaded_files_for_current_iteration(all_files, loop_internal_state)
    assert len(filtered) == 1


# --- Tests for capture_block_download_baseline marker logic ---


@pytest.mark.asyncio
async def test_baseline_captured_when_loop_internal_state_is_none():
    """When loop_internal_state is None, the helper creates a new baseline with the marker."""
    file1 = _make_file_info("s3://bucket/file1.pdf", "file1.pdf", "checksum1")
    context = SkyvernContext(run_id="wr_test", loop_internal_state=None)

    mock_storage = AsyncMock()
    mock_storage.get_downloaded_files = AsyncMock(return_value=[file1])

    with patch("skyvern.forge.sdk.workflow.models.block.app") as mock_app:
        mock_app.STORAGE = mock_storage
        await capture_block_download_baseline(context, "org_1", "wr_test", "block_1")

    assert context.loop_internal_state is not None
    assert BLOCK_BASELINE_MARKER in context.loop_internal_state
    assert len(context.loop_internal_state[DOWNLOADED_FILE_SIGS_KEY]) == 1


@pytest.mark.asyncio
async def test_baseline_recaptured_when_set_by_previous_block():
    """When loop_internal_state was set by a previous block (has marker), it gets overwritten."""
    file1 = _make_file_info("s3://bucket/file1.pdf", "file1.pdf", "checksum1")
    file2 = _make_file_info("s3://bucket/file2.pdf", "file2.pdf", "checksum2")

    # Simulate baseline set by a previous block
    context = SkyvernContext(
        run_id="wr_test",
        loop_internal_state={
            DOWNLOADED_FILE_SIGS_KEY: [to_downloaded_file_signature(file1)],
            BLOCK_BASELINE_MARKER: True,
        },
    )

    mock_storage = AsyncMock()
    mock_storage.get_downloaded_files = AsyncMock(return_value=[file1, file2])

    with patch("skyvern.forge.sdk.workflow.models.block.app") as mock_app:
        mock_app.STORAGE = mock_storage
        await capture_block_download_baseline(context, "org_1", "wr_test", "block_2")

    assert context.loop_internal_state is not None
    assert BLOCK_BASELINE_MARKER in context.loop_internal_state
    # New baseline includes both files
    assert len(context.loop_internal_state[DOWNLOADED_FILE_SIGS_KEY]) == 2


@pytest.mark.asyncio
async def test_loop_baseline_preserved_when_no_marker():
    """When loop_internal_state was set by ForLoopBlock (no marker), it is NOT overwritten."""
    loop_baseline = {
        DOWNLOADED_FILE_SIGS_KEY: [("a.pdf", "abc", "s3://a.pdf")],
    }
    context = SkyvernContext(run_id="wr_test", loop_internal_state=loop_baseline)

    mock_storage = AsyncMock()
    mock_storage.get_downloaded_files = AsyncMock(return_value=[])

    with patch("skyvern.forge.sdk.workflow.models.block.app") as mock_app:
        mock_app.STORAGE = mock_storage
        await capture_block_download_baseline(context, "org_1", "wr_test", "block_1")

    # Should be unchanged — loop baseline preserved
    assert context.loop_internal_state is loop_baseline
    mock_storage.get_downloaded_files.assert_not_called()


@pytest.mark.asyncio
async def test_baseline_capture_degrades_on_timeout():
    """TimeoutError clears loop_internal_state and lets the block proceed."""
    context = SkyvernContext(run_id="wr_test", loop_internal_state=None)
    mock_storage = AsyncMock()
    mock_storage.get_downloaded_files = AsyncMock(side_effect=asyncio.TimeoutError)

    with patch("skyvern.forge.sdk.workflow.models.block.app") as mock_app:
        mock_app.STORAGE = mock_storage
        # Must not raise
        await capture_block_download_baseline(context, "org_1", "wr_test", "block_1")

    assert context.loop_internal_state is None


@pytest.mark.asyncio
async def test_stale_loop_baseline_blocks_fresh_capture_without_restore():
    """Regression guard: a loop-set baseline (no marker) left in context
    causes capture_block_download_baseline to skip capture. ForLoopBlock
    must clear/restore its state so subsequent blocks can re-capture."""
    stale_loop_state = {
        DOWNLOADED_FILE_SIGS_KEY: [("a.pdf", "abc", "s3://a.pdf")],
    }
    context = SkyvernContext(run_id="wr_test", loop_internal_state=stale_loop_state)
    mock_storage = AsyncMock()
    mock_storage.get_downloaded_files = AsyncMock(return_value=[])

    with patch("skyvern.forge.sdk.workflow.models.block.app") as mock_app:
        mock_app.STORAGE = mock_storage
        await capture_block_download_baseline(context, "org_1", "wr_test", "block_1")

    # Capture was skipped because the loop baseline has no marker — this is
    # why ForLoopBlock.execute() must save/restore loop_internal_state.
    assert context.loop_internal_state is stale_loop_state
    mock_storage.get_downloaded_files.assert_not_called()


@pytest.mark.asyncio
async def test_baseline_capture_degrades_on_generic_exception():
    """Non-timeout exceptions (e.g. S3 errors) clear state and let the block proceed."""
    context = SkyvernContext(run_id="wr_test", loop_internal_state=None)
    mock_storage = AsyncMock()
    mock_storage.get_downloaded_files = AsyncMock(side_effect=RuntimeError("S3 blip"))

    with patch("skyvern.forge.sdk.workflow.models.block.app") as mock_app:
        mock_app.STORAGE = mock_storage
        # Must not raise — baseline capture is best-effort
        await capture_block_download_baseline(context, "org_1", "wr_test", "block_1")

    assert context.loop_internal_state is None


@pytest.mark.asyncio
async def test_forloopblock_execute_restores_outer_loop_state():
    """ForLoopBlock.execute() save/restore wrapper preserves the outer
    loop_internal_state even when the inner _run_loop mutates or fails."""
    from skyvern.forge.sdk.workflow.models.block import ForLoopBlock
    from skyvern.forge.sdk.workflow.models.parameter import OutputParameter, ParameterType

    outer_state = {DOWNLOADED_FILE_SIGS_KEY: [("outer.pdf", "abc", "s3://outer.pdf")]}
    context = SkyvernContext(run_id="wr_test", loop_internal_state=outer_state)

    output_param = OutputParameter(
        parameter_type=ParameterType.OUTPUT,
        key="loop_output",
        output_parameter_id="op_1",
        workflow_id="wf_1",
        created_at="2026-04-14T00:00:00Z",
        modified_at="2026-04-14T00:00:00Z",
    )
    loop = ForLoopBlock(
        label="loop_1",
        output_parameter=output_param,
        loop_over=None,
        loop_blocks=[],
    )

    async def _raises(*a: object, **kw: object) -> None:
        # Simulate inner execution mutating state before raising.
        context.loop_internal_state = {"mutated": True}
        raise RuntimeError("inner failure")

    with patch("skyvern.forge.sdk.core.skyvern_context.current", return_value=context):
        with patch.object(ForLoopBlock, "_run_loop", _raises):
            with pytest.raises(RuntimeError, match="inner failure"):
                await loop.execute(workflow_run_id="wr_test", workflow_run_block_id="wrb_1")

    # Outer state must be restored even though _run_loop mutated and raised.
    assert context.loop_internal_state is outer_state
