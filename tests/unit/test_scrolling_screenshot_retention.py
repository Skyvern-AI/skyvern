"""The multi-viewport LITE stitch must release its decoded images eagerly.

``SkyvernFrame.take_scrolling_screenshot`` decodes one ``PIL.Image`` per viewport,
stitches them, and PNG-encodes the result. Those objects land in reference cycles that
generation-0 GC defers, so plain refcount drop leaves ~100 MB/event resident until a full
collection runs (which can exhaust worker memory). These tests pin the fix: explicit close of the decoded
images, the stitched image, and the PNG buffer, followed by a full ``gc.collect()`` scoped
to the multi-viewport stitch — without changing the returned bytes or scroll restoration.
"""

from __future__ import annotations

from io import BytesIO
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from PIL import Image

import skyvern.webeye.utils.page as page_module
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
) -> bytes:
    tracking_bytesio = _tracking_bytesio_factory(bytesio_instances)
    with (
        patch.object(page_module.SkyvernFrame, "create_instance", AsyncMock(return_value=fake_frame)),
        patch.object(
            page_module,
            "_scrolling_screenshots_helper",
            AsyncMock(return_value=(list(screenshots), list(positions))),
        ),
        patch.object(page_module, "_merge_images_by_position", MagicMock(side_effect=merge_impl)),
        patch.object(
            page_module,
            "_current_viewpoint_screenshot_helper",
            AsyncMock(return_value=fallback_bytes),
        ),
        patch.object(page_module, "BytesIO", tracking_bytesio),
        patch.object(page_module, "gc", fake_gc, create=True),
    ):
        return await page_module.SkyvernFrame.take_scrolling_screenshot(
            page=MagicMock(name="page"),
            mode=ScreenshotMode.LITE,
            scrolling_number=scrolling_number,
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
