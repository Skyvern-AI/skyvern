"""A stuck page load-state must not fail an otherwise-capturable screenshot."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest
from playwright._impl._errors import Error as PlaywrightError
from playwright._impl._errors import TimeoutError as PlaywrightTimeoutError
from playwright.async_api import Page

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
