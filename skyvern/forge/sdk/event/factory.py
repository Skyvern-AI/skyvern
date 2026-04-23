import time
from collections import defaultdict

import structlog
from playwright.async_api import Locator, Page

from skyvern.forge.sdk.event.base import CursorEventStrategy, InputEventStrategy, ScrollEventStrategy
from skyvern.forge.sdk.event.default import DefaultCursorStrategy, DefaultInputStrategy, DefaultScrollStrategy

LOG = structlog.get_logger(__name__)

_default_cursor = DefaultCursorStrategy()
_default_input = DefaultInputStrategy()
_default_scroll = DefaultScrollStrategy()


class _EventMetrics:
    """Accumulates per-event-type timing within a step."""

    def __init__(self) -> None:
        self._counts: dict[str, int] = defaultdict(int)
        self._durations: dict[str, float] = defaultdict(float)

    def record(self, event_type: str, duration: float) -> None:
        self._counts[event_type] += 1
        self._durations[event_type] += duration

    def flush(self, **log_kwargs: object) -> None:
        """Log per-event-type summary and reset. No-op if nothing was recorded."""
        if not self._counts:
            return

        total_duration = sum(self._durations.values())
        total_count = sum(self._counts.values())
        per_event = {
            event_type: {"count": self._counts[event_type], "duration_seconds": round(self._durations[event_type], 4)}
            for event_type in sorted(self._counts)
        }
        LOG.info(
            "Event strategy duration metrics",
            total_count=total_count,
            total_duration_seconds=round(total_duration, 4),
            per_event=per_event,
            **log_kwargs,
        )
        self._counts.clear()
        self._durations.clear()


class EventStrategyFactory:
    __cursor: CursorEventStrategy | None = None
    __input: InputEventStrategy | None = None
    __scroll: ScrollEventStrategy | None = None
    __metrics: _EventMetrics = _EventMetrics()

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
        EventStrategyFactory.__metrics = _EventMetrics()

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

    # -- metrics ----------------------------------------------------------------

    @staticmethod
    def flush_metrics(**log_kwargs: object) -> None:
        """Log and reset accumulated event strategy metrics."""
        EventStrategyFactory.__metrics.flush(**log_kwargs)

    # -- cursor convenience methods ---------------------------------------------

    @staticmethod
    async def move_cursor(page: Page, x: float, y: float) -> None:
        """Move cursor using the active strategy."""
        start = time.perf_counter()
        try:
            await EventStrategyFactory.get_cursor_strategy().move_to(page, x, y)
        finally:
            EventStrategyFactory.__metrics.record("move_cursor", time.perf_counter() - start)

    @staticmethod
    async def move_to_element(page: Page, locator: Locator) -> None:
        """Move cursor to element. Failures are logged and swallowed."""
        start = time.perf_counter()
        try:
            await EventStrategyFactory.get_cursor_strategy().move_to_element(page, locator)
        except Exception:
            LOG.debug("Cursor move_to_element failed, proceeding with action", exc_info=True)
        finally:
            EventStrategyFactory.__metrics.record("move_to_element", time.perf_counter() - start)

    @staticmethod
    def sync_cursor_position(page: Page, x: float, y: float) -> None:
        """Update cursor position without generating movement."""
        EventStrategyFactory.get_cursor_strategy().sync_position(page, x, y)

    @staticmethod
    async def click(page: Page, locator: Locator, timeout: float | None = None) -> None:
        """Click an element using the active cursor strategy."""
        start = time.perf_counter()
        try:
            await EventStrategyFactory.get_cursor_strategy().click(page, locator, timeout)
        finally:
            EventStrategyFactory.__metrics.record("click", time.perf_counter() - start)

    # -- input convenience methods ----------------------------------------------

    @staticmethod
    async def type_text(page: Page, locator: Locator | None, text: str) -> None:
        """Type text using the active input strategy."""
        start = time.perf_counter()
        try:
            await EventStrategyFactory.get_input_strategy().type_text(page, locator, text)
        finally:
            EventStrategyFactory.__metrics.record("type_text", time.perf_counter() - start)

    @staticmethod
    async def clear_field(page: Page, locator: Locator, char_count: int) -> None:
        """Clear field using the active input strategy."""
        start = time.perf_counter()
        try:
            await EventStrategyFactory.get_input_strategy().clear_field(page, locator, char_count)
        finally:
            EventStrategyFactory.__metrics.record("clear_field", time.perf_counter() - start)

    # -- scroll convenience methods ---------------------------------------------

    @staticmethod
    async def scroll_by(page: Page, scroll_x: float, scroll_y: float) -> None:
        """Scroll using the active strategy for vertical-only, raw wheel for horizontal."""
        start = time.perf_counter()
        try:
            if scroll_x == 0:
                await EventStrategyFactory.get_scroll_strategy().scroll_by(page, scroll_y)
            else:
                await page.mouse.wheel(scroll_x, scroll_y)
        finally:
            EventStrategyFactory.__metrics.record("scroll_by", time.perf_counter() - start)

    @staticmethod
    async def scroll_to_element(page: Page, locator: Locator) -> None:
        """Scroll to element using the active strategy. Failures are logged and swallowed."""
        start = time.perf_counter()
        try:
            await EventStrategyFactory.get_scroll_strategy().scroll_to_element(page, locator)
        except Exception:
            LOG.debug("scroll_to_element failed, proceeding with action", exc_info=True)
        finally:
            EventStrategyFactory.__metrics.record("scroll_to_element", time.perf_counter() - start)
