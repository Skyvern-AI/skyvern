"""The frame and input boundary used by computer-use agents."""

import asyncio
from collections.abc import Awaitable, Callable
from typing import Any, Literal, Protocol

import structlog
from playwright.async_api import Page

from skyvern.exceptions import SkyvernException
from skyvern.forge.sdk.event.factory import EventStrategyFactory
from skyvern.webeye.actions import handler_utils
from skyvern.webeye.browser_artifacts import BrowserArtifacts
from skyvern.webeye.scraper.scraped_page import ScrapedPage
from skyvern.webeye.utils.dom import DomUtil

LOG = structlog.get_logger()


class ComputerSurface(Protocol):
    async def get_frame(self) -> ScrapedPage: ...

    async def click(self, x: int, y: int, button: str, repeat: int) -> None: ...

    async def type_text(self, text: str) -> None: ...

    async def key(self, keys: list[str], hold: bool = False, duration: float = 0, repeat: int = 1) -> None: ...

    async def scroll(self, x: int | None, y: int | None, scroll_x: int, scroll_y: int) -> None: ...

    async def drag(self, start_x: int | None, start_y: int | None, path: list[tuple[int, int]]) -> None: ...

    async def wait(self, seconds: float) -> None: ...

    async def move(self, x: int, y: int) -> None: ...

    async def left_mouse(self, x: int | None, y: int | None, direction: Literal["down", "up"]) -> None: ...


class ComputerSurfaceEngineRefused(SkyvernException):
    def __init__(self, engine: str) -> None:
        super().__init__(f"A run bound to a computer surface needs a computer-use engine, not {engine}.")


class ComputerUseTargetRequired(SkyvernException):
    def __init__(self) -> None:
        super().__init__("A computer-use block runs only on a run targeted at an enrolled desktop machine.")


class SurfaceHasNoBrowser(AttributeError):
    """A step bound to a non-browser surface reached code that needs a hosted browser page."""


class SurfaceBrowserState:
    """The browser state of a step bound to a non-browser surface: it has no page, every screenshot is a fresh frame
    from the surface, and any other browser member raises, so nothing on this path can open a hosted browser."""

    browser_context = None
    browser_context_route_policy_url = None
    engine_selection = None
    last_navigation_status = None
    last_navigation_url = None

    def __init__(self, surface: ComputerSurface) -> None:
        self.surface = surface
        self.browser_artifacts = BrowserArtifacts()

    def __getattr__(self, name: str) -> Any:
        raise SurfaceHasNoBrowser(name)

    async def get_working_page(self, *, prune_excess_pages: bool = True) -> None:
        return None

    async def take_post_action_screenshot(self, scrolling_number: int, file_path: str | None = None) -> bytes:
        return (await self.surface.get_frame()).screenshots[0]

    async def take_fullpage_screenshot(self, file_path: str | None = None) -> bytes:
        return (await self.surface.get_frame()).screenshots[0]


class BrowserComputerSurface:
    def __init__(
        self,
        page: Page | None = None,
        scraped_page: ScrapedPage | None = None,
        capture: Callable[[], Awaitable[ScrapedPage]] | None = None,
    ) -> None:
        self._page = page
        self.scraped_page = scraped_page
        self.capture = capture

    @property
    def page(self) -> Page:
        if self._page is None:
            raise RuntimeError("A browser page is required for computer input")
        return self._page

    async def get_frame(self) -> ScrapedPage:
        if self.capture is None:
            raise RuntimeError("A capture operation is required for a computer frame")
        return await self.capture()

    async def click(self, x: int, y: int, button: str, repeat: int) -> None:
        if self.scraped_page is None:
            raise RuntimeError("A scraped page is required for browser clicks")
        dom = DomUtil(scraped_page=self.scraped_page, page=self.page)
        element_id: str | None = await self.page.evaluate(
            """data => {
            const element = document.elementFromPoint(data.x, data.y);
            if (!element) return null;

            // Function to get the unique_id attribute of an element
            function getElementUniqueId(element) {
                if (element && element.nodeType === 1) {
                    // Check if the element has the unique_id attribute
                    if (element.hasAttribute('unique_id')) {
                        return element.getAttribute('unique_id');
                    }

                    // If no unique_id attribute is found, return null
                    return null;
                }
                return null;
            }

            return getElementUniqueId(element);
        }""",
            {"x": x, "y": y},
        )
        LOG.info("Clicked element at location", x=x, y=y, element_id=element_id, button=button)
        if (
            element_id
            and (skyvern_element := await dom.safe_get_skyvern_element_by_id(element_id))
            and await skyvern_element.navigate_to_a_href(page=self.page)
        ):
            return

        await EventStrategyFactory.move_cursor(self.page, x, y)
        if repeat == 1:
            await self.page.mouse.click(x=x, y=y, button=button)
        elif repeat == 2:
            await self.page.mouse.dblclick(x=x, y=y, button=button)
        elif repeat == 3:
            await self.page.mouse.click(x=x, y=y, button=button, click_count=3)
        else:
            raise ValueError(f"Invalid repeat value: {repeat}")

    async def type_text(self, text: str) -> None:
        await EventStrategyFactory.type_text(self.page, None, text)

    async def key(self, keys: list[str], hold: bool = False, duration: float = 0, repeat: int = 1) -> None:
        await handler_utils.keypress(self.page, keys, hold=hold, duration=duration, repeat=repeat)

    async def scroll(self, x: int | None, y: int | None, scroll_x: int, scroll_y: int) -> None:
        if x and y:
            await EventStrategyFactory.move_cursor(self.page, x, y)
        await EventStrategyFactory.scroll_by(self.page, scroll_x, scroll_y)

    async def drag(self, start_x: int | None, start_y: int | None, path: list[tuple[int, int]]) -> None:
        await handler_utils.drag(self.page, start_x, start_y, path)

    async def wait(self, seconds: float) -> None:
        await asyncio.sleep(seconds)

    async def move(self, x: int, y: int) -> None:
        await EventStrategyFactory.move_cursor(self.page, x, y)

    async def left_mouse(self, x: int | None, y: int | None, direction: Literal["down", "up"]) -> None:
        await handler_utils.left_mouse(self.page, x, y, direction)
