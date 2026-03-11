import asyncio

import pytest

from skyvern.forge.sdk.schemas.files import FileInfo
from skyvern.forge.sdk.workflow.loop_download_filter import (
    filter_downloaded_files_for_current_iteration,
    to_downloaded_file_signature,
)


def _file(url: str, filename: str, checksum: str) -> FileInfo:
    return FileInfo(url=url, filename=filename, checksum=checksum)


async def _capture_baseline(
    get_files_coro: asyncio.coroutines,
    timeout_seconds: float,
) -> dict | None:
    """Mirrors the exact baseline capture pattern from block.py / script_service.py."""
    sigs: list = []
    timed_out = False
    try:
        async with asyncio.timeout(timeout_seconds):
            sigs = [to_downloaded_file_signature(fi) for fi in await get_files_coro]
    except TimeoutError:
        timed_out = True

    if timed_out:
        return None
    return {"downloaded_file_signatures_before_iteration": sigs}


@pytest.mark.asyncio
async def test_baseline_capture_returns_none_on_timeout() -> None:
    """When get_downloaded_files hangs, the baseline must be None (not empty sigs)."""

    async def _hang_forever() -> list[FileInfo]:
        await asyncio.Event().wait()
        return []  # never reached

    result = await _capture_baseline(_hang_forever(), timeout_seconds=0.01)
    assert result is None


@pytest.mark.asyncio
async def test_baseline_capture_returns_signatures_on_success() -> None:
    """Normal completion should return the signature dict."""

    async def _return_files() -> list[FileInfo]:
        return [
            _file("https://files/a.pdf?sig=x", "a.pdf", "abc"),
            _file("https://files/b.pdf?sig=y", "b.pdf", "def"),
        ]

    result = await _capture_baseline(_return_files(), timeout_seconds=5.0)
    assert result is not None
    sigs = result["downloaded_file_signatures_before_iteration"]
    assert len(sigs) == 2
    assert sigs[0] == ("a.pdf", "abc", "https://files/a.pdf")
    assert sigs[1] == ("b.pdf", "def", "https://files/b.pdf")


@pytest.mark.asyncio
async def test_baseline_capture_returns_empty_sigs_when_no_files() -> None:
    """No files is distinct from a timeout — returns empty list, not None."""

    async def _return_empty() -> list[FileInfo]:
        return []

    result = await _capture_baseline(_return_empty(), timeout_seconds=5.0)
    assert result is not None
    assert result == {"downloaded_file_signatures_before_iteration": []}


@pytest.mark.asyncio
async def test_timeout_then_filter_returns_all_files_unfiltered() -> None:
    """End-to-end: timeout → None state → filter returns all files."""

    async def _hang_forever() -> list[FileInfo]:
        await asyncio.Event().wait()
        return []

    loop_internal_state = await _capture_baseline(_hang_forever(), timeout_seconds=0.01)
    assert loop_internal_state is None

    all_files = [
        _file("https://files/a.pdf", "a.pdf", "abc"),
        _file("https://files/b.pdf", "b.pdf", "def"),
    ]
    result = filter_downloaded_files_for_current_iteration(all_files, loop_internal_state)
    assert result == all_files


@pytest.mark.asyncio
async def test_success_then_filter_excludes_baseline_files() -> None:
    """End-to-end: successful capture → filter excludes baseline files."""

    async def _return_baseline() -> list[FileInfo]:
        return [_file("https://files/a.pdf?sig=old", "a.pdf", "abc")]

    loop_internal_state = await _capture_baseline(_return_baseline(), timeout_seconds=5.0)
    assert loop_internal_state is not None

    all_files = [
        _file("https://files/a.pdf?sig=old", "a.pdf", "abc"),
        _file("https://files/b.pdf?sig=new", "b.pdf", "def"),
    ]
    result = filter_downloaded_files_for_current_iteration(all_files, loop_internal_state)
    assert len(result) == 1
    assert result[0].filename == "b.pdf"
