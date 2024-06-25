import asyncio
import copy
import json
from collections import defaultdict
from enum import StrEnum
from typing import Any, Awaitable, Callable

import structlog
from playwright.async_api import Frame, Page
from pydantic import BaseModel

from skyvern.constants import SKYVERN_DIR, SKYVERN_ID_ATTR
from skyvern.exceptions import FailedToTakeScreenshot, UnknownElementTreeFormat
from skyvern.forge.sdk.settings_manager import SettingsManager
from skyvern.webeye.browser_factory import BrowserState

LOG = structlog.get_logger()

RESERVED_ATTRIBUTES = {
    "accept",  # for input file
    "alt",
    "aria-checked",  # for option tag
    "aria-current",
    "aria-label",
    "aria-required",
    "aria-role",
    "aria-selected",  # for option tag
    "checked",
    "data-original-title",  # for bootstrap tooltip
    "data-ui",
    "for",
    "href",  # For a tags
    "maxlength",
    "name",
    "pattern",
    "placeholder",
    "readonly",
    "required",
    "selected",  # for option tag
    "src",  # do we need this?
    "text-value",
    "title",
    "type",
    "value",
}

ELEMENT_NODE_ATTRIBUTES = {
    "id",
}


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


# function to convert JSON element to HTML
def build_attribute(key: str, value: Any) -> str:
    if isinstance(value, bool) or isinstance(value, int):
        return f'{key}="{str(value).lower()}"'

    return f'{key}="{str(value)}"' if value else key


def json_to_html(element: dict) -> str:
    attributes: dict[str, Any] = copy.deepcopy(element.get("attributes", {}))

    # adding the node attribute to attributes
    for attr in ELEMENT_NODE_ATTRIBUTES:
        value = element.get(attr)
        if value is None:
            continue
        attributes[attr] = value

    attributes_html = " ".join(build_attribute(key, value) for key, value in attributes.items())

    tag = element["tagName"]
    text = element.get("text", "")
    # build children HTML
    children_html = "".join(json_to_html(child) for child in element.get("children", []))
    # build option HTML
    option_html = "".join(
        f'<option index="{option.get("optionIndex")}">{option.get("text")}</option>'
        for option in element.get("options", [])
    )

    # Check if the element is self-closing
    if tag in ["img", "input", "br", "hr", "meta", "link"]:
        return f'<{tag}{attributes_html if not attributes_html else " "+attributes_html}>'
    else:
        return f'<{tag}{attributes_html if not attributes_html else " "+attributes_html}>{text}{children_html+option_html}</{tag}>'


class ElementTreeFormat(StrEnum):
    JSON = "json"
    HTML = "html"


class ScrapedPage(BaseModel):
    """
    Scraped response from a webpage, including:
    1. List of elements
    2. ID to xpath map
    3. The element tree of the page (list of dicts). Each element has children and attributes.
    4. The screenshot (base64 encoded)
    5. The URL of the page
    6. The HTML of the page
    7. The extracted text from the page
    """

    elements: list[dict]
    id_to_element_dict: dict[str, dict] = {}
    id_to_frame_dict: dict[str, str] = {}
    id_to_xpath_dict: dict[str, str]
    element_tree: list[dict]
    element_tree_trimmed: list[dict]
    screenshots: list[bytes]
    url: str
    html: str
    extracted_text: str | None = None

    def build_element_tree(self, fmt: ElementTreeFormat = ElementTreeFormat.JSON) -> str:
        if fmt == ElementTreeFormat.JSON:
            return json.dumps(self.element_tree_trimmed)

        if fmt == ElementTreeFormat.HTML:
            return "".join(json_to_html(element) for element in self.element_tree_trimmed)

        raise UnknownElementTreeFormat(fmt=fmt)


async def scrape_website(
    browser_state: BrowserState,
    url: str,
    num_retry: int = 0,
    scrape_exclude: Callable[[Page, Frame], Awaitable[bool]] | None = None,
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
        return await scrape_web_unsafe(browser_state, url, scrape_exclude)
    except Exception as e:
        # NOTE: MAX_SCRAPING_RETRIES is set to 0 in both staging and production
        if num_retry > SettingsManager.get_settings().MAX_SCRAPING_RETRIES:
            LOG.error(
                "Scraping failed after max retries, aborting.",
                max_retries=SettingsManager.get_settings().MAX_SCRAPING_RETRIES,
                url=url,
                exc_info=True,
            )
            if isinstance(e, FailedToTakeScreenshot):
                raise e
            else:
                raise Exception("Scraping failed.")
        LOG.info("Scraping failed, will retry", num_retry=num_retry, url=url)
        return await scrape_website(
            browser_state,
            url,
            num_retry=num_retry,
            scrape_exclude=scrape_exclude,
        )


async def get_frame_text(iframe: Frame) -> str:
    """
    Get all the visible text in the iframe.
    :param iframe: Frame instance to get the text from.
    :return: All the visible text from the iframe.
    """
    js_script = "() => document.body.innerText"

    try:
        text = await iframe.evaluate(js_script)
    except Exception:
        LOG.warning(
            "failed to get text from iframe",
            exc_info=True,
        )
        return ""

    for child_frame in iframe.child_frames:
        if child_frame.is_detached():
            continue

        text += await get_frame_text(child_frame)

    return text


async def scrape_web_unsafe(
    browser_state: BrowserState,
    url: str,
    scrape_exclude: Callable[[Page, Frame], Awaitable[bool]] | None = None,
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
    # We only create a new page if one does not exist. This is to allow keeping the same page since we want to
    # continue working on the same page that we're taking actions on.
    # *This also means URL is only used when creating a new page, and not when using an existing page.
    page = await browser_state.get_or_create_page(url)
    # Take screenshots of the page with the bounding boxes. We will remove the bounding boxes later.
    # Scroll to the top of the page and take a screenshot.
    # Scroll to the next page and take a screenshot until we reach the end of the page.
    # We check if the scroll_y_px_old is the same as scroll_y_px to determine if we have reached the end of the page.
    # This also solves the issue where we can't scroll due to a popup.(e.g. geico first popup on the homepage after
    # clicking start my quote)

    LOG.info("Waiting for 5 seconds before scraping the website.")
    await asyncio.sleep(5)

    screenshots: list[bytes] = []
    scroll_y_px_old = -30.0
    scroll_y_px = await scroll_to_top(page, drow_boxes=True)
    # Checking max number of screenshots to prevent infinite loop
    # We are checking the difference between the old and new scroll_y_px to determine if we have reached the end of the
    # page. If the difference is less than 25, we assume we have reached the end of the page.
    while (
        abs(scroll_y_px_old - scroll_y_px) > 25
        and len(screenshots) < SettingsManager.get_settings().MAX_NUM_SCREENSHOTS
    ):
        screenshot = await browser_state.take_screenshot(full_page=False)
        screenshots.append(screenshot)
        scroll_y_px_old = scroll_y_px
        LOG.info("Scrolling to next page", url=url, num_screenshots=len(screenshots))
        scroll_y_px = await scroll_to_next_page(page, drow_boxes=True)
        LOG.info(
            "Scrolled to next page",
            scroll_y_px=scroll_y_px,
            scroll_y_px_old=scroll_y_px_old,
        )
    await remove_bounding_boxes(page)
    await scroll_to_top(page, drow_boxes=False)

    elements, element_tree = await get_interactable_element_tree(page, scrape_exclude)
    element_tree = cleanup_elements(copy.deepcopy(element_tree))

    _build_element_links(elements)

    id_to_xpath_dict = {}
    id_to_element_dict = {}
    id_to_frame_dict = {}

    for element in elements:
        element_id = element["id"]
        # get_interactable_element_tree marks each interactable element with a unique_id attribute
        id_to_xpath_dict[element_id] = f"//*[@{SKYVERN_ID_ATTR}='{element_id}']"
        id_to_element_dict[element_id] = element
        id_to_frame_dict[element_id] = element["frame"]

    text_content = await get_frame_text(page.main_frame)

    return ScrapedPage(
        elements=elements,
        id_to_xpath_dict=id_to_xpath_dict,
        id_to_element_dict=id_to_element_dict,
        id_to_frame_dict=id_to_frame_dict,
        element_tree=element_tree,
        element_tree_trimmed=trim_element_tree(copy.deepcopy(element_tree)),
        screenshots=screenshots,
        url=page.url,
        html=await page.content(),
        extracted_text=text_content,
    )


async def get_select2_options(page: Page) -> list[dict[str, Any]]:
    await page.evaluate(JS_FUNCTION_DEFS)
    js_script = "async () => await getSelect2Options()"
    return await page.evaluate(js_script)


async def get_interactable_element_tree_in_frame(
    frames: list[Frame],
    elements: list[dict],
    element_tree: list[dict],
    scrape_exclude: Callable[[Page, Frame], Awaitable[bool]] | None = None,
) -> tuple[list[dict], list[dict]]:
    for frame in frames:
        if frame.is_detached():
            continue

        if scrape_exclude is not None and await scrape_exclude(frame.page, frame):
            continue

        try:
            frame_element = await frame.frame_element()
        except Exception:
            LOG.warning(
                "Unable to get frame_element",
                exc_info=True,
            )
            continue

        unique_id = await frame_element.get_attribute("unique_id")

        frame_js_script = f"async () => await buildTreeFromBody('{unique_id}', true)"

        await frame.evaluate(JS_FUNCTION_DEFS)
        frame_elements, frame_element_tree = await frame.evaluate(frame_js_script)

        if len(frame.child_frames) > 0:
            frame_elements, frame_element_tree = await get_interactable_element_tree_in_frame(
                frame.child_frames,
                frame_elements,
                frame_element_tree,
                scrape_exclude=scrape_exclude,
            )

        for element in elements:
            if element["id"] == unique_id:
                element["children"] = frame_elements

        for element_tree_item in element_tree:
            if element_tree_item["id"] == unique_id:
                element_tree_item["children"] = frame_element_tree

        elements = elements + frame_elements

    return elements, element_tree


async def get_interactable_element_tree(
    page: Page,
    scrape_exclude: Callable[[Page, Frame], Awaitable[bool]] | None = None,
) -> tuple[list[dict], list[dict]]:
    """
    Get the element tree of the page, including all the elements that are interactable.
    :param page: Page instance to get the element tree from.
    :return: Tuple containing the element tree and a map of element IDs to elements.
    """
    await page.evaluate(JS_FUNCTION_DEFS)
    main_frame_js_script = "async () => await buildTreeFromBody('main.frame', true)"
    elements, element_tree = await page.evaluate(main_frame_js_script)

    if len(page.main_frame.child_frames) > 0:
        elements, element_tree = await get_interactable_element_tree_in_frame(
            page.main_frame.child_frames,
            elements,
            element_tree,
            scrape_exclude=scrape_exclude,
        )

    return elements, element_tree


async def scroll_to_top(page: Page, drow_boxes: bool) -> float:
    """
    Scroll to the top of the page and take a screenshot.
    :param drow_boxes: If True, draw bounding boxes around the elements.
    :param page: Page instance to take the screenshot from.
    :return: Screenshot of the page.
    """
    await page.evaluate(JS_FUNCTION_DEFS)
    js_script = f"async () => await scrollToTop({str(drow_boxes).lower()})"
    scroll_y_px = await page.evaluate(js_script)
    return scroll_y_px


async def scroll_to_next_page(page: Page, drow_boxes: bool) -> bool:
    """
    Scroll to the next page and take a screenshot.
    :param drow_boxes: If True, draw bounding boxes around the elements.
    :param page: Page instance to take the screenshot from.
    :return: Screenshot of the page.
    """
    await page.evaluate(JS_FUNCTION_DEFS)
    js_script = f"async () => await scrollToNextPage({str(drow_boxes).lower()})"
    scroll_y_px = await page.evaluate(js_script)
    return scroll_y_px


async def remove_bounding_boxes(page: Page) -> None:
    """
    Remove the bounding boxes from the page.
    :param page: Page instance to remove the bounding boxes from.
    """
    js_script = "() => removeBoundingBoxes()"
    await page.evaluate(js_script)


def cleanup_elements(elements: list[dict]) -> list[dict]:
    """
    Remove rect and attribute.unique_id from the elements.
    The reason we're doing it is to
    1. reduce unnecessary data so that llm get less distrction
    # TODO later: 2. reduce tokens sent to llm to save money
    :param elements: List of elements to remove xpaths from.
    :return: List of elements without xpaths.
    """
    queue = []
    for element in elements:
        queue.append(element)
    while queue:
        queue_ele = queue.pop(0)
        _remove_rect(queue_ele)
        # TODO: we can come back to test removing the unique_id
        # from element attributes to make sure this won't increase hallucination
        # _remove_unique_id(queue_ele)
        if "children" in queue_ele:
            queue.extend(queue_ele["children"])
    return elements


def trim_element_tree(elements: list[dict]) -> list[dict]:
    queue = []
    for element in elements:
        queue.append(element)
    while queue:
        queue_ele = queue.pop(0)
        if "frame" in queue_ele:
            del queue_ele["frame"]

        if not queue_ele.get("interactable"):
            del queue_ele["id"]

        if "attributes" in queue_ele and not queue_ele.get("keepAllAttr", False):
            tag_name = queue_ele["tagName"] if "tagName" in queue_ele else ""
            new_attributes = _trimmed_attributes(tag_name, queue_ele["attributes"])
            if new_attributes:
                queue_ele["attributes"] = new_attributes
            else:
                del queue_ele["attributes"]
        # remove the tag, don't need it in the HTML tree
        del queue_ele["keepAllAttr"]

        if "children" in queue_ele:
            queue.extend(queue_ele["children"])
            if not queue_ele["children"]:
                del queue_ele["children"]
        if "text" in queue_ele:
            element_text = str(queue_ele["text"]).strip()
            if not element_text:
                del queue_ele["text"]
    return elements


def _trimmed_attributes(tag_name: str, attributes: dict) -> dict:
    new_attributes: dict = {}
    for key in attributes:
        if key == "id" and tag_name in ["input", "textarea", "select"]:
            # We don't want to remove the id attribute any of these elements in case there's a label for it
            new_attributes[key] = attributes[key]
        if key == "role" and attributes[key] in ["listbox", "option"]:
            new_attributes[key] = attributes[key]
        if key in RESERVED_ATTRIBUTES and attributes[key]:
            new_attributes[key] = attributes[key]
    return new_attributes


def _remove_rect(element: dict) -> None:
    if "rect" in element:
        del element["rect"]


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
