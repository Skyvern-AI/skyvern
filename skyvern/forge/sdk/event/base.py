from abc import ABC, abstractmethod

from playwright.async_api import Locator, Page


class CursorEventStrategy(ABC):
    """Strategy for dispatching cursor events to a page."""

    @abstractmethod
    async def move_to(self, page: Page, x: float, y: float) -> None:
        pass

    def sync_position(self, page: Page, x: float, y: float) -> None:
        """Notify the strategy that the cursor is now at (x, y) without generating movement.

        Override in stateful strategies that track cursor position.
        """

    async def warmup(self, page: Page) -> None:
        """Optional per-page warmup hook fired before the first interaction on a page.

        Default is a no-op. Strategies that want to emit pre-interaction signals
        (e.g. an initial wheel event) can override this.
        Implementations should be idempotent — callers may invoke this more than once.
        """

    @abstractmethod
    async def move_to_element(self, page: Page, locator: Locator) -> tuple[float, float]:
        pass

    @abstractmethod
    async def click(self, page: Page, locator: Locator, *, timeout: float | None = None) -> None:
        """Click *locator* on *page*, honouring the per-call *timeout* in milliseconds.

        Default implementations should forward ``timeout`` to
        Playwright's ``locator.click(timeout=...)`` so the caller's actionability
        guarantee is preserved. Alternate implementations may dispatch explicit
        mouse events at coordinates resolved from the locator's bounding box; they
        still receive ``timeout`` for use in bounding-box / visibility waits.
        """


class InputEventStrategy(ABC):
    """Strategy for dispatching keyboard input events to a page."""

    @abstractmethod
    async def type_text(self, page: Page, locator: Locator | None, text: str) -> None:
        pass

    @abstractmethod
    async def clear_field(self, page: Page, locator: Locator, char_count: int) -> None:
        pass


class ScrollEventStrategy(ABC):
    """Strategy for dispatching scroll events to a page."""

    @abstractmethod
    async def scroll_to_element(self, page: Page, locator: Locator) -> None:
        pass

    @abstractmethod
    async def scroll_by(self, page: Page, delta_y: float) -> None:
        pass
