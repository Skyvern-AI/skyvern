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

    async def click(self, page: Page, locator: Locator, *, timeout: float | None = None) -> None:
        if timeout is None:
            await locator.click()
        else:
            await locator.click(timeout=timeout)


class DefaultInputStrategy(InputEventStrategy):
    """Input strategy that uses plain Playwright typing."""

    async def type_text(
        self,
        page: Page,
        locator: Locator | None,
        text: str,
        *,
        timeout: float | None = settings.BROWSER_ACTION_TIMEOUT_MS,
        delay: float | None = None,
        no_wait_after: bool | None = None,
        allow_batched_playwright: bool = False,
    ) -> None:
        if locator is not None:
            type_options = {"timeout": timeout, "delay": TEXT_INPUT_DELAY if delay is None else delay}
            if no_wait_after is not None:
                type_options["no_wait_after"] = no_wait_after
            if allow_batched_playwright:
                await locator.type(text, **type_options)
                return
            for char in text:
                await locator.type(char, **type_options)
        else:
            await page.keyboard.type(text)

    async def clear_field(
        self,
        page: Page,
        locator: Locator,
        char_count: int,
        *,
        timeout: float | None = settings.BROWSER_ACTION_TIMEOUT_MS,
        force: bool | None = None,
        no_wait_after: bool | None = None,
    ) -> None:
        clear_options: dict[str, float | bool | None] = {"timeout": timeout}
        if force is not None:
            clear_options["force"] = force
        if no_wait_after is not None:
            clear_options["no_wait_after"] = no_wait_after
        await locator.clear(**clear_options)


class DefaultScrollStrategy(ScrollEventStrategy):
    """Scroll strategy that uses plain Playwright wheel events."""

    async def scroll_to_element(self, page: Page, locator: Locator) -> None:
        await locator.scroll_into_view_if_needed()

    async def scroll_by(self, page: Page, delta_y: float) -> None:
        await page.mouse.wheel(0, delta_y)
