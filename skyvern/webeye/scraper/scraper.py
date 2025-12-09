import asyncio
import copy
import json
from collections import defaultdict

import structlog
from playwright._impl._errors import TimeoutError
from playwright.async_api import ElementHandle, Frame, Locator, Page

from skyvern.config import settings
from skyvern.constants import DEFAULT_MAX_TOKENS, SKYVERN_DIR, SKYVERN_ID_ATTR
from skyvern.exceptions import (
    FailedToTakeScreenshot,
    NoElementFound,
    ScrapingFailed,
    ScrapingFailedBlankPage,
    UnknownElementTreeFormat,
)
from skyvern.experimentation.wait_utils import empty_page_retry_wait
from skyvern.forge.sdk.api.crypto import calculate_sha256
from skyvern.forge.sdk.core import skyvern_context
from skyvern.forge.sdk.settings_manager import SettingsManager
from skyvern.forge.sdk.trace import TraceManager
from skyvern.utils.image_resizer import Resolution
from skyvern.utils.token_counter import count_tokens
from skyvern.webeye.browser_state import BrowserState
from skyvern.webeye.scraper.scraped_page import (
    CleanupElementTreeFunc,
    ElementTreeBuilder,
    ElementTreeFormat,
    ScrapedPage,
    ScrapeExcludeFunc,
    json_to_html,
)
from skyvern.webeye.utils.page import SkyvernFrame

LOG = structlog.get_logger()
RESERVED_ATTRIBUTES = {
    "accept",  # for input file
    "alt",
    "aria-checked",  # for option tag
    "aria-current",
    "aria-disabled",
    "aria-label",
    "aria-readonly",
    "aria-required",
    "aria-role",
    "aria-selected",  # for option tag
    "checked",
    "data-original-title",  # for bootstrap tooltip
    "data-ui",
    "disabled",  # for button
    "for",
    "href",  # For a tags
    "maxlength",
    "name",
    "pattern",
    "placeholder",
    "readonly",
    "required",
    "selected",  # for option tag
    "shape-description",  # for css shape
    "src",  # do we need this?
    "text-value",
    "title",
    "type",
    "value",
}

BASE64_INCLUDE_ATTRIBUTES = {
    "href",
    "src",
    "poster",
    "srcset",
    "icon",
}


def load_js_script() -> str:
    # TODO: Handle file location better. This is a hacky way to find the file location.
    path = f"{SKYVERN_DIR}/webeye/scraper/domUtils.js"
    try:
        # TODO: Implement TS of domUtils.js and use the complied JS file instead of the raw JS file.
        # This will allow our code to be type safe.
        with open(path) as f:
            return f.read()
    except FileNotFoundError as e:
        LOG.exception("Failed to load the JS script", path=path)
        raise e


JS_FUNCTION_DEFS = load_js_script()


def clean_element_before_hashing(element: dict) -> dict:
    def clean_nested(element: dict) -> dict:
        element_cleaned = {key: value for key, value in element.items() if key not in {"id", "rect", "frame_index"}}
        if "attributes" in element:
            attributes_cleaned = {key: value for key, value in element["attributes"].items() if key != SKYVERN_ID_ATTR}
            element_cleaned["attributes"] = attributes_cleaned
        if "children" in element:
            children_cleaned = [clean_nested(child) for child in element["children"]]
            element_cleaned["children"] = children_cleaned
        return element_cleaned

    return clean_nested(element)


def hash_element(element: dict) -> str:
    hash_ready_element = clean_element_before_hashing(element)
    # Sort the keys to ensure consistent ordering
    element_string = json.dumps(hash_ready_element, sort_keys=True)

    return calculate_sha256(element_string)


def build_element_dict(
    elements: list[dict],
) -> tuple[dict[str, str], dict[str, dict], dict[str, str], dict[str, str], dict[str, list[str]]]:
    id_to_css_dict: dict[str, str] = {}
    id_to_element_dict: dict[str, dict] = {}
    id_to_frame_dict: dict[str, str] = {}
    id_to_element_hash: dict[str, str] = {}
    hash_to_element_ids: dict[str, list[str]] = {}

    for element in elements:
        element_id: str = element.get("id", "")
        # get_interactable_element_tree marks each interactable element with a SKYVERN_ID_ATTR attribute
        id_to_css_dict[element_id] = f"[{SKYVERN_ID_ATTR}='{element_id}']"
        id_to_element_dict[element_id] = element
        id_to_frame_dict[element_id] = element["frame"]
        element_hash = hash_element(element)
        id_to_element_hash[element_id] = element_hash
        hash_to_element_ids[element_hash] = hash_to_element_ids.get(element_hash, []) + [element_id]

    return id_to_css_dict, id_to_element_dict, id_to_frame_dict, id_to_element_hash, hash_to_element_ids


@TraceManager.traced_async(ignore_input=True)
async def scrape_website(
    browser_state: BrowserState,
    url: str,
    cleanup_element_tree: CleanupElementTreeFunc,
    num_retry: int = 0,
    max_retries: int = settings.MAX_SCRAPING_RETRIES,
    scrape_exclude: ScrapeExcludeFunc | None = None,
    take_screenshots: bool = True,
    draw_boxes: bool = True,
    max_screenshot_number: int = settings.MAX_NUM_SCREENSHOTS,
    scroll: bool = True,
    support_empty_page: bool = False,
    wait_seconds: float = 0,
) -> ScrapedPage:
    """
    ************************************************************************************************
    ************ NOTE: MAX_SCRAPING_RETRIES is set to 0 in both staging and production *************
    ************************************************************************************************
    High-level asynchronous function to scrape a web page. It sets up the Playwright environment, handles browser and
    page initialization, and calls the safe scraping function. This function is ideal for general use where initial
    setup and safety measures are required.

    Asynchronous function that safely scrapes a web page. It handles exceptions and retries scraping up to a maximum
    number of attempts. This function should be used when reliability and error handling are crucial, such as in
    automated scraping tasks.

    :param browser_context: BrowserContext instance used for scraping.
    :param url: URL of the web page to be scraped.
    :param page: Optional Page instance for scraping, a new page is created if None.
    :param num_retry: Tracks number of retries if scraping fails, defaults to 0.

    :return: Tuple containing Page instance, base64 encoded screenshot, and page elements.

    :raises Exception: When scraping fails after maximum retries.
    """

    try:
        num_retry += 1
        return await scrape_web_unsafe(
            browser_state=browser_state,
            url=url,
            cleanup_element_tree=cleanup_element_tree,
            scrape_exclude=scrape_exclude,
            take_screenshots=take_screenshots,
            draw_boxes=draw_boxes,
            max_screenshot_number=max_screenshot_number,
            scroll=scroll,
            support_empty_page=support_empty_page,
            wait_seconds=wait_seconds,
        )
    except ScrapingFailedBlankPage:
        raise
    except Exception as e:
        # NOTE: MAX_SCRAPING_RETRIES is set to 0 in both staging and production
        if num_retry > max_retries:
            LOG.error(
                "Scraping failed after max retries, aborting.",
                max_retries=max_retries,
                num_retry=num_retry,
                url=url,
                exc_info=True,
            )
            if isinstance(e, FailedToTakeScreenshot):
                raise e
            else:
                raise ScrapingFailed() from e
        LOG.info("Scraping failed, will retry", max_retries=max_retries, num_retry=num_retry, url=url, wait_seconds=0.5)
        await asyncio.sleep(0.5)
        return await scrape_website(
            browser_state,
            url,
            cleanup_element_tree,
            num_retry=num_retry,
            max_retries=max_retries,
            scrape_exclude=scrape_exclude,
            take_screenshots=take_screenshots,
            draw_boxes=draw_boxes,
            max_screenshot_number=max_screenshot_number,
            scroll=scroll,
        )


async def get_frame_text(iframe: Frame) -> str:
    """
    Get all the visible text in the iframe.
    :param iframe: Frame instance to get the text from.
    :return: All the visible text from the iframe.
    """
    js_script = "() => document.body.innerText"

    try:
        text = await SkyvernFrame.evaluate(frame=iframe, expression=js_script)
    except Exception:
        LOG.warning(
            "failed to get text from iframe",
            exc_info=True,
        )
        return ""

    for child_frame in iframe.child_frames:
        if child_frame.is_detached():
            continue

        try:
            child_frame_element = await child_frame.frame_element()
        except Exception:
            LOG.warning(
                "Unable to get child_frame_element",
                exc_info=True,
            )
            continue

        # it will get stuck when we `frame.evaluate()` on an invisible iframe
        if not await child_frame_element.is_visible():
            continue

        text += await get_frame_text(child_frame)

    return text


async def scrape_web_unsafe(
    browser_state: BrowserState,
    url: str,
    cleanup_element_tree: CleanupElementTreeFunc,
    scrape_exclude: ScrapeExcludeFunc | None = None,
    take_screenshots: bool = True,
    draw_boxes: bool = True,
    max_screenshot_number: int = settings.MAX_NUM_SCREENSHOTS,
    scroll: bool = True,
    support_empty_page: bool = False,
    wait_seconds: float = 0,
) -> ScrapedPage:
    """
    Asynchronous function that performs web scraping without any built-in error handling. This function is intended
    for use cases where the caller handles exceptions or in controlled environments. It directly scrapes the provided
    URL or continues on the given page.

    :param browser_context: BrowserContext instance used for scraping.
    :param url: URL of the web page to be scraped. Used only when creating a new page.
    :param page: Optional Page instance for scraping, a new page is created if None.
    :return: Tuple containing Page instance, base64 encoded screenshot, and page elements.
    :note: This function does not handle exceptions. Ensure proper error handling in the calling context.
    """

    # browser state must have the page instance, otherwise we should not do scraping
    page = await browser_state.must_get_working_page()
    # Take screenshots of the page with the bounding boxes. We will remove the bounding boxes later.
    # Scroll to the top of the page and take a screenshot.
    # Scroll to the next page and take a screenshot until we reach the end of the page.
    # We check if the scroll_y_px_old is the same as scroll_y_px to determine if we have reached the end of the page.
    # This also solves the issue where we can't scroll due to a popup.(e.g. geico first popup on the homepage after
    # clicking start my quote)
    url = page.url
    if url == "about:blank" and not support_empty_page:
        raise ScrapingFailedBlankPage()

    skyvern_frame = await SkyvernFrame.create_instance(page)
    await skyvern_frame.safe_wait_for_animation_end()

    if wait_seconds > 0:
        LOG.info(f"Waiting for {wait_seconds} seconds before scraping the website.", wait_seconds=wait_seconds)
        await asyncio.sleep(wait_seconds)

    elements, element_tree = await get_interactable_element_tree(page, scrape_exclude)
    if not elements and not support_empty_page:
        LOG.warning("No elements found on the page, wait and retry")
        await empty_page_retry_wait()
        elements, element_tree = await get_interactable_element_tree(page, scrape_exclude)

    element_tree = await cleanup_element_tree(page, url, copy.deepcopy(element_tree))
    element_tree_trimmed = trim_element_tree(copy.deepcopy(element_tree))

    screenshots = []
    if take_screenshots:
        element_tree_trimmed_html_str = "".join(
            json_to_html(element, need_skyvern_attrs=False) for element in element_tree_trimmed
        )
        token_count = count_tokens(element_tree_trimmed_html_str)
        if token_count > DEFAULT_MAX_TOKENS:
            max_screenshot_number = min(max_screenshot_number, 1)

        # get current x, y position of the page
        x: int | None = None
        y: int | None = None
        try:
            x, y = await skyvern_frame.get_scroll_x_y()
            LOG.debug("Current x, y position of the page before scraping", x=x, y=y)
        except Exception:
            LOG.warning("Failed to get current x, y position of the page", exc_info=True)

        screenshots = await SkyvernFrame.take_split_screenshots(
            page=page,
            url=url,
            draw_boxes=draw_boxes,
            max_number=max_screenshot_number,
            scroll=scroll,
        )

        # scroll back to the original x, y position of the page
        if x is not None and y is not None:
            await skyvern_frame.safe_scroll_to_x_y(x, y)
            LOG.debug("Scrolled back to the original x, y position of the page after scraping", x=x, y=y)

    id_to_css_dict, id_to_element_dict, id_to_frame_dict, id_to_element_hash, hash_to_element_ids = build_element_dict(
        elements
    )

    # if there are no elements, fail the scraping unless support_empty_page is True
    if not elements and not support_empty_page:
        raise NoElementFound()

    text_content = await get_frame_text(page.main_frame)

    html = ""
    window_dimension = None
    try:
        skyvern_frame = await SkyvernFrame.create_instance(frame=page)
        html = await skyvern_frame.get_content()
        if page.viewport_size:
            window_dimension = Resolution(width=page.viewport_size["width"], height=page.viewport_size["height"])
    except Exception:
        LOG.error(
            "Failed out to get HTML content",
            url=url,
            exc_info=True,
        )

    return ScrapedPage(
        elements=elements,
        id_to_css_dict=id_to_css_dict,
        id_to_element_dict=id_to_element_dict,
        id_to_frame_dict=id_to_frame_dict,
        id_to_element_hash=id_to_element_hash,
        hash_to_element_ids=hash_to_element_ids,
        element_tree=element_tree,
        element_tree_trimmed=element_tree_trimmed,
        screenshots=screenshots,
        url=url,
        html=html,
        extracted_text=text_content,
        window_dimension=window_dimension,
        _browser_state=browser_state,
        _clean_up_func=cleanup_element_tree,
        _scrape_exclude=scrape_exclude,
    )


async def get_all_children_frames(page: Page) -> list[Frame]:
    start_index = 0
    frames = page.main_frame.child_frames

    while start_index < len(frames):
        frame = frames[start_index]
        start_index += 1
        frames.extend(frame.child_frames)

    return frames


async def filter_frames(frames: list[Frame], scrape_exclude: ScrapeExcludeFunc | None = None) -> list[Frame]:
    filtered_frames = []
    for frame in frames:
        if frame.is_detached():
            continue

        if scrape_exclude is not None and await scrape_exclude(frame.page, frame):
            continue

        filtered_frames.append(frame)
    return filtered_frames


async def add_frame_interactable_elements(
    frame: Frame,
    frame_index: int,
    elements: list[dict],
    element_tree: list[dict],
) -> tuple[list[dict], list[dict]]:
    """
    Add the interactable element of the frame to the elements and element_tree.
    """
    try:
        frame_element = await frame.frame_element()
        # it will get stuck when we `frame.evaluate()` on an invisible iframe
        if not await frame_element.is_visible():
            return elements, element_tree
        skyvern_id = await frame_element.get_attribute(SKYVERN_ID_ATTR)
        if not skyvern_id:
            LOG.info(
                "No Skyvern id found for frame, skipping",
                frame_index=frame_index,
                attr=SKYVERN_ID_ATTR,
            )
            return elements, element_tree
    except Exception:
        LOG.warning(
            "Unable to get Skyvern id from frame_element",
            attr=SKYVERN_ID_ATTR,
            exc_info=True,
        )
        return elements, element_tree

    skyvern_frame = await SkyvernFrame.create_instance(frame)
    await skyvern_frame.safe_wait_for_animation_end()

    frame_elements, frame_element_tree = await skyvern_frame.build_tree_from_body(
        frame_name=skyvern_id, frame_index=frame_index
    )

    for element in elements:
        if element["id"] == skyvern_id:
            element["children"] = frame_element_tree

    elements = elements + frame_elements

    return elements, element_tree


@TraceManager.traced_async(ignore_input=True)
async def get_interactable_element_tree(
    page: Page,
    scrape_exclude: ScrapeExcludeFunc | None = None,
) -> tuple[list[dict], list[dict]]:
    """
    Get the element tree of the page, including all the elements that are interactable.
    :param page: Page instance to get the element tree from.
    :return: Tuple containing the element tree and a map of element IDs to elements.
    """
    # main page index is 0
    skyvern_page = await SkyvernFrame.create_instance(page)
    elements, element_tree = await skyvern_page.build_tree_from_body(frame_name="main.frame", frame_index=0)

    context = skyvern_context.ensure_context()
    frames = await get_all_children_frames(page)
    frames = await filter_frames(frames, scrape_exclude)

    for frame in frames:
        frame_index = context.frame_index_map.get(frame, None)
        if frame_index is None:
            frame_index = len(context.frame_index_map) + 1
            context.frame_index_map[frame] = frame_index

    for frame in frames:
        frame_index = context.frame_index_map[frame]
        elements, element_tree = await add_frame_interactable_elements(
            frame,
            frame_index,
            elements,
            element_tree,
        )

    return elements, element_tree


class IncrementalScrapePage(ElementTreeBuilder):
    def __init__(self, skyvern_frame: SkyvernFrame) -> None:
        self.id_to_element_dict: dict[str, dict] = dict()
        self.id_to_css_dict: dict[str, str] = dict()
        self.elements: list[dict] = list()
        self.element_tree: list[dict] = list()
        self.element_tree_trimmed: list[dict] = list()
        self.skyvern_frame = skyvern_frame

    def set_element_tree_trimmed(self, element_tree_trimmed: list[dict]) -> None:
        self.element_tree_trimmed = element_tree_trimmed

    def check_id_in_page(self, element_id: str) -> bool:
        css_selector = self.id_to_css_dict.get(element_id, "")
        if css_selector:
            return True
        return False

    @TraceManager.traced_async(ignore_input=True)
    async def get_incremental_element_tree(
        self,
        cleanup_element_tree: CleanupElementTreeFunc,
    ) -> list[dict]:
        frame = self.skyvern_frame.get_frame()

        try:
            incremental_elements, incremental_tree = await self.skyvern_frame.get_incremental_element_tree(
                wait_until_finished=True
            )
        except TimeoutError:
            LOG.warning(
                "Timeout to get incremental elements with wait_until_finished, going to get incremental elements without waiting",
            )
            incremental_elements, incremental_tree = await self.skyvern_frame.get_incremental_element_tree(
                wait_until_finished=False
            )

        # we listen the incremental elements seperated by frames, so all elements will be in the same SkyvernFrame
        self.id_to_css_dict, self.id_to_element_dict, _, _, _ = build_element_dict(incremental_elements)

        self.elements = incremental_elements

        incremental_tree = await cleanup_element_tree(frame, frame.url, copy.deepcopy(incremental_tree))
        trimmed_element_tree = trim_element_tree(copy.deepcopy(incremental_tree))

        self.element_tree = incremental_tree
        self.element_tree_trimmed = trimmed_element_tree

        return self.element_tree_trimmed

    async def start_listen_dom_increment(self, element: ElementHandle | None = None) -> None:
        js_script = "async (element) => await startGlobalIncrementalObserver(element)"
        await SkyvernFrame.evaluate(frame=self.skyvern_frame.get_frame(), expression=js_script, arg=element)

    async def stop_listen_dom_increment(self) -> None:
        # check if the DOM has navigated away or refreshed
        js_script = "() => window.globalObserverForDOMIncrement === undefined"
        if await SkyvernFrame.evaluate(frame=self.skyvern_frame.get_frame(), expression=js_script):
            return
        js_script = "async () => await stopGlobalIncrementalObserver()"
        await SkyvernFrame.evaluate(
            frame=self.skyvern_frame.get_frame(),
            expression=js_script,
            timeout_ms=SettingsManager.get_settings().BROWSER_SCRAPING_BUILDING_ELEMENT_TREE_TIMEOUT_MS,
        )

    async def get_incremental_elements_num(self) -> int:
        # check if the DOM has navigated away or refreshed
        js_script = "() => window.globalOneTimeIncrementElements === undefined"
        if await SkyvernFrame.evaluate(frame=self.skyvern_frame.get_frame(), expression=js_script):
            return 0

        js_script = "() => window.globalOneTimeIncrementElements.length"
        return await SkyvernFrame.evaluate(frame=self.skyvern_frame.get_frame(), expression=js_script)

    async def __validate_element_by_value(self, value: str, element: dict) -> tuple[Locator | None, bool]:
        """
        Locator: the locator of the matched element. None if no valid element to interact;
        bool: is_matched. True, found an intercatable alternative one; False, not found  any alternative;

        If is_matched is True, but Locator is None. It means the value is matched, but the current element is non-interactable
        """

        interactable = element.get("interactable", False)
        element_id = element.get("id", "")

        parent_locator: Locator | None = None
        if element_id:
            parent_locator = self.skyvern_frame.get_frame().locator(f'[{SKYVERN_ID_ATTR}="{element_id}"]')

        # DFS to validate the children first:
        # if the child element matched and is interactable, return the child node directly
        # if the child element matched value but not interactable, try to interact with the parent node
        children = element.get("children", [])
        for child in children:
            child_locator, is_match = await self.__validate_element_by_value(value, child)
            if is_match:
                if child_locator:
                    return child_locator, True
                if interactable and parent_locator and await parent_locator.count() > 0:
                    return parent_locator, True
                return None, True

        if not parent_locator:
            return None, False

        text = element.get("text", "")
        if text != value:
            return None, False

        if await parent_locator.count() == 0:
            return None, False

        if not interactable:
            LOG.debug("Find the target element by text, but the element is not interactable", text=text)
            return None, True

        return parent_locator, True

    async def select_one_element_by_value(self, value: str) -> Locator | None:
        for element in self.element_tree:
            locator, _ = await self.__validate_element_by_value(value=value, element=element)
            if locator:
                return locator
        return None

    def build_html_tree(self, element_tree: list[dict] | None = None, need_skyvern_attrs: bool = True) -> str:
        return "".join(
            [
                json_to_html(element, need_skyvern_attrs=need_skyvern_attrs)
                for element in (element_tree or self.element_tree_trimmed)
            ]
        )

    def support_economy_elements_tree(self) -> bool:
        return False

    def build_element_tree(
        self, fmt: ElementTreeFormat = ElementTreeFormat.HTML, html_need_skyvern_attrs: bool = True
    ) -> str:
        if fmt == ElementTreeFormat.HTML:
            return self.build_html_tree(
                element_tree=self.element_tree_trimmed, need_skyvern_attrs=html_need_skyvern_attrs
            )
        if fmt == ElementTreeFormat.JSON:
            return json.dumps(self.element_tree_trimmed)

        raise UnknownElementTreeFormat(fmt=fmt)

    def build_economy_elements_tree(
        self,
        fmt: ElementTreeFormat = ElementTreeFormat.HTML,
        html_need_skyvern_attrs: bool = True,
        percent_to_keep: float = 1,
    ) -> str:
        raise NotImplementedError("Not implemented")


def _should_keep_unique_id(element: dict) -> bool:
    # case where we shouldn't keep unique_id
    # 1. no readonly attr and not disable attr and no interactable
    # 2. readonly=false and disable=false and interactable=false

    if element.get("hoverOnly"):
        return True

    attributes = element.get("attributes", {})
    if (
        "disabled" not in attributes
        and "aria-disabled" not in attributes
        and "readonly" not in attributes
        and "aria-readonly" not in attributes
    ):
        return element.get("interactable", False)

    disabled = attributes.get("disabled")
    aria_disabled = attributes.get("aria-disabled")
    readonly = attributes.get("readonly")
    aria_readonly = attributes.get("aria-readonly")
    if disabled or aria_disabled or readonly or aria_readonly:
        return True
    return element.get("interactable", False)


def trim_element(element: dict) -> dict:
    queue = [element]
    while queue:
        queue_ele = queue.pop(0)
        if "frame" in queue_ele:
            del queue_ele["frame"]

        if "frame_index" in queue_ele:
            del queue_ele["frame_index"]

        if "id" in queue_ele and not _should_keep_unique_id(queue_ele):
            del queue_ele["id"]

        if "attributes" in queue_ele:
            new_attributes = _trimmed_base64_data(queue_ele["attributes"])
            if new_attributes:
                queue_ele["attributes"] = new_attributes
            else:
                del queue_ele["attributes"]

        if "attributes" in queue_ele and not queue_ele.get("keepAllAttr", False):
            new_attributes = _trimmed_attributes(queue_ele["attributes"])
            if new_attributes:
                queue_ele["attributes"] = new_attributes
            else:
                del queue_ele["attributes"]
        # remove the tag, don't need it in the HTML tree
        if "keepAllAttr" in queue_ele:
            del queue_ele["keepAllAttr"]

        if "children" in queue_ele:
            queue.extend(queue_ele["children"])
            if not queue_ele["children"]:
                del queue_ele["children"]
        if "text" in queue_ele:
            element_text = str(queue_ele["text"]).strip()
            if not element_text:
                del queue_ele["text"]

        if (
            "attributes" in queue_ele
            and "name" in queue_ele["attributes"]
            and len(queue_ele["attributes"]["name"]) > 500
        ):
            queue_ele["attributes"]["name"] = queue_ele["attributes"]["name"][:500]

        if "beforePseudoText" in queue_ele and not queue_ele.get("beforePseudoText"):
            del queue_ele["beforePseudoText"]

        if "afterPseudoText" in queue_ele and not queue_ele.get("afterPseudoText"):
            del queue_ele["afterPseudoText"]

    return element


def trim_element_tree(elements: list[dict]) -> list[dict]:
    for element in elements:
        trim_element(element)
    return elements


def _trimmed_base64_data(attributes: dict) -> dict:
    new_attributes: dict = {}

    for key in attributes:
        if key in BASE64_INCLUDE_ATTRIBUTES and "data:" in attributes.get(key, ""):
            continue
        new_attributes[key] = attributes[key]

    return new_attributes


def _trimmed_attributes(attributes: dict) -> dict:
    new_attributes: dict = {}

    for key in attributes:
        if key == "role" and attributes[key] in ["listbox", "option"]:
            new_attributes[key] = attributes[key]
        if key in RESERVED_ATTRIBUTES:
            new_attributes[key] = attributes[key]

    return new_attributes


def _remove_unique_id(element: dict) -> None:
    if "attributes" not in element:
        return
    if SKYVERN_ID_ATTR in element["attributes"]:
        del element["attributes"][SKYVERN_ID_ATTR]


def _build_element_links(elements: list[dict]) -> None:
    """
    Build the links for listbox. A listbox could be mapped back to another element if:
        1. The listbox element's text matches context or text of an element
    """
    # first, build mapping between text/context and elements
    text_to_elements_map: dict[str, list[dict]] = defaultdict(list)
    context_to_elements_map: dict[str, list[dict]] = defaultdict(list)
    for element in elements:
        if "text" in element:
            text_to_elements_map[element["text"]].append(element)
        if "context" in element:
            context_to_elements_map[element["context"]].append(element)

    # then, build the links from element to listbox elements
    for element in elements:
        if not (
            "attributes" in element and "role" in element["attributes"] and "listbox" == element["attributes"]["role"]
        ):
            continue
        listbox_text = element["text"] if "text" in element else ""

        # WARNING: If a listbox has really little commont content (yes/no, etc.),
        #   it might have conflict and will connect to wrong element
        # if len(listbox_text) < 10:
        #     # do not support small listbox text for now as it's error proning. larger text match is more reliable
        #     LOG.info("Skip because too short listbox text", listbox_text=listbox_text)
        #     continue

        for text, linked_elements in text_to_elements_map.items():
            if listbox_text in text:
                for linked_element in linked_elements:
                    if linked_element["id"] != element["id"]:
                        LOG.info(
                            "Match listbox to target element text",
                            listbox_text=listbox_text,
                            text=text,
                            listbox_id=element["id"],
                            linked_element_id=linked_element["id"],
                        )
                        linked_element["linked_element"] = element["id"]

        for context, linked_elements in context_to_elements_map.items():
            if listbox_text in context:
                for linked_element in linked_elements:
                    if linked_element["id"] != element["id"]:
                        LOG.info(
                            "Match listbox to target element context",
                            listbox_text=listbox_text,
                            context=context,
                            listbox_id=element["id"],
                            linked_element_id=linked_element["id"],
                        )
                        linked_element["linked_element"] = element["id"]
