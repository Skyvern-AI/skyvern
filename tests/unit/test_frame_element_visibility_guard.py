"""SKY-12311: probing an iframe's ElementHandle.is_visible must not hang the scrape.

``ElementHandle.is_visible()`` takes no timeout argument and falls back to
Playwright's built-in 30s action timeout. On a detached / mid-navigation
cross-process iframe it can stall for the full 30s and then raise
``ElementHandle.is_visible: Timeout 30000ms exceeded``. The scrape must treat an
unreadable frame as "not visible" and skip it -- bounded and fast -- instead of
stalling or crashing.
"""

import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from playwright._impl._errors import TimeoutError as PlaywrightTimeoutError

from skyvern.webeye.scraper import scraper


@pytest.mark.asyncio
async def test_frame_element_is_visible_true_when_handle_visible() -> None:
    handle = MagicMock()
    handle.is_visible = AsyncMock(return_value=True)
    assert await scraper._frame_element_is_visible(handle) is True


@pytest.mark.asyncio
async def test_frame_element_is_visible_false_on_playwright_timeout() -> None:
    handle = MagicMock()
    handle.is_visible = AsyncMock(
        side_effect=PlaywrightTimeoutError("ElementHandle.is_visible: Timeout 30000ms exceeded.")
    )
    assert await scraper._frame_element_is_visible(handle) is False


@pytest.mark.asyncio
async def test_frame_element_is_visible_bounds_a_hang_and_returns_false_fast() -> None:
    async def _never_returns() -> bool:
        await asyncio.sleep(60)
        return True

    handle = MagicMock()
    handle.is_visible = AsyncMock(side_effect=_never_returns)

    # A tiny bound must win long before the (mocked) 60s hang would resolve.
    result = await asyncio.wait_for(scraper._frame_element_is_visible(handle, timeout=0.05), timeout=5)
    assert result is False


@pytest.mark.asyncio
async def test_get_frame_text_skips_child_whose_visibility_probe_fails() -> None:
    """A child frame whose visibility probe blows up must be skipped, not fatal."""
    child = MagicMock()
    child.url = "https://embed.example.invalid/iframe"
    child.child_frames = []
    child.is_detached = MagicMock(return_value=False)
    child.page = MagicMock()

    bad_element = MagicMock()
    bad_element.is_visible = AsyncMock(
        side_effect=PlaywrightTimeoutError("ElementHandle.is_visible: Timeout 30000ms exceeded.")
    )
    child.frame_element = AsyncMock(return_value=bad_element)

    main = MagicMock()
    main.url = "https://example.com/"
    main.child_frames = [child]
    main.is_detached = MagicMock(return_value=False)
    main.page = MagicMock()

    with patch.object(scraper.SkyvernFrame, "evaluate", new=AsyncMock(return_value="root-text")):
        text = await scraper.get_frame_text(main)

    # Main frame text still returned; the unreadable child contributed nothing and did not raise.
    assert text == "root-text"
