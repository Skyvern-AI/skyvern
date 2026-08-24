"""A stuck page load-state must not fail an otherwise-capturable screenshot."""

from __future__ import annotations

import sys
import types
from unittest.mock import AsyncMock, MagicMock

import pytest
from playwright._impl._errors import Error as PlaywrightError
from playwright._impl._errors import TargetClosedError
from playwright._impl._errors import TimeoutError as PlaywrightTimeoutError
from playwright.async_api import Page

from skyvern.exceptions import FailedToTakeScreenshot, ScreenshotTargetClosed
from skyvern.forge.sdk.settings_manager import SettingsManager
from skyvern.webeye import browser_driver_errors
from skyvern.webeye.browser_engine import BrowserEngineSelection
from skyvern.webeye.browser_errors import BrowserAutomationError, BrowserTargetClosedError
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


class _SelectedError(Exception):
    pass


class _SelectedTimeout(_SelectedError):
    pass


class _SelectedTargetClosed(_SelectedError):
    pass


def _selection(*, native_target_closed: bool = False) -> BrowserEngineSelection:
    engine_error_types = (_SelectedError, TargetClosedError) if native_target_closed else (_SelectedError,)
    selection = MagicMock(spec=BrowserEngineSelection)
    selection.is_engine_error.side_effect = lambda exc: isinstance(exc, engine_error_types)
    selection.is_engine_timeout_error.side_effect = lambda exc: isinstance(exc, _SelectedTimeout)
    selection.classify_error.side_effect = lambda exc: (
        BrowserTargetClosedError(str(exc)) if isinstance(exc, (_SelectedTargetClosed, TargetClosedError)) else None
    )
    return selection


def _stock_selection() -> BrowserEngineSelection:
    """A bound selection modeling stock Playwright: the base Error family classifies to
    BrowserAutomationError, and only the native TargetClosedError maps to the rich target-closed type."""
    selection = MagicMock(spec=BrowserEngineSelection)
    selection.is_engine_error.side_effect = lambda exc: isinstance(exc, PlaywrightError)
    selection.is_engine_timeout_error.side_effect = lambda exc: isinstance(exc, PlaywrightTimeoutError)
    selection.classify_error.side_effect = lambda exc: (
        BrowserTargetClosedError(str(exc))
        if isinstance(exc, TargetClosedError)
        else BrowserAutomationError(str(exc))
        if isinstance(exc, PlaywrightError)
        else None
    )
    return selection


class TestScreenshotLoadStateNonFatal:
    @pytest.mark.asyncio
    async def test_zero_height_viewport_is_restored_before_screenshot(self) -> None:
        page = _make_page(b"image-bytes")
        page.viewport_size = {"width": 800, "height": 0}
        page.set_viewport_size = AsyncMock()

        result = await _current_viewpoint_screenshot_helper(page)

        assert result == b"image-bytes"
        page.set_viewport_size.assert_awaited_once_with(
            {"width": 800, "height": SettingsManager.get_settings().BROWSER_HEIGHT}
        )
        page.screenshot.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_no_viewport_mode_is_not_mutated(self) -> None:
        page = _make_page(b"image-bytes")
        page.viewport_size = None
        page.set_viewport_size = AsyncMock()

        assert await _current_viewpoint_screenshot_helper(page) == b"image-bytes"

        page.set_viewport_size.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_valid_viewport_is_not_mutated(self) -> None:
        page = _make_page(b"image-bytes")
        page.set_viewport_size = AsyncMock()

        assert await _current_viewpoint_screenshot_helper(page) == b"image-bytes"

        page.set_viewport_size.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_viewport_restore_failure_remains_a_screenshot_failure(self) -> None:
        page = _make_page(b"image-bytes")
        page.viewport_size = {"width": 800, "height": 0}
        restore_error = PlaywrightError("viewport restore failed")
        page.set_viewport_size = AsyncMock(side_effect=restore_error)

        with pytest.raises(FailedToTakeScreenshot) as exc_info:
            await _current_viewpoint_screenshot_helper(page)

        assert exc_info.value.__cause__ is restore_error
        page.screenshot.assert_not_awaited()

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
    async def test_load_state_foreign_error_propagates_under_selected_engine(self) -> None:
        page = _make_page(b"image-bytes")
        error = PlaywrightError("foreign")
        page.wait_for_load_state = AsyncMock(side_effect=error)

        with pytest.raises(PlaywrightError) as exc_info:
            await _current_viewpoint_screenshot_helper(
                page,
                mode=ScreenshotMode.DETAILED,
                engine_selection=_selection(),
            )

        assert exc_info.value is error
        page.screenshot.assert_not_awaited()

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
            _SelectedTargetClosed("Page.screenshot: Target was disposed"),
        ],
    )
    async def test_midflight_target_close_uses_expected_classification(self, error: Exception) -> None:
        page = _make_page(b"image-bytes")
        page.screenshot = AsyncMock(side_effect=error)
        engine_selection = _selection() if isinstance(error, _SelectedTargetClosed) else None
        log = MagicMock()

        with pytest.raises(ScreenshotTargetClosed) as exc_info:
            with pytest.MonkeyPatch.context() as monkeypatch:
                monkeypatch.setattr(page_module, "LOG", log)
                await _current_viewpoint_screenshot_helper(page, engine_selection=engine_selection)

        assert exc_info.value.__cause__ is error
        log.info.assert_called_once()
        log.error.assert_not_called()

    @pytest.mark.asyncio
    async def test_bound_stock_selection_canonical_base_error_uses_expected_classification(self) -> None:
        page = _make_page(b"image-bytes")
        error = PlaywrightError(
            "Page.screenshot: Target page, context or browser has been closed\n"
            "Call log:\n"
            "  - taking page screenshot\n"
            "  - disabled all CSS animations\n"
            "  - waiting for fonts to load..."
        )
        page.screenshot = AsyncMock(side_effect=error)
        log = MagicMock()

        with pytest.raises(ScreenshotTargetClosed) as exc_info:
            with pytest.MonkeyPatch.context() as monkeypatch:
                monkeypatch.setattr(page_module, "LOG", log)
                await _current_viewpoint_screenshot_helper(page, engine_selection=_stock_selection())

        assert exc_info.value.__cause__ is error
        log.info.assert_called_once()
        log.error.assert_not_called()

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        "error",
        [
            PlaywrightError("Page.screenshot: Target crashed"),
            TargetClosedError("Page.screenshot: Target crashed"),
            PlaywrightError("Page.screenshot: some other unrelated failure"),
        ],
    )
    async def test_bound_stock_selection_crash_and_generic_remain_generic(self, error: PlaywrightError) -> None:
        page = _make_page(b"image-bytes")
        page.screenshot = AsyncMock(side_effect=error)
        log = MagicMock()

        with pytest.raises(FailedToTakeScreenshot) as exc_info:
            with pytest.MonkeyPatch.context() as monkeypatch:
                monkeypatch.setattr(page_module, "LOG", log)
                await _current_viewpoint_screenshot_helper(page, engine_selection=_stock_selection())

        assert type(exc_info.value) is FailedToTakeScreenshot
        assert exc_info.value.__cause__ is error
        log.error.assert_called_once()

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        "message",
        [
            "Page.screenshot: Target crashed",
            "Page.screenshot: Target closed because renderer crashed",
        ],
    )
    async def test_selected_engine_native_target_crash_remains_generic_error(self, message: str) -> None:
        page = _make_page(b"image-bytes")
        error = TargetClosedError(message)
        page.screenshot = AsyncMock(side_effect=error)
        log = MagicMock()

        with pytest.raises(FailedToTakeScreenshot) as exc_info:
            with pytest.MonkeyPatch.context() as monkeypatch:
                monkeypatch.setattr(page_module, "LOG", log)
                await _current_viewpoint_screenshot_helper(
                    page,
                    engine_selection=_selection(native_target_closed=True),
                )

        assert type(exc_info.value) is FailedToTakeScreenshot
        assert exc_info.value.__cause__ is error
        log.error.assert_called_once()
        log.info.assert_not_called()

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


@pytest.fixture
def second_driver_package(monkeypatch: pytest.MonkeyPatch) -> tuple[type[Exception], type[Exception]]:
    """Model the browser image's two live Playwright-family driver packages: a persistent session's
    pages are driven by the package this module's imports were NOT rewritten to, so they raise error
    classes with a different identity."""

    class _ForkError(Exception):
        pass

    class _ForkTimeout(_ForkError):
        pass

    api = types.ModuleType("patchright.async_api")
    api.Error = _ForkError  # type: ignore[attr-defined]
    api.TimeoutError = _ForkTimeout  # type: ignore[attr-defined]
    package = types.ModuleType("patchright")
    package.async_api = api  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "patchright", package)
    monkeypatch.setitem(sys.modules, "patchright.async_api", api)

    error_types, timeout_types = browser_driver_errors._load_driver_error_types()
    monkeypatch.setattr(browser_driver_errors, "DRIVER_ERROR_TYPES", error_types)
    monkeypatch.setattr(browser_driver_errors, "DRIVER_TIMEOUT_ERROR_TYPES", timeout_types)
    return _ForkError, _ForkTimeout


class TestSecondDriverPackageClassification:
    @pytest.mark.asyncio
    async def test_timeout_uses_timeout_arm(
        self, second_driver_package: tuple[type[Exception], type[Exception]]
    ) -> None:
        _, fork_timeout = second_driver_package
        page = _make_page(b"image-bytes")
        error = fork_timeout("Page.screenshot: Timeout 30000ms exceeded")
        page.screenshot = AsyncMock(side_effect=error)
        log = MagicMock()

        with pytest.raises(FailedToTakeScreenshot) as exc_info:
            with pytest.MonkeyPatch.context() as monkeypatch:
                monkeypatch.setattr(page_module, "LOG", log)
                await _current_viewpoint_screenshot_helper(page)

        assert exc_info.value.__cause__ is error
        log.warning.assert_called_once()
        log.error.assert_not_called()

    @pytest.mark.asyncio
    async def test_target_close_uses_target_closed_arm(
        self, second_driver_package: tuple[type[Exception], type[Exception]]
    ) -> None:
        fork_error, _ = second_driver_package
        page = _make_page(b"image-bytes")
        error = fork_error("Page.screenshot: Target page, context or browser has been closed")
        page.screenshot = AsyncMock(side_effect=error)
        log = MagicMock()

        with pytest.raises(ScreenshotTargetClosed) as exc_info:
            with pytest.MonkeyPatch.context() as monkeypatch:
                monkeypatch.setattr(page_module, "LOG", log)
                await _current_viewpoint_screenshot_helper(page)

        assert exc_info.value.__cause__ is error
        log.info.assert_called_once()
        log.error.assert_not_called()


class TestScrollingScreenshotClosedTargetFallback:
    """``take_scrolling_screenshot`` falls back to a whole-page Playwright capture when the scrolling
    merge fails. That fallback screenshots the same page, so a closed target can only fail again --
    once with a WARNING traceback here, once more from the fallback's own classification.
    """

    def _rig(self, monkeypatch: pytest.MonkeyPatch, error: Exception) -> tuple[MagicMock, AsyncMock, MagicMock]:
        page = _make_page(b"image-bytes")
        monkeypatch.setattr(page_module, "_scrolling_screenshots_helper", AsyncMock(side_effect=error))
        fallback = AsyncMock(return_value=b"fallback-bytes")
        monkeypatch.setattr(page_module, "_current_viewpoint_screenshot_helper", fallback)
        frame = MagicMock()
        frame.get_scroll_x_y = AsyncMock(return_value=(0, 120))
        frame.safe_scroll_to_x_y = AsyncMock()
        monkeypatch.setattr(page_module.SkyvernFrame, "create_instance", AsyncMock(return_value=frame))
        return page, fallback, frame

    @pytest.mark.asyncio
    async def test_closed_target_skips_the_doomed_fallback(self, monkeypatch: pytest.MonkeyPatch) -> None:
        error = ScreenshotTargetClosed(error_message="Page is closed")
        page, fallback, frame = self._rig(monkeypatch, error)

        with pytest.raises(ScreenshotTargetClosed) as exc_info:
            await page_module.SkyvernFrame.take_scrolling_screenshot(page, scrolling_number=2)

        assert exc_info.value is error
        fallback.assert_not_awaited()
        frame.safe_scroll_to_x_y.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_other_failures_still_fall_back(self, monkeypatch: pytest.MonkeyPatch) -> None:
        page, fallback, frame = self._rig(monkeypatch, RuntimeError("merge failed"))

        result = await page_module.SkyvernFrame.take_scrolling_screenshot(page, scrolling_number=2)

        assert result == b"fallback-bytes"
        fallback.assert_awaited_once()
