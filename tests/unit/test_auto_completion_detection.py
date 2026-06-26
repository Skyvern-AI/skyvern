"""Regression tests for autocomplete input detection.

Covers direct attribute detection in is_auto_completion_input().
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest

from skyvern.webeye.utils.dom import SkyvernElement


def _make_element(
    attributes: dict[str, str] | None = None,
    *,
    tag_name: str = "input",
) -> tuple[SkyvernElement, MagicMock]:
    locator = MagicMock()
    locator.get_attribute = AsyncMock(return_value=None)
    locator.element_handle = AsyncMock(return_value=MagicMock())
    element = SkyvernElement(
        locator=locator,
        frame=MagicMock(),
        static_element={
            "id": "AA1",
            "tagName": tag_name,
            "attributes": attributes or {},
        },
    )
    return element, locator


@pytest.mark.asyncio
async def test_direct_aria_autocomplete_list() -> None:
    element, _ = _make_element({"aria-autocomplete": "list"})
    assert await element.is_auto_completion_input() is True


@pytest.mark.asyncio
@pytest.mark.parametrize("value", ["both", "inline"])
async def test_aria_autocomplete_both_inline_not_detected(value: str) -> None:
    """After partial revert of #9417, only 'list' triggers autocomplete."""
    element, _ = _make_element({"aria-autocomplete": value})
    assert await element.is_auto_completion_input() is False


@pytest.mark.asyncio
async def test_role_combobox_not_detected() -> None:
    """After partial revert of #9417, role=combobox alone does not trigger."""
    element, _ = _make_element({"role": "combobox"})
    assert await element.is_auto_completion_input() is False


@pytest.mark.asyncio
async def test_non_input_ignored() -> None:
    element, _ = _make_element({"aria-autocomplete": "list"}, tag_name="textarea")
    assert await element.is_auto_completion_input() is False


@pytest.mark.asyncio
async def test_plain_text_input_not_detected() -> None:
    element, _ = _make_element({"type": "text", "autocomplete": "off"})
    assert await element.is_auto_completion_input() is False


@pytest.mark.asyncio
async def test_autocomplete_class() -> None:
    element, _ = _make_element({"class": "my-autocomplete-input"})
    assert await element.is_auto_completion_input() is True


@pytest.mark.asyncio
async def test_data_x_bind_autocomplete() -> None:
    element, _ = _make_element({"data-x-bind": "someAutocomplete"})
    assert await element.is_auto_completion_input() is True
