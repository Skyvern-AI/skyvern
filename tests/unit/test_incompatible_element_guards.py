from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest
from playwright.async_api import Error as PlaywrightError

from skyvern.exceptions import InvalidElementForTextInput
from skyvern.webeye.actions.handler import (
    _is_selected_engine_error,
    _is_selected_engine_timeout,
    get_input_value,
)
from skyvern.webeye.browser_engine import BrowserEngineMetadata, BrowserEngineSelection
from skyvern.webeye.utils.dom import SkyvernElement, is_incompatible_text_input_error

_NOT_INPUT_ERROR = "Element is not an <input>, <textarea> or [contenteditable] element"
_NOT_HTMLELEMENT_ERROR = "Node is not an HTMLElement"
_NOT_INPUTELEMENT_ERROR = "Node is not an HTMLInputElement, HTMLTextAreaElement or HTMLSelectElement"
_NON_FILLABLE_TYPE_ERROR = 'Input of type "checkbox" cannot be filled'
_TIMEOUT_ERROR = "Timeout 30000ms exceeded"


class _EngineError(Exception):
    pass


class _EngineTimeout(_EngineError):
    pass


async def _never_start():  # pragma: no cover - never awaited
    raise AssertionError("start_driver must not be called")


def _engine_selection() -> BrowserEngineSelection:
    return BrowserEngineSelection(
        name="engine-a",
        start_driver=_never_start,
        error_type=_EngineError,
        timeout_error_type=_EngineTimeout,
        metadata=BrowserEngineMetadata(name="engine-a", version="0.0.0"),
        selection_reason="test",
    )


def test_is_selected_engine_timeout_falls_back_to_stock_playwright_when_no_selection() -> None:
    from playwright.async_api import TimeoutError as PlaywrightTimeoutError

    assert _is_selected_engine_timeout(PlaywrightTimeoutError("boom"), None) is True
    assert _is_selected_engine_timeout(_EngineTimeout("boom"), None) is False


def test_is_selected_engine_timeout_uses_selected_engine_identity() -> None:
    from playwright.async_api import TimeoutError as PlaywrightTimeoutError

    selection = _engine_selection()
    assert _is_selected_engine_timeout(_EngineTimeout("boom"), selection) is True
    # A foreign (stock Playwright) timeout is NOT this engine's timeout.
    assert _is_selected_engine_timeout(PlaywrightTimeoutError("boom"), selection) is False


def test_is_selected_engine_error_uses_selected_engine_identity() -> None:
    selection = _engine_selection()
    assert _is_selected_engine_error(_EngineError("boom"), selection) is True
    assert _is_selected_engine_error(PlaywrightError("boom"), selection) is False
    # No selection keeps the stock Playwright identity.
    assert _is_selected_engine_error(PlaywrightError("boom"), None) is True
    assert _is_selected_engine_error(_EngineError("boom"), None) is False


def _make_element(tag_name: str, locator: MagicMock) -> SkyvernElement:
    return SkyvernElement(locator, MagicMock(), {"id": "el-1", "tagName": tag_name})


def test_predicate_matches_incompatible_type_errors() -> None:
    assert is_incompatible_text_input_error(PlaywrightError(_NOT_INPUT_ERROR)) is True
    assert is_incompatible_text_input_error(PlaywrightError(_NOT_HTMLELEMENT_ERROR)) is True
    assert is_incompatible_text_input_error(PlaywrightError(_NOT_INPUTELEMENT_ERROR)) is True
    assert is_incompatible_text_input_error(PlaywrightError(_NON_FILLABLE_TYPE_ERROR)) is True
    assert is_incompatible_text_input_error(PlaywrightError(_TIMEOUT_ERROR)) is False


@pytest.mark.asyncio
async def test_get_input_value_returns_none_when_input_value_rejects_node() -> None:
    locator = MagicMock()
    locator.input_value = AsyncMock(side_effect=PlaywrightError(_NOT_INPUTELEMENT_ERROR))
    assert await get_input_value("input", locator) is None


@pytest.mark.asyncio
async def test_get_input_value_returns_none_when_inner_text_rejects_node() -> None:
    locator = MagicMock()
    locator.inner_text = AsyncMock(side_effect=PlaywrightError(_NOT_HTMLELEMENT_ERROR))
    assert await get_input_value("svg", locator) is None


@pytest.mark.asyncio
async def test_get_input_value_reraises_unrelated_errors() -> None:
    locator = MagicMock()
    locator.input_value = AsyncMock(side_effect=PlaywrightError(_TIMEOUT_ERROR))
    with pytest.raises(PlaywrightError):
        await get_input_value("input", locator)


@pytest.mark.asyncio
async def test_input_fill_classifies_incompatible_element() -> None:
    locator = MagicMock()
    locator.fill = AsyncMock(side_effect=PlaywrightError(_NOT_INPUT_ERROR))
    element = _make_element("a", locator)
    with pytest.raises(InvalidElementForTextInput):
        await element.input_fill("hello")


@pytest.mark.asyncio
async def test_input_fill_reraises_unrelated_errors() -> None:
    locator = MagicMock()
    locator.fill = AsyncMock(side_effect=PlaywrightError(_TIMEOUT_ERROR))
    element = _make_element("input", locator)
    with pytest.raises(PlaywrightError):
        await element.input_fill("hello")


@pytest.mark.asyncio
async def test_get_input_value_translates_selected_engine_incompatible_read() -> None:
    # Under a non-stock selected engine, an incompatible-node read raised as THAT engine's error
    # must still be translated to "value unknown" (None), not escape raw.
    locator = MagicMock()
    locator.input_value = AsyncMock(side_effect=_EngineError(_NOT_INPUTELEMENT_ERROR))
    assert await get_input_value("input", locator, engine_selection=_engine_selection()) is None


@pytest.mark.asyncio
async def test_get_input_value_reraises_foreign_error_under_selected_engine() -> None:
    # A stock Playwright error is foreign to the pinned engine; it must propagate untranslated
    # instead of being silently swallowed as an incompatible read.
    locator = MagicMock()
    locator.input_value = AsyncMock(side_effect=PlaywrightError(_NOT_INPUTELEMENT_ERROR))
    with pytest.raises(PlaywrightError):
        await get_input_value("input", locator, engine_selection=_engine_selection())


@pytest.mark.asyncio
async def test_get_input_value_reraises_selected_engine_non_incompatible_error() -> None:
    locator = MagicMock()
    locator.input_value = AsyncMock(side_effect=_EngineError(_TIMEOUT_ERROR))
    with pytest.raises(_EngineError):
        await get_input_value("input", locator, engine_selection=_engine_selection())


@pytest.mark.asyncio
async def test_input_clear_classifies_incompatible_element(monkeypatch: pytest.MonkeyPatch) -> None:
    element = _make_element("button", MagicMock())

    async def _raise(*args: object, **kwargs: object) -> None:
        raise PlaywrightError(_NOT_INPUT_ERROR)

    monkeypatch.setattr("skyvern.webeye.utils.dom.EventStrategyFactory.clear_field", _raise)
    with pytest.raises(InvalidElementForTextInput):
        await element.input_clear()
