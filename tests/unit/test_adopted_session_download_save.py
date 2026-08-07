"""Eager-save-then-refetch for adopted persistent-session downloads.

On an adopted session the run connection owns the download artifact, but in prod the
worker pod can tear the shared browser down before a deferred save_as runs. The helper
saves eagerly and, when save_as raises (TargetClosedError) or yields a 0-byte file,
re-fetches the replayable download url through the run page's request context.

For ``blob:`` URLs (client-side blobs minted by the page) the request-context fetch
cannot be used; the helper runs an in-page ``fetch`` from a frame whose origin owns
the blob and returns the bytes that way.
"""

import asyncio
import base64
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from skyvern.webeye.actions.handler import (
    _EagerAdoptedBlobCapture,
    _read_adopted_session_blob_bytes,
    _save_adopted_session_download,
)
from skyvern.webeye.browser_artifacts import DownloadBinding

PDF_BODY = b"%PDF-1.4\n" + b"x" * 830


def _download(suggested: str = "153743777.pdf", url: str = "https://example.com/download") -> MagicMock:
    download = MagicMock()
    download.suggested_filename = suggested
    download.url = url
    download.save_as = AsyncMock()
    # Playwright reports the owning page here; default to unknown so tests exercise the
    # click-page + context fan-out unless they set it explicitly.
    download.page = None
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
async def test_session_dir_non_blob_suppresses_worker_replay(tmp_path) -> None:
    # A provider-owned remote binding delivers the file through the provider destination, so the run
    # connection has no bytes and a URL replay would run through the wrong identity. The helper must NOT
    # save_as or replay; it returns None (signal only) so the loop keeps polling.
    download = _download()
    page = _page_with_refetch()

    saved = await _save_adopted_session_download(
        download, page, tmp_path, workflow_run_id="wr", download_binding=DownloadBinding.SESSION_DIR
    )

    assert saved is None
    page.context.request.get.assert_not_awaited()
    download.save_as.assert_not_awaited()
    assert list(tmp_path.iterdir()) == []


@pytest.mark.asyncio
async def test_session_dir_blob_still_recovers_in_page(tmp_path) -> None:
    # A blob download on a SESSION_DIR session is identity-safe via the in-page read (the bytes live in
    # the page, not on any network), so it is still delivered — the suppression is non-blob only.
    download = _download(url="blob:https://example.com/abc")
    page = _page_with_refetch()

    saved = await _save_adopted_session_download(
        download,
        page,
        tmp_path,
        workflow_run_id="wr",
        download_binding=DownloadBinding.SESSION_DIR,
        eager_blob_bytes=PDF_BODY,
    )

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
    page.context.request.get.assert_awaited_once_with(download.url, max_redirects=0)


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
    page.context.request.get.assert_awaited_once_with(download.url, max_redirects=0)
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
    # A lone adopted-session page is the only page in its context by default; multi-tab
    # tests override this to add the blob's true owner.
    page.context.pages = [page]
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

    # The live-blob-iframe recovery seam runs after the download-url read fails and would issue its
    # own DOM probe; stub it out so this test isolates the download-url in-page fetch behavior.
    with patch(
        "skyvern.webeye.actions.handler._recover_adopted_session_blob_pdf_iframe",
        new=AsyncMock(return_value=None),
    ) as recover:
        saved = await _save_adopted_session_download(download, page, tmp_path, workflow_run_id="wr")

    assert saved is None
    page.context.request.get.assert_not_awaited()
    failing_frame.evaluate.assert_awaited_once()
    recover.assert_awaited_once_with(page, download, "wr")
    assert list(tmp_path.iterdir()) == []


@pytest.mark.asyncio
async def test_blob_url_evaluate_raises_returns_none(tmp_path) -> None:
    download = _download(url=BLOB_URL)
    download.save_as.side_effect = Exception("closed")
    raising_frame = _frame(BLOB_ORIGIN_FRAME_URL, evaluate_return=Exception("frame detached"))
    page = _blob_capable_page(raising_frame)

    # The live-blob-iframe recovery seam runs after the download-url read fails and would issue its
    # own DOM probe; stub it out so this test isolates the download-url in-page fetch behavior.
    with patch(
        "skyvern.webeye.actions.handler._recover_adopted_session_blob_pdf_iframe",
        new=AsyncMock(return_value=None),
    ) as recover:
        saved = await _save_adopted_session_download(download, page, tmp_path, workflow_run_id="wr")

    assert saved is None
    page.context.request.get.assert_not_awaited()
    raising_frame.evaluate.assert_awaited_once()
    recover.assert_awaited_once_with(page, download, "wr")
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


# ---------------------------------------------------------------------------
# Multi-tab blobs: "View Document" opens the statement in a new tab, so the blob is
# owned by a page other than the one clicked. The helper must fan out over every open
# page in the context instead of reading from the click page alone (SKY-12621).
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_blob_url_reads_from_other_context_page_when_click_page_lacks_blob(tmp_path) -> None:
    download = _download(url=BLOB_URL)
    download.save_as.side_effect = Exception("closed")

    click_page = _blob_capable_page(_frame(OTHER_ORIGIN_FRAME_URL))
    owner_frame = _frame(
        BLOB_ORIGIN_FRAME_URL,
        evaluate_return={"ok": True, "base64": base64.b64encode(PDF_BODY).decode("ascii")},
    )
    owner_page = _blob_capable_page(owner_frame)
    click_page.context.pages = [click_page, owner_page]

    saved = await _save_adopted_session_download(download, click_page, tmp_path, workflow_run_id="wr")

    assert saved is not None and saved.exists(), "blob bytes from the owning tab must be persisted"
    assert saved.read_bytes() == PDF_BODY
    owner_frame.evaluate.assert_awaited_once()
    assert sorted(p.name for p in tmp_path.iterdir()) == [saved.name]


@pytest.mark.asyncio
async def test_blob_url_prefers_download_owner_page(tmp_path) -> None:
    download = _download(url=BLOB_URL)
    download.save_as.side_effect = Exception("closed")

    owner_frame = _frame(
        BLOB_ORIGIN_FRAME_URL,
        evaluate_return={"ok": True, "base64": base64.b64encode(PDF_BODY).decode("ascii")},
    )
    owner_page = _blob_capable_page(owner_frame)
    click_frame = _frame(
        BLOB_ORIGIN_FRAME_URL,
        evaluate_return={"ok": True, "base64": base64.b64encode(b"WRONG").decode("ascii")},
    )
    click_page = _blob_capable_page(click_frame)
    click_page.context.pages = [click_page, owner_page]
    download.page = owner_page

    saved = await _save_adopted_session_download(download, click_page, tmp_path, workflow_run_id="wr")

    assert saved is not None and saved.read_bytes() == PDF_BODY
    owner_frame.evaluate.assert_awaited_once()
    click_frame.evaluate.assert_not_awaited()


@pytest.mark.asyncio
async def test_blob_url_no_page_owns_blob_returns_none(tmp_path) -> None:
    download = _download(url=BLOB_URL)
    download.save_as.side_effect = Exception("closed")
    click_page = _blob_capable_page(_frame(OTHER_ORIGIN_FRAME_URL))
    other_page = _blob_capable_page(_frame(OTHER_ORIGIN_FRAME_URL))
    click_page.context.pages = [click_page, other_page]

    saved = await _save_adopted_session_download(download, click_page, tmp_path, workflow_run_id="wr")

    assert saved is None
    assert list(tmp_path.iterdir()) == []


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


# ---------------------------------------------------------------------------
# Event-time eager capture (SKY-12621 fix #2). A blob download's owning document is
# frequently torn down before the ~1s download poll, so the post-hoc fan-out reads a
# context where the owner is already gone. Bytes captured at the download event must be
# used outright, and the fan-out kept only as a fallback.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_eager_blob_bytes_used_even_when_owner_unreadable_at_save_time(tmp_path) -> None:
    """The pre-captured bytes must persist even though save_as and every open page fail by save time."""
    download = _download(url=BLOB_URL)
    download.save_as = AsyncMock(side_effect=AssertionError("save_as must not run when eager bytes exist"))
    # Owner is gone: no frame can resolve the blob and the context has only the click page.
    page = _blob_capable_page(_frame(OTHER_ORIGIN_FRAME_URL))

    saved = await _save_adopted_session_download(
        download, page, tmp_path, workflow_run_id="wr", eager_blob_bytes=PDF_BODY
    )

    assert saved is not None and saved.exists()
    assert saved.read_bytes() == PDF_BODY
    download.save_as.assert_not_awaited()
    page.main_frame.evaluate.assert_not_awaited()
    assert sorted(p.name for p in tmp_path.iterdir()) == [saved.name]


@pytest.mark.asyncio
async def test_eager_capture_reads_popup_owner_page(tmp_path) -> None:
    """When the blob is minted in a popup tab, the eager read must resolve it from download.page."""
    download = _download(url=BLOB_URL)
    owner_frame = _frame(
        BLOB_ORIGIN_FRAME_URL,
        evaluate_return={"ok": True, "base64": base64.b64encode(PDF_BODY).decode("ascii")},
    )
    owner_popup = _blob_capable_page(owner_frame)
    download.page = owner_popup
    clicked_page = _blob_capable_page(_frame(OTHER_ORIGIN_FRAME_URL))

    capture = _EagerAdoptedBlobCapture(enabled=True, clicked_page=clicked_page, workflow_run_id="wr")
    capture.maybe_start(download)
    captured = await capture.result(timeout=5)

    assert captured == PDF_BODY
    owner_frame.evaluate.assert_awaited_once()


@pytest.mark.asyncio
async def test_eager_none_falls_through_to_save_as_and_fan_out(tmp_path) -> None:
    """eager_blob_bytes=None must leave the existing save_as + fan-out recovery path intact."""
    download = _download(url=BLOB_URL)
    download.save_as.side_effect = Exception("closed")
    owner_frame = _frame(
        BLOB_ORIGIN_FRAME_URL,
        evaluate_return={"ok": True, "base64": base64.b64encode(PDF_BODY).decode("ascii")},
    )
    click_page = _blob_capable_page(_frame(OTHER_ORIGIN_FRAME_URL))
    owner_page = _blob_capable_page(owner_frame)
    click_page.context.pages = [click_page, owner_page]

    saved = await _save_adopted_session_download(
        download, click_page, tmp_path, workflow_run_id="wr", eager_blob_bytes=None
    )

    assert saved is not None and saved.read_bytes() == PDF_BODY
    download.save_as.assert_awaited_once()
    owner_frame.evaluate.assert_awaited_once()


@pytest.mark.asyncio
async def test_eager_zero_byte_bytes_fall_through_to_fallback(tmp_path) -> None:
    """A zero-byte eager capture is a false success: don't write it, fall through to save_as/fan-out."""
    download = _download(url=BLOB_URL)

    async def _save(target: object) -> None:
        Path(str(target)).write_bytes(PDF_BODY)

    download.save_as = AsyncMock(side_effect=_save)
    page = _blob_capable_page(_frame(OTHER_ORIGIN_FRAME_URL))

    saved = await _save_adopted_session_download(download, page, tmp_path, workflow_run_id="wr", eager_blob_bytes=b"")

    # save_as fallback ran and produced the real artifact; no zero-byte file was persisted as success.
    download.save_as.assert_awaited_once()
    assert saved is not None and saved.read_bytes() == PDF_BODY
    assert sorted(p.name for p in tmp_path.iterdir()) == [saved.name]


@pytest.mark.asyncio
async def test_non_blob_download_skips_eager_capture(tmp_path) -> None:
    """A non-blob download must not arm the eager blob reader, and eager bytes are ignored for it."""
    http_download = _download(url="https://example.com/statement.pdf")
    capture = _EagerAdoptedBlobCapture(enabled=True, clicked_page=MagicMock(), workflow_run_id="wr")
    capture.maybe_start(http_download)
    assert await capture.result(timeout=1) is None

    async def _save(target: object) -> None:
        Path(str(target)).write_bytes(PDF_BODY)

    http_download.save_as.side_effect = _save
    page = _page_with_refetch()
    saved = await _save_adopted_session_download(
        http_download, page, tmp_path, workflow_run_id="wr", eager_blob_bytes=b"IGNORED"
    )
    assert saved is not None and saved.read_bytes() == PDF_BODY
    http_download.save_as.assert_awaited_once()


@pytest.mark.asyncio
async def test_eager_capture_disabled_for_managed_session() -> None:
    """Managed sessions (enabled=False) never arm the eager reader."""
    download = _download(url=BLOB_URL)
    capture = _EagerAdoptedBlobCapture(enabled=False, clicked_page=MagicMock(), workflow_run_id="wr")
    capture.maybe_start(download)
    assert await capture.result(timeout=1) is None


@pytest.mark.asyncio
async def test_eager_capture_lifecycle_success_no_leak() -> None:
    download = _download(url=BLOB_URL)
    with patch(
        "skyvern.webeye.actions.handler._read_adopted_session_blob_bytes",
        AsyncMock(return_value=PDF_BODY),
    ):
        capture = _EagerAdoptedBlobCapture(enabled=True, clicked_page=MagicMock(), workflow_run_id="wr")
        capture.maybe_start(download)
        assert await capture.result(timeout=5) == PDF_BODY
        await capture.aclose()
        assert capture._task is not None and capture._task.done()


@pytest.mark.asyncio
async def test_eager_capture_lifecycle_timeout_then_cleanup() -> None:
    download = _download(url=BLOB_URL)
    started = asyncio.Event()

    async def _hang(*args, **kwargs) -> bytes:
        started.set()
        await asyncio.Event().wait()
        return PDF_BODY

    with patch("skyvern.webeye.actions.handler._read_adopted_session_blob_bytes", _hang):
        capture = _EagerAdoptedBlobCapture(enabled=True, clicked_page=MagicMock(), workflow_run_id="wr")
        capture.maybe_start(download)
        await started.wait()
        # A short deadline returns None and cancel+drains the read so no read runs concurrently
        # with save_as/fan-out.
        assert await capture.result(timeout=0.01) is None
        assert capture._task is not None and capture._task.done()
        # aclose is idempotent once result already drained the task.
        await capture.aclose()
        assert capture._task.done()


@pytest.mark.asyncio
async def test_eager_capture_aclose_cancels_before_completion() -> None:
    download = _download(url=BLOB_URL)
    started = asyncio.Event()

    async def _hang(*args, **kwargs) -> bytes:
        started.set()
        await asyncio.Event().wait()
        return PDF_BODY

    with patch("skyvern.webeye.actions.handler._read_adopted_session_blob_bytes", _hang):
        capture = _EagerAdoptedBlobCapture(enabled=True, clicked_page=MagicMock(), workflow_run_id="wr")
        capture.maybe_start(download)
        await started.wait()
        await capture.aclose()
        assert capture._task is not None and capture._task.cancelled()


@pytest.mark.asyncio
async def test_eager_capture_aclose_reraises_outer_cancellation() -> None:
    """aclose must swallow only the cancellation it requested; an outer cancel on the awaiting
    coroutine must propagate so the enclosing timeout/cancel scope still sees it."""
    download = _download(url=BLOB_URL)
    started = asyncio.Event()

    async def _hang(*args, **kwargs) -> bytes:
        started.set()
        await asyncio.Event().wait()
        return PDF_BODY

    with patch("skyvern.webeye.actions.handler._read_adopted_session_blob_bytes", _hang):
        capture = _EagerAdoptedBlobCapture(enabled=True, clicked_page=MagicMock(), workflow_run_id="wr")
        capture.maybe_start(download)
        await started.wait()

        # Request cancellation of this coroutine; it lands on aclose's ``await task`` suspension.
        this = asyncio.current_task()
        assert this is not None
        this.cancel()
        reraised = False
        try:
            await capture.aclose()
        except asyncio.CancelledError:
            reraised = True
            this.uncancel()

    assert reraised, "aclose must propagate an outer cancellation, not swallow it"


@pytest.mark.asyncio
async def test_retention_teardown_runs_even_when_aclose_is_cancelled() -> None:
    """If closing the eager capture raises CancelledError, the page-realm retention wrapper must still
    be torn down for an adopted session, and the original cancellation must propagate afterwards."""
    from skyvern.webeye.actions.handler import _close_eager_capture_then_teardown_retention

    capture = MagicMock()
    capture.aclose = AsyncMock(side_effect=asyncio.CancelledError())
    page = MagicMock()

    with patch("skyvern.webeye.actions.handler.teardown_blob_url_retention", AsyncMock()) as teardown:
        with pytest.raises(asyncio.CancelledError):
            await _close_eager_capture_then_teardown_retention(
                capture, page, browser_session_id="pbs-1", workflow_run_id="wr"
            )

    teardown.assert_awaited_once()


@pytest.mark.asyncio
async def test_retention_teardown_failure_does_not_replace_cancellation() -> None:
    """A teardown failure stays fail-open/debug-only and must not swallow or replace the original
    cancellation raised by aclose."""
    from skyvern.webeye.actions.handler import _close_eager_capture_then_teardown_retention

    capture = MagicMock()
    capture.aclose = AsyncMock(side_effect=asyncio.CancelledError())
    page = MagicMock()

    with patch(
        "skyvern.webeye.actions.handler.teardown_blob_url_retention",
        AsyncMock(side_effect=RuntimeError("teardown boom")),
    ) as teardown:
        with pytest.raises(asyncio.CancelledError):
            await _close_eager_capture_then_teardown_retention(
                capture, page, browser_session_id="pbs-1", workflow_run_id="wr"
            )

    teardown.assert_awaited_once()


@pytest.mark.asyncio
async def test_retention_teardown_skipped_without_browser_session() -> None:
    """Non-adopted sessions never installed the retention wrapper, so teardown must not run."""
    from skyvern.webeye.actions.handler import _close_eager_capture_then_teardown_retention

    capture = MagicMock()
    capture.aclose = AsyncMock()
    page = MagicMock()

    with patch("skyvern.webeye.actions.handler.teardown_blob_url_retention", AsyncMock()) as teardown:
        await _close_eager_capture_then_teardown_retention(capture, page, browser_session_id=None, workflow_run_id="wr")

    capture.aclose.assert_awaited_once()
    teardown.assert_not_awaited()


@pytest.mark.asyncio
async def test_read_adopted_session_blob_bytes_prefers_owner_then_fans_out() -> None:
    """The extracted reader returns the first page that owns the blob, owner (download.page) first."""
    download = _download(url=BLOB_URL)
    owner_frame = _frame(
        BLOB_ORIGIN_FRAME_URL,
        evaluate_return={"ok": True, "base64": base64.b64encode(PDF_BODY).decode("ascii")},
    )
    owner_page = _blob_capable_page(owner_frame)
    click_page = _blob_capable_page(_frame(OTHER_ORIGIN_FRAME_URL))
    click_page.context.pages = [click_page, owner_page]
    download.page = owner_page

    assert await _read_adopted_session_blob_bytes(download, click_page, workflow_run_id="wr") == PDF_BODY
    owner_frame.evaluate.assert_awaited_once()


@pytest.mark.asyncio
async def test_read_adopted_session_blob_bytes_passes_memory_cap() -> None:
    """The reader must cap eager base64 capture with the canonical MAX_FILE_SIZE_BYTES."""
    from skyvern.webeye.actions.handler import MAX_FILE_SIZE_BYTES

    download = _download(url=BLOB_URL)
    click_page = _blob_capable_page(_frame(OTHER_ORIGIN_FRAME_URL))
    with patch(
        "skyvern.webeye.actions.handler.SkyvernFrame.read_blob_url_bytes",
        AsyncMock(return_value=PDF_BODY),
    ) as read_mock:
        result = await _read_adopted_session_blob_bytes(download, click_page, workflow_run_id="wr")

    assert result == PDF_BODY
    assert read_mock.await_count >= 1
    for read_call in read_mock.await_args_list:
        assert read_call.kwargs["max_size_bytes"] == MAX_FILE_SIZE_BYTES


@pytest.mark.asyncio
async def test_read_adopted_session_blob_bytes_oversized_returns_none() -> None:
    """When read_blob_url_bytes rejects an over-cap blob (None), the reader yields None (fallback)."""
    download = _download(url=BLOB_URL)
    click_page = _blob_capable_page(_frame(OTHER_ORIGIN_FRAME_URL))
    with patch(
        "skyvern.webeye.actions.handler.SkyvernFrame.read_blob_url_bytes",
        AsyncMock(return_value=None),
    ):
        assert await _read_adopted_session_blob_bytes(download, click_page, workflow_run_id="wr") is None


@pytest.fixture(autouse=True)
def _resolvable_example_host(monkeypatch: pytest.MonkeyPatch) -> None:
    """Keep the module's ``example.com`` fixtures off real DNS once destinations are checked."""
    import socket

    real_getaddrinfo = socket.getaddrinfo

    def fake_getaddrinfo(host: str, port: object = None, *args: object, **kwargs: object) -> list:
        if host == "example.com":
            return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("93.184.216.34", port or 0))]
        return real_getaddrinfo(host, port, *args, **kwargs)

    monkeypatch.setattr(socket, "getaddrinfo", fake_getaddrinfo)


@pytest.mark.asyncio
async def test_refetch_refuses_internal_destination(tmp_path, download_destinations, fake_api_request_context) -> None:
    download = _download(url=f"{download_destinations.internal_base}/internal")
    download.save_as.side_effect = Exception("Target page, context or browser has been closed")
    page = _page_with_refetch()
    page.context.request = fake_api_request_context()

    saved = await _save_adopted_session_download(download, page, tmp_path, workflow_run_id="wr")

    assert saved is None
    assert download_destinations.reached_internal() is False
    assert all(path.read_bytes() != download_destinations.INTERNAL_BODY for path in tmp_path.iterdir())


@pytest.mark.asyncio
async def test_refetch_refuses_redirect_hop_to_internal_destination(
    tmp_path, download_destinations, fake_api_request_context
) -> None:
    download = _download(url=f"{download_destinations.public_base}/redirect-to-internal")
    download.save_as.side_effect = Exception("Target page, context or browser has been closed")
    page = _page_with_refetch()
    page.context.request = fake_api_request_context()

    saved = await _save_adopted_session_download(download, page, tmp_path, workflow_run_id="wr")

    assert saved is None
    assert download_destinations.reached_internal() is False
    assert all(path.read_bytes() != download_destinations.INTERNAL_BODY for path in tmp_path.iterdir())


@pytest.mark.asyncio
async def test_refetch_allows_permitted_destination(tmp_path, download_destinations, fake_api_request_context) -> None:
    # Non-vacuity: a permitted destination must still round-trip through the re-fetch path.
    download = _download(url=f"{download_destinations.public_base}/attachment")
    download.save_as.side_effect = Exception("Target page, context or browser has been closed")
    page = _page_with_refetch()
    page.context.request = fake_api_request_context()

    saved = await _save_adopted_session_download(download, page, tmp_path, workflow_run_id="wr")

    assert saved is not None and saved.exists()
    assert saved.read_bytes() == download_destinations.PUBLIC_BODY
