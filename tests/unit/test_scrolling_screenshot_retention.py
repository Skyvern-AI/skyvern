"""The multi-viewport LITE stitch must release its decoded images eagerly.

``SkyvernFrame.take_scrolling_screenshot`` decodes one ``PIL.Image`` per viewport,
stitches them, and PNG-encodes the result. These tests pin explicit close of the decoded
images, the stitched image, and the PNG buffer, followed by a rate-limited full ``gc.collect()``
after a multi-viewport stitch, without changing the returned bytes or scroll restoration.

Viewport CDP recovery contracts live in ``webeye/test_screenshot_cdp_fallback.py``.
"""

from __future__ import annotations

import asyncio
import concurrent.futures
import contextvars
import gc
import hashlib
import threading
import time
import weakref
from collections.abc import Iterator
from contextlib import asynccontextmanager, contextmanager
from io import BytesIO
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from PIL import Image
from playwright.async_api import TimeoutError as PlaywrightTimeoutError

import skyvern.webeye.utils.page as page_module
from skyvern.webeye.browser_engine import BrowserEngineSelection
from skyvern.webeye.utils.page import ScreenshotMode
from tests.unit.conftest import settle_or_fail, stalling_async_mock
from tests.unit.scoped_asyncio import ScopedAsyncio


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


@contextmanager
def _stitch_path_patches(
    *,
    create_instance: AsyncMock,
    scroll_helper: AsyncMock,
    merge_impl: object,
    fallback: AsyncMock,
    fake_gc: MagicMock,
    bytesio: type[BytesIO] = BytesIO,
) -> Iterator[None]:
    with (
        patch.object(page_module.SkyvernFrame, "create_instance", create_instance),
        patch.object(page_module, "_scrolling_screenshots_helper", scroll_helper),
        patch.object(page_module, "_merge_images_by_position", MagicMock(side_effect=merge_impl)),
        patch.object(page_module, "_current_viewpoint_screenshot_helper", fallback),
        patch.object(page_module, "BytesIO", bytesio),
        patch.object(page_module, "gc", fake_gc, create=True),
        patch.object(page_module.skyvern_context, "record_browser_success", MagicMock()),
        patch.object(page_module, "CDP_RESCUE_SESSION_TIMEOUT_SECONDS", 0.05),
        patch.object(page_module, "CDP_RESCUE_CAPTURE_TIMEOUT_SECONDS", 0.05),
        patch.object(page_module, "CDP_RESCUE_DETACH_TIMEOUT_SECONDS", 0.05),
    ):
        yield


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
    file_path: str | None = None,
    timeout_ms: float | None = None,
    scroll_helper: AsyncMock | None = None,
    fallback: AsyncMock | None = None,
    create_instance: AsyncMock | None = None,
) -> bytes:
    tracking_bytesio = _tracking_bytesio_factory(bytesio_instances)
    if scroll_helper is None:
        scroll_helper = (
            AsyncMock(side_effect=scroll_helper_exc)
            if scroll_helper_exc is not None
            else AsyncMock(return_value=(list(screenshots), list(positions)))
        )
    timeout_kwargs = {} if timeout_ms is None else {"timeout": timeout_ms}
    with _stitch_path_patches(
        create_instance=create_instance if create_instance is not None else AsyncMock(return_value=fake_frame),
        scroll_helper=scroll_helper,
        merge_impl=merge_impl,
        fallback=fallback if fallback is not None else AsyncMock(return_value=fallback_bytes),
        fake_gc=fake_gc,
        bytesio=tracking_bytesio,
    ):
        return await page_module.SkyvernFrame.take_scrolling_screenshot(
            page=page if page is not None else MagicMock(name="page"),
            file_path=file_path,
            mode=ScreenshotMode.LITE,
            scrolling_number=scrolling_number,
            engine_selection=engine_selection,
            **timeout_kwargs,
        )


@pytest.fixture(autouse=True)
def _fresh_stitch_gc_interval(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(page_module, "_last_stitch_gc_at", float("-inf"))


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
async def test_multi_viewport_stitch_collects_at_most_once_per_interval() -> None:
    screenshots = [_png_bytes(120, 100, (10, 20, 30)), _png_bytes(120, 100, (40, 50, 60))]
    fake_gc = MagicMock()

    async def _stitch_once() -> None:
        await _invoke_take_scrolling_screenshot(
            screenshots=screenshots,
            positions=[0, 80],
            scrolling_number=2,
            merge_impl=page_module._merge_images_by_position,
            fake_gc=fake_gc,
            bytesio_instances=[],
            fake_frame=_FakeSkyvernFrame(),
        )

    await _stitch_once()
    await _stitch_once()
    assert fake_gc.collect.call_count == 1, "a second stitch inside the interval must not pause the loop again"

    page_module._last_stitch_gc_at -= (
        page_module.SettingsManager.get_settings().SCREENSHOT_STITCH_GC_MIN_INTERVAL_SECONDS
    )
    await _stitch_once()
    assert fake_gc.collect.call_count == 2, "the first stitch after the interval must collect"


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


@pytest.mark.asyncio
async def test_hung_scroll_restoration_cannot_extend_the_screenshot_budget(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The outer finally restores the pre-screenshot scroll position as courtesy cleanup, and it
    runs while the caller's screenshot budget is already unwinding. An unresponsive page there must
    not extend that budget: a caller unwinding this call to publish an established failure would
    otherwise wait for the hang."""

    class _HangingFrame(_FakeSkyvernFrame):
        async def safe_scroll_to_x_y(self, x: int, y: int) -> None:
            self.scroll_restore_calls.append((x, y))
            await asyncio.Event().wait()

    monkeypatch.setattr(page_module.SettingsManager.get_settings(), "BROWSER_ACTION_TIMEOUT_MS", 50)
    screenshots = [_png_bytes(120, 100, (10, 20, 30))]
    positions = [0]
    expected = _expected_merged_png(screenshots, positions)
    fake_frame = _HangingFrame()

    result = await asyncio.wait_for(
        _invoke_take_scrolling_screenshot(
            screenshots=screenshots,
            positions=positions,
            scrolling_number=1,
            merge_impl=page_module._merge_images_by_position,
            fake_gc=MagicMock(),
            bytesio_instances=[],
            fake_frame=fake_frame,
        ),
        timeout=5,
    )

    # The screenshot still comes back, and the restoration was attempted and then abandoned.
    assert result == expected
    assert fake_frame.scroll_restore_calls == [(11, 22)]


class _HangingRestoreFrame(_FakeSkyvernFrame):
    async def safe_scroll_to_x_y(self, x: int, y: int) -> None:
        self.scroll_restore_calls.append((x, y))
        await asyncio.Event().wait()


@pytest.mark.asyncio
async def test_stalled_capture_settles_within_the_millisecond_budget() -> None:
    entered = asyncio.Event()
    fallback = AsyncMock(return_value=b"FALLBACK")
    fake_frame = _HangingRestoreFrame()

    task, elapsed = await settle_or_fail(
        _invoke_take_scrolling_screenshot(
            screenshots=[],
            positions=[],
            scrolling_number=1,
            merge_impl=page_module._merge_images_by_position,
            fake_gc=MagicMock(),
            bytesio_instances=[],
            fake_frame=fake_frame,
            timeout_ms=200,
            scroll_helper=stalling_async_mock(entered),
            fallback=fallback,
        )
    )

    assert entered.is_set()
    assert isinstance(task.exception(), TimeoutError)
    assert 0.15 <= elapsed < 1.0, elapsed
    fallback.assert_not_awaited()
    assert fake_frame.scroll_restore_calls == [(11, 22)]


@pytest.mark.asyncio
async def test_failed_capture_and_stalled_fallback_share_one_budget(monkeypatch: pytest.MonkeyPatch) -> None:
    class _Clock:
        def __init__(self) -> None:
            self.now = 0.0
            self.active_deadlines: list[float] = []

        def time(self) -> float:
            return self.now

        async def sleep(self, seconds: float) -> None:
            self.now += seconds
            if self.active_deadlines:
                deadline = min(self.active_deadlines)
                if self.now >= deadline:
                    self.now = deadline
                    raise TimeoutError

    clock = _Clock()
    armed_deadlines: list[float] = []
    fallback_deadlines: list[float] = []
    capture_failed_at: list[float] = []
    timeout_ms = 300
    budget_started_at = clock.now

    def _timeout_at(deadline: float):
        armed_deadlines.append(deadline)

        @asynccontextmanager
        async def _scope():
            clock.active_deadlines.append(deadline)
            try:
                yield
            finally:
                clock.active_deadlines.remove(deadline)

        return _scope()

    monkeypatch.setattr(
        page_module,
        "asyncio",
        ScopedAsyncio(
            get_running_loop=lambda: clock,
            sleep=clock.sleep,
            timeout_at=_timeout_at,
        ),
    )

    async def _fail_late(*args: object, **kwargs: object) -> None:
        await page_module.asyncio.sleep(0.25)
        capture_failed_at.append(clock.now)
        raise RuntimeError("capture boom")

    async def _stall_fallback(*args: object, **kwargs: object) -> None:
        fallback_deadlines.extend(clock.active_deadlines)
        await page_module.asyncio.sleep(1.0)

    with patch.object(page_module.LOG, "warning", MagicMock()):
        task, _ = await settle_or_fail(
            _invoke_take_scrolling_screenshot(
                screenshots=[],
                positions=[],
                scrolling_number=1,
                merge_impl=page_module._merge_images_by_position,
                fake_gc=MagicMock(),
                bytesio_instances=[],
                fake_frame=_HangingRestoreFrame(),
                timeout_ms=timeout_ms,
                scroll_helper=AsyncMock(side_effect=_fail_late),
                fallback=AsyncMock(side_effect=_stall_fallback),
            )
        )

    assert isinstance(task.exception(), TimeoutError)
    assert capture_failed_at == [pytest.approx(0.25)]
    assert armed_deadlines == [pytest.approx(budget_started_at + timeout_ms / 1000)]
    assert fallback_deadlines == armed_deadlines
    assert clock.now == pytest.approx(armed_deadlines[0])


@pytest.mark.asyncio
async def test_healthy_capture_returns_and_writes_bytes_under_an_explicit_budget(tmp_path: Path) -> None:
    screenshots = [_png_bytes(120, 100, (10, 20, 30)), _png_bytes(120, 100, (40, 50, 60))]
    positions = [0, 80]
    expected = _expected_merged_png(screenshots, positions)
    fake_frame = _FakeSkyvernFrame()
    out = tmp_path / "full.png"

    task, _ = await settle_or_fail(
        _invoke_take_scrolling_screenshot(
            screenshots=screenshots,
            positions=positions,
            scrolling_number=2,
            merge_impl=page_module._merge_images_by_position,
            fake_gc=MagicMock(),
            bytesio_instances=[],
            fake_frame=fake_frame,
            timeout_ms=2000,
            file_path=str(out),
        )
    )

    assert task.result() == expected
    assert out.read_bytes() == expected
    assert fake_frame.scroll_restore_calls == [(11, 22)]


@pytest.mark.asyncio
async def test_stalled_create_instance_is_inside_the_budget() -> None:
    entered = asyncio.Event()
    fake_frame = _FakeSkyvernFrame()

    task, elapsed = await settle_or_fail(
        _invoke_take_scrolling_screenshot(
            screenshots=[],
            positions=[],
            scrolling_number=1,
            merge_impl=page_module._merge_images_by_position,
            fake_gc=MagicMock(),
            bytesio_instances=[],
            fake_frame=fake_frame,
            timeout_ms=200,
            create_instance=stalling_async_mock(entered),
        )
    )

    assert entered.is_set()
    assert isinstance(task.exception(), TimeoutError)
    assert 0.15 <= elapsed < 1.0, elapsed
    assert fake_frame.scroll_restore_calls == []


_REQUEST_CONTEXT: contextvars.ContextVar[str] = contextvars.ContextVar("stitch_request_context", default="unset")


def _three_viewports() -> tuple[list[bytes], list[int]]:
    return [_png_bytes(240, 200, (30 * i, 20, 90)) for i in range(3)], [0, 160, 320]


def _loop_thread_collects(collect_threads: list[int]) -> MagicMock:
    fake_gc = MagicMock()
    fake_gc.collect.side_effect = lambda: collect_threads.append(threading.get_ident())
    return fake_gc


def _blocking_merge(entered: threading.Event, release: threading.Event, fail: bool = False) -> object:
    real_merge = page_module._merge_images_by_position

    def _merge(images: list[Image.Image], pos: list[int]) -> Image.Image:
        entered.set()
        assert release.wait(timeout=5), "test never released the stitch"
        if fail:
            raise RuntimeError("merge boom")
        return real_merge(images, pos)

    return _merge


@pytest.mark.asyncio
async def test_stitch_keeps_the_event_loop_running_with_identical_bytes(tmp_path: Path) -> None:
    screenshots, positions = _three_viewports()
    expected = _expected_merged_png(screenshots, positions)
    in_pillow = threading.Event()
    loop_ran_during_pillow = threading.Event()
    seen: dict[str, object] = {}
    real_merge = page_module._merge_images_by_position

    def _merge_while_watching_the_loop(images: list[Image.Image], pos: list[int]) -> Image.Image:
        seen["context"] = _REQUEST_CONTEXT.get()
        in_pillow.set()
        seen["loop_ran"] = loop_ran_during_pillow.wait(timeout=2)
        return real_merge(images, pos)

    async def _tick_while_pillow_runs() -> None:
        while not in_pillow.is_set():
            await asyncio.sleep(0.001)
        for _ in range(3):
            await asyncio.sleep(0)
        loop_ran_during_pillow.set()

    collect_threads: list[int] = []
    out = tmp_path / "full.png"
    ticker = asyncio.ensure_future(_tick_while_pillow_runs())
    token = _REQUEST_CONTEXT.set("request")
    try:
        result = await _invoke_take_scrolling_screenshot(
            screenshots=screenshots,
            positions=positions,
            scrolling_number=3,
            merge_impl=_merge_while_watching_the_loop,
            fake_gc=_loop_thread_collects(collect_threads),
            bytesio_instances=[],
            fake_frame=_FakeSkyvernFrame(),
            file_path=str(out),
        )
    finally:
        _REQUEST_CONTEXT.reset(token)
        await ticker

    assert seen["loop_ran"] is True, "the event loop must keep running while Pillow stitches"
    assert result == expected
    assert hashlib.sha256(result).hexdigest() == hashlib.sha256(expected).hexdigest()
    assert out.read_bytes() == expected
    assert seen["context"] == "request", "the stitch must see the caller's context"
    assert collect_threads == [threading.get_ident()], "the explicit full collection stays on the loop thread"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("failure", "collects"),
    [("second_viewport_unreadable", 0), ("third_viewport_unreadable", 1), ("cleanup_raises", 0), (None, 1)],
)
async def test_explicit_collect_follows_the_viewports_that_loaded(failure: str | None, collects: int) -> None:
    screenshots, positions = _three_viewports()
    expected = _expected_merged_png(screenshots, positions)
    if failure == "second_viewport_unreadable":
        screenshots[1] = b"not a png"
    elif failure == "third_viewport_unreadable":
        screenshots[2] = b"not a png"
    real_close = page_module._close_screenshot_stitch_resources

    def _close_then_raise(*args: object) -> None:
        real_close(*args)  # type: ignore[arg-type]
        raise RuntimeError("cleanup boom")

    collect_threads: list[int] = []
    with patch.object(
        page_module,
        "_close_screenshot_stitch_resources",
        _close_then_raise if failure == "cleanup_raises" else real_close,
    ):
        result = await _invoke_take_scrolling_screenshot(
            screenshots=screenshots,
            positions=positions,
            scrolling_number=3,
            merge_impl=page_module._merge_images_by_position,
            fake_gc=_loop_thread_collects(collect_threads),
            bytesio_instances=[],
            fake_frame=_FakeSkyvernFrame(),
        )

    assert result == (expected if failure is None else b"FALLBACK")
    assert collect_threads == [threading.get_ident()] * collects


def _recording_timeout_at(timeouts: list[tuple[float, asyncio.Timeout]]) -> object:
    def _timeout_at(when: float) -> asyncio.Timeout:
        timeout = asyncio.timeout_at(when)
        timeouts.append((when, timeout))
        return timeout

    return _timeout_at


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "processing", ["succeeds", "fails_inline_fallback", "fails_raising_fallback", "fails_suspending_fallback"]
)
async def test_capture_deadline_passing_during_the_stitch(processing: str, tmp_path: Path) -> None:
    """A finished capture's stitch is not cut short by the deadline; a failed one falls back under it."""
    screenshots, positions = _three_viewports()
    expected = _expected_merged_png(screenshots, positions)
    real_merge = page_module._merge_images_by_position

    def _slow_merge(images: list[Image.Image], pos: list[int]) -> Image.Image:
        time.sleep(0.25)
        if processing != "succeeds":
            raise RuntimeError("merge boom")
        return real_merge(images, pos)

    entered = asyncio.Event()
    if processing == "fails_suspending_fallback":
        fallback = stalling_async_mock(entered)
    elif processing == "fails_raising_fallback":
        fallback = AsyncMock(side_effect=RuntimeError("fallback boom"))
    else:
        fallback = AsyncMock(return_value=b"FALLBACK")
    out = tmp_path / "full.png"
    timeouts: list[tuple[float, asyncio.Timeout]] = []

    with patch.object(page_module, "asyncio", ScopedAsyncio(timeout_at=_recording_timeout_at(timeouts))):
        task, elapsed = await settle_or_fail(
            _invoke_take_scrolling_screenshot(
                screenshots=screenshots,
                positions=positions,
                scrolling_number=3,
                merge_impl=_slow_merge,
                fake_gc=MagicMock(),
                bytesio_instances=[],
                fake_frame=_FakeSkyvernFrame(),
                timeout_ms=100,
                file_path=str(out),
                fallback=fallback,
            )
        )

    assert len(timeouts) == 1, "one capture deadline object governs capture, stitch and fallback"
    original_deadline, capture_timeout = timeouts[0]
    if processing == "succeeds":
        assert task.result() == expected
        assert out.read_bytes() == expected
        assert capture_timeout.when() is None and not capture_timeout.expired(), "disarmed once capture returned"
    else:
        assert capture_timeout.when() == original_deadline, "the fallback runs under the original deadline"
        fallback.assert_awaited_once()
        assert fallback.await_args.kwargs["timeout"] == 100
        if processing == "fails_inline_fallback":
            assert task.result() == b"FALLBACK"
            assert not capture_timeout.expired()
        elif processing == "fails_raising_fallback":
            assert isinstance(task.exception(), RuntimeError)
        else:
            assert isinstance(task.exception(), TimeoutError)
            assert capture_timeout.expired(), "this capture's own deadline ended the fallback"
            assert entered.is_set()
            assert elapsed < 1.0, elapsed


@pytest.mark.asyncio
async def test_capture_that_swallowed_its_deadline_still_stitches_and_writes(tmp_path: Path) -> None:
    screenshots, positions = _three_viewports()
    expected = _expected_merged_png(screenshots, positions)
    timeouts: list[tuple[float, asyncio.Timeout]] = []

    async def _capture_swallowing_the_deadline(**kwargs: object) -> tuple[list[bytes], list[int]]:
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            pass
        assert timeouts[0][1].expired()
        return list(screenshots), list(positions)

    collect_threads: list[int] = []
    out = tmp_path / "full.png"
    fallback = AsyncMock(return_value=b"FALLBACK")
    with patch.object(page_module, "asyncio", ScopedAsyncio(timeout_at=_recording_timeout_at(timeouts))):
        task, _ = await settle_or_fail(
            _invoke_take_scrolling_screenshot(
                screenshots=screenshots,
                positions=positions,
                scrolling_number=3,
                merge_impl=page_module._merge_images_by_position,
                fake_gc=_loop_thread_collects(collect_threads),
                bytesio_instances=[],
                fake_frame=_FakeSkyvernFrame(),
                timeout_ms=50,
                file_path=str(out),
                scroll_helper=AsyncMock(side_effect=_capture_swallowing_the_deadline),
                fallback=fallback,
            )
        )

    assert task.result() == expected
    assert out.read_bytes() == expected
    assert collect_threads == [threading.get_ident()]
    fallback.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("outcome", ["success", "stitch_error", "write_error", "fallback_raises"])
@pytest.mark.parametrize("cancels", ["single", "repeated", "coalesced"])
@pytest.mark.parametrize("deadline_passed", [False, True])
async def test_caller_cancel_during_the_stitch_is_delivered_after_it_finishes(
    outcome: str, cancels: str, deadline_passed: bool, tmp_path: Path
) -> None:
    screenshots, positions = _three_viewports()
    expected = _expected_merged_png(screenshots, positions)
    entered, release = threading.Event(), threading.Event()
    out = tmp_path if outcome == "write_error" else tmp_path / "full.png"
    fallback = (
        AsyncMock(side_effect=RuntimeError("fallback boom"))
        if outcome == "fallback_raises"
        else AsyncMock(return_value=b"FALLBACK")
    )
    failure_telemetry = MagicMock()

    with patch.object(page_module, "log_screenshot_failure", failure_telemetry):
        task = asyncio.ensure_future(
            _invoke_take_scrolling_screenshot(
                screenshots=screenshots,
                positions=positions,
                scrolling_number=3,
                merge_impl=_blocking_merge(entered, release, fail=outcome in ("stitch_error", "fallback_raises")),
                fake_gc=MagicMock(),
                bytesio_instances=[],
                fake_frame=_FakeSkyvernFrame(),
                file_path=str(out),
                fallback=fallback,
                timeout_ms=150 if deadline_passed else None,
            )
        )
        assert await asyncio.to_thread(entered.wait, 5)
        if deadline_passed:
            await asyncio.sleep(0.2)
        task.cancel()
        if cancels == "repeated":
            await asyncio.sleep(0)
        if cancels != "single":
            task.cancel()
        await asyncio.sleep(0.05)
        assert not task.done(), "cancellation must wait for the submitted stitch"
        release.set()
        await asyncio.wait({task}, timeout=5)

    assert task.cancelled(), "the accepted cancellation must reach the caller before the call returns"
    assert task.cancelling() == (1 if cancels == "single" else 2)
    failure_telemetry.assert_not_called()
    if outcome == "success":
        assert out.read_bytes() == expected, "a finished stitch is written before the cancellation lands"
        fallback.assert_not_awaited()
    else:
        fallback.assert_awaited_once()


@pytest.mark.asyncio
@pytest.mark.parametrize("outer", ["wait_for", "timeout"])
async def test_outer_deadline_during_the_stitch_raises_after_the_write(outer: str, tmp_path: Path) -> None:
    screenshots, positions = _three_viewports()
    expected = _expected_merged_png(screenshots, positions)
    entered, release = threading.Event(), threading.Event()
    out = tmp_path / "full.png"
    threading.Timer(0.3, release.set).start()
    call = _invoke_take_scrolling_screenshot(
        screenshots=screenshots,
        positions=positions,
        scrolling_number=3,
        merge_impl=_blocking_merge(entered, release),
        fake_gc=MagicMock(),
        bytesio_instances=[],
        fake_frame=_FakeSkyvernFrame(),
        file_path=str(out),
    )

    with pytest.raises(TimeoutError):
        if outer == "wait_for":
            await asyncio.wait_for(call, 0.1)
        else:
            async with asyncio.timeout(0.1):
                await call

    assert out.read_bytes() == expected


@pytest.mark.asyncio
async def test_ordinary_and_salvage_stitches_share_one_admission(tmp_path: Path) -> None:
    screenshots, positions = _three_viewports()
    expected = _expected_merged_png(screenshots, positions)
    partial_frame = _png_bytes(240, 200, (1, 2, 3))
    entered, release = threading.Event(), threading.Event()
    active = {"now": 0, "max": 0, "calls": 0}
    lock = threading.Lock()
    real_merge = page_module._merge_images_by_position

    def _counting_merge(images: list[Image.Image], pos: list[int]) -> Image.Image:
        with lock:
            active["now"] += 1
            active["calls"] += 1
            active["max"] = max(active["max"], active["now"])
        try:
            if active["calls"] == 1:
                entered.set()
                assert release.wait(timeout=5)
            return real_merge(images, pos)
        finally:
            with lock:
                active["now"] -= 1

    pages = {name: MagicMock(name=name) for name in ("first", "waiter", "salvage", "salvage_waiter")}

    async def _capture(**kwargs: object) -> tuple[list[bytes], list[int]]:
        if kwargs["page"] in (pages["salvage"], pages["salvage_waiter"]):
            partial_frames = kwargs["partial_frames"]
            assert isinstance(partial_frames, list)
            partial_frames.append((partial_frame, 0))
            await asyncio.Event().wait()
        return list(screenshots), list(positions)

    def _take(name: str, **kwargs: object) -> asyncio.Future[bytes]:
        return asyncio.ensure_future(
            page_module.SkyvernFrame.take_scrolling_screenshot(
                page=pages[name],
                file_path=str(tmp_path / f"{name}.png"),
                mode=ScreenshotMode.LITE,
                scrolling_number=3,
                **kwargs,
            )
        )

    with (
        _stitch_path_patches(
            create_instance=AsyncMock(return_value=_FakeSkyvernFrame()),
            scroll_helper=AsyncMock(side_effect=_capture),
            merge_impl=_counting_merge,
            fallback=AsyncMock(return_value=b"FALLBACK"),
            fake_gc=MagicMock(),
        ),
        patch.object(page_module, "log_screenshot_failure", MagicMock()),
    ):
        first = _take("first")
        assert await asyncio.to_thread(entered.wait, 5)
        waiter = _take("waiter")
        salvage = _take("salvage", timeout=100, keep_partial_on_timeout=True)
        salvage_waiter = _take("salvage_waiter", timeout=100, keep_partial_on_timeout=True)
        await asyncio.sleep(0.3)
        waiter.cancel()
        salvage_waiter.cancel()
        await asyncio.wait({waiter, salvage_waiter}, timeout=1)
        assert waiter.cancelled(), "a caller waiting for admission is cancelled without waiting for the stitch"
        assert salvage_waiter.cancelled(), "a salvage waiting for admission is cancelled without waiting either"
        assert active["calls"] == 1, "neither waiter may stitch while the first one runs"
        release.set()
        assert await first == expected
        salvaged = await salvage

    assert active["max"] == 1, "ordinary and salvage stitches must never decode at the same time"
    assert active["calls"] == 2
    assert salvaged == _expected_merged_png([partial_frame], [0])
    assert not (tmp_path / "waiter.png").exists(), "a cancelled waiter never submits or writes"


def test_stitch_admission_does_not_keep_finished_loops_alive() -> None:
    async def _contend() -> weakref.ref[asyncio.AbstractEventLoop]:
        admission = page_module._stitch_admission()
        assert page_module._stitch_admission() is admission
        async with admission:
            waiter = asyncio.ensure_future(admission.acquire())
            await asyncio.sleep(0)
        await waiter
        admission.release()
        return weakref.ref(asyncio.get_running_loop())

    for _ in range(2):
        loop_ref = asyncio.run(_contend())
        gc.collect()
        assert loop_ref() is None
        assert len(page_module._stitch_admission_locks) == 0


def _thread_recording_merge(
    threads: list[threading.Thread], gate: threading.Event | None = None, gated: threading.Event | None = None
) -> object:
    real_merge = page_module._merge_images_by_position

    def _merge(images: list[Image.Image], pos: list[int]) -> Image.Image:
        threads.append(threading.current_thread())
        if gate is not None and len(threads) == 1:
            assert gated is not None
            gated.set()
            assert gate.wait(timeout=5), "test never released the first stitch"
        return real_merge(images, pos)

    return _merge


def _take_in_fresh_loop(screenshots: list[bytes], positions: list[int], out: Path) -> bytes:
    async def _take() -> bytes:
        return await page_module.SkyvernFrame.take_scrolling_screenshot(
            page=MagicMock(name="page"), file_path=str(out), mode=ScreenshotMode.LITE, scrolling_number=3
        )

    return asyncio.run(_take())


@pytest.mark.asyncio
async def test_stitch_completes_while_the_default_executor_is_occupied(tmp_path: Path) -> None:
    """Unrelated work filling the default executor must not hold up a screenshot stitch."""
    screenshots, positions = _three_viewports()
    expected = _expected_merged_png(screenshots, positions)
    occupied, release = threading.Event(), threading.Event()
    threads: list[threading.Thread] = []
    out = tmp_path / "full.png"

    async def _with_busy_default_executor() -> bytes:
        loop = asyncio.get_running_loop()
        pool = concurrent.futures.ThreadPoolExecutor(max_workers=1, thread_name_prefix="busy-default")
        loop.set_default_executor(pool)
        blocker = loop.run_in_executor(None, lambda: (occupied.set(), release.wait(10)))
        try:
            assert occupied.wait(5)  # to_thread would itself queue behind the blocker
            task = asyncio.ensure_future(
                page_module.SkyvernFrame.take_scrolling_screenshot(
                    page=MagicMock(name="page"), file_path=str(out), mode=ScreenshotMode.LITE, scrolling_number=3
                )
            )
            await asyncio.wait({task}, timeout=3)
            finished_while_occupied = task.done()
            release.set()
            result = await task
            assert finished_while_occupied, "the stitch waited for unrelated default-executor work"
            return result
        finally:
            release.set()
            await blocker

    with _stitch_path_patches(
        create_instance=AsyncMock(return_value=_FakeSkyvernFrame()),
        scroll_helper=AsyncMock(return_value=(list(screenshots), list(positions))),
        merge_impl=_thread_recording_merge(threads),
        fallback=AsyncMock(return_value=b"FALLBACK"),
        fake_gc=MagicMock(),
    ):
        result = await asyncio.to_thread(lambda: asyncio.run(_with_busy_default_executor()))

    assert result == expected
    assert out.read_bytes() == expected
    assert threads and not threads[0].name.startswith("busy-default")


@pytest.mark.asyncio
async def test_stitches_reuse_one_dedicated_worker_across_calls_and_loops(tmp_path: Path) -> None:
    screenshots, positions = _three_viewports()
    expected = _expected_merged_png(screenshots, positions)
    threads: list[threading.Thread] = []

    with _stitch_path_patches(
        create_instance=AsyncMock(return_value=_FakeSkyvernFrame()),
        scroll_helper=AsyncMock(return_value=(list(screenshots), list(positions))),
        merge_impl=_thread_recording_merge(threads),
        fallback=AsyncMock(return_value=b"FALLBACK"),
        fake_gc=MagicMock(),
    ):
        for index in range(2):
            out = tmp_path / ("loop-%d.png" % index)
            assert (
                await page_module.SkyvernFrame.take_scrolling_screenshot(
                    page=MagicMock(name="page"), file_path=str(out), mode=ScreenshotMode.LITE, scrolling_number=3
                )
                == expected
            )
        for index in range(2):
            out = tmp_path / ("fresh-%d.png" % index)
            assert await asyncio.to_thread(_take_in_fresh_loop, screenshots, positions, out) == expected

    assert len(threads) == 4
    assert all(thread is threads[0] for thread in threads), [thread.name for thread in threads]
    assert threads[0].name.startswith("screenshot-stitch"), threads[0].name


@pytest.mark.asyncio
async def test_cancel_of_a_stitch_queued_behind_another_loop_drains_before_it_returns(tmp_path: Path) -> None:
    """Two loops share the one stitch worker: a stitch queued behind another is still owned once submitted."""
    screenshots, positions = _three_viewports()
    expected = _expected_merged_png(screenshots, positions)
    gated, gate = threading.Event(), threading.Event()
    threads: list[threading.Thread] = []
    queued: dict[str, object] = {}
    first_out, second_out = tmp_path / "first.png", tmp_path / "second.png"

    def _second_loop() -> str:
        async def _take() -> str:
            queued["loop"], queued["task"] = asyncio.get_running_loop(), asyncio.current_task()
            try:
                await page_module.SkyvernFrame.take_scrolling_screenshot(
                    page=MagicMock(name="page"), file_path=str(second_out), mode=ScreenshotMode.LITE, scrolling_number=3
                )
                return "completed"
            except asyncio.CancelledError:
                return "cancelled"

        return asyncio.run(_take())

    with _stitch_path_patches(
        create_instance=AsyncMock(return_value=_FakeSkyvernFrame()),
        scroll_helper=AsyncMock(return_value=(list(screenshots), list(positions))),
        merge_impl=_thread_recording_merge(threads, gate, gated),
        fallback=AsyncMock(return_value=b"FALLBACK"),
        fake_gc=MagicMock(),
    ):
        first = asyncio.ensure_future(
            page_module.SkyvernFrame.take_scrolling_screenshot(
                page=MagicMock(name="page"), file_path=str(first_out), mode=ScreenshotMode.LITE, scrolling_number=3
            )
        )
        second = asyncio.ensure_future(asyncio.to_thread(_second_loop))
        try:
            assert await asyncio.to_thread(gated.wait, 5)
            await asyncio.sleep(0.3)
            assert len(threads) == 1, "the other loop's stitch must queue behind the running one"
            loop, task = queued["loop"], queued["task"]
            assert isinstance(loop, asyncio.AbstractEventLoop) and isinstance(task, asyncio.Task)
            loop.call_soon_threadsafe(task.cancel)
            await asyncio.sleep(0.3)
            assert not second.done(), "a queued, submitted stitch is drained before its cancellation lands"
        finally:
            gate.set()
            done, _ = await asyncio.wait({first, second}, timeout=10)
        assert done == {first, second}
        assert first.result() == expected
        assert second.result() == "cancelled"

    assert second_out.read_bytes() == expected, "the drained stitch still writes its file before the cancel lands"
    assert len(threads) == 2 and threads[0] is threads[1]
