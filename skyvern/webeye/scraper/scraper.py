import asyncio
import copy
import json
from collections import defaultdict
from enum import StrEnum
from typing import Any, Awaitable, Callable, Self

import structlog
from playwright.async_api import Frame, Locator, Page
from pydantic import BaseModel, PrivateAttr

from skyvern.config import settings
from skyvern.constants import BUILDING_ELEMENT_TREE_TIMEOUT_MS, SKYVERN_DIR, SKYVERN_ID_ATTR
from skyvern.exceptions import FailedToTakeScreenshot, UnknownElementTreeFormat
from skyvern.forge.sdk.api.crypto import calculate_sha256
from skyvern.forge.sdk.core import skyvern_context
from skyvern.webeye.browser_factory import BrowserState
from skyvern.webeye.utils.page import SkyvernFrame

LOG = structlog.get_logger()
CleanupElementTreeFunc = Callable[[Page | Frame, str, list[dict]], Awaitable[list[dict]]]
ScrapeExcludeFunc = Callable[[Page, Frame], Awaitable[bool]]

RESERVED_ATTRIBUTES = {
    "accept",  # for input file
    "alt",
    "shape-description",  # for css shape
    "aria-checked",  # for option tag
    "aria-current",
    "aria-label",
    "aria-required",
    "aria-role",
    "aria-selected",  # for option tag
    "checked",
    "data-original-title",  # for bootstrap tooltip
    "data-ui",
    "disabled",  # for button
    "aria-disabled",
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

BASE64_INCLUDE_ATTRIBUTES = {
    "href",
    "src",
    "poster",
    "srcset",
    "icon",
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


def json_to_html(element: dict, need_skyvern_attrs: bool = True) -> str:
    """
    if element is flagged as dropped, the html format is empty
    """
    tag = element["tagName"]
    attributes: dict[str, Any] = copy.deepcopy(element.get("attributes", {}))

    interactable = element.get("interactable", False)
    if element.get("isDropped", False):
        if not interactable:
            return ""
        else:
            LOG.info("Element is interactable. Trimmed all attributes instead of dropping it", element=element)
            attributes = {}

    if element.get("isCheckable", False) and tag != "input":
        tag = "input"
        attributes["type"] = "checkbox"

    context = skyvern_context.ensure_context()

    # FIXME: Theoretically, all href links with over 69(64+1+4) length could be hashed
    # but currently, just hash length>150 links to confirm the solution goes well
    if "href" in attributes and len(attributes.get("href", "")) > 150:
        href = attributes.get("href", "")
        # jinja style can't accept the variable name starts with number
        # adding "_" to make sure the variable name is valid.
        hashed_href = "_" + calculate_sha256(href)
        context.hashed_href_map[hashed_href] = href
        attributes["href"] = "{{" + hashed_href + "}}"

    if need_skyvern_attrs:
        # adding the node attribute to attributes
        for attr in ELEMENT_NODE_ATTRIBUTES:
            value = element.get(attr)
            if value is None:
                continue
            attributes[attr] = value

    attributes_html = " ".join(build_attribute(key, value) for key, value in attributes.items())

    if element.get("isSelectable", False):
        tag = "select"

    text = element.get("text", "")
    # build children HTML
    children_html = "".join(
        json_to_html(child, need_skyvern_attrs=need_skyvern_attrs) for child in element.get("children", [])
    )
    # build option HTML
    option_html = "".join(
        f'<option index="{option.get("optionIndex")}">{option.get("text")}</option>'
        for option in element.get("options", [])
    )

    if element.get("purgeable", False):
        return children_html + option_html

    before_pseudo_text = element.get("beforePseudoText") or ""
    after_pseudo_text = element.get("afterPseudoText") or ""

    # Check if the element is self-closing
    if (
        tag in ["img", "input", "br", "hr", "meta", "link"]
        and not option_html
        and not children_html
        and not before_pseudo_text
        and not after_pseudo_text
    ):
        return f"<{tag}{attributes_html if not attributes_html else ' ' + attributes_html}>"
    else:
        return f"<{tag}{attributes_html if not attributes_html else ' ' + attributes_html}>{before_pseudo_text}{text}{children_html + option_html}{after_pseudo_text}</{tag}>"


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
        # get_interactable_element_tree marks each interactable element with a unique_id attribute
        id_to_css_dict[element_id] = f"[{SKYVERN_ID_ATTR}='{element_id}']"
        id_to_element_dict[element_id] = element
        id_to_frame_dict[element_id] = element["frame"]
        element_hash = hash_element(element)
        id_to_element_hash[element_id] = element_hash
        hash_to_element_ids[element_hash] = hash_to_element_ids.get(element_hash, []) + [element_id]

    return id_to_css_dict, id_to_element_dict, id_to_frame_dict, id_to_element_hash, hash_to_element_ids


class ElementTreeFormat(StrEnum):
    JSON = "json"  # deprecate JSON format soon. please use HTML format
    HTML = "html"


class ScrapedPage(BaseModel):
    """
    Scraped response from a webpage, including:
    1. List of elements
    2. ID to css map
    3. The element tree of the page (list of dicts). Each element has children and attributes.
    4. The screenshot (base64 encoded)
    5. The URL of the page
    6. The HTML of the page
    7. The extracted text from the page
    """

    elements: list[dict]
    id_to_element_dict: dict[str, dict] = {}
    id_to_frame_dict: dict[str, str] = {}
    id_to_css_dict: dict[str, str]
    id_to_element_hash: dict[str, str]
    hash_to_element_ids: dict[str, list[str]]
    element_tree: list[dict]
    element_tree_trimmed: list[dict]
    screenshots: list[bytes]
    url: str
    html: str
    extracted_text: str | None = None

    _browser_state: BrowserState = PrivateAttr()
    _clean_up_func: CleanupElementTreeFunc = PrivateAttr()
    _scrape_exclude: ScrapeExcludeFunc | None = PrivateAttr(default=None)

    def __init__(self, **data: Any) -> None:
        missing_attrs = [attr for attr in ["_browser_state", "_clean_up_func"] if attr not in data]
        if len(missing_attrs) > 0:
            raise ValueError(f"Missing required private attributes: {', '.join(missing_attrs)}")

        # popup private attributes
        browser_state = data.pop("_browser_state")
        clean_up_func = data.pop("_clean_up_func")
        scrape_exclude = data.pop("_scrape_exclude")

        super().__init__(**data)

        self._browser_state = browser_state
        self._clean_up_func = clean_up_func
        self._scrape_exclude = scrape_exclude

    def build_element_tree(
        self, fmt: ElementTreeFormat = ElementTreeFormat.HTML, html_need_skyvern_attrs: bool = True
    ) -> str:
        if fmt == ElementTreeFormat.JSON:
            return json.dumps(self.element_tree_trimmed)

        if fmt == ElementTreeFormat.HTML:
            return "".join(
                json_to_html(element, need_skyvern_attrs=html_need_skyvern_attrs)
                for element in self.element_tree_trimmed
            )

        raise UnknownElementTreeFormat(fmt=fmt)

    async def refresh(self) -> Self:
        refreshed_page = await scrape_website(
            browser_state=self._browser_state,
            url=self.url,
            cleanup_element_tree=self._clean_up_func,
            scrape_exclude=self._scrape_exclude,
        )
        self.elements = refreshed_page.elements
        self.id_to_css_dict = refreshed_page.id_to_css_dict
        self.id_to_element_dict = refreshed_page.id_to_element_dict
        self.id_to_frame_dict = refreshed_page.id_to_frame_dict
        self.id_to_element_hash = refreshed_page.id_to_element_hash
        self.hash_to_element_ids = refreshed_page.hash_to_element_ids
        self.element_tree = refreshed_page.element_tree
        self.element_tree_trimmed = refreshed_page.element_tree_trimmed
        self.screenshots = refreshed_page.screenshots or self.screenshots
        self.html = refreshed_page.html
        self.extracted_text = refreshed_page.extracted_text
        self.url = refreshed_page.url
        return self

    async def generate_scraped_page_without_screenshots(self) -> Self:
        return await scrape_website(
            browser_state=self._browser_state,
            url=self.url,
            cleanup_element_tree=self._clean_up_func,
            scrape_exclude=self._scrape_exclude,
            take_screenshots=False,
        )


async def scrape_website(
    browser_state: BrowserState,
    url: str,
    cleanup_element_tree: CleanupElementTreeFunc,
    num_retry: int = 0,
    scrape_exclude: ScrapeExcludeFunc | None = None,
    take_screenshots: bool = True,
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
        )
    except Exception as e:
        # NOTE: MAX_SCRAPING_RETRIES is set to 0 in both staging and production
        if num_retry > settings.MAX_SCRAPING_RETRIES:
            LOG.error(
                "Scraping failed after max retries, aborting.",
                max_retries=settings.MAX_SCRAPING_RETRIES,
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
            cleanup_element_tree,
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

    LOG.info("Waiting for 5 seconds before scraping the website.")
    await asyncio.sleep(5)

    screenshots = []
    if take_screenshots:
        screenshots = await SkyvernFrame.take_split_screenshots(page=page, url=url, draw_boxes=True)

    elements, element_tree = await get_interactable_element_tree(page, scrape_exclude)
    element_tree = await cleanup_element_tree(page, url, copy.deepcopy(element_tree))

    id_to_css_dict, id_to_element_dict, id_to_frame_dict, id_to_element_hash, hash_to_element_ids = build_element_dict(
        elements
    )

    # if there are no elements, fail the scraping
    if not elements:
        raise Exception("No elements found on the page")

    text_content = await get_frame_text(page.main_frame)

    html = ""
    try:
        skyvern_frame = await SkyvernFrame.create_instance(frame=page)
        html = await skyvern_frame.get_content()
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
        element_tree_trimmed=trim_element_tree(copy.deepcopy(element_tree)),
        screenshots=screenshots,
        url=page.url,
        html=html,
        extracted_text=text_content,
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
        unique_id = await frame_element.get_attribute("unique_id")
    except Exception:
        LOG.warning(
            "Unable to get unique_id from frame_element",
            exc_info=True,
        )
        return elements, element_tree

    frame_js_script = f"async () => await buildTreeFromBody('{unique_id}', {frame_index})"

    await SkyvernFrame.evaluate(frame=frame, expression=JS_FUNCTION_DEFS)
    frame_elements, frame_element_tree = await SkyvernFrame.evaluate(
        frame=frame, expression=frame_js_script, timeout_ms=BUILDING_ELEMENT_TREE_TIMEOUT_MS
    )

    for element in elements:
        if element["id"] == unique_id:
            element["children"] = frame_element_tree

    elements = elements + frame_elements

    return elements, element_tree


async def get_interactable_element_tree(
    page: Page,
    scrape_exclude: ScrapeExcludeFunc | None = None,
) -> tuple[list[dict], list[dict]]:
    """
    Get the element tree of the page, including all the elements that are interactable.
    :param page: Page instance to get the element tree from.
    :return: Tuple containing the element tree and a map of element IDs to elements.
    """
    await SkyvernFrame.evaluate(frame=page, expression=JS_FUNCTION_DEFS)
    # main page index is 0
    main_frame_js_script = "async () => await buildTreeFromBody('main.frame', 0)"
    elements, element_tree = await SkyvernFrame.evaluate(
        frame=page, expression=main_frame_js_script, timeout_ms=BUILDING_ELEMENT_TREE_TIMEOUT_MS
    )

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


class IncrementalScrapePage:
    def __init__(self, skyvern_frame: SkyvernFrame) -> None:
        self.id_to_element_dict: dict[str, dict] = dict()
        self.id_to_css_dict: dict[str, str] = dict()
        self.elements: list[dict] = list()
        self.element_tree: list[dict] = list()
        self.element_tree_trimmed: list[dict] = list()
        self.skyvern_frame = skyvern_frame

    def check_id_in_page(self, element_id: str) -> bool:
        css_selector = self.id_to_css_dict.get(element_id, "")
        if css_selector:
            return True
        return False

    async def get_incremental_element_tree(
        self,
        cleanup_element_tree: CleanupElementTreeFunc,
    ) -> list[dict]:
        frame = self.skyvern_frame.get_frame()

        js_script = "async () => await getIncrementElements()"
        incremental_elements, incremental_tree = await SkyvernFrame.evaluate(
            frame=frame, expression=js_script, timeout_ms=BUILDING_ELEMENT_TREE_TIMEOUT_MS
        )
        # we listen the incremental elements seperated by frames, so all elements will be in the same SkyvernFrame
        self.id_to_css_dict, self.id_to_element_dict, _, _, _ = build_element_dict(incremental_elements)

        self.elements = incremental_elements

        incremental_tree = await cleanup_element_tree(frame, frame.url, copy.deepcopy(incremental_tree))
        trimmed_element_tree = trim_element_tree(copy.deepcopy(incremental_tree))

        self.element_tree = incremental_tree
        self.element_tree_trimmed = trimmed_element_tree

        return self.element_tree_trimmed

    async def start_listen_dom_increment(self) -> None:
        js_script = "() => startGlobalIncrementalObserver()"
        await SkyvernFrame.evaluate(frame=self.skyvern_frame.get_frame(), expression=js_script)

    async def stop_listen_dom_increment(self) -> None:
        # check if the DOM has navigated away or refreshed
        js_script = "() => window.globalObserverForDOMIncrement === undefined"
        if await SkyvernFrame.evaluate(frame=self.skyvern_frame.get_frame(), expression=js_script):
            return
        js_script = "async () => await stopGlobalIncrementalObserver()"
        await SkyvernFrame.evaluate(
            frame=self.skyvern_frame.get_frame(), expression=js_script, timeout_ms=BUILDING_ELEMENT_TREE_TIMEOUT_MS
        )

    async def get_incremental_elements_num(self) -> int:
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

    def build_html_tree(self, element_tree: list[dict] | None = None) -> str:
        return "".join([json_to_html(element) for element in (element_tree or self.element_tree_trimmed)])


def _should_keep_unique_id(element: dict) -> bool:
    # case where we shouldn't keep unique_id
    # 1. not disable attr and no interactable
    # 2. disable=false and intrecatable=false

    attributes = element.get("attributes", {})
    if "disabled" not in attributes and "aria-disabled" not in attributes:
        return element.get("interactable", False)

    disabled = attributes.get("disabled")
    aria_disabled = attributes.get("aria-disabled")
    if disabled or aria_disabled:
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
