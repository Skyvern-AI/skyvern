"""A stuck page load-state must not fail an otherwise-capturable screenshot."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest
from playwright._impl._errors import Error as PlaywrightError
from playwright._impl._errors import TargetClosedError
from playwright._impl._errors import TimeoutError as PlaywrightTimeoutError
from playwright.async_api import Page

from skyvern.exceptions import FailedToTakeScreenshot, ScreenshotTargetClosed
from skyvern.webeye.utils import page as page_module
from skyvern.webeye.utils.page import ScreenshotMode, _current_viewpoint_screenshot_helper


def _make_page(screenshot_bytes: bytes) -> MagicMock:
    page = MagicMock(spec=Page)
    page.is_closed.return_value = False
    page.url = "https://example.test/stream"
    page.viewport_size = {"width": 1280, "height": 720}
    page.wait_for_load_state = AsyncMock()
    page.screenshot = AsyncMock(return_value=screenshot_bytes)
    return page


class TestScreenshotLoadStateNonFatal:
    @pytest.mark.asyncio
    async def test_load_state_timeout_does_not_block_screenshot(self) -> None:
        page = _make_page(b"image-bytes")
        page.wait_for_load_state = AsyncMock(side_effect=PlaywrightTimeoutError("Timeout 60000ms exceeded"))

        result = await _current_viewpoint_screenshot_helper(page, mode=ScreenshotMode.DETAILED)

        assert result == b"image-bytes"
        page.screenshot.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_load_state_playwright_error_does_not_block_screenshot(self) -> None:
        page = _make_page(b"image-bytes")
        page.wait_for_load_state = AsyncMock(side_effect=PlaywrightError("Target closed"))

        result = await _current_viewpoint_screenshot_helper(page, mode=ScreenshotMode.DETAILED)

        assert result == b"image-bytes"
        page.screenshot.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_detailed_mode_waits_for_domcontentloaded(self) -> None:
        page = _make_page(b"image-bytes")

        await _current_viewpoint_screenshot_helper(page, mode=ScreenshotMode.DETAILED)

        page.wait_for_load_state.assert_awaited_once()
        assert page.wait_for_load_state.await_args.args[0] == "domcontentloaded"

    @pytest.mark.asyncio
    async def test_lite_mode_skips_load_state_wait(self) -> None:
        page = _make_page(b"image-bytes")

        result = await _current_viewpoint_screenshot_helper(page, mode=ScreenshotMode.LITE)

        assert result == b"image-bytes"
        page.wait_for_load_state.assert_not_awaited()


class TestScreenshotTargetClosedClassification:
    @pytest.mark.asyncio
    async def test_preclosed_page_skips_capture_with_target_closed_classification(self) -> None:
        page = _make_page(b"image-bytes")
        page.is_closed.return_value = True

        with pytest.raises(ScreenshotTargetClosed):
            await _current_viewpoint_screenshot_helper(page)

        page.screenshot.assert_not_awaited()

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        "error",
        [
            TargetClosedError("Page.screenshot: Target page, context or browser has been closed"),
            PlaywrightError(
                "Page.screenshot: Target page, context or browser has been closed\n"
                "Call log:\n"
                "  - taking page screenshot\n"
                "  - disabled all CSS animations\n"
                "  - waiting for fonts to load..."
            ),
        ],
    )
    async def test_midflight_target_close_uses_expected_classification(self, error: PlaywrightError) -> None:
        page = _make_page(b"image-bytes")
        page.screenshot = AsyncMock(side_effect=error)
        log = MagicMock()

        with pytest.raises(ScreenshotTargetClosed) as exc_info:
            with pytest.MonkeyPatch.context() as monkeypatch:
                monkeypatch.setattr(page_module, "LOG", log)
                await _current_viewpoint_screenshot_helper(page)

        assert exc_info.value.__cause__ is error
        log.info.assert_called_once()
        log.error.assert_not_called()

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        "error",
        [
            PlaywrightError("Page.screenshot: Target crashed"),
            TargetClosedError("Page.screenshot: Target crashed"),
            PlaywrightError(
                "Page.captureScreenshot: Protocol error (Page.captureScreenshot): Unable to capture screenshot "
                "while waiting for fonts to load"
            ),
            RuntimeError("Cleanup failed after Target page, context or browser has been closed"),
        ],
    )
    async def test_other_screenshot_failures_remain_generic(self, error: Exception) -> None:
        page = _make_page(b"image-bytes")
        page.screenshot = AsyncMock(side_effect=error)

        with pytest.raises(FailedToTakeScreenshot) as exc_info:
            await _current_viewpoint_screenshot_helper(page)

        assert type(exc_info.value) is FailedToTakeScreenshot
