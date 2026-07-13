"""Eager-save-then-refetch for adopted persistent-session downloads.

On an adopted session the run connection owns the download artifact, but in prod the
worker pod can tear the shared browser down before a deferred save_as runs. The helper
saves eagerly and, when save_as raises (TargetClosedError) or yields a 0-byte file,
re-fetches the replayable download url through the run page's request context.

For ``blob:`` URLs (client-side blobs minted by the page) the request-context fetch
cannot be used; the helper runs an in-page ``fetch`` from a frame whose origin owns
the blob and returns the bytes that way.
"""

import base64
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
    page.frames = []
    page.main_frame = MagicMock()
    page.main_frame.url = "https://example.com/"
    page.main_frame.evaluate = AsyncMock()
    page.evaluate = AsyncMock()
    return page


def _frame(url: str, evaluate_return: object | Exception | None = None) -> MagicMock:
    frame = MagicMock()
    frame.url = url
    if isinstance(evaluate_return, Exception):
        frame.evaluate = AsyncMock(side_effect=evaluate_return)
    else:
        frame.evaluate = AsyncMock(return_value=evaluate_return)
    return frame


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
    page.frames = []
    page.main_frame = MagicMock()
    page.main_frame.url = "https://example.com/"
    page.main_frame.evaluate = AsyncMock()

    saved = await _save_adopted_session_download(download, page, tmp_path, workflow_run_id="wr")

    assert saved is None
    assert list(tmp_path.iterdir()) == []


# ---------------------------------------------------------------------------
# blob: URL handling -- save_as yields 0 bytes for client-side blobs, and the
# APIRequestContext path raises Protocol "blob:" not supported. The helper
# must fall through to an in-page fetch executed in a frame at the blob's origin.
# ---------------------------------------------------------------------------

BLOB_URL = "blob:https://files.example.org/7da434f6-d9c2-4582-8c70-60a8e380e78a#view=FitH"
BLOB_ORIGIN_FRAME_URL = "https://files.example.org/preview"
OTHER_ORIGIN_FRAME_URL = "https://app.example.com/dashboard"


def _blob_capable_page(*frames: MagicMock, main_frame_url: str = OTHER_ORIGIN_FRAME_URL) -> MagicMock:
    page = MagicMock()
    page.context.request.get = AsyncMock(
        side_effect=Exception("page.context.request.get must not be called for blob: URLs")
    )
    page.main_frame = MagicMock()
    page.main_frame.url = main_frame_url
    page.main_frame.evaluate = AsyncMock(
        side_effect=Exception("main_frame.evaluate must not be called when no origin match")
    )
    # page.evaluate is the call evaluate_in_main_world delegates to when no main-world
    # prefix is configured. Sub-frame matches must not reach it.
    page.evaluate = AsyncMock(
        side_effect=Exception("page.evaluate must not be called when matched frame is a sub-frame")
    )
    page.frames = list(frames)
    return page


@pytest.mark.asyncio
async def test_blob_url_reads_via_in_page_fetch_in_matching_frame(tmp_path) -> None:
    download = _download(url=BLOB_URL)

    async def _save_empty(target: object) -> None:
        Path(str(target)).write_bytes(b"")

    download.save_as.side_effect = _save_empty
    matching_frame = _frame(
        BLOB_ORIGIN_FRAME_URL,
        evaluate_return={"ok": True, "base64": base64.b64encode(PDF_BODY).decode("ascii")},
    )
    other_frame = _frame(OTHER_ORIGIN_FRAME_URL)
    page = _blob_capable_page(matching_frame, other_frame)

    saved = await _save_adopted_session_download(download, page, tmp_path, workflow_run_id="wr")

    assert saved is not None and saved.exists(), "blob bytes must be persisted to disk"
    assert saved.read_bytes() == PDF_BODY
    page.context.request.get.assert_not_awaited()
    matching_frame.evaluate.assert_awaited_once()
    other_frame.evaluate.assert_not_awaited()
    assert sorted(p.name for p in tmp_path.iterdir()) == [saved.name]


@pytest.mark.asyncio
async def test_blob_url_no_matching_frame_returns_none(tmp_path) -> None:
    download = _download(url=BLOB_URL)
    download.save_as.side_effect = Exception("closed")
    page = _blob_capable_page(_frame(OTHER_ORIGIN_FRAME_URL))

    saved = await _save_adopted_session_download(download, page, tmp_path, workflow_run_id="wr")

    assert saved is None
    page.context.request.get.assert_not_awaited()
    assert list(tmp_path.iterdir()) == []


@pytest.mark.asyncio
async def test_blob_url_in_page_fetch_returns_not_ok(tmp_path) -> None:
    download = _download(url=BLOB_URL)
    download.save_as.side_effect = Exception("closed")
    failing_frame = _frame(BLOB_ORIGIN_FRAME_URL, evaluate_return={"ok": False, "status": 0})
    page = _blob_capable_page(failing_frame)

    saved = await _save_adopted_session_download(download, page, tmp_path, workflow_run_id="wr")

    assert saved is None
    page.context.request.get.assert_not_awaited()
    failing_frame.evaluate.assert_awaited_once()
    assert list(tmp_path.iterdir()) == []


@pytest.mark.asyncio
async def test_blob_url_evaluate_raises_returns_none(tmp_path) -> None:
    download = _download(url=BLOB_URL)
    download.save_as.side_effect = Exception("closed")
    raising_frame = _frame(BLOB_ORIGIN_FRAME_URL, evaluate_return=Exception("frame detached"))
    page = _blob_capable_page(raising_frame)

    saved = await _save_adopted_session_download(download, page, tmp_path, workflow_run_id="wr")

    assert saved is None
    page.context.request.get.assert_not_awaited()
    raising_frame.evaluate.assert_awaited_once()
    assert list(tmp_path.iterdir()) == []


@pytest.mark.asyncio
async def test_blob_url_uses_main_frame_when_origin_matches(tmp_path) -> None:
    """If the page's main frame is at the blob origin, route through
    ``evaluate_in_main_world`` (which delegates to ``page.evaluate`` when no
    main-world prefix is configured) instead of calling ``frame.evaluate`` on
    the main frame. The refactor preserves any context-level main-world prefix
    that may be configured on the browser context."""
    download = _download(url=BLOB_URL)
    download.save_as.side_effect = Exception("closed")
    page = MagicMock()
    page.context.request.get = AsyncMock(side_effect=Exception("must not be called"))
    page.main_frame = MagicMock()
    page.main_frame.url = BLOB_ORIGIN_FRAME_URL
    page.main_frame.evaluate = AsyncMock(side_effect=Exception("main_frame.evaluate must not be called"))
    page.evaluate = AsyncMock(return_value={"ok": True, "base64": base64.b64encode(PDF_BODY).decode("ascii")})
    page.frames = [page.main_frame]

    saved = await _save_adopted_session_download(download, page, tmp_path, workflow_run_id="wr")

    assert saved is not None and saved.exists()
    assert saved.read_bytes() == PDF_BODY
    page.evaluate.assert_awaited_once()
    page.main_frame.evaluate.assert_not_awaited()


@pytest.mark.asyncio
async def test_blob_url_recovers_when_save_as_raises_not_just_empty(tmp_path) -> None:
    """``save_as`` can raise outright (e.g. browser tear-down) before producing any
    bytes. The blob recovery path must engage on that branch too, not only on the
    empty-file branch."""
    download = _download(url=BLOB_URL)
    download.save_as.side_effect = Exception("Target page, context or browser has been closed")
    matching_frame = _frame(
        BLOB_ORIGIN_FRAME_URL,
        evaluate_return={"ok": True, "base64": base64.b64encode(PDF_BODY).decode("ascii")},
    )
    page = _blob_capable_page(matching_frame)

    saved = await _save_adopted_session_download(download, page, tmp_path, workflow_run_id="wr")

    assert saved is not None and saved.exists()
    assert saved.read_bytes() == PDF_BODY
    page.context.request.get.assert_not_awaited()
    matching_frame.evaluate.assert_awaited_once()
    assert sorted(p.name for p in tmp_path.iterdir()) == [saved.name]


@pytest.mark.asyncio
async def test_blob_url_main_frame_routes_through_main_world_prefix_when_configured(tmp_path) -> None:
    """When the page context has a main-world prefix configured on the browser
    context, the main-frame dispatch must route through the CDP
    ``Runtime.evaluate`` path so the prefix stays attached. ``page.evaluate``
    must not be called in that case."""
    from skyvern.webeye.main_world_eval import (
        clear_main_world_prefix,
        configure_main_world_prefix,
    )

    download = _download(url=BLOB_URL)
    download.save_as.side_effect = Exception("closed")

    cdp_session = MagicMock()
    cdp_session.send = AsyncMock(
        return_value={"result": {"value": {"ok": True, "base64": base64.b64encode(PDF_BODY).decode("ascii")}}}
    )
    cdp_session.detach = AsyncMock()

    class _FakeContext:
        """Real instance so WeakKeyDictionary can hold it as a key."""

        def __init__(self) -> None:
            self.new_cdp_session = AsyncMock(return_value=cdp_session)

    context = _FakeContext()
    configure_main_world_prefix(context, "/* context-prefix */")
    try:
        page = MagicMock()
        page.context = context
        page.context.request = MagicMock()
        page.context.request.get = AsyncMock(side_effect=Exception("must not be called"))
        page.main_frame = MagicMock()
        page.main_frame.url = BLOB_ORIGIN_FRAME_URL
        page.main_frame.evaluate = AsyncMock(side_effect=Exception("main_frame.evaluate must not be called"))
        page.evaluate = AsyncMock(side_effect=Exception("page.evaluate must not be called when prefix is configured"))
        page.frames = [page.main_frame]

        saved = await _save_adopted_session_download(download, page, tmp_path, workflow_run_id="wr")

        assert saved is not None and saved.exists()
        assert saved.read_bytes() == PDF_BODY
        cdp_session.send.assert_awaited_once()
        send_kwargs = cdp_session.send.await_args
        assert send_kwargs.args[0] == "Runtime.evaluate"
        assert send_kwargs.args[1]["expression"].startswith("/* context-prefix */")
        page.evaluate.assert_not_awaited()
        page.main_frame.evaluate.assert_not_awaited()
    finally:
        clear_main_world_prefix(context)


@pytest.mark.asyncio
async def test_blob_url_matches_frame_whose_url_is_itself_blob(tmp_path) -> None:
    """A frame whose own ``url`` is a ``blob:`` URL with the same origin is a valid
    execution context for the download blob and must be selected as a match.
    When that frame is the page main frame, the dispatch routes through
    ``evaluate_in_main_world``, which delegates to ``page.evaluate`` in the
    no-prefix path."""
    download = _download(url=BLOB_URL)
    download.save_as.side_effect = Exception("closed")
    blob_frame_url = "blob:https://files.example.org/0ff20000-aaaa-bbbb-cccc-111122223333"
    page = MagicMock()
    page.context.request.get = AsyncMock(side_effect=Exception("must not be called"))
    page.main_frame = MagicMock()
    page.main_frame.url = blob_frame_url
    page.main_frame.evaluate = AsyncMock(side_effect=Exception("main_frame.evaluate must not be called"))
    page.evaluate = AsyncMock(return_value={"ok": True, "base64": base64.b64encode(PDF_BODY).decode("ascii")})
    page.frames = [page.main_frame]

    saved = await _save_adopted_session_download(download, page, tmp_path, workflow_run_id="wr")

    assert saved is not None and saved.exists()
    assert saved.read_bytes() == PDF_BODY
    page.evaluate.assert_awaited_once()
    page.main_frame.evaluate.assert_not_awaited()
