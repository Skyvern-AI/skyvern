"""Default event strategies that delegate to standard Playwright calls.

These are used as fallbacks when no custom strategy is registered or when
the feature flag disables custom strategies at runtime.
"""

import structlog
from playwright.async_api import Locator, Page

from skyvern.config import settings
from skyvern.constants import TEXT_INPUT_DELAY
from skyvern.forge.sdk.event.base import CursorEventStrategy, InputEventStrategy, ScrollEventStrategy

LOG = structlog.get_logger(__name__)


class DefaultCursorStrategy(CursorEventStrategy):
    """Cursor strategy that uses plain Playwright mouse movement."""

    async def move_to(self, page: Page, x: float, y: float) -> None:
        await page.mouse.move(x, y)

    async def move_to_element(self, page: Page, locator: Locator) -> tuple[float, float]:
        try:
            bbox = await locator.bounding_box()
            if bbox is None:
                LOG.debug("move_to_element: element has no bounding box, skipping")
                return 0.0, 0.0
            x = bbox["x"] + bbox["width"] / 2
            y = bbox["y"] + bbox["height"] / 2
            await page.mouse.move(x, y)
            return x, y
        except Exception:
            LOG.debug("move_to_element failed", exc_info=True)
            return 0.0, 0.0

    async def click(self, page: Page, locator: Locator, timeout: float | None = None) -> None:
        LOG.debug("DefaultCursorStrategy.click", timeout=timeout)
        kwargs: dict = {}
        if timeout is not None:
            kwargs["timeout"] = timeout
        await locator.click(**kwargs)


class DefaultInputStrategy(InputEventStrategy):
    """Input strategy that uses plain Playwright typing."""

    async def type_text(self, page: Page, locator: Locator | None, text: str) -> None:
        if locator is not None:
            for char in text:
                await locator.type(char, delay=TEXT_INPUT_DELAY, timeout=settings.BROWSER_ACTION_TIMEOUT_MS)
        else:
            await page.keyboard.type(text)

    async def clear_field(self, page: Page, locator: Locator, char_count: int) -> None:
        await locator.clear(timeout=settings.BROWSER_ACTION_TIMEOUT_MS)


class DefaultScrollStrategy(ScrollEventStrategy):
    """Scroll strategy that uses plain Playwright wheel events."""

    async def scroll_to_element(self, page: Page, locator: Locator) -> None:
        await locator.scroll_into_view_if_needed()

    async def scroll_by(self, page: Page, delta_y: float) -> None:
        await page.mouse.wheel(0, delta_y)
