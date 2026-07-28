import base64
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from skyvern.webeye.utils.page import _SAME_ORIGIN_FETCH_JS, SkyvernFrame

_MOD = "skyvern.webeye.utils.page"


@pytest.mark.asyncio
async def test_read_blob_url_bytes_rejects_oversized_blob() -> None:
    """An oversized blob is rejected in-page (before base64 serialization) and yields None."""
    page = MagicMock()
    main_frame = MagicMock()
    page.main_frame = main_frame
    eval_mock = AsyncMock(return_value={"ok": False, "error": "too_large", "size": 500_000_000})

    with (
        patch(f"{_MOD}._frames_for_blob_origin", return_value=[main_frame]),
        patch(f"{_MOD}.evaluate_in_main_world", new=eval_mock),
    ):
        result = await SkyvernFrame.read_blob_url_bytes(
            page=page, blob_url="blob:https://example.com/big", max_size_bytes=1024
        )

    assert result is None
    # max size is threaded into the in-page arg so the guard runs before serialization
    _page, _js, arg = eval_mock.await_args.args
    assert arg == {"url": "blob:https://example.com/big", "maxSizeBytes": 1024}


@pytest.mark.asyncio
async def test_read_blob_url_bytes_returns_bytes_within_limit() -> None:
    page = MagicMock()
    main_frame = MagicMock()
    page.main_frame = main_frame
    payload = b"%PDF small within limit"
    eval_mock = AsyncMock(return_value={"ok": True, "base64": base64.b64encode(payload).decode()})

    with (
        patch(f"{_MOD}._frames_for_blob_origin", return_value=[main_frame]),
        patch(f"{_MOD}.evaluate_in_main_world", new=eval_mock),
    ):
        result = await SkyvernFrame.read_blob_url_bytes(
            page=page, blob_url="blob:https://example.com/ok", max_size_bytes=1024
        )

    assert result == payload
    _page, _js, arg = eval_mock.await_args.args
    assert arg["maxSizeBytes"] == 1024


@pytest.mark.asyncio
async def test_read_blob_url_bytes_returns_empty_bytes_for_zero_byte_blob() -> None:
    page = MagicMock()
    main_frame = MagicMock()
    page.main_frame = main_frame
    eval_mock = AsyncMock(return_value={"ok": True, "base64": ""})

    with (
        patch(f"{_MOD}._frames_for_blob_origin", return_value=[main_frame]),
        patch(f"{_MOD}.evaluate_in_main_world", new=eval_mock),
    ):
        result = await SkyvernFrame.read_blob_url_bytes(
            page=page, blob_url="blob:https://example.com/empty", max_size_bytes=1024
        )

    assert result == b""


@pytest.mark.asyncio
async def test_read_blob_url_bytes_no_limit_passes_none() -> None:
    """Existing callers that pass no limit still work; maxSizeBytes is None (no in-page check)."""
    page = MagicMock()
    main_frame = MagicMock()
    page.main_frame = main_frame
    payload = b"unbounded"
    eval_mock = AsyncMock(return_value={"ok": True, "base64": base64.b64encode(payload).decode()})

    with (
        patch(f"{_MOD}._frames_for_blob_origin", return_value=[main_frame]),
        patch(f"{_MOD}.evaluate_in_main_world", new=eval_mock),
    ):
        result = await SkyvernFrame.read_blob_url_bytes(page=page, blob_url="blob:https://example.com/x")

    assert result == payload
    _page, _js, arg = eval_mock.await_args.args
    assert arg["maxSizeBytes"] is None


@pytest.mark.asyncio
async def test_read_blob_url_bytes_opaque_origin_probes_frames_and_succeeds() -> None:
    """Opaque-origin blob (blob:null/...) has no matchable origin; probe frames and read the
    bytes from whichever frame owns it (here a later sub-frame)."""
    page = MagicMock()
    main_frame = MagicMock()
    page.main_frame = main_frame
    sub_frame = MagicMock()
    payload = b"%PDF opaque-origin blob"
    sub_frame.evaluate = AsyncMock(return_value={"ok": True, "base64": base64.b64encode(payload).decode()})
    main_eval = AsyncMock(return_value={"ok": False, "error": "not_owner"})  # main frame doesn't own it

    with (
        patch(f"{_MOD}._all_page_frames", return_value=[main_frame, sub_frame]) as all_frames,
        patch(f"{_MOD}._frames_for_blob_origin") as origin_frames,
        patch(f"{_MOD}.evaluate_in_main_world", new=main_eval),
    ):
        result = await SkyvernFrame.read_blob_url_bytes(page=page, blob_url="blob:null/abc-123", max_size_bytes=1024)

    assert result == payload
    all_frames.assert_called_once()  # opaque path probes all frames
    origin_frames.assert_not_called()  # not the origin-matched path


@pytest.mark.asyncio
async def test_read_blob_url_bytes_opaque_origin_returns_none_when_all_probes_fail() -> None:
    page = MagicMock()
    main_frame = MagicMock()
    page.main_frame = main_frame
    sub_frame = MagicMock()
    sub_frame.evaluate = AsyncMock(return_value={"ok": False, "error": "not_owner"})
    main_eval = AsyncMock(return_value={"ok": False, "error": "not_owner"})

    with (
        patch(f"{_MOD}._all_page_frames", return_value=[main_frame, sub_frame]),
        patch(f"{_MOD}.evaluate_in_main_world", new=main_eval),
    ):
        result = await SkyvernFrame.read_blob_url_bytes(page=page, blob_url="blob:null/xyz")

    assert result is None


@pytest.mark.asyncio
async def test_read_blob_url_bytes_probe_mode_does_not_emit_error_on_miss() -> None:
    """A page that doesn't own the blob origin is an expected miss during multi-page fallback —
    probe=True must not spam ERROR logs; the caller keeps the one final failure signal."""
    page = MagicMock()
    page.main_frame = MagicMock()
    log_mock = MagicMock()

    with (
        patch(f"{_MOD}._frames_for_blob_origin", return_value=[]),
        patch(f"{_MOD}.LOG", log_mock),
    ):
        result = await SkyvernFrame.read_blob_url_bytes(page=page, blob_url="blob:https://example.com/x", probe=True)

    assert result is None
    log_mock.error.assert_not_called()
    assert log_mock.debug.called


@pytest.mark.asyncio
async def test_read_blob_url_bytes_non_probe_logs_error_on_miss() -> None:
    page = MagicMock()
    page.main_frame = MagicMock()
    log_mock = MagicMock()

    with (
        patch(f"{_MOD}._frames_for_blob_origin", return_value=[]),
        patch(f"{_MOD}.LOG", log_mock),
    ):
        result = await SkyvernFrame.read_blob_url_bytes(page=page, blob_url="blob:https://example.com/x", probe=False)

    assert result is None
    log_mock.error.assert_called()


@pytest.mark.asyncio
async def test_read_http_url_bytes_routes_main_frame_through_skyvern_frame_evaluate() -> None:
    """The main-frame fetch goes through the unified SkyvernFrame.evaluate seam, targeting the
    Page so the context-level main-world prefix stays attached (not a raw evaluate bypass)."""
    page = MagicMock()
    main_frame = MagicMock()
    page.main_frame = main_frame
    payload = b"%PDF-1.7 statement bytes"
    eval_mock = AsyncMock(return_value={"ok": True, "base64": base64.b64encode(payload).decode()})

    with (
        patch(f"{_MOD}._frames_for_origin", return_value=[main_frame]),
        patch.object(SkyvernFrame, "evaluate", eval_mock),
    ):
        result = await SkyvernFrame.read_http_url_bytes(
            page=page, url="https://host.example/api/StatementPdf?access_token=x", max_size_bytes=1024
        )

    assert result == payload
    eval_mock.assert_awaited_once()
    kwargs = eval_mock.await_args.kwargs
    assert kwargs["frame"] is page  # main-frame targets the Page for main-world routing
    assert kwargs["expression"] == _SAME_ORIGIN_FETCH_JS
    assert kwargs["arg"] == {"url": "https://host.example/api/StatementPdf?access_token=x", "maxSizeBytes": 1024}


@pytest.mark.asyncio
async def test_read_http_url_bytes_forwards_timeout_ms_to_skyvern_frame_evaluate() -> None:
    """The optional timeout_ms is forwarded to SkyvernFrame.evaluate so a caller (blocked-inline
    PDF recovery) can widen past the generic ~5s action timeout without a per-candidate budget."""
    page = MagicMock()
    main_frame = MagicMock()
    page.main_frame = main_frame
    payload = b"%PDF-1.7 slow-but-alive statement"
    eval_mock = AsyncMock(return_value={"ok": True, "base64": base64.b64encode(payload).decode()})

    with (
        patch(f"{_MOD}._frames_for_origin", return_value=[main_frame]),
        patch.object(SkyvernFrame, "evaluate", eval_mock),
    ):
        result = await SkyvernFrame.read_http_url_bytes(page=page, url="https://host.example/x.pdf", timeout_ms=30_000)

    assert result == payload
    assert eval_mock.await_args.kwargs["timeout_ms"] == 30_000


@pytest.mark.asyncio
async def test_read_http_url_bytes_routes_sub_frame_through_skyvern_frame_evaluate() -> None:
    """A same-origin sub-frame is routed through SkyvernFrame.evaluate targeting the Frame itself
    (main-world prefix is page-scoped, so sub-frames evaluate in-frame)."""
    page = MagicMock()
    page.main_frame = MagicMock()
    sub_frame = MagicMock()
    payload = b"%PDF sub-frame bytes"
    eval_mock = AsyncMock(return_value={"ok": True, "base64": base64.b64encode(payload).decode()})

    with (
        patch(f"{_MOD}._frames_for_origin", return_value=[sub_frame]),
        patch.object(SkyvernFrame, "evaluate", eval_mock),
    ):
        result = await SkyvernFrame.read_http_url_bytes(page=page, url="https://host.example/x.pdf")

    assert result == payload
    kwargs = eval_mock.await_args.kwargs
    assert kwargs["frame"] is sub_frame
    assert kwargs["expression"] == _SAME_ORIGIN_FETCH_JS


@pytest.mark.asyncio
async def test_read_http_url_bytes_dispatches_sub_frame_to_frame_evaluate() -> None:
    """End-to-end through the real SkyvernFrame.evaluate: a sub-frame target dispatches to
    frame.evaluate (not the page main-world path)."""
    page = MagicMock()
    page.main_frame = MagicMock()
    sub_frame = MagicMock()
    payload = b"%PDF sub-frame bytes"
    sub_frame.evaluate = AsyncMock(return_value={"ok": True, "base64": base64.b64encode(payload).decode()})

    with patch(f"{_MOD}._frames_for_origin", return_value=[sub_frame]):
        result = await SkyvernFrame.read_http_url_bytes(page=page, url="https://host.example/x.pdf")

    assert result == payload
    sub_frame.evaluate.assert_awaited_once()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "url",
    [
        "blob:https://host.example/abc",
        "data:application/pdf;base64,AAAA",
        "about:blank",
        "javascript:void(0)",
        "file:///etc/passwd",
        "chrome-error://chromewebdata/",
    ],
)
async def test_read_http_url_bytes_rejects_non_http_urls(url: str) -> None:
    page = MagicMock()
    with patch(f"{_MOD}._frames_for_origin") as frames:
        result = await SkyvernFrame.read_http_url_bytes(page=page, url=url)

    assert result is None
    frames.assert_not_called()  # scheme rejected before any frame lookup


@pytest.mark.asyncio
async def test_read_http_url_bytes_returns_none_without_same_origin_frame() -> None:
    page = MagicMock()
    page.main_frame = MagicMock()
    with patch(f"{_MOD}._frames_for_origin", return_value=[]):
        result = await SkyvernFrame.read_http_url_bytes(page=page, url="https://host.example/x.pdf")

    assert result is None


@pytest.mark.asyncio
async def test_read_http_url_bytes_returns_none_when_fetch_not_ok() -> None:
    page = MagicMock()
    main_frame = MagicMock()
    page.main_frame = main_frame
    eval_mock = AsyncMock(return_value={"ok": False, "status": 403})

    with (
        patch(f"{_MOD}._frames_for_origin", return_value=[main_frame]),
        patch(f"{_MOD}.evaluate_in_main_world", new=eval_mock),
    ):
        result = await SkyvernFrame.read_http_url_bytes(page=page, url="https://host.example/x.pdf")

    assert result is None


@pytest.mark.asyncio
async def test_read_http_url_bytes_rejects_oversized() -> None:
    page = MagicMock()
    main_frame = MagicMock()
    page.main_frame = main_frame
    eval_mock = AsyncMock(return_value={"ok": False, "error": "too_large", "size": 999_999_999})

    with (
        patch(f"{_MOD}._frames_for_origin", return_value=[main_frame]),
        patch(f"{_MOD}.evaluate_in_main_world", new=eval_mock),
    ):
        result = await SkyvernFrame.read_http_url_bytes(page=page, url="https://host.example/x.pdf", max_size_bytes=10)

    assert result is None
