import base64
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from skyvern.webeye.utils.page import SkyvernFrame

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
    assert arg == {"blobUrl": "blob:https://example.com/big", "maxSizeBytes": 1024}


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
