"""Cursor overlay decorator strategy for Playwright video recordings.

All JS calls go through ``SkyvernFrame`` static methods.
The JS functions themselves live in ``cursorOverlay.js``.
"""

import structlog
from playwright.async_api import Locator, Page

from skyvern.forge.sdk.event.base import CursorEventStrategy
from skyvern.webeye.utils.page import SkyvernFrame

LOG = structlog.get_logger(__name__)


class VisualizingCursorStrategy(CursorEventStrategy):
    """Decorator that adds visual cursor overlay on top of any ``CursorEventStrategy``."""

    def __init__(self, inner: CursorEventStrategy) -> None:
        self._inner = inner

    async def _show(self, page: Page, x: float, y: float) -> None:
        try:
            await SkyvernFrame.ensure_cursor_overlay_loaded(page)
            await SkyvernFrame.cursor_init(page)
            await SkyvernFrame.cursor_move(page, x, y)
        except Exception:
            LOG.debug("cursor_vis: move failed", exc_info=True)

    # -- CursorEventStrategy interface -----------------------------------------

    async def move_to(self, page: Page, x: float, y: float) -> None:
        await self._inner.move_to(page, x, y)
        await self._show(page, x, y)

    def sync_position(self, page: Page, x: float, y: float) -> None:
        self._inner.sync_position(page, x, y)

    async def move_to_element(self, page: Page, locator: Locator) -> tuple[float, float]:
        x, y = await self._inner.move_to_element(page, locator)
        if x != 0.0 or y != 0.0:
            await self._show(page, x, y)
        return x, y

    async def click(self, page: Page, locator: Locator) -> None:
        cx: float | None = None
        cy: float | None = None

        try:
            bbox = await locator.bounding_box()
            if bbox:
                cx = bbox["x"] + bbox["width"] / 2
                cy = bbox["y"] + bbox["height"] / 2
                await self._show(page, cx, cy)
        except Exception:
            LOG.debug("cursor_vis: pre-click positioning failed", exc_info=True)

        await self._inner.click(page, locator)

        if cx is not None and cy is not None:
            try:
                await SkyvernFrame.cursor_click_ring(page, cx, cy)
            except Exception:
                LOG.debug("cursor_vis: click ring failed", exc_info=True)
