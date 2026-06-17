"""Eager-save-then-refetch for adopted persistent-session downloads.

On an adopted session the run connection owns the download artifact, but in prod the
worker pod can tear the shared browser down before a deferred save_as runs. The helper
saves eagerly and, when save_as raises (TargetClosedError) or yields a 0-byte file,
re-fetches the replayable download url through the run page's request context.
"""

from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest

from skyvern.webeye.actions.handler import _save_adopted_session_download

PDF_BODY = b"%PDF-1.4\n" + b"x" * 830


def _download(suggested: str = "153743777.pdf", url: str = "https://example.com/download") -> MagicMock:
    download = MagicMock()
    download.suggested_filename = suggested
    download.url = url
    download.save_as = AsyncMock()
    return download


def _page_with_refetch(status: int = 200, body: bytes = PDF_BODY) -> MagicMock:
    response = MagicMock()
    response.status = status
    response.body = AsyncMock(return_value=body)
    page = MagicMock()
    page.context.request.get = AsyncMock(return_value=response)
    return page


@pytest.mark.asyncio
async def test_happy_path_eager_save_writes_bytes(tmp_path) -> None:
    download = _download()

    async def _save(target: object) -> None:
        Path(str(target)).write_bytes(PDF_BODY)

    download.save_as.side_effect = _save
    page = _page_with_refetch()

    saved = await _save_adopted_session_download(download, page, tmp_path, workflow_run_id="wr")

    assert saved is not None and saved.exists()
    assert saved.read_bytes() == PDF_BODY
    page.context.request.get.assert_not_awaited()


@pytest.mark.asyncio
async def test_save_as_raises_target_closed_falls_back_to_refetch(tmp_path) -> None:
    download = _download()
    download.save_as.side_effect = Exception("Target page, context or browser has been closed")
    page = _page_with_refetch()

    saved = await _save_adopted_session_download(download, page, tmp_path, workflow_run_id="wr")

    assert saved is not None and saved.exists()
    assert saved.read_bytes() == PDF_BODY
    page.context.request.get.assert_awaited_once_with(download.url)


@pytest.mark.asyncio
async def test_zero_byte_save_as_falls_back_to_refetch(tmp_path) -> None:
    download = _download()

    async def _save_empty(target: object) -> None:
        Path(str(target)).write_bytes(b"")

    download.save_as.side_effect = _save_empty
    page = _page_with_refetch()

    saved = await _save_adopted_session_download(download, page, tmp_path, workflow_run_id="wr")

    assert saved is not None and saved.exists()
    assert saved.read_bytes() == PDF_BODY
    page.context.request.get.assert_awaited_once_with(download.url)
    # the empty placeholder must not survive alongside the recovered file
    assert sorted(p.name for p in tmp_path.iterdir()) == [saved.name]


@pytest.mark.asyncio
async def test_refetch_non_200_returns_none(tmp_path) -> None:
    download = _download()
    download.save_as.side_effect = Exception("closed")
    page = _page_with_refetch(status=403, body=b"forbidden")

    saved = await _save_adopted_session_download(download, page, tmp_path, workflow_run_id="wr")

    assert saved is None
    assert list(tmp_path.iterdir()) == []


@pytest.mark.asyncio
async def test_refetch_empty_body_returns_none(tmp_path) -> None:
    download = _download()
    download.save_as.side_effect = Exception("closed")
    page = _page_with_refetch(status=200, body=b"")

    saved = await _save_adopted_session_download(download, page, tmp_path, workflow_run_id="wr")

    assert saved is None
    assert list(tmp_path.iterdir()) == []


@pytest.mark.asyncio
async def test_refetch_raises_returns_none(tmp_path) -> None:
    download = _download()
    download.save_as.side_effect = Exception("closed")
    page = MagicMock()
    page.context.request.get = AsyncMock(side_effect=Exception("connection gone"))

    saved = await _save_adopted_session_download(download, page, tmp_path, workflow_run_id="wr")

    assert saved is None
    assert list(tmp_path.iterdir()) == []


@pytest.mark.asyncio
async def test_partial_save_as_then_failed_refetch_leaves_no_orphan(tmp_path) -> None:
    """A partial (non-empty) write followed by a save_as raise must not orphan a corrupt file
    when the subsequent re-fetch also fails."""
    download = _download()

    async def _save_partial_then_raise(target: object) -> None:
        Path(str(target)).write_bytes(b"%PDF-1.4 truncated")
        raise Exception("Target page, context or browser has been closed")

    download.save_as.side_effect = _save_partial_then_raise
    page = MagicMock()
    page.context.request.get = AsyncMock(side_effect=Exception("connection gone"))

    saved = await _save_adopted_session_download(download, page, tmp_path, workflow_run_id="wr")

    assert saved is None
    assert list(tmp_path.iterdir()) == []
