from __future__ import annotations

import asyncio
import time
from typing import Any, Dict, List

import structlog
from playwright._impl._errors import TimeoutError
from playwright.async_api import ElementHandle, Frame, Page

from skyvern.config import settings
from skyvern.constants import BUILDING_ELEMENT_TREE_TIMEOUT_MS, PAGE_CONTENT_TIMEOUT, SKYVERN_DIR
from skyvern.exceptions import FailedToTakeScreenshot

LOG = structlog.get_logger()


def load_js_script() -> str:
    # TODO: Handle file location better. This is a hacky way to find the file location.
    path = f"{SKYVERN_DIR}/webeye/scraper/domUtils.js"
    try:
        # TODO: Implement TS of domUtils.js and use the complied JS file instead of the raw JS file.
        # This will allow our code to be type safe.
        with open(path, "r") as f:
            return f.read()
    except FileNotFoundError as e:
        LOG.exception("Failed to load the JS script", path=path)
        raise e


JS_FUNCTION_DEFS = load_js_script()


class SkyvernFrame:
    @staticmethod
    async def evaluate(
        frame: Page | Frame,
        expression: str,
        arg: Any | None = None,
        timeout_ms: float = settings.BROWSER_ACTION_TIMEOUT_MS,
    ) -> Any:
        try:
            async with asyncio.timeout(timeout_ms / 1000):
                return await frame.evaluate(expression=expression, arg=arg)
        except asyncio.TimeoutError:
            LOG.exception("Timeout to evaluate expression", expression=expression)
            raise TimeoutError("timeout to evaluate expression")

    @staticmethod
    async def get_url(frame: Page | Frame) -> str:
        return await SkyvernFrame.evaluate(frame=frame, expression="() => document.location.href")

    @staticmethod
    async def take_screenshot(
        page: Page,
        full_page: bool = False,
        file_path: str | None = None,
        timeout: float = settings.BROWSER_LOADING_TIMEOUT_MS,
    ) -> bytes:
        if page.is_closed():
            raise FailedToTakeScreenshot(error_message="Page is closed")
        try:
            await page.wait_for_load_state(timeout=settings.BROWSER_LOADING_TIMEOUT_MS)
            LOG.debug("Page is fully loaded, agent is about to take screenshots")
            start_time = time.time()
            screenshot: bytes = bytes()
            if file_path:
                screenshot = await page.screenshot(
                    path=file_path,
                    full_page=full_page,
                    timeout=timeout,
                )
            else:
                screenshot = await page.screenshot(
                    full_page=full_page,
                    timeout=timeout,
                    animations="disabled",
                )
            end_time = time.time()
            LOG.debug(
                "Screenshot taking time",
                screenshot_time=end_time - start_time,
                full_page=full_page,
                file_path=file_path,
            )
            return screenshot
        except TimeoutError as e:
            LOG.exception(f"Timeout error while taking screenshot: {str(e)}")
            raise FailedToTakeScreenshot(error_message=str(e)) from e
        except Exception as e:
            LOG.exception(f"Unknown error while taking screenshot: {str(e)}")
            raise FailedToTakeScreenshot(error_message=str(e)) from e

    @staticmethod
    async def take_split_screenshots(
        page: Page,
        url: str,
        draw_boxes: bool = False,
        max_number: int = settings.MAX_NUM_SCREENSHOTS,
    ) -> List[bytes]:
        skyvern_page = await SkyvernFrame.create_instance(frame=page)

        # page is the main frame and the index must be 0
        assert isinstance(skyvern_page.frame, Page)
        frame = "main.frame"
        frame_index = 0

        screenshots: List[bytes] = []
        if await skyvern_page.is_window_scrollable():
            scroll_y_px_old = -30.0
            scroll_y_px = await skyvern_page.scroll_to_top(draw_boxes=draw_boxes, frame=frame, frame_index=frame_index)
            # Checking max number of screenshots to prevent infinite loop
            # We are checking the difference between the old and new scroll_y_px to determine if we have reached the end of the
            # page. If the difference is less than 25, we assume we have reached the end of the page.
            while abs(scroll_y_px_old - scroll_y_px) > 25 and len(screenshots) < max_number:
                screenshot = await SkyvernFrame.take_screenshot(page=skyvern_page.frame, full_page=False)
                screenshots.append(screenshot)
                scroll_y_px_old = scroll_y_px
                LOG.debug("Scrolling to next page", url=url, num_screenshots=len(screenshots))
                scroll_y_px = await skyvern_page.scroll_to_next_page(
                    draw_boxes=draw_boxes, frame=frame, frame_index=frame_index
                )
                LOG.debug(
                    "Scrolled to next page",
                    scroll_y_px=scroll_y_px,
                    scroll_y_px_old=scroll_y_px_old,
                )
            if draw_boxes:
                await skyvern_page.remove_bounding_boxes()
            await skyvern_page.scroll_to_top(draw_boxes=False, frame=frame, frame_index=frame_index)
            # wait until animation ends, which is triggered by scrolling
            LOG.debug("Waiting for 2 seconds until animation ends.")
            await asyncio.sleep(2)
        else:
            if draw_boxes:
                await skyvern_page.build_elements_and_draw_bounding_boxes(frame=frame, frame_index=frame_index)

            LOG.debug("Page is not scrollable", url=url, num_screenshots=len(screenshots))
            screenshot = await SkyvernFrame.take_screenshot(page=skyvern_page.frame, full_page=False)
            screenshots.append(screenshot)

            if draw_boxes:
                await skyvern_page.remove_bounding_boxes()

        return screenshots

    @classmethod
    async def create_instance(cls, frame: Page | Frame) -> SkyvernFrame:
        instance = cls(frame=frame)
        await cls.evaluate(frame=instance.frame, expression=JS_FUNCTION_DEFS)
        return instance

    def __init__(self, frame: Page | Frame) -> None:
        self.frame = frame

    def get_frame(self) -> Page | Frame:
        return self.frame

    async def get_content(self, timeout: float = PAGE_CONTENT_TIMEOUT) -> str:
        async with asyncio.timeout(timeout):
            return await self.frame.content()

    async def get_scroll_x_y(self) -> tuple[int, int]:
        js_script = "() => getScrollXY()"
        return await self.evaluate(frame=self.frame, expression=js_script)

    async def scroll_to_x_y(self, x: int, y: int) -> None:
        js_script = "([x, y]) => scrollToXY(x, y)"
        return await self.evaluate(frame=self.frame, expression=js_script, arg=[x, y])

    async def scroll_to_element_bottom(self, element: ElementHandle, page_by_page: bool = False) -> None:
        js_script = "([element, page_by_page]) => scrollToElementBottom(element, page_by_page)"
        return await self.evaluate(frame=self.frame, expression=js_script, arg=[element, page_by_page])

    async def scroll_to_element_top(self, element: ElementHandle) -> None:
        js_script = "(element) => scrollToElementTop(element)"
        return await self.evaluate(frame=self.frame, expression=js_script, arg=element)

    async def parse_element_from_html(self, frame: str, element: ElementHandle, interactable: bool) -> Dict:
        js_script = "async ([frame, element, interactable]) => await buildElementObject(frame, element, interactable)"
        return await self.evaluate(frame=self.frame, expression=js_script, arg=[frame, element, interactable])

    async def get_element_scrollable(self, element: ElementHandle) -> bool:
        js_script = "(element) => isScrollable(element)"
        return await self.evaluate(frame=self.frame, expression=js_script, arg=element)

    async def get_element_visible(self, element: ElementHandle) -> bool:
        js_script = "(element) => isElementVisible(element) && !isHidden(element)"
        return await self.evaluate(frame=self.frame, expression=js_script, arg=element)

    async def get_disabled_from_style(self, element: ElementHandle) -> bool:
        js_script = "(element) => checkDisabledFromStyle(element)"
        return await self.evaluate(frame=self.frame, expression=js_script, arg=element)

    async def get_blocking_element_id(self, element: ElementHandle) -> tuple[str, bool]:
        js_script = "(element) => getBlockElementUniqueID(element)"
        return await self.evaluate(frame=self.frame, expression=js_script, arg=element)

    async def scroll_to_top(self, draw_boxes: bool, frame: str, frame_index: int) -> float:
        """
        Scroll to the top of the page and take a screenshot.
        :param drow_boxes: If True, draw bounding boxes around the elements.
        :param page: Page instance to take the screenshot from.
        :return: Screenshot of the page.
        """
        js_script = "async ([draw_boxes, frame, frame_index]) => await scrollToTop(draw_boxes, frame, frame_index)"
        scroll_y_px = await self.evaluate(
            frame=self.frame,
            expression=js_script,
            timeout_ms=BUILDING_ELEMENT_TREE_TIMEOUT_MS,
            arg=[draw_boxes, frame, frame_index],
        )
        return scroll_y_px

    async def scroll_to_next_page(self, draw_boxes: bool, frame: str, frame_index: int) -> float:
        """
        Scroll to the next page and take a screenshot.
        :param drow_boxes: If True, draw bounding boxes around the elements.
        :param page: Page instance to take the screenshot from.
        :return: Screenshot of the page.
        """
        js_script = "async ([draw_boxes, frame, frame_index]) => await scrollToNextPage(draw_boxes, frame, frame_index)"
        scroll_y_px = await self.evaluate(
            frame=self.frame,
            expression=js_script,
            timeout_ms=BUILDING_ELEMENT_TREE_TIMEOUT_MS,
            arg=[draw_boxes, frame, frame_index],
        )
        return scroll_y_px

    async def remove_bounding_boxes(self) -> None:
        """
        Remove the bounding boxes from the page.
        :param page: Page instance to remove the bounding boxes from.
        """
        js_script = "() => removeBoundingBoxes()"
        await self.evaluate(frame=self.frame, expression=js_script, timeout_ms=BUILDING_ELEMENT_TREE_TIMEOUT_MS)

    async def build_elements_and_draw_bounding_boxes(self, frame: str, frame_index: int) -> None:
        js_script = "async ([frame, frame_index]) => await buildElementsAndDrawBoundingBoxes(frame, frame_index)"
        await self.evaluate(
            frame=self.frame,
            expression=js_script,
            timeout_ms=BUILDING_ELEMENT_TREE_TIMEOUT_MS,
            arg=[frame, frame_index],
        )

    async def is_window_scrollable(self) -> bool:
        js_script = "() => isWindowScrollable()"
        return await self.evaluate(frame=self.frame, expression=js_script)

    async def is_parent(self, parent: ElementHandle, child: ElementHandle) -> bool:
        js_script = "([parent, child]) => isParent(parent, child)"
        return await self.evaluate(frame=self.frame, expression=js_script, arg=[parent, child])

    async def is_sibling(self, el1: ElementHandle, el2: ElementHandle) -> bool:
        js_script = "([el1, el2]) => isSibling(el1, el2)"
        return await self.evaluate(frame=self.frame, expression=js_script, arg=[el1, el2])

    async def has_ASP_client_control(self) -> bool:
        js_script = "() => hasASPClientControl()"
        return await self.evaluate(frame=self.frame, expression=js_script)

    async def click_element_in_javascript(self, element: ElementHandle) -> None:
        js_script = "(element) => element.click()"
        return await self.evaluate(frame=self.frame, expression=js_script, arg=element)
