from unittest.mock import AsyncMock, patch

import pytest

from skyvern.config import settings
from skyvern.webeye.scraper import scraper


@pytest.mark.asyncio
async def test_wait_for_scrape_ready_uses_animation_wait_by_default() -> None:
    skyvern_frame = AsyncMock()

    with patch.object(scraper, "_should_use_page_ready_wait", return_value=False):
        await scraper._wait_for_scrape_ready(skyvern_frame)

    skyvern_frame.safe_wait_for_animation_end.assert_awaited_once()
    skyvern_frame.wait_for_page_ready.assert_not_awaited()


@pytest.mark.asyncio
async def test_wait_for_scrape_ready_uses_page_ready_when_enabled() -> None:
    skyvern_frame = AsyncMock()

    with patch.object(scraper, "_should_use_page_ready_wait", return_value=True):
        await scraper._wait_for_scrape_ready(skyvern_frame)

    skyvern_frame.wait_for_page_ready.assert_awaited_once_with(
        network_idle_timeout_ms=settings.PAGE_READY_NETWORK_IDLE_TIMEOUT_MS,
        loading_indicator_timeout_ms=settings.PAGE_READY_LOADING_INDICATOR_TIMEOUT_MS,
        dom_stable_ms=settings.PAGE_READY_DOM_STABLE_MS,
        dom_stability_timeout_ms=settings.PAGE_READY_DOM_STABILITY_TIMEOUT_MS,
    )
    skyvern_frame.safe_wait_for_animation_end.assert_not_awaited()
