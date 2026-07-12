"""SKY-12337: clearing must only target text-input-capable elements.

When the LLM picks a non-editable element (e.g. an ``h2``) for an input action,
``Locator.clear`` raises ``Element is not an <input>, <textarea> or
[contenteditable] element``. Two guards:

1. ``SkyvernElement.input_clear`` no-ops when the element cannot accept text, so
   the doomed ``clear`` (and its noisy exception log) never runs.
2. ``DefaultInputStrategy.clear_field`` swallows the specific "not editable"
   Playwright validation error as a safety net (clearing a non-editable element
   is a no-op by definition), while still propagating real failures.
"""

from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from playwright._impl._errors import Error as PlaywrightError
from playwright._impl._errors import TimeoutError as PlaywrightTimeoutError

from skyvern.forge.sdk.event.default import DefaultInputStrategy
from skyvern.forge.sdk.event.factory import EventStrategyFactory
from skyvern.webeye.utils.dom import SkyvernElement

_NOT_EDITABLE_MESSAGE = "Locator.clear: Error: Element is not an <input>, <textarea> or [contenteditable] element"


def _make_element(tag_name: str) -> SkyvernElement:
    locator = MagicMock()
    locator.page = MagicMock()
    return SkyvernElement(locator, MagicMock(), {"id": "el-1", "tagName": tag_name, "attributes": {}})


@pytest.mark.asyncio
async def test_input_clear_skips_non_text_input_element() -> None:
    element = _make_element("h2")
    element.supports_text_input = AsyncMock(return_value=False)  # type: ignore[method-assign]

    with patch.object(EventStrategyFactory, "clear_field", new=AsyncMock()) as mock_clear:
        await element.input_clear()

    mock_clear.assert_not_awaited()


@pytest.mark.asyncio
async def test_input_clear_runs_for_text_input_element() -> None:
    element = _make_element("input")
    element.supports_text_input = AsyncMock(return_value=True)  # type: ignore[method-assign]

    with patch.object(EventStrategyFactory, "clear_field", new=AsyncMock()) as mock_clear:
        await element.input_clear()

    mock_clear.assert_awaited_once()


@pytest.mark.asyncio
async def test_clear_field_swallows_non_editable_validation_error() -> None:
    strategy = DefaultInputStrategy()
    page = MagicMock()
    locator = MagicMock()
    locator.clear = AsyncMock(side_effect=PlaywrightError(_NOT_EDITABLE_MESSAGE))

    # Must not raise -- clearing a non-editable element is a no-op.
    await strategy.clear_field(page, locator, char_count=0)


@pytest.mark.asyncio
async def test_clear_field_propagates_other_errors() -> None:
    strategy = DefaultInputStrategy()
    page = MagicMock()
    locator = MagicMock()
    locator.clear = AsyncMock(side_effect=PlaywrightTimeoutError("Locator.clear: Timeout 5000ms exceeded."))

    with pytest.raises(PlaywrightTimeoutError):
        await strategy.clear_field(page, locator, char_count=0)
