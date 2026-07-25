"""Tests for the navigation-timeout fallback gate in click paths."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest
from playwright.async_api import TimeoutError as PlaywrightTimeoutError

from skyvern.webeye.utils import dom as dom_module
from skyvern.webeye.utils.dom import SkyvernElement, is_post_dispatch_click_timeout

_NAVIGATION_TIMEOUT_MSG = (
    "Locator.click: Timeout 10000ms exceeded.\n"
    "Call log:\n"
    "  - performing click action\n"
    "  - click action done\n"
    "  - waiting for scheduled navigations to finish\n"
)


class SelectedEngineError(Exception):
    pass


class SelectedEngineTimeout(SelectedEngineError):
    pass


def _selected_engine():
    selection = MagicMock()
    selection.is_engine_timeout_error.side_effect = lambda exc: isinstance(exc, SelectedEngineTimeout)
    return selection


class TestPostDispatchTimeoutClassifier:
    def test_timeout_with_scheduled_navigation_message_is_post_dispatch(self) -> None:
        assert is_post_dispatch_click_timeout(PlaywrightTimeoutError(_NAVIGATION_TIMEOUT_MSG))

    def test_timeout_without_scheduled_navigation_keyword_is_not_post_dispatch(self) -> None:
        msg = (
            "Locator.click: Timeout 10000ms exceeded.\n"
            "Call log:\n"
            '  - waiting for locator("#submit")\n'
            "  - locator resolved to 0 elements\n"
        )
        assert is_post_dispatch_click_timeout(PlaywrightTimeoutError(msg)) is False

    def test_page_goto_navigation_timeout_does_not_match(self) -> None:
        """`page.goto` raises 'Navigation timeout...'; we deliberately do not
        match that phrase because it is broader than the post-click signature
        and could appear in selector text or in non-click code paths."""
        assert is_post_dispatch_click_timeout(PlaywrightTimeoutError("Navigation timeout of 30000ms exceeded")) is False

    def test_non_timeout_exception_is_not_post_dispatch(self) -> None:
        assert is_post_dispatch_click_timeout(ValueError("not a click timeout")) is False
        assert is_post_dispatch_click_timeout(RuntimeError("element not visible")) is False

    def test_classifier_is_case_insensitive(self) -> None:
        assert is_post_dispatch_click_timeout(PlaywrightTimeoutError("Scheduled Navigation never completed"))

    def test_selected_native_timeout_is_post_dispatch(self) -> None:
        assert is_post_dispatch_click_timeout(SelectedEngineTimeout(_NAVIGATION_TIMEOUT_MSG), _selected_engine())

    def test_foreign_timeout_is_not_post_dispatch_for_selected_engine(self) -> None:
        assert (
            is_post_dispatch_click_timeout(PlaywrightTimeoutError(_NAVIGATION_TIMEOUT_MSG), _selected_engine()) is False
        )

    def test_selected_native_timeout_without_scheduled_navigation_is_not_post_dispatch(self) -> None:
        assert is_post_dispatch_click_timeout(SelectedEngineTimeout("Timeout"), _selected_engine()) is False


def _make_element() -> SkyvernElement:
    """Build a `SkyvernElement` without invoking its real `__init__`. The
    `object.__new__` bypass is intentional — `click()` only touches a small
    set of methods, all of which we stub below."""
    elem = object.__new__(SkyvernElement)
    elem.is_disabled = AsyncMock(return_value=False)  # type: ignore[method-assign]
    elem.get_id = MagicMock(return_value="AAEi")  # type: ignore[method-assign]
    elem.get_locator = MagicMock(return_value=MagicMock())  # type: ignore[method-assign]
    elem.scroll_into_view = AsyncMock(return_value=None)  # type: ignore[method-assign]
    elem.find_blocking_element = AsyncMock(return_value=(None, False))  # type: ignore[method-assign]
    elem.coordinate_click = AsyncMock(return_value=None)  # type: ignore[method-assign]
    elem.click_in_javascript = AsyncMock(return_value=None)  # type: ignore[method-assign]
    return elem


@pytest.mark.asyncio
async def test_click_navigation_timeout_skips_fallback_chain(monkeypatch: pytest.MonkeyPatch) -> None:
    """The SKY-10921 fix: a navigation-wait timeout from the first Playwright
    click means the click already produced its side effect; the fallback chain
    must not re-click and duplicate it."""
    elem = _make_element()
    monkeypatch.setattr(
        dom_module.EventStrategyFactory,
        "click_element",
        AsyncMock(side_effect=PlaywrightTimeoutError(_NAVIGATION_TIMEOUT_MSG)),
    )

    await elem.click(page=MagicMock(), dom=None, timeout=1000.0)

    elem.coordinate_click.assert_not_called()
    elem.click_in_javascript.assert_not_called()
    elem.scroll_into_view.assert_not_called()


@pytest.mark.asyncio
async def test_click_non_navigation_timeout_runs_full_fallback_chain(monkeypatch: pytest.MonkeyPatch) -> None:
    """A timeout without a navigation reference is a real actionability
    failure — preserve the existing fallback chain."""
    elem = _make_element()
    monkeypatch.setattr(
        dom_module.EventStrategyFactory,
        "click_element",
        AsyncMock(
            side_effect=PlaywrightTimeoutError(
                "Locator.click: Timeout 10000ms exceeded.\nCall log:\n  - waiting for element to be visible\n"
            )
        ),
    )
    elem.coordinate_click = AsyncMock(side_effect=RuntimeError("no bbox"))  # type: ignore[method-assign]

    await elem.click(page=MagicMock(), dom=None, timeout=1000.0)

    elem.coordinate_click.assert_awaited_once()
    elem.click_in_javascript.assert_awaited_once()


@pytest.mark.asyncio
async def test_click_non_timeout_exception_runs_fallback_chain(monkeypatch: pytest.MonkeyPatch) -> None:
    """A non-Timeout exception (e.g. element not found) is not a side-effect
    signal — preserve the existing fallback chain."""
    elem = _make_element()
    monkeypatch.setattr(
        dom_module.EventStrategyFactory,
        "click_element",
        AsyncMock(side_effect=RuntimeError("element not attached")),
    )
    elem.coordinate_click = AsyncMock(side_effect=RuntimeError("no bbox"))  # type: ignore[method-assign]

    await elem.click(page=MagicMock(), dom=None, timeout=1000.0)

    elem.coordinate_click.assert_awaited_once()
    elem.click_in_javascript.assert_awaited_once()


@pytest.mark.asyncio
async def test_click_happy_path_returns_without_fallback(monkeypatch: pytest.MonkeyPatch) -> None:
    elem = _make_element()
    monkeypatch.setattr(
        dom_module.EventStrategyFactory,
        "click_element",
        AsyncMock(return_value=None),
    )

    await elem.click(page=MagicMock(), dom=None, timeout=1000.0)

    elem.coordinate_click.assert_not_called()
    elem.click_in_javascript.assert_not_called()
