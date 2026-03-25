import structlog
from playwright.async_api import Locator, Page

from skyvern.forge.sdk.event.base import CursorEventStrategy, InputEventStrategy, ScrollEventStrategy
from skyvern.forge.sdk.event.default import DefaultCursorStrategy, DefaultInputStrategy, DefaultScrollStrategy

LOG = structlog.get_logger(__name__)

_default_cursor = DefaultCursorStrategy()
_default_input = DefaultInputStrategy()
_default_scroll = DefaultScrollStrategy()


class EventStrategyFactory:
    __cursor: CursorEventStrategy | None = None
    __input: InputEventStrategy | None = None
    __scroll: ScrollEventStrategy | None = None

    # -- setters ----------------------------------------------------------------

    @staticmethod
    def set_cursor_strategy(strategy: CursorEventStrategy) -> None:
        EventStrategyFactory.__cursor = strategy

    @staticmethod
    def set_input_strategy(strategy: InputEventStrategy) -> None:
        EventStrategyFactory.__input = strategy

    @staticmethod
    def set_scroll_strategy(strategy: ScrollEventStrategy) -> None:
        EventStrategyFactory.__scroll = strategy

    @staticmethod
    def reset() -> None:
        """Clear all custom strategies, reverting to defaults."""
        EventStrategyFactory.__cursor = None
        EventStrategyFactory.__input = None
        EventStrategyFactory.__scroll = None

    # -- getters (always return a non-None strategy) ----------------------------

    @staticmethod
    def get_cursor_strategy() -> CursorEventStrategy:
        return EventStrategyFactory.__cursor or _default_cursor

    @staticmethod
    def get_input_strategy() -> InputEventStrategy:
        return EventStrategyFactory.__input or _default_input

    @staticmethod
    def get_scroll_strategy() -> ScrollEventStrategy:
        return EventStrategyFactory.__scroll or _default_scroll

    # -- cursor convenience methods ---------------------------------------------

    @staticmethod
    async def move_cursor(page: Page, x: float, y: float) -> None:
        """Move cursor using the active strategy."""
        await EventStrategyFactory.get_cursor_strategy().move_to(page, x, y)

    @staticmethod
    async def move_to_element(page: Page, locator: Locator) -> None:
        """Move cursor to element. Failures are logged and swallowed."""
        try:
            await EventStrategyFactory.get_cursor_strategy().move_to_element(page, locator)
        except Exception:
            LOG.debug("Cursor move_to_element failed, proceeding with action", exc_info=True)

    @staticmethod
    def sync_cursor_position(page: Page, x: float, y: float) -> None:
        """Update cursor position without generating movement."""
        EventStrategyFactory.get_cursor_strategy().sync_position(page, x, y)

    # -- input convenience methods ----------------------------------------------

    @staticmethod
    async def type_text(page: Page, locator: Locator | None, text: str) -> None:
        """Type text using the active input strategy."""
        await EventStrategyFactory.get_input_strategy().type_text(page, locator, text)

    @staticmethod
    async def clear_field(page: Page, locator: Locator, char_count: int) -> None:
        """Clear field using the active input strategy."""
        await EventStrategyFactory.get_input_strategy().clear_field(page, locator, char_count)

    # -- scroll convenience methods ---------------------------------------------

    @staticmethod
    async def scroll_by(page: Page, scroll_x: float, scroll_y: float) -> None:
        """Scroll using the active strategy for vertical-only, raw wheel for horizontal."""
        if scroll_x == 0:
            await EventStrategyFactory.get_scroll_strategy().scroll_by(page, scroll_y)
        else:
            await page.mouse.wheel(scroll_x, scroll_y)

    @staticmethod
    async def scroll_to_element(page: Page, locator: Locator) -> None:
        """Scroll to element using the active strategy. Failures are logged and swallowed."""
        try:
            await EventStrategyFactory.get_scroll_strategy().scroll_to_element(page, locator)
        except Exception:
            LOG.debug("scroll_to_element failed, proceeding with action", exc_info=True)
