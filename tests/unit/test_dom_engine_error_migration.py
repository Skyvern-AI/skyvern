"""Selected-engine error-identity migration for ``skyvern/webeye/utils/dom.py`` (RUS-5 / SKY-12007).

The DOM catch sites historically keyed on stock Playwright's ``Error`` / ``TimeoutError`` classes. A
run pinned to a non-Playwright engine raises class-disjoint natives, so those ``except`` clauses would
stop firing and the typed translations (``MissingElement``, ``InvalidElementForTextInput``) would
silently degrade. These tests pin the two contracts: with no selection the stock identity is preserved
exactly, and with a selection the run's own engine natives are recognised while a foreign engine's
natives propagate untranslated.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest
from playwright.async_api import Error as PlaywrightError
from playwright.async_api import TimeoutError as PlaywrightTimeoutError

from skyvern.exceptions import InvalidElementForTextInput, MissingElement
from skyvern.webeye.browser_engine import BrowserEngineMetadata, BrowserEngineSelection
from skyvern.webeye.utils.dom import (
    DomUtil,
    SkyvernElement,
    resolve_locator,
)

_DETACHED = "Element is not attached to the DOM"
_FRAME_DETACHED = "Frame was detached"
_NOT_INPUT = "Element is not an <input>, <textarea> or [contenteditable] element"
_TIMEOUT = "Timeout 30000ms exceeded"


class _EngineError(Exception):
    pass


class _EngineTimeout(_EngineError):
    pass


class _ForeignError(Exception):
    pass


class _ForeignTimeout(_ForeignError):
    pass


async def _never_start() -> None:  # pragma: no cover - selection is never provisioned in these tests
    raise AssertionError("start_driver must not be awaited")


def _selection() -> BrowserEngineSelection:
    return BrowserEngineSelection(
        name="synthetic",
        start_driver=_never_start,
        error_type=_EngineError,
        timeout_error_type=_EngineTimeout,
        metadata=BrowserEngineMetadata(name="synthetic", version="0.0.0"),
        selection_reason="test",
    )


def _element(
    tag_name: str,
    locator: MagicMock,
    *,
    engine_selection: BrowserEngineSelection | None = None,
) -> SkyvernElement:
    return SkyvernElement(
        locator,
        MagicMock(),
        {"id": "el-1", "tagName": tag_name},
        engine_selection=engine_selection,
    )


# --- input_fill: incompatible-node translation (typed contract) ---


@pytest.mark.asyncio
async def test_input_fill_stock_fallback_classifies_playwright_error() -> None:
    locator = MagicMock()
    locator.fill = AsyncMock(side_effect=PlaywrightError(_NOT_INPUT))
    with pytest.raises(InvalidElementForTextInput):
        await _element("a", locator).input_fill("hello")


@pytest.mark.asyncio
async def test_input_fill_stock_fallback_reraises_unrelated_playwright_error() -> None:
    locator = MagicMock()
    locator.fill = AsyncMock(side_effect=PlaywrightError(_TIMEOUT))
    with pytest.raises(PlaywrightError):
        await _element("input", locator).input_fill("hello")


@pytest.mark.asyncio
async def test_input_fill_classifies_selected_engine_error() -> None:
    locator = MagicMock()
    locator.fill = AsyncMock(side_effect=_EngineError(_NOT_INPUT))
    with pytest.raises(InvalidElementForTextInput):
        await _element("a", locator, engine_selection=_selection()).input_fill("hello")


@pytest.mark.asyncio
async def test_input_fill_propagates_foreign_engine_error_untranslated() -> None:
    locator = MagicMock()
    locator.fill = AsyncMock(side_effect=_ForeignError(_NOT_INPUT))
    with pytest.raises(_ForeignError):
        await _element("a", locator, engine_selection=_selection()).input_fill("hello")


# --- input_clear: incompatible-node translation (typed contract) ---


@pytest.mark.asyncio
async def test_input_clear_classifies_selected_engine_error(monkeypatch: pytest.MonkeyPatch) -> None:
    async def _raise(*args: object, **kwargs: object) -> None:
        raise _EngineError(_NOT_INPUT)

    monkeypatch.setattr("skyvern.webeye.utils.dom.EventStrategyFactory.clear_field", _raise)
    with pytest.raises(InvalidElementForTextInput):
        await _element("button", MagicMock(), engine_selection=_selection()).input_clear()


@pytest.mark.asyncio
async def test_input_clear_propagates_foreign_engine_error(monkeypatch: pytest.MonkeyPatch) -> None:
    async def _raise(*args: object, **kwargs: object) -> None:
        raise _ForeignError(_NOT_INPUT)

    monkeypatch.setattr("skyvern.webeye.utils.dom.EventStrategyFactory.clear_field", _raise)
    with pytest.raises(_ForeignError):
        await _element("button", MagicMock(), engine_selection=_selection()).input_clear()


# --- _safe_match_count: detached-node tolerance ---


@pytest.mark.asyncio
async def test_safe_match_count_stock_fallback_swallows_detached() -> None:
    locator = MagicMock()
    locator.count = AsyncMock(side_effect=PlaywrightError(_DETACHED))
    assert await _element("input", MagicMock())._safe_match_count(locator) == 0


@pytest.mark.asyncio
async def test_safe_match_count_swallows_selected_engine_detached() -> None:
    locator = MagicMock()
    locator.count = AsyncMock(side_effect=_EngineError(_FRAME_DETACHED))
    element = _element("input", MagicMock(), engine_selection=_selection())
    assert await element._safe_match_count(locator) == 0


@pytest.mark.asyncio
async def test_safe_match_count_reraises_selected_engine_non_detached() -> None:
    locator = MagicMock()
    locator.count = AsyncMock(side_effect=_EngineError(_TIMEOUT))
    element = _element("input", MagicMock(), engine_selection=_selection())
    with pytest.raises(_EngineError):
        await element._safe_match_count(locator)


@pytest.mark.asyncio
async def test_safe_match_count_propagates_foreign_engine_error() -> None:
    locator = MagicMock()
    locator.count = AsyncMock(side_effect=_ForeignError(_DETACHED))
    element = _element("input", MagicMock(), engine_selection=_selection())
    with pytest.raises(_ForeignError):
        await element._safe_match_count(locator)


# A Locator op can still surface "not attached to the DOM" if the node keeps getting replaced
# under the same selector across the whole wait window, so the detached-error guard stays needed.
def _scroll_into_view_element(
    monkeypatch: pytest.MonkeyPatch,
    locator: MagicMock,
    *,
    engine_selection: BrowserEngineSelection | None = None,
) -> SkyvernElement:
    from skyvern.webeye.utils import page as page_module

    fake_frame = MagicMock()
    fake_frame.scroll_into_view = AsyncMock(side_effect=Exception("native scrollIntoView unavailable in test"))
    fake_frame.get_element_visible = AsyncMock(return_value=True)
    fake_frame.safe_scroll_to_x_y = AsyncMock()

    monkeypatch.setattr(page_module.SkyvernFrame, "create_instance", AsyncMock(return_value=fake_frame))
    monkeypatch.setattr(page_module.SkyvernFrame, "evaluate", AsyncMock(return_value=None))

    locator.count = AsyncMock(return_value=1)
    locator.element_handle = AsyncMock(return_value=MagicMock())
    locator.bounding_box = AsyncMock(return_value=None)
    locator.evaluate = AsyncMock(return_value=True)
    locator.focus = AsyncMock()

    return _element("select", locator, engine_selection=engine_selection)


@pytest.mark.asyncio
async def test_scroll_into_view_swallows_stock_detached_confirmation(monkeypatch: pytest.MonkeyPatch) -> None:
    locator = MagicMock()
    locator.scroll_into_view_if_needed = AsyncMock(side_effect=PlaywrightError(_DETACHED))
    element = _scroll_into_view_element(monkeypatch, locator)

    await element.scroll_into_view()  # must not raise

    locator.scroll_into_view_if_needed.assert_awaited_once()
    locator.focus.assert_awaited_once()


@pytest.mark.asyncio
async def test_scroll_into_view_swallows_selected_engine_detached_confirmation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    locator = MagicMock()
    locator.scroll_into_view_if_needed = AsyncMock(side_effect=_EngineError(_DETACHED))
    element = _scroll_into_view_element(monkeypatch, locator, engine_selection=_selection())

    await element.scroll_into_view()  # must not raise

    locator.focus.assert_awaited_once()


@pytest.mark.asyncio
async def test_scroll_into_view_reraises_unrelated_confirmation_error(monkeypatch: pytest.MonkeyPatch) -> None:
    locator = MagicMock()
    locator.scroll_into_view_if_needed = AsyncMock(side_effect=PlaywrightError("Some other actionability failure"))
    element = _scroll_into_view_element(monkeypatch, locator)

    with pytest.raises(PlaywrightError):
        await element.scroll_into_view()

    locator.focus.assert_not_awaited()


# --- input_sequentially: typing-timeout classification ---


@pytest.mark.asyncio
async def test_input_sequentially_classifies_selected_engine_timeout(monkeypatch: pytest.MonkeyPatch) -> None:
    locator = MagicMock()
    locator.count = AsyncMock(return_value=0)
    element = _element("input", locator, engine_selection=_selection())

    async def _raise(*args: object, **kwargs: object) -> None:
        raise _EngineTimeout(_TIMEOUT)

    monkeypatch.setattr("skyvern.webeye.utils.dom.handler_utils.input_sequentially", _raise)
    with pytest.raises(MissingElement):
        await element.input_sequentially("hello")


@pytest.mark.asyncio
async def test_input_sequentially_propagates_foreign_timeout(monkeypatch: pytest.MonkeyPatch) -> None:
    locator = MagicMock()
    locator.count = AsyncMock(return_value=1)
    element = _element("input", locator, engine_selection=_selection())

    async def _raise(*args: object, **kwargs: object) -> None:
        raise _ForeignTimeout(_TIMEOUT)

    monkeypatch.setattr("skyvern.webeye.utils.dom.handler_utils.input_sequentially", _raise)
    with pytest.raises(_ForeignTimeout):
        await element.input_sequentially("hello")


@pytest.mark.asyncio
async def test_input_sequentially_stock_fallback_reraises_after_classify(monkeypatch: pytest.MonkeyPatch) -> None:
    locator = MagicMock()
    locator.count = AsyncMock(return_value=1)
    element = _element("input", locator)

    async def _raise(*args: object, **kwargs: object) -> None:
        raise PlaywrightTimeoutError(_TIMEOUT)

    monkeypatch.setattr("skyvern.webeye.utils.dom.handler_utils.input_sequentially", _raise)
    with pytest.raises(PlaywrightTimeoutError):
        await element.input_sequentially("hello")


# --- resolve_locator: detached-iframe translation ---


@pytest.mark.asyncio
async def test_resolve_locator_translates_selected_engine_detach_to_missing_element() -> None:
    frame_handler = MagicMock()
    frame_handler.content_frame = AsyncMock(side_effect=_EngineError(_FRAME_DETACHED))
    page = MagicMock()
    page.query_selector = AsyncMock(return_value=frame_handler)

    scrape_page = MagicMock()
    scrape_page.id_to_element_dict = {"child": {"frame": "main.frame"}}

    with pytest.raises(MissingElement):
        await resolve_locator(scrape_page, page, "child", "css", engine_selection=_selection())


@pytest.mark.asyncio
async def test_resolve_locator_propagates_foreign_engine_detach() -> None:
    frame_handler = MagicMock()
    frame_handler.content_frame = AsyncMock(side_effect=_ForeignError(_FRAME_DETACHED))
    page = MagicMock()
    page.query_selector = AsyncMock(return_value=frame_handler)

    scrape_page = MagicMock()
    scrape_page.id_to_element_dict = {"child": {"frame": "main.frame"}}

    with pytest.raises(_ForeignError):
        await resolve_locator(scrape_page, page, "child", "css", engine_selection=_selection())


# --- wiring: the selection reaches the element without touching call sites ---


@pytest.mark.asyncio
async def test_create_from_incremental_inherits_engine_selection() -> None:
    selection = _selection()
    incre = MagicMock()
    incre.engine_selection = selection
    incre.id_to_element_dict = {"el-1": {"id": "el-1", "tagName": "input"}}
    incre.id_to_css_dict = {"el-1": "css-1"}
    frame = MagicMock()
    locator = MagicMock()
    locator.count = AsyncMock(return_value=1)
    frame.locator = MagicMock(return_value=locator)
    incre.skyvern_frame.get_frame = MagicMock(return_value=frame)

    element = await SkyvernElement.create_from_incremental(incre, "el-1")

    locator.fill = AsyncMock(side_effect=_EngineError(_NOT_INPUT))
    with pytest.raises(InvalidElementForTextInput):
        await element.input_fill("hello")


def test_dom_util_derives_engine_selection_from_scraped_page() -> None:
    from skyvern.webeye.scraper.scraped_page import ScrapedPage

    selection = _selection()
    scraped_page = MagicMock(spec=ScrapedPage)
    scraped_page._browser_state = MagicMock()
    scraped_page._browser_state.engine_selection = selection

    dom = DomUtil(scraped_page=scraped_page, page=MagicMock())
    assert dom.engine_selection is selection


def test_dom_util_ignores_non_scraped_page_for_derivation() -> None:
    dom = DomUtil(scraped_page=MagicMock(), page=MagicMock())
    assert dom.engine_selection is None


def test_dom_util_explicit_engine_selection_wins() -> None:
    selection = _selection()
    dom = DomUtil(scraped_page=MagicMock(), page=MagicMock(), engine_selection=selection)
    assert dom.engine_selection is selection
