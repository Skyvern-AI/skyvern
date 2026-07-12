"""SKY-12337: clearing must only no-op on Playwright's exact non-editable error.

`DefaultInputStrategy.clear_field` is the shared choke point for every agent
`input_clear`. Clearing a non-editable element (e.g. an LLM-mispicked `h2`)
raises `Element is not an <input>, <textarea> or [contenteditable] element`;
that specific validation error is a safe no-op. Everything else -- including a
genuine clear() timeout whose call log happens to mention `contenteditable` --
must still propagate, or stale text survives for the next `type()` to append to.
"""

from unittest.mock import AsyncMock, MagicMock

import pytest
from playwright._impl._errors import Error as PlaywrightError
from playwright._impl._errors import TimeoutError as PlaywrightTimeoutError

from skyvern.forge.sdk.event.default import DefaultInputStrategy

_NOT_EDITABLE_MESSAGE = "Locator.clear: Error: Element is not an <input>, <textarea> or [contenteditable] element"


@pytest.mark.asyncio
async def test_clear_field_swallows_non_editable_validation_error() -> None:
    strategy = DefaultInputStrategy()
    locator = MagicMock()
    locator.clear = AsyncMock(side_effect=PlaywrightError(_NOT_EDITABLE_MESSAGE))

    # Must not raise -- clearing a non-editable element is a no-op.
    await strategy.clear_field(MagicMock(), locator, char_count=0)


@pytest.mark.asyncio
async def test_clear_field_propagates_timeout_even_when_call_log_mentions_contenteditable() -> None:
    # A real timeout on a valid contenteditable field embeds the resolved element in
    # its call log; that must NOT be swallowed as a no-op (would leave stale content).
    strategy = DefaultInputStrategy()
    locator = MagicMock()
    locator.clear = AsyncMock(
        side_effect=PlaywrightTimeoutError(
            "Locator.clear: Timeout 5000ms exceeded.\nCall log:\n  - waiting for locator resolved to "
            '<div contenteditable="true">…</div>'
        )
    )

    with pytest.raises(PlaywrightTimeoutError):
        await strategy.clear_field(MagicMock(), locator, char_count=0)


@pytest.mark.asyncio
async def test_clear_field_propagates_other_errors() -> None:
    strategy = DefaultInputStrategy()
    locator = MagicMock()
    locator.clear = AsyncMock(side_effect=PlaywrightTimeoutError("Locator.clear: Timeout 5000ms exceeded."))

    with pytest.raises(PlaywrightTimeoutError):
        await strategy.clear_field(MagicMock(), locator, char_count=0)
