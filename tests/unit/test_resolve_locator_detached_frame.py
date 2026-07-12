"""SKY-12186: a detached iframe handle must not leak a raw Playwright error.

``resolve_locator`` calls ``query_selector`` then ``content_frame`` on the
returned handle. When the iframe detaches between those two awaits, Playwright
raises ``ElementHandle.content_frame: Element is not attached to the DOM``.
That means the scraped frame is gone -- the same condition ``resolve_locator``
already signals with ``NoneFrameError`` -- so it must be normalized to
``NoneFrameError`` rather than surfacing the raw Playwright error.
"""

from unittest.mock import AsyncMock, MagicMock

import pytest
from playwright._impl._errors import TargetClosedError
from playwright.async_api import Error as PlaywrightError

from skyvern.exceptions import NoneFrameError
from skyvern.webeye.utils.dom import resolve_locator


def _scrape_page_with_single_iframe(frame_id: str) -> MagicMock:
    scrape_page = MagicMock()
    # The frame's parent is the main frame, so resolve_locator walks exactly one hop.
    scrape_page.id_to_element_dict = {frame_id: {"frame": "main.frame"}}
    return scrape_page


@pytest.mark.asyncio
async def test_resolve_locator_normalizes_detached_content_frame_to_none_frame_error() -> None:
    frame_id = "iframe-1"
    scrape_page = _scrape_page_with_single_iframe(frame_id)

    detached_handle = MagicMock()
    detached_handle.content_frame = AsyncMock(
        side_effect=PlaywrightError("ElementHandle.content_frame: Element is not attached to the DOM")
    )

    page = MagicMock()
    page.query_selector = AsyncMock(return_value=detached_handle)

    with pytest.raises(NoneFrameError):
        await resolve_locator(scrape_page, page, frame_id, "div.target")


@pytest.mark.asyncio
async def test_resolve_locator_normalizes_detached_query_selector_to_none_frame_error() -> None:
    # An already-detached parent frame fails at query_selector, not content_frame.
    frame_id = "iframe-1"
    scrape_page = _scrape_page_with_single_iframe(frame_id)

    page = MagicMock()
    page.query_selector = AsyncMock(side_effect=PlaywrightError("Frame was detached"))

    with pytest.raises(NoneFrameError):
        await resolve_locator(scrape_page, page, frame_id, "div.target")


@pytest.mark.asyncio
async def test_resolve_locator_reraises_target_closed_error() -> None:
    # A closed/crashed target is NOT a DOM race -- it must surface, not be masked as NoneFrameError.
    frame_id = "iframe-1"
    scrape_page = _scrape_page_with_single_iframe(frame_id)

    handle = MagicMock()
    handle.content_frame = AsyncMock(side_effect=TargetClosedError("Target page, context or browser has been closed"))
    page = MagicMock()
    page.query_selector = AsyncMock(return_value=handle)

    with pytest.raises(TargetClosedError):
        await resolve_locator(scrape_page, page, frame_id, "div.target")


@pytest.mark.asyncio
async def test_resolve_locator_raises_none_frame_error_when_content_frame_is_none() -> None:
    # Existing contract must be preserved: a None content_frame is still NoneFrameError.
    frame_id = "iframe-1"
    scrape_page = _scrape_page_with_single_iframe(frame_id)

    handle = MagicMock()
    handle.content_frame = AsyncMock(return_value=None)

    page = MagicMock()
    page.query_selector = AsyncMock(return_value=handle)

    with pytest.raises(NoneFrameError):
        await resolve_locator(scrape_page, page, frame_id, "div.target")
