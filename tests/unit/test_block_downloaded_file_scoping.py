"""
Test that downloaded files are correctly scoped to each task block.

When two task blocks run sequentially in the same workflow run, each block's
downloaded_file_urls should contain only files it downloaded, not files from
previous blocks.
"""

import asyncio
import os
from datetime import UTC, datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest

from skyvern.forge.sdk.artifact.storage import base as base_module
from skyvern.forge.sdk.artifact.storage.local import LocalStorage
from skyvern.forge.sdk.core import skyvern_context
from skyvern.forge.sdk.core.skyvern_context import SkyvernContext
from skyvern.forge.sdk.schemas.files import FileInfo
from skyvern.forge.sdk.workflow.loop_download_filter import (
    DOWNLOADED_FILE_SIGS_KEY,
    filter_downloaded_files_for_current_iteration,
    to_downloaded_file_signature,
)
from skyvern.forge.sdk.workflow.models.block import (
    DOWNLOAD_BINDING_FAILURE_REASON,
    CodeBlock,
    capture_block_download_baseline,
)
from tests.unit.test_code_block_downloads import (
    _fake_storage_app,
    _output_parameter,
    _wire_block_runtime,
    _wire_secure_runner,
)


def _make_file_info(url: str, filename: str, checksum: str | None = None) -> FileInfo:
    return FileInfo(url=url, filename=filename, checksum=checksum)


def test_downloaded_file_signature_strips_query_params():
    file_info = _make_file_info("https://files/a.pdf?sig=x", "a.pdf", "abc")

    assert to_downloaded_file_signature(file_info) == ("a.pdf", "abc", "https://files/a.pdf")


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

    # After block 2 runs, get_current_attempt_downloaded_files returns both files
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


# --- Tests for capture_block_download_baseline per-block capture ---


@pytest.mark.asyncio
async def test_baseline_captured_when_loop_internal_state_is_none():
    """When loop_internal_state is None, the helper creates a new baseline."""
    file1 = _make_file_info("s3://bucket/file1.pdf", "file1.pdf", "checksum1")
    context = SkyvernContext(run_id="wr_test", loop_internal_state=None)

    mock_storage = AsyncMock()
    mock_storage.get_current_attempt_downloaded_files = AsyncMock(return_value=[file1])

    with patch("skyvern.forge.sdk.workflow.models.block.app") as mock_app:
        mock_app.STORAGE = mock_storage
        await capture_block_download_baseline(context, "org_1", "wr_test", "block_1")

    assert context.loop_internal_state is not None
    assert len(context.loop_internal_state[DOWNLOADED_FILE_SIGS_KEY]) == 1


@pytest.mark.asyncio
async def test_baseline_recaptured_when_set_by_previous_block():
    """When loop_internal_state was set by a previous block, it gets overwritten with the
    files that exist now — so the current block is scoped to only what it adds next."""
    file1 = _make_file_info("s3://bucket/file1.pdf", "file1.pdf", "checksum1")
    file2 = _make_file_info("s3://bucket/file2.pdf", "file2.pdf", "checksum2")

    # Simulate baseline set by a previous block
    context = SkyvernContext(
        run_id="wr_test",
        loop_internal_state={
            DOWNLOADED_FILE_SIGS_KEY: [to_downloaded_file_signature(file1)],
        },
    )

    mock_storage = AsyncMock()
    mock_storage.get_current_attempt_downloaded_files = AsyncMock(return_value=[file1, file2])

    with patch("skyvern.forge.sdk.workflow.models.block.app") as mock_app:
        mock_app.STORAGE = mock_storage
        await capture_block_download_baseline(context, "org_1", "wr_test", "block_2")

    assert context.loop_internal_state is not None
    # New baseline includes both files
    assert len(context.loop_internal_state[DOWNLOADED_FILE_SIGS_KEY]) == 2


@pytest.mark.asyncio
async def test_baseline_recaptured_even_when_loop_set_it():
    """SKY-10784: a block inside a loop must re-capture its own baseline rather than
    defer to the loop's per-iteration baseline. Otherwise sibling download-producing
    blocks in the same iteration accumulate each other's files."""
    loop_baseline = {
        DOWNLOADED_FILE_SIGS_KEY: [("a.pdf", "abc", "s3://a.pdf")],
    }
    context = SkyvernContext(run_id="wr_test", loop_internal_state=loop_baseline)

    # A sibling block earlier in this iteration already produced b.pdf, so it now
    # exists alongside the loop's pre-iteration a.pdf.
    sibling_file = _make_file_info("s3://bucket/b.pdf", "b.pdf", "checksum_b")
    mock_storage = AsyncMock()
    mock_storage.get_current_attempt_downloaded_files = AsyncMock(
        return_value=[_make_file_info("s3://a.pdf", "a.pdf", "abc"), sibling_file]
    )

    with patch("skyvern.forge.sdk.workflow.models.block.app") as mock_app:
        mock_app.STORAGE = mock_storage
        await capture_block_download_baseline(context, "org_1", "wr_test", "block_2")

    # Re-captured: the sibling's file is now part of this block's baseline, so it
    # will be filtered out of this block's own output.
    mock_storage.get_current_attempt_downloaded_files.assert_called_once()
    assert len(context.loop_internal_state[DOWNLOADED_FILE_SIGS_KEY]) == 2


@pytest.mark.asyncio
async def test_baseline_capture_degrades_on_timeout():
    """TimeoutError clears loop_internal_state and lets the block proceed."""
    context = SkyvernContext(run_id="wr_test", loop_internal_state=None)
    mock_storage = AsyncMock()
    mock_storage.get_current_attempt_downloaded_files = AsyncMock(side_effect=asyncio.TimeoutError)

    with patch("skyvern.forge.sdk.workflow.models.block.app") as mock_app:
        mock_app.STORAGE = mock_storage
        # Must not raise
        await capture_block_download_baseline(context, "org_1", "wr_test", "block_1")

    assert context.loop_internal_state is None


@pytest.mark.asyncio
async def test_stale_loop_baseline_overwritten_by_fresh_capture():
    """A stale baseline left in context (e.g. from a prior loop iteration) is
    overwritten by the current files, so it can never cause stale filtering."""
    stale_loop_state = {
        DOWNLOADED_FILE_SIGS_KEY: [("a.pdf", "abc", "s3://a.pdf")],
    }
    context = SkyvernContext(run_id="wr_test", loop_internal_state=stale_loop_state)
    mock_storage = AsyncMock()
    mock_storage.get_current_attempt_downloaded_files = AsyncMock(return_value=[])

    with patch("skyvern.forge.sdk.workflow.models.block.app") as mock_app:
        mock_app.STORAGE = mock_storage
        await capture_block_download_baseline(context, "org_1", "wr_test", "block_1")

    mock_storage.get_current_attempt_downloaded_files.assert_called_once()
    assert context.loop_internal_state is not stale_loop_state
    assert context.loop_internal_state[DOWNLOADED_FILE_SIGS_KEY] == []


@pytest.mark.asyncio
async def test_baseline_capture_degrades_on_generic_exception():
    """Non-timeout exceptions (e.g. S3 errors) clear state and let the block proceed."""
    context = SkyvernContext(run_id="wr_test", loop_internal_state=None)
    mock_storage = AsyncMock()
    mock_storage.get_current_attempt_downloaded_files = AsyncMock(side_effect=RuntimeError("S3 blip"))

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


@pytest.mark.asyncio
async def test_sibling_download_blocks_in_loop_iteration_scope_to_own_files():
    """SKY-10784 regression: multiple download-producing blocks inside one loop
    iteration must each surface only the file they produced — not their siblings'.
    Mirrors the reported production run, where three Print Page blocks in a single loop
    iteration showed 1, then 2, then 3 PDFs instead of 1 each.
    """
    pp1 = _make_file_info("https://api/v1/artifacts/a_pp1/content", "page_1.pdf", "c1")
    pp2 = _make_file_info("https://api/v1/artifacts/a_pp2/content", "page_2.pdf", "c2")
    pp3 = _make_file_info("https://api/v1/artifacts/a_pp3/content", "page_3.pdf", "c3")
    pp4 = _make_file_info("https://api/v1/artifacts/a_pp4/content", "page_4.pdf", "c4")

    # The run's download directory, shared across blocks and growing as each produces a file.
    run_files: list[FileInfo] = []

    # ForLoopBlock sets the per-iteration baseline (no marker) at iteration start.
    context = SkyvernContext(run_id="wr_test", loop_internal_state={DOWNLOADED_FILE_SIGS_KEY: []})

    mock_storage = AsyncMock()
    # Each baseline capture reads a snapshot of the files that exist at that moment.
    mock_storage.get_current_attempt_downloaded_files = AsyncMock(side_effect=lambda **_: list(run_files))

    async def run_download_block(produced: FileInfo) -> list[FileInfo]:
        with patch("skyvern.forge.sdk.workflow.models.block.app") as mock_app:
            mock_app.STORAGE = mock_storage
            await capture_block_download_baseline(context, "org_1", "wr_test", "print")
        run_files.append(produced)
        return filter_downloaded_files_for_current_iteration(list(run_files), context.loop_internal_state)

    out1 = await run_download_block(pp1)
    out2 = await run_download_block(pp2)
    out3 = await run_download_block(pp3)

    assert [f.filename for f in out1] == ["page_1.pdf"]
    assert [f.filename for f in out2] == ["page_2.pdf"]
    assert [f.filename for f in out3] == ["page_3.pdf"]

    # Next loop iteration: ForLoopBlock re-captures the per-iteration baseline, and the
    # first block of the new iteration must still exclude every prior iteration's file.
    context.loop_internal_state = {DOWNLOADED_FILE_SIGS_KEY: [to_downloaded_file_signature(f) for f in run_files]}
    out4 = await run_download_block(pp4)
    assert [f.filename for f in out4] == ["page_4.pdf"]


@pytest.mark.asyncio
@pytest.mark.parametrize("attempt_number", [1, 2])
@pytest.mark.parametrize("start_kind", ["aware", "naive", "missing"])
async def test_current_attempt_downloads_preserve_unknown_dates_and_include_start_boundary(
    tmp_path, monkeypatch, attempt_number, start_kind
):
    boundary = datetime(2026, 9, 9, 12, tzinfo=UTC)
    started_at = {"aware": boundary, "naive": boundary.replace(tzinfo=None), "missing": None}[start_kind]
    files = [
        FileInfo(url="file:///old.pdf", modified_at=boundary - timedelta(microseconds=1)),
        FileInfo(url="file:///old-naive.pdf", modified_at=(boundary - timedelta(seconds=1)).replace(tzinfo=None)),
        FileInfo(url="file:///boundary.pdf", modified_at=boundary.replace(tzinfo=None)),
        FileInfo(url="file:///offset.pdf", modified_at=boundary.astimezone(timezone(timedelta(hours=-4)))),
        FileInfo(url="file:///new.pdf", modified_at=boundary + timedelta(seconds=1)),
        FileInfo(url="file:///unknown.pdf", modified_at=None),
    ]
    storage = LocalStorage(str(tmp_path / "artifacts"))
    monkeypatch.setattr(storage, "get_downloaded_files", AsyncMock(return_value=files))
    resolve_attempt = AsyncMock(return_value=("wr_owner", attempt_number, started_at))
    monkeypatch.setattr(base_module, "resolve_download_attempt", resolve_attempt)

    result = await storage.get_current_attempt_downloaded_files("o_1", "wr_child")

    assert result == (files[2:] if attempt_number > 1 and started_at is not None else files)
    resolve_attempt.assert_awaited_once_with("o_1", "wr_child", attempt_number=None)
    storage.get_downloaded_files.assert_awaited_once_with(
        organization_id="o_1",
        run_id="wr_child",
        attempt_started_at=boundary if attempt_number > 1 and started_at else None,
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("attempt_number", [1, 2])
@pytest.mark.parametrize("change", ["rewrite", "late_stale", "unchanged"])
@pytest.mark.parametrize("engine", ["inline", "secure"])
async def test_code_block_binding_verdict_recognizes_proven_retry_rewrite(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, attempt_number: int, change: str, engine: str
) -> None:
    download_dir = tmp_path / "wr_1"
    download_dir.mkdir()
    monkeypatch.setattr("skyvern.forge.sdk.api.files.settings.DOWNLOAD_PATH", str(tmp_path))
    old_file = download_dir / "report.pdf"
    old_file.write_bytes(b"before")
    old_time = datetime(2026, 1, 1, tzinfo=UTC).timestamp()
    os.utime(old_file, (old_time, old_time))
    cutoff = datetime(2026, 1, 2, tzinfo=UTC)
    monkeypatch.setattr(
        base_module, "resolve_download_attempt", AsyncMock(return_value=("wr_1", attempt_number, cutoff))
    )
    _fake_storage_app(monkeypatch, save=AsyncMock(), get=AsyncMock(return_value=[]))
    _wire_block_runtime(monkeypatch)

    def produce_download() -> None:
        if change == "unchanged":
            return
        target = old_file if change == "rewrite" else download_dir / "late.pdf"
        target.write_bytes(b"newly generated bytes")
        os.utime(target, (old_time, old_time))

    async def execute_code(*args: object, **kwargs: object) -> dict[str, bool]:
        produce_download()
        return {"ok": True}

    if engine == "secure":
        _wire_secure_runner(monkeypatch, output={"ok": True}, on_execute=produce_download)
    else:
        monkeypatch.setattr(CodeBlock, "execute_user_function_with_timeout", AsyncMock(side_effect=execute_code))
    block = CodeBlock(label="download", code="value = 'unused'", output_parameter=_output_parameter("code_out"))
    previous_context = skyvern_context.current()
    skyvern_context.set(SkyvernContext(organization_id="o_1", workflow_run_id="wr_1", run_id="wr_1"))
    try:
        result = await block.execute(workflow_run_id="wr_1", workflow_run_block_id="", organization_id="o_1")
    finally:
        if previous_context is None:
            skyvern_context.reset()
        else:
            skyvern_context.set(previous_context)

    has_new_download = change == "rewrite" or (change == "late_stale" and attempt_number == 1)
    assert (result.failure_reason == DOWNLOAD_BINDING_FAILURE_REASON) == has_new_download
    assert result.success is not has_new_download
    assert old_file.exists()


@pytest.mark.asyncio
@pytest.mark.parametrize("first_lookup_fails", [False, True])
async def test_secure_code_block_reuses_retry_cutoff_when_lookup_is_transient(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, first_lookup_fails: bool
) -> None:
    download_dir = tmp_path / "wr_1"
    download_dir.mkdir()
    monkeypatch.setattr("skyvern.forge.sdk.api.files.settings.DOWNLOAD_PATH", str(tmp_path))
    cutoff = datetime(2026, 1, 2, tzinfo=UTC)
    resolved_attempt = ("wr_1", 2, cutoff)
    lookup_error = RuntimeError("download attempt lookup unavailable")
    resolve_attempt = AsyncMock(
        side_effect=[lookup_error, resolved_attempt] if first_lookup_fails else [resolved_attempt, lookup_error]
    )
    monkeypatch.setattr(base_module, "resolve_download_attempt", resolve_attempt)
    _fake_storage_app(monkeypatch, save=AsyncMock(), get=AsyncMock(return_value=[]))
    _wire_block_runtime(monkeypatch)
    register_downloads = AsyncMock(return_value=([], set()))
    monkeypatch.setattr(CodeBlock, "_register_downloaded_files", register_downloads)

    def materialize_stale_download() -> None:
        late_file = download_dir / "late.pdf"
        late_file.write_bytes(b"retained download")
        old_time = (cutoff - timedelta(days=1)).timestamp()
        os.utime(late_file, (old_time, old_time))

    _wire_secure_runner(monkeypatch, output={"ok": True}, on_execute=materialize_stale_download)
    block = CodeBlock(label="download", code="value = 'unused'", output_parameter=_output_parameter("code_out"))
    previous_context = skyvern_context.current()
    skyvern_context.set(SkyvernContext(organization_id="o_1", workflow_run_id="wr_1", run_id="wr_1"))
    try:
        result = await block.execute(workflow_run_id="wr_1", workflow_run_block_id="", organization_id="o_1")
    finally:
        if previous_context is None:
            skyvern_context.reset()
        else:
            skyvern_context.set(previous_context)

    if first_lookup_fails:
        register_downloads.assert_awaited_once()
        assert result.failure_reason == DOWNLOAD_BINDING_FAILURE_REASON
        assert result.output_parameter_value["download_registration_failed"] is True
    else:
        register_downloads.assert_not_awaited()
        assert result.success
        assert result.failure_reason is None
        assert result.output_parameter_value == {"ok": True}
    resolve_attempt.assert_awaited_once_with("o_1", "wr_1", attempt_number=None)
