"""Element screenshot capture must surface a classified FailedToTakeScreenshot, not raw playwright errors."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, PropertyMock

import pytest
from playwright._impl._errors import TargetClosedError
from playwright._impl._errors import TimeoutError as PlaywrightTimeoutError
from playwright.async_api import Locator, Page

from skyvern.exceptions import FailedToTakeScreenshot
from skyvern.webeye.utils.page import take_element_screenshot


def _make_locator(*, closed: bool = False) -> MagicMock:
    page = MagicMock(spec=Page)
    page.is_closed.return_value = closed
    locator = MagicMock(spec=Locator)
    locator.page = page
    locator.screenshot = AsyncMock(return_value=b"image-bytes")
    return locator


class TestTakeElementScreenshot:
    @pytest.mark.asyncio
    async def test_returns_bytes_on_success(self) -> None:
        locator = _make_locator()

        result = await take_element_screenshot(locator, timeout=1000)

        assert result == b"image-bytes"
        locator.screenshot.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_closed_page_raises_classified_failure(self) -> None:
        locator = _make_locator(closed=True)

        with pytest.raises(FailedToTakeScreenshot):
            await take_element_screenshot(locator, timeout=1000)

        locator.screenshot.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_detached_frame_page_lookup_raises_classified_failure(self) -> None:
        locator = _make_locator()
        type(locator).page = PropertyMock(side_effect=AssertionError("Frame has no page"))

        with pytest.raises(FailedToTakeScreenshot) as exc_info:
            await take_element_screenshot(locator, timeout=1000)

        assert isinstance(exc_info.value.__cause__, AssertionError)
        locator.screenshot.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_target_closed_is_wrapped_not_raw(self) -> None:
        locator = _make_locator()
        locator.screenshot = AsyncMock(
            side_effect=TargetClosedError("Locator.screenshot: Target page, context or browser has been closed")
        )

        with pytest.raises(FailedToTakeScreenshot):
            await take_element_screenshot(locator, timeout=1000)

    @pytest.mark.asyncio
    async def test_timeout_retries_with_animations_allowed(self) -> None:
        locator = _make_locator()
        locator.screenshot = AsyncMock(side_effect=[PlaywrightTimeoutError("Timeout 5000ms exceeded"), b"image-bytes"])

        result = await take_element_screenshot(locator, timeout=1000)

        assert result == b"image-bytes"
        assert locator.screenshot.await_count == 2
        assert locator.screenshot.await_args_list[0].kwargs["animations"] == "disabled"
        assert locator.screenshot.await_args_list[1].kwargs["animations"] == "allow"

    @pytest.mark.asyncio
    async def test_retry_failure_is_wrapped(self) -> None:
        locator = _make_locator()
        locator.screenshot = AsyncMock(
            side_effect=[PlaywrightTimeoutError("Timeout 5000ms exceeded"), TargetClosedError("closed")]
        )

        with pytest.raises(FailedToTakeScreenshot):
            await take_element_screenshot(locator, timeout=1000)
