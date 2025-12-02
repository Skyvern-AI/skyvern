from __future__ import annotations

import asyncio
import time
from enum import StrEnum
from io import BytesIO
from typing import Any

import structlog
from PIL import Image
from playwright._impl._errors import TimeoutError
from playwright.async_api import ElementHandle, Frame, Page

from skyvern.constants import PAGE_CONTENT_TIMEOUT, SKYVERN_DIR
from skyvern.exceptions import FailedToTakeScreenshot
from skyvern.forge.sdk.settings_manager import SettingsManager
from skyvern.forge.sdk.trace import TraceManager

LOG = structlog.get_logger()


def load_js_script() -> str:
    # TODO: Handle file location better. This is a hacky way to find the file location.
    path = f"{SKYVERN_DIR}/webeye/scraper/domUtils.js"
    try:
        # TODO: Implement TS of domUtils.js and use the complied JS file instead of the raw JS file.
        # This will allow our code to be type safe.
        with open(path, encoding="utf-8") as f:
            return f.read()
    except FileNotFoundError as e:
        LOG.exception("Failed to load the JS script", path=path)
        raise e


JS_FUNCTION_DEFS = load_js_script()


class ScreenshotMode(StrEnum):
    LITE = "lite"
    DETAILED = "detailed"


async def _page_screenshot_helper(
    page: Page,
    file_path: str | None = None,
    full_page: bool = False,
    timeout: float = SettingsManager.get_settings().BROWSER_SCREENSHOT_TIMEOUT_MS,
) -> bytes:
    try:
        return await page.screenshot(
            path=file_path,
            timeout=timeout,
            full_page=full_page,
            animations="disabled",
        )
    except TimeoutError as timeout_error:
        LOG.info(
            f"Timeout error while taking screenshot: {str(timeout_error)}. Going to take a screenshot again with animation allowed."
        )
        return await page.screenshot(
            path=file_path,
            timeout=timeout,
            full_page=full_page,
            animations="allow",
        )


async def _current_viewpoint_screenshot_helper(
    page: Page,
    file_path: str | None = None,
    full_page: bool = False,
    timeout: float = SettingsManager.get_settings().BROWSER_SCREENSHOT_TIMEOUT_MS,
    mode: ScreenshotMode = ScreenshotMode.DETAILED,
) -> bytes:
    if page.is_closed():
        raise FailedToTakeScreenshot(error_message="Page is closed")
    try:
        if mode == ScreenshotMode.DETAILED:
            await page.wait_for_load_state(timeout=SettingsManager.get_settings().BROWSER_LOADING_TIMEOUT_MS)
            LOG.debug("Page is fully loaded, agent is about to take screenshots")
        start_time = time.time()
        screenshot: bytes = b""
        if file_path:
            screenshot = await _page_screenshot_helper(
                page=page, file_path=file_path, full_page=full_page, timeout=timeout
            )
        else:
            screenshot = await _page_screenshot_helper(page=page, full_page=full_page, timeout=timeout)
        end_time = time.time()
        LOG.debug(
            "Screenshot taking time",
            screenshot_time=end_time - start_time,
            file_path=file_path,
        )
        return screenshot
    except TimeoutError as e:
        LOG.exception(f"Timeout error while taking screenshot: {str(e)}")
        raise FailedToTakeScreenshot(error_message=str(e)) from e
    except Exception as e:
        LOG.exception(f"Unknown error while taking screenshot: {str(e)}")
        raise FailedToTakeScreenshot(error_message=str(e)) from e


async def _scrolling_screenshots_helper(
    page: Page,
    url: str | None = None,
    draw_boxes: bool = False,
    max_number: int = SettingsManager.get_settings().MAX_NUM_SCREENSHOTS,
    mode: ScreenshotMode = ScreenshotMode.DETAILED,
) -> tuple[list[bytes], list[int]]:
    # page is the main frame and the index must be 0
    skyvern_page = await SkyvernFrame.create_instance(frame=page)
    frame = "main.frame"
    frame_index = 0

    # when mode is lite, we don't draw bounding boxes
    # since draw_boxes impacts the performance of processing
    if mode == ScreenshotMode.LITE:
        draw_boxes = False

    screenshots: list[bytes] = []
    positions: list[int] = []
    if await skyvern_page.is_window_scrollable():
        scroll_y_px_old = -30.0
        _, initial_scroll_height = await skyvern_page.get_scroll_width_and_height()
        scroll_y_px = await skyvern_page.scroll_to_top(draw_boxes=draw_boxes, frame=frame, frame_index=frame_index)
        # Checking max number of screenshots to prevent infinite loop
        # We are checking the difference between the old and new scroll_y_px to determine if we have reached the end of the
        # page. If the difference is less than 25, we assume we have reached the end of the page.
        while abs(scroll_y_px_old - scroll_y_px) > 25 and len(screenshots) < max_number:
            # check if the scroll height changed, if so, rebuild the element tree
            _, scroll_height = await skyvern_page.get_scroll_width_and_height()
            if scroll_height != initial_scroll_height:
                LOG.warning(
                    "Scroll height changed, rebuild the element tree",
                    scroll_height=scroll_height,
                    initial_scroll_height=initial_scroll_height,
                )
                await skyvern_page.build_tree_from_body(frame_name=frame, frame_index=frame_index)
                initial_scroll_height = scroll_height

            screenshot = await _current_viewpoint_screenshot_helper(page=page, mode=mode)
            screenshots.append(screenshot)
            positions.append(int(scroll_y_px))
            scroll_y_px_old = scroll_y_px
            LOG.debug("Scrolling to next page", url=url, num_screenshots=len(screenshots))
            scroll_y_px = await skyvern_page.scroll_to_next_page(
                draw_boxes=draw_boxes,
                frame=frame,
                frame_index=frame_index,
                need_overlap=(mode == ScreenshotMode.DETAILED),
            )
            LOG.debug(
                "Scrolled to next page",
                scroll_y_px=scroll_y_px,
                scroll_y_px_old=scroll_y_px_old,
            )
        if draw_boxes:
            await skyvern_page.remove_bounding_boxes()
        await skyvern_page.scroll_to_top(draw_boxes=False, frame=frame, frame_index=frame_index)

        if mode == ScreenshotMode.DETAILED:
            # wait until animation ends, which is triggered by scrolling
            await skyvern_page.safe_wait_for_animation_end()
    else:
        if draw_boxes:
            await skyvern_page.build_elements_and_draw_bounding_boxes(frame=frame, frame_index=frame_index)

        LOG.debug("Page is not scrollable", url=url, num_screenshots=len(screenshots))
        screenshot = await _current_viewpoint_screenshot_helper(page=page, mode=mode)
        screenshots.append(screenshot)
        positions.append(0)

        if draw_boxes:
            await skyvern_page.remove_bounding_boxes()

    return screenshots, positions


def _merge_images_by_position(images: list[Image.Image], positions: list[int]) -> Image.Image:
    """Merge screenshots vertically using scroll positions to remove overlaps."""
    if not images:
        raise ValueError("no images to merge")
    if len(images) != len(positions):
        raise ValueError("images and positions length mismatch")

    if len(images) == 1:
        return images[0]

    max_width = max(img.width for img in images)

    merged_height = images[0].height
    for i in range(1, len(images)):
        merged_height += positions[i] - positions[i - 1]

    merged_img = Image.new("RGB", (max_width, merged_height), color=(255, 255, 255))

    current_y = 0
    merged_img.paste(images[0], (0, current_y))
    current_y += images[0].height

    for i in range(1, len(images)):
        step = positions[i] - positions[i - 1]
        overlap = images[i].height - step
        if overlap > 0:
            cropped = images[i].crop((0, overlap, images[i].width, images[i].height))
        else:
            cropped = images[i]

        merged_img.paste(cropped, (0, current_y))
        current_y += cropped.height

    return merged_img


class SkyvernFrame:
    @staticmethod
    async def evaluate(
        frame: Page | Frame,
        expression: str,
        arg: Any | None = None,
        timeout_ms: float = SettingsManager.get_settings().BROWSER_ACTION_TIMEOUT_MS,
    ) -> Any:
        try:
            async with asyncio.timeout(timeout_ms / 1000):
                return await frame.evaluate(expression=expression, arg=arg)
        except asyncio.TimeoutError:
            LOG.exception("Skyvern timed out trying to analyze the page", expression=expression)
            raise TimeoutError("Skyvern timed out trying to analyze the page")

    @staticmethod
    async def get_url(frame: Page | Frame) -> str:
        return await SkyvernFrame.evaluate(frame=frame, expression="() => document.location.href")

    @staticmethod
    @TraceManager.traced_async(ignore_inputs=["file_path", "timeout"])
    async def take_scrolling_screenshot(
        page: Page,
        file_path: str | None = None,
        timeout: float = SettingsManager.get_settings().BROWSER_SCREENSHOT_TIMEOUT_MS,
        mode: ScreenshotMode = ScreenshotMode.DETAILED,
        scrolling_number: int = SettingsManager.get_settings().MAX_NUM_SCREENSHOTS,
    ) -> bytes:
        if scrolling_number <= 0:
            return await _current_viewpoint_screenshot_helper(
                page=page, file_path=file_path, timeout=timeout, mode=mode
            )

        if scrolling_number > SettingsManager.get_settings().MAX_NUM_SCREENSHOTS:
            LOG.warning(
                "scrolling_number is greater than the max number of screenshots, setting it to the max number of screenshots",
                scrolling_number=scrolling_number,
                max_number=SettingsManager.get_settings().MAX_NUM_SCREENSHOTS,
            )
            scrolling_number = SettingsManager.get_settings().MAX_NUM_SCREENSHOTS

        # use spilt screenshot with lite mode, isntead of fullpage screenshot from playwright
        LOG.debug("Page is fully loaded, agent is about to generate the full page screenshot")
        start_time = time.time()
        skyvern_frame = await SkyvernFrame.create_instance(frame=page)
        x: int | None = None
        y: int | None = None
        try:
            x, y = await skyvern_frame.get_scroll_x_y()
            async with asyncio.timeout(timeout):
                screenshots, positions = await _scrolling_screenshots_helper(
                    page=page, mode=mode, max_number=scrolling_number
                )
                images = []

                for screenshot in screenshots:
                    with Image.open(BytesIO(screenshot)) as img:
                        img.load()
                        images.append(img)

                merged_img = _merge_images_by_position(images, positions)

                buffer = BytesIO()
                merged_img.save(buffer, format="PNG")
                buffer.seek(0)

                img_data = buffer.read()
                if file_path is not None:
                    with open(file_path, "wb") as f:
                        f.write(img_data)

                end_time = time.time()
                LOG.debug(
                    "Full page screenshot taking time",
                    screenshot_time=end_time - start_time,
                    file_path=file_path,
                )
                return img_data
        except Exception:
            LOG.warning(
                "Failed to take full page screenshot, fallback to use playwright full page screenshot",
                exc_info=True,
            )
            # reset x and y to None to avoid the scroll_to_x_y call in finally block
            x = None
            y = None
            return await _current_viewpoint_screenshot_helper(
                page=page, file_path=file_path, timeout=timeout, full_page=True
            )
        finally:
            if x is not None and y is not None:
                await skyvern_frame.safe_scroll_to_x_y(x, y)

    @staticmethod
    @TraceManager.traced_async(ignore_inputs=["page"])
    async def take_split_screenshots(
        page: Page,
        url: str | None = None,
        draw_boxes: bool = False,
        max_number: int = SettingsManager.get_settings().MAX_NUM_SCREENSHOTS,
        scroll: bool = True,
    ) -> list[bytes]:
        if not scroll:
            return [await _current_viewpoint_screenshot_helper(page=page, mode=ScreenshotMode.DETAILED)]

        screenshots, _ = await _scrolling_screenshots_helper(
            page=page,
            url=url,
            max_number=max_number,
            draw_boxes=draw_boxes,
            mode=ScreenshotMode.DETAILED,
        )
        return screenshots

    @classmethod
    async def create_instance(cls, frame: Page | Frame) -> SkyvernFrame:
        instance = cls(frame=frame)
        await cls.evaluate(frame=instance.frame, expression=JS_FUNCTION_DEFS)
        if SettingsManager.get_settings().ENABLE_EXP_ALL_TEXTUAL_ELEMENTS_INTERACTABLE:
            await instance.evaluate(
                frame=instance.frame, expression="() => window.GlobalEnableAllTextualElements = true"
            )
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

    async def get_scroll_width_and_height(self) -> tuple[int, int]:
        js_script = "() => getScrollWidthAndHeight()"
        return await self.evaluate(frame=self.frame, expression=js_script)

    async def scroll_to_x_y(self, x: int, y: int) -> None:
        js_script = "([x, y]) => scrollToXY(x, y)"
        return await self.evaluate(frame=self.frame, expression=js_script, arg=[x, y])

    async def safe_scroll_to_x_y(self, x: int, y: int) -> None:
        try:
            await self.scroll_to_x_y(x, y)
        except Exception:
            LOG.warning("Failed to scroll to x, y, ignore it", x=x, y=y, exc_info=True)

    async def scroll_to_element_bottom(self, element: ElementHandle, page_by_page: bool = False) -> None:
        js_script = "([element, page_by_page]) => scrollToElementBottom(element, page_by_page)"
        return await self.evaluate(frame=self.frame, expression=js_script, arg=[element, page_by_page])

    async def scroll_to_element_top(self, element: ElementHandle) -> None:
        js_script = "(element) => scrollToElementTop(element)"
        return await self.evaluate(frame=self.frame, expression=js_script, arg=element)

    async def parse_element_from_html(self, frame: str, element: ElementHandle, interactable: bool) -> dict:
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
        js_script = "async ([draw_boxes, frame, frame_index]) => await safeScrollToTop(draw_boxes, frame, frame_index)"
        scroll_y_px = await self.evaluate(
            frame=self.frame,
            expression=js_script,
            timeout_ms=SettingsManager.get_settings().BROWSER_SCRAPING_BUILDING_ELEMENT_TREE_TIMEOUT_MS,
            arg=[draw_boxes, frame, frame_index],
        )
        return scroll_y_px

    async def scroll_to_next_page(
        self, draw_boxes: bool, frame: str, frame_index: int, need_overlap: bool = True
    ) -> float:
        """
        Scroll to the next page and take a screenshot.
        :param drow_boxes: If True, draw bounding boxes around the elements.
        :param page: Page instance to take the screenshot from.
        :return: Screenshot of the page.
        """
        js_script = "async ([draw_boxes, frame, frame_index, need_overlap]) => await scrollToNextPage(draw_boxes, frame, frame_index, need_overlap)"
        scroll_y_px = await self.evaluate(
            frame=self.frame,
            expression=js_script,
            timeout_ms=SettingsManager.get_settings().BROWSER_SCRAPING_BUILDING_ELEMENT_TREE_TIMEOUT_MS,
            arg=[draw_boxes, frame, frame_index, need_overlap],
        )
        return scroll_y_px

    async def remove_bounding_boxes(self) -> None:
        """
        Remove the bounding boxes from the page.
        :param page: Page instance to remove the bounding boxes from.
        """
        js_script = "() => removeBoundingBoxes()"
        await self.evaluate(
            frame=self.frame,
            expression=js_script,
            timeout_ms=SettingsManager.get_settings().BROWSER_SCRAPING_BUILDING_ELEMENT_TREE_TIMEOUT_MS,
        )

    async def build_elements_and_draw_bounding_boxes(self, frame: str, frame_index: int) -> None:
        js_script = "async ([frame, frame_index]) => await buildElementsAndDrawBoundingBoxes(frame, frame_index)"
        await self.evaluate(
            frame=self.frame,
            expression=js_script,
            timeout_ms=SettingsManager.get_settings().BROWSER_SCRAPING_BUILDING_ELEMENT_TREE_TIMEOUT_MS,
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

    async def remove_target_attr(self, element: ElementHandle) -> None:
        js_script = "(element) => element.removeAttribute('target')"
        return await self.evaluate(frame=self.frame, expression=js_script, arg=element)

    async def get_select_options(self, element: ElementHandle) -> tuple[list, str]:
        js_script = "([element]) => getSelectOptions(element)"
        return await self.evaluate(frame=self.frame, expression=js_script, arg=[element])

    async def get_element_dom_depth(self, element: ElementHandle) -> int:
        js_script = "([element]) => getElementDomDepth(element)"
        return await self.evaluate(frame=self.frame, expression=js_script, arg=[element])

    async def remove_all_unique_ids(self) -> None:
        js_script = "() => removeAllUniqueIds()"
        await self.evaluate(frame=self.frame, expression=js_script)

    @TraceManager.traced_async()
    async def build_tree_from_body(
        self,
        frame_name: str | None,
        frame_index: int,
        timeout_ms: float = SettingsManager.get_settings().BROWSER_SCRAPING_BUILDING_ELEMENT_TREE_TIMEOUT_MS,
    ) -> tuple[list[dict], list[dict]]:
        js_script = "async ([frame_name, frame_index]) => await buildTreeFromBody(frame_name, frame_index)"
        return await self.evaluate(
            frame=self.frame, expression=js_script, timeout_ms=timeout_ms, arg=[frame_name, frame_index]
        )

    @TraceManager.traced_async()
    async def get_incremental_element_tree(
        self,
        wait_until_finished: bool = True,
        timeout_ms: float = SettingsManager.get_settings().BROWSER_SCRAPING_BUILDING_ELEMENT_TREE_TIMEOUT_MS,
    ) -> tuple[list[dict], list[dict]]:
        js_script = "async ([wait_until_finished]) => await getIncrementElements(wait_until_finished)"
        return await self.evaluate(
            frame=self.frame, expression=js_script, timeout_ms=timeout_ms, arg=[wait_until_finished]
        )

    @TraceManager.traced_async()
    async def build_tree_from_element(
        self,
        starter: ElementHandle,
        frame: str,
        full_tree: bool = False,
        timeout_ms: float = SettingsManager.get_settings().BROWSER_SCRAPING_BUILDING_ELEMENT_TREE_TIMEOUT_MS,
    ) -> tuple[list[dict], list[dict]]:
        js_script = "async ([starter, frame, full_tree]) => await buildElementTree(starter, frame, full_tree)"
        return await self.evaluate(
            frame=self.frame, expression=js_script, timeout_ms=timeout_ms, arg=[starter, frame, full_tree]
        )

    async def safe_wait_for_animation_end(self, before_wait_sec: float = 0, timeout_ms: float = 3000) -> None:
        try:
            await asyncio.sleep(before_wait_sec)
            await self.frame.wait_for_load_state("load", timeout=timeout_ms)
            await self.wait_for_animation_end(timeout_ms=timeout_ms)
        except Exception:
            LOG.debug("Failed to wait for animation end, but ignore it", exc_info=True)
            return

    async def wait_for_animation_end(self, timeout_ms: float = 3000) -> None:
        async with asyncio.timeout(timeout_ms / 1000):
            while True:
                is_finished = await self.evaluate(
                    frame=self.frame,
                    expression="() => isAnimationFinished()",
                    timeout_ms=timeout_ms,
                )
                if is_finished:
                    return
                await asyncio.sleep(0.1)
