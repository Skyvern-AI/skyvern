from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest
from playwright.async_api import Error as PlaywrightError

from skyvern.exceptions import InvalidElementForTextInput
from skyvern.webeye.actions.handler import get_input_value
from skyvern.webeye.utils.dom import SkyvernElement, is_incompatible_text_input_error

_NOT_INPUT_ERROR = "Element is not an <input>, <textarea> or [contenteditable] element"
_NOT_HTMLELEMENT_ERROR = "Node is not an HTMLElement"
_NOT_INPUTELEMENT_ERROR = "Node is not an HTMLInputElement, HTMLTextAreaElement or HTMLSelectElement"
_TIMEOUT_ERROR = "Timeout 30000ms exceeded"


def _make_element(tag_name: str, locator: MagicMock) -> SkyvernElement:
    return SkyvernElement(locator, MagicMock(), {"id": "el-1", "tagName": tag_name})


def test_predicate_matches_incompatible_type_errors() -> None:
    assert is_incompatible_text_input_error(PlaywrightError(_NOT_INPUT_ERROR)) is True
    assert is_incompatible_text_input_error(PlaywrightError(_NOT_HTMLELEMENT_ERROR)) is True
    assert is_incompatible_text_input_error(PlaywrightError(_NOT_INPUTELEMENT_ERROR)) is True
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
async def test_input_clear_classifies_incompatible_element(monkeypatch: pytest.MonkeyPatch) -> None:
    element = _make_element("button", MagicMock())

    async def _raise(*args: object, **kwargs: object) -> None:
        raise PlaywrightError(_NOT_INPUT_ERROR)

    monkeypatch.setattr("skyvern.webeye.utils.dom.EventStrategyFactory.clear_field", _raise)
    with pytest.raises(InvalidElementForTextInput):
        await element.input_clear()
