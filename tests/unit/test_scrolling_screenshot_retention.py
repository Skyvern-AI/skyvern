"""The multi-viewport LITE stitch must release its decoded images eagerly.

``SkyvernFrame.take_scrolling_screenshot`` decodes one ``PIL.Image`` per viewport,
stitches them, and PNG-encodes the result. Those objects land in reference cycles that
generation-0 GC defers, so plain refcount drop leaves ~100 MB/event resident until a full
collection runs (which can exhaust worker memory). These tests pin the fix: explicit close of the decoded
images, the stitched image, and the PNG buffer, followed by a full ``gc.collect()`` scoped
to the multi-viewport stitch — without changing the returned bytes or scroll restoration.

The ``test_cdp_rescue_*`` cases cover the same handler's other branch: the raw-CDP rescue that runs
when the capture primitive itself failed, and the conditions under which it must stay out of the way.
"""

from __future__ import annotations

import asyncio
import base64
from io import BytesIO
from pathlib import Path
from typing import cast
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from PIL import Image
from playwright.async_api import TimeoutError as PlaywrightTimeoutError

import skyvern.webeye.utils.page as page_module
from skyvern.exceptions import FailedToTakeScreenshot
from skyvern.webeye.browser_engine import (
    SKYCDP_ENGINE_NAME,
    BrowserDriverStarter,
    BrowserEngineMetadata,
    BrowserEngineSelection,
)
from skyvern.webeye.browser_health import BrowserOperation
from skyvern.webeye.utils.page import ScreenshotMode


def _png_bytes(width: int, height: int, color: tuple[int, int, int]) -> bytes:
    buffer = BytesIO()
    Image.new("RGB", (width, height), color).save(buffer, format="PNG")
    return buffer.getvalue()


def _is_closed(image: Image.Image) -> bool:
    try:
        image.load()
        return False
    except ValueError as exc:
        return "closed image" in str(exc)


def _expected_merged_png(screenshots: list[bytes], positions: list[int]) -> bytes:
    """Reproduce the shipped stitch on a throwaway image set to lock byte-compatibility."""
    images: list[Image.Image] = []
    for screenshot in screenshots:
        img = Image.open(BytesIO(screenshot))
        img.load()
        images.append(img)
    merged = page_module._merge_images_by_position(images, positions)
    out = BytesIO()
    merged.save(out, format="PNG")
    out.seek(0)
    data = out.read()
    merged.close()
    for img in images:
        img.close()
    out.close()
    return data


class _FakeSkyvernFrame:
    """Records the scroll-restoration call issued by the outer finally."""

    def __init__(self) -> None:
        self.scroll_restore_calls: list[tuple[int, int]] = []

    async def get_scroll_x_y(self) -> tuple[int, int]:
        return (11, 22)

    async def safe_scroll_to_x_y(self, x: int, y: int) -> None:
        self.scroll_restore_calls.append((x, y))


def _tracking_bytesio_factory(instances: list[BytesIO]) -> type[BytesIO]:
    class _TrackingBytesIO(BytesIO):
        def __init__(self, initial_bytes: bytes = b"") -> None:
            super().__init__(initial_bytes)
            self.initial_len = len(initial_bytes)
            self.close_count = 0
            instances.append(self)

        def close(self) -> None:
            self.close_count += 1
            super().close()

    return _TrackingBytesIO


async def _invoke_take_scrolling_screenshot(
    *,
    screenshots: list[bytes],
    positions: list[int],
    scrolling_number: int,
    merge_impl: object,
    fake_gc: MagicMock,
    bytesio_instances: list[BytesIO],
    fake_frame: _FakeSkyvernFrame,
    fallback_bytes: bytes = b"FALLBACK",
    page: MagicMock | None = None,
    engine_selection: BrowserEngineSelection | None = None,
    scroll_helper_exc: Exception | None = None,
    record_recovery: MagicMock | None = None,
    file_path: str | None = None,
) -> bytes:
    tracking_bytesio = _tracking_bytesio_factory(bytesio_instances)
    scroll_helper = (
        AsyncMock(side_effect=scroll_helper_exc)
        if scroll_helper_exc is not None
        else AsyncMock(return_value=(list(screenshots), list(positions)))
    )
    with (
        patch.object(page_module.SkyvernFrame, "create_instance", AsyncMock(return_value=fake_frame)),
        patch.object(page_module, "_scrolling_screenshots_helper", scroll_helper),
        patch.object(page_module, "_merge_images_by_position", MagicMock(side_effect=merge_impl)),
        patch.object(
            page_module,
            "_current_viewpoint_screenshot_helper",
            AsyncMock(return_value=fallback_bytes),
        ),
        patch.object(page_module, "BytesIO", tracking_bytesio),
        patch.object(page_module, "gc", fake_gc, create=True),
        patch.object(page_module.skyvern_context, "record_browser_success", MagicMock()),
        patch.object(page_module.skyvern_context, "record_browser_recovery", record_recovery or MagicMock()),
        patch.object(page_module, "CDP_RESCUE_SESSION_TIMEOUT_SECONDS", 0.05),
        patch.object(page_module, "CDP_RESCUE_CAPTURE_TIMEOUT_SECONDS", 0.05),
        patch.object(page_module, "CDP_RESCUE_DETACH_TIMEOUT_SECONDS", 0.05),
    ):
        return await page_module.SkyvernFrame.take_scrolling_screenshot(
            page=page if page is not None else MagicMock(name="page"),
            file_path=file_path,
            mode=ScreenshotMode.LITE,
            scrolling_number=scrolling_number,
            engine_selection=engine_selection,
        )


@pytest.mark.asyncio
async def test_multi_viewport_stitch_closes_resources_and_collects() -> None:
    screenshots = [
        _png_bytes(120, 100, (10, 20, 30)),
        _png_bytes(120, 100, (40, 50, 60)),
        _png_bytes(120, 100, (70, 80, 90)),
    ]
    positions = [0, 80, 160]
    expected = _expected_merged_png(screenshots, positions)

    captured: dict[str, object] = {}
    real_merge = page_module._merge_images_by_position

    def _spy_merge(images: list[Image.Image], pos: list[int]) -> Image.Image:
        captured["images"] = list(images)
        merged = real_merge(images, pos)
        captured["merged"] = merged
        return merged

    gc_state: dict[str, bool] = {}
    fake_gc = MagicMock()

    def _on_collect() -> None:
        imgs = captured["images"]
        merged = captured["merged"]
        assert isinstance(imgs, list)
        assert isinstance(merged, Image.Image)
        gc_state["all_closed_at_gc"] = all(_is_closed(i) for i in imgs) and _is_closed(merged)

    fake_gc.collect.side_effect = _on_collect

    bytesio_instances: list[BytesIO] = []
    fake_frame = _FakeSkyvernFrame()

    result = await _invoke_take_scrolling_screenshot(
        screenshots=screenshots,
        positions=positions,
        scrolling_number=3,
        merge_impl=_spy_merge,
        fake_gc=fake_gc,
        bytesio_instances=bytesio_instances,
        fake_frame=fake_frame,
    )

    assert result == expected

    decoded = captured["images"]
    merged_img = captured["merged"]
    assert isinstance(decoded, list) and len(decoded) == 3
    assert all(_is_closed(img) for img in decoded), "all decoded viewport images must be closed"
    assert isinstance(merged_img, Image.Image)
    assert _is_closed(merged_img), "the stitched image must be closed"

    output_buffers = [b for b in bytesio_instances if getattr(b, "initial_len", None) == 0]
    assert len(output_buffers) == 1, "exactly one PNG output buffer expected"
    assert output_buffers[0].close_count >= 1 and output_buffers[0].closed, "PNG buffer must be closed"

    fake_gc.collect.assert_called_once()
    assert gc_state.get("all_closed_at_gc") is True, "gc.collect() must run AFTER explicit cleanup"

    assert fake_frame.scroll_restore_calls == [(11, 22)], "scroll position must be restored"


@pytest.mark.asyncio
async def test_single_viewport_stitch_skips_full_gc_and_avoids_double_close() -> None:
    screenshots = [_png_bytes(120, 100, (10, 20, 30))]
    positions = [0]
    expected = _expected_merged_png(screenshots, positions)

    captured: dict[str, object] = {}
    close_counts: dict[int, int] = {}
    real_merge = page_module._merge_images_by_position

    def _spy_merge(images: list[Image.Image], pos: list[int]) -> Image.Image:
        captured["images"] = list(images)
        merged = real_merge(images, pos)
        captured["merged"] = merged
        # single-item merge returns the sole input image; count closes to prove no double-close
        target = merged
        original_close = target.close

        def _counting_close() -> None:
            close_counts[id(target)] = close_counts.get(id(target), 0) + 1
            original_close()

        target.close = _counting_close  # type: ignore[method-assign]
        return merged

    fake_gc = MagicMock()
    bytesio_instances: list[BytesIO] = []
    fake_frame = _FakeSkyvernFrame()

    result = await _invoke_take_scrolling_screenshot(
        screenshots=screenshots,
        positions=positions,
        scrolling_number=1,
        merge_impl=_spy_merge,
        fake_gc=fake_gc,
        bytesio_instances=bytesio_instances,
        fake_frame=fake_frame,
    )

    assert result == expected

    merged_img = captured["merged"]
    decoded = captured["images"]
    assert isinstance(decoded, list) and len(decoded) == 1
    assert isinstance(merged_img, Image.Image)
    assert merged_img is decoded[0], "single-item merge returns the sole input image"
    assert _is_closed(merged_img), "the sole image must be closed"
    assert close_counts.get(id(merged_img)) == 1, "aliased image must be closed exactly once (no double-close)"

    fake_gc.collect.assert_not_called()
    assert fake_frame.scroll_restore_calls == [(11, 22)]


@pytest.mark.asyncio
async def test_merge_exception_still_closes_images_and_falls_back() -> None:
    screenshots = [
        _png_bytes(120, 100, (10, 20, 30)),
        _png_bytes(120, 100, (40, 50, 60)),
    ]
    positions = [0, 80]

    captured: dict[str, object] = {}

    def _boom_merge(images: list[Image.Image], pos: list[int]) -> Image.Image:
        captured["images"] = list(images)
        raise RuntimeError("merge boom")

    fake_gc = MagicMock()
    bytesio_instances: list[BytesIO] = []
    fake_frame = _FakeSkyvernFrame()

    result = await _invoke_take_scrolling_screenshot(
        screenshots=screenshots,
        positions=positions,
        scrolling_number=2,
        merge_impl=_boom_merge,
        fake_gc=fake_gc,
        bytesio_instances=bytesio_instances,
        fake_frame=fake_frame,
        fallback_bytes=b"FELLBACK",
    )

    assert result == b"FELLBACK", "merge failure must fall back to the full-page screenshot"

    decoded = captured["images"]
    assert isinstance(decoded, list) and len(decoded) == 2
    assert all(_is_closed(img) for img in decoded), "decoded images must be closed even on merge failure"

    fake_gc.collect.assert_called_once()
    assert fake_frame.scroll_restore_calls == [], "fallback path must not restore scroll (x/y reset to None)"


@pytest.mark.asyncio
async def test_save_exception_closes_images_merged_and_buffer() -> None:
    screenshots = [
        _png_bytes(120, 100, (10, 20, 30)),
        _png_bytes(120, 100, (40, 50, 60)),
    ]
    positions = [0, 80]

    captured: dict[str, object] = {}
    real_merge = page_module._merge_images_by_position

    def _merge_then_break_save(images: list[Image.Image], pos: list[int]) -> Image.Image:
        captured["images"] = list(images)
        merged = real_merge(images, pos)
        captured["merged"] = merged

        def _raise_save(*args: object, **kwargs: object) -> None:
            raise RuntimeError("save boom")

        merged.save = _raise_save  # type: ignore[method-assign]
        return merged

    fake_gc = MagicMock()
    bytesio_instances: list[BytesIO] = []
    fake_frame = _FakeSkyvernFrame()

    result = await _invoke_take_scrolling_screenshot(
        screenshots=screenshots,
        positions=positions,
        scrolling_number=2,
        merge_impl=_merge_then_break_save,
        fake_gc=fake_gc,
        bytesio_instances=bytesio_instances,
        fake_frame=fake_frame,
    )

    assert result == b"FALLBACK"

    decoded = captured["images"]
    merged_img = captured["merged"]
    assert isinstance(decoded, list) and len(decoded) == 2
    assert all(_is_closed(img) for img in decoded), "decoded images must be closed on save failure"
    assert isinstance(merged_img, Image.Image)
    assert _is_closed(merged_img), "stitched image must be closed on save failure"

    output_buffers = [b for b in bytesio_instances if getattr(b, "initial_len", None) == 0]
    assert len(output_buffers) == 1
    assert output_buffers[0].close_count >= 1 and output_buffers[0].closed, "PNG buffer must be closed on save failure"

    fake_gc.collect.assert_called_once()


def test_merge_images_closes_crops_but_preserves_inputs_and_output() -> None:
    images = [
        Image.open(BytesIO(_png_bytes(120, 100, (10, 20, 30)))),
        Image.open(BytesIO(_png_bytes(120, 100, (40, 50, 60)))),
        Image.open(BytesIO(_png_bytes(120, 100, (70, 80, 90)))),
    ]
    for img in images:
        img.load()
    positions = [0, 80, 160]  # every step overlaps => a crop is created for i=1 and i=2

    created_crops: list[Image.Image] = []
    real_crop = Image.Image.crop

    def _spy_crop(self: Image.Image, *args: object, **kwargs: object) -> Image.Image:
        cropped = real_crop(self, *args, **kwargs)
        created_crops.append(cropped)
        return cropped

    with patch.object(page_module.Image.Image, "crop", _spy_crop):
        merged = page_module._merge_images_by_position(images, positions)

    assert len(created_crops) == 2, "each overlapping viewport past the first is cropped"
    assert all(_is_closed(crop) for crop in created_crops), "temporary cropped images must be closed"

    assert not _is_closed(merged), "merged output must remain usable"
    assert merged.size == (120, 260)
    assert all(not _is_closed(img) for img in images), "input images must not be closed by the merge"

    merged.close()
    for img in images:
        img.close()


def test_merge_images_closes_canvas_and_crop_when_paste_fails_after_allocation() -> None:
    """A raise after the canvas/crop is allocated must not leak them: the caller never receives
    ``merged_img`` and so cannot close it, so the merge itself must release both before propagating."""
    images = [
        Image.open(BytesIO(_png_bytes(120, 100, (10, 20, 30)))),
        Image.open(BytesIO(_png_bytes(120, 100, (40, 50, 60)))),
        Image.open(BytesIO(_png_bytes(120, 100, (70, 80, 90)))),
    ]
    for img in images:
        img.load()
    positions = [0, 80, 160]  # overlaps => a crop temporary is allocated before the failing paste

    created_canvas: list[Image.Image] = []
    created_crops: list[Image.Image] = []
    real_new = page_module.Image.new
    real_crop = Image.Image.crop

    def _spy_crop(self: Image.Image, *args: object, **kwargs: object) -> Image.Image:
        cropped = real_crop(self, *args, **kwargs)
        created_crops.append(cropped)
        return cropped

    def _spy_new(*args: object, **kwargs: object) -> Image.Image:
        canvas = real_new(*args, **kwargs)
        created_canvas.append(canvas)
        real_paste = canvas.paste
        paste_calls = {"n": 0}

        def _paste_then_fail(*paste_args: object, **paste_kwargs: object) -> None:
            paste_calls["n"] += 1
            # let the images[0] paste succeed so the canvas and the first crop are allocated,
            # then fail the next paste to exercise the post-allocation raise
            if paste_calls["n"] >= 2:
                raise RuntimeError("paste boom after allocation")
            return real_paste(*paste_args, **paste_kwargs)

        canvas.paste = _paste_then_fail  # type: ignore[method-assign]
        return canvas

    with (
        patch.object(page_module.Image, "new", _spy_new),
        patch.object(page_module.Image.Image, "crop", _spy_crop),
    ):
        with pytest.raises(RuntimeError, match="paste boom after allocation"):
            page_module._merge_images_by_position(images, positions)

    assert len(created_canvas) == 1, "the stitched canvas must have been allocated before the failure"
    assert _is_closed(created_canvas[0]), "the stitched canvas must be closed when merge raises after allocation"
    assert len(created_crops) >= 1, "a crop temporary must have been allocated before the failure"
    assert all(_is_closed(crop) for crop in created_crops), "allocated crop temporaries must be closed on failure"
    assert all(not _is_closed(img) for img in images), "input images must never be closed by the merge"

    for img in images:
        img.close()


async def _hang(*args: object, **kwargs: object) -> None:
    await asyncio.sleep(30)


def _skycdp_selection() -> BrowserEngineSelection:
    return BrowserEngineSelection(
        name=SKYCDP_ENGINE_NAME,
        start_driver=cast("BrowserDriverStarter", lambda: None),
        error_type=Exception,
        timeout_error_type=TimeoutError,
        metadata=BrowserEngineMetadata(name=SKYCDP_ENGINE_NAME),
        selection_reason="test-skycdp",
    )


def _cdp_page(
    *,
    send_result: dict[str, str] | None = None,
    send_exc: Exception | None = None,
    send_hang: bool = False,
    detach_exc: Exception | None = None,
    detach_hang: bool = False,
    open_hang: bool = False,
) -> tuple[MagicMock, AsyncMock]:
    session = AsyncMock(name="cdp_session")
    if send_hang:
        session.send = AsyncMock(side_effect=_hang)
    else:
        session.send = AsyncMock(side_effect=send_exc) if send_exc else AsyncMock(return_value=send_result or {})
    if detach_hang:
        session.detach = AsyncMock(side_effect=_hang)
    else:
        session.detach = AsyncMock(side_effect=detach_exc) if detach_exc else AsyncMock(return_value=None)
    page = MagicMock(name="page")
    page.context.new_cdp_session = AsyncMock(side_effect=_hang) if open_hang else AsyncMock(return_value=session)
    return page, session


async def _invoke_rescue_case(
    *,
    page: MagicMock,
    fake_frame: _FakeSkyvernFrame,
    scroll_helper_exc: Exception,
    engine_selection: BrowserEngineSelection | None = None,
    record_recovery: MagicMock | None = None,
    file_path: str | None = None,
) -> bytes:
    return await _invoke_take_scrolling_screenshot(
        screenshots=[],
        positions=[],
        scrolling_number=2,
        merge_impl=MagicMock(),
        fake_gc=MagicMock(),
        bytesio_instances=[],
        fake_frame=fake_frame,
        page=page,
        engine_selection=engine_selection,
        scroll_helper_exc=scroll_helper_exc,
        record_recovery=record_recovery,
        file_path=file_path,
    )


@pytest.mark.asyncio
async def test_cdp_rescue_returns_png_writes_file_and_records_success(tmp_path: Path) -> None:
    expected_png = _png_bytes(800, 600, (43, 108, 176))
    page, session = _cdp_page(send_result={"data": base64.b64encode(expected_png).decode()})
    fake_frame = _FakeSkyvernFrame()
    record_recovery = MagicMock()
    out_path = tmp_path / "not" / "yet" / "created" / "rescued.png"

    result = await _invoke_rescue_case(
        page=page,
        fake_frame=fake_frame,
        scroll_helper_exc=FailedToTakeScreenshot(error_message="capture stalled"),
        record_recovery=record_recovery,
        file_path=str(out_path),
    )

    assert result == expected_png
    assert out_path.read_bytes() == expected_png, (
        "the rescue must create missing parent directories like page.screenshot"
    )
    record_recovery.assert_called_once_with(BrowserOperation.SCREENSHOT)
    session.send.assert_awaited_once_with("Page.captureScreenshot", {"format": "png", "captureBeyondViewport": False})
    session.detach.assert_awaited_once()
    assert fake_frame.scroll_restore_calls == [], "rescue must not scroll the page that just failed to capture"


@pytest.mark.asyncio
@pytest.mark.parametrize("detach_exc,detach_hang", [(RuntimeError("detach boom"), False), (None, True)])
async def test_cdp_rescue_keeps_bytes_when_detach_fails_or_hangs(
    detach_exc: Exception | None, detach_hang: bool
) -> None:
    expected_png = _png_bytes(120, 100, (10, 20, 30))
    page, session = _cdp_page(
        send_result={"data": base64.b64encode(expected_png).decode()},
        detach_exc=detach_exc,
        detach_hang=detach_hang,
    )

    result = await _invoke_rescue_case(
        page=page,
        fake_frame=_FakeSkyvernFrame(),
        scroll_helper_exc=FailedToTakeScreenshot(error_message="capture stalled"),
    )

    assert result == expected_png
    session.detach.assert_awaited_once()


@pytest.mark.asyncio
@pytest.mark.parametrize("send_exc,send_hang", [(RuntimeError("cdp send boom"), False), (None, True)])
async def test_cdp_rescue_send_failure_or_hang_falls_back_and_detaches(
    send_exc: Exception | None, send_hang: bool, tmp_path: Path
) -> None:
    page, session = _cdp_page(send_exc=send_exc, send_hang=send_hang)
    record_recovery = MagicMock()
    out_path = tmp_path / "rescued.png"

    result = await _invoke_rescue_case(
        page=page,
        fake_frame=_FakeSkyvernFrame(),
        scroll_helper_exc=FailedToTakeScreenshot(error_message="capture stalled"),
        record_recovery=record_recovery,
        file_path=str(out_path),
    )

    assert result == b"FALLBACK"
    assert not out_path.exists()
    record_recovery.assert_not_called()
    session.detach.assert_awaited_once()


@pytest.mark.asyncio
@pytest.mark.parametrize("send_result", [{}, {"data": ""}, {"data": "abcde"}, {"data": "!!!!YQ=="}])
async def test_cdp_rescue_rejects_non_png_payloads(send_result: dict[str, str], tmp_path: Path) -> None:
    page, _ = _cdp_page(send_result=send_result)
    record_recovery = MagicMock()
    out_path = tmp_path / "rescued.png"

    result = await _invoke_rescue_case(
        page=page,
        fake_frame=_FakeSkyvernFrame(),
        scroll_helper_exc=FailedToTakeScreenshot(error_message="capture stalled"),
        record_recovery=record_recovery,
        file_path=str(out_path),
    )

    assert result == b"FALLBACK"
    assert not out_path.exists(), "a non-PNG payload must never be written to the artifact path"
    record_recovery.assert_not_called()


@pytest.mark.asyncio
async def test_cdp_rescue_session_open_timeout_falls_back() -> None:
    page, _ = _cdp_page(open_hang=True)

    result = await _invoke_rescue_case(
        page=page,
        fake_frame=_FakeSkyvernFrame(),
        scroll_helper_exc=FailedToTakeScreenshot(error_message="capture stalled"),
    )

    assert result == b"FALLBACK"


@pytest.mark.asyncio
async def test_cdp_rescue_skipped_on_skycdp_engine() -> None:
    page, _ = _cdp_page(send_result={"data": base64.b64encode(_png_bytes(120, 100, (1, 2, 3))).decode()})

    result = await _invoke_rescue_case(
        page=page,
        fake_frame=_FakeSkyvernFrame(),
        scroll_helper_exc=FailedToTakeScreenshot(error_message="capture stalled"),
        engine_selection=_skycdp_selection(),
    )

    assert result == b"FALLBACK"
    page.context.new_cdp_session.assert_not_called()


@pytest.mark.asyncio
async def test_cdp_rescue_skipped_for_non_screenshot_exception() -> None:
    page, _ = _cdp_page(send_result={"data": base64.b64encode(_png_bytes(120, 100, (1, 2, 3))).decode()})

    result = await _invoke_rescue_case(
        page=page,
        fake_frame=_FakeSkyvernFrame(),
        scroll_helper_exc=RuntimeError("stitch boom"),
    )

    assert result == b"FALLBACK"
    page.context.new_cdp_session.assert_not_called()


def _fake_timeout_page(message: str) -> MagicMock:
    page = MagicMock(name="page")
    page.is_closed.return_value = False
    page.url = "https://example.test"
    page.viewport_size = {"width": 800, "height": 600}
    page.screenshot = AsyncMock(side_effect=PlaywrightTimeoutError(message))
    return page


async def _screenshot_timeout_warning_kwargs(message: str) -> dict[str, object]:
    page = _fake_timeout_page(message)
    captured: list[dict[str, object]] = []

    def _capture(event: str, **kwargs: object) -> None:
        if event == "Screenshot timeout":
            captured.append(kwargs)

    with (
        patch.object(page_module.LOG, "warning", side_effect=_capture),
        pytest.raises(page_module.FailedToTakeScreenshot),
    ):
        await page_module.SkyvernFrame.take_scrolling_screenshot(
            page=page,
            scrolling_number=0,
            mode=ScreenshotMode.LITE,
            engine_selection=None,
        )

    assert len(captured) == 1, "exactly one Screenshot timeout warning expected"
    return captured[0]


@pytest.mark.asyncio
async def test_screenshot_timeout_warning_names_the_last_call_log_stage() -> None:
    kwargs = await _screenshot_timeout_warning_kwargs(
        "Page.screenshot: Timeout 20000ms exceeded.\n"
        "Call log:\n"
        "  - taking page screenshot\n"
        "  - waiting for fonts to load...\n"
    )
    assert kwargs["screenshot_stage"] == "waiting for fonts to load..."


@pytest.mark.asyncio
async def test_screenshot_timeout_warning_reads_an_undashed_single_entry_call_log() -> None:
    kwargs = await _screenshot_timeout_warning_kwargs(
        "Page.screenshot: Timeout 20000ms exceeded.\nCall log:\ntaking page screenshot\n"
    )
    assert kwargs["screenshot_stage"] == "taking page screenshot"


@pytest.mark.asyncio
async def test_screenshot_timeout_warning_stage_is_unknown_without_a_call_log() -> None:
    kwargs = await _screenshot_timeout_warning_kwargs("Page.screenshot: Timeout 20000ms exceeded.")
    assert kwargs["screenshot_stage"] == "unknown"
