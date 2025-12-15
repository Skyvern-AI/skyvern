import copy
import json
import typing
from abc import ABC, abstractmethod
from enum import StrEnum
from typing import Any, Awaitable, Callable, Self

import structlog
from playwright.async_api import Frame, Page
from pydantic import BaseModel, PrivateAttr

from skyvern.exceptions import UnknownElementTreeFormat
from skyvern.forge.sdk.api.crypto import calculate_sha256
from skyvern.forge.sdk.core import skyvern_context

if typing.TYPE_CHECKING:
    from skyvern.webeye.browser_state import BrowserState

LOG = structlog.get_logger()

CleanupElementTreeFunc = Callable[[Page | Frame, str, list[dict]], Awaitable[list[dict]]]
ScrapeExcludeFunc = Callable[[Page, Frame], Awaitable[bool]]

ELEMENT_NODE_ATTRIBUTES = {
    "id",
}


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
            LOG.debug("Element is interactable. Trimmed all attributes instead of dropping it", element=element)
            attributes = {}

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
        if option.get("text")
        else f'<option index="{option.get("optionIndex")}" value="{option.get("value")}">{option.get("text")}</option>'
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


class ElementTreeFormat(StrEnum):
    JSON = "json"  # deprecate JSON format soon. please use HTML format
    HTML = "html"


class ElementTreeBuilder(ABC):
    @abstractmethod
    def support_economy_elements_tree(self) -> bool:
        pass

    @abstractmethod
    def build_element_tree(
        self, fmt: ElementTreeFormat = ElementTreeFormat.HTML, html_need_skyvern_attrs: bool = True
    ) -> str:
        pass

    @abstractmethod
    def build_economy_elements_tree(
        self,
        fmt: ElementTreeFormat = ElementTreeFormat.HTML,
        html_need_skyvern_attrs: bool = True,
        percent_to_keep: float = 1,
    ) -> str:
        pass


class ScrapedPage(BaseModel, ElementTreeBuilder):
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
    id_to_css_dict: dict[str, str] = {}
    id_to_element_hash: dict[str, str] = {}
    hash_to_element_ids: dict[str, list[str]] = {}
    element_tree: list[dict]
    element_tree_trimmed: list[dict]
    economy_element_tree: list[dict] | None = None
    last_used_element_tree: list[dict] | None = None
    screenshots: list[bytes] = []
    url: str = ""
    html: str = ""
    extracted_text: str | None = None
    window_dimension: dict[str, int] | None = None
    _browser_state: "BrowserState" = PrivateAttr()
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

    def check_pdf_viewer_embed(self) -> str | None:
        """
        Check if the page contains a PDF viewer embed.
        If found, return the src attribute of the embed.
        """
        if len(self.elements) != 1:
            return None

        element = self.elements[0]
        if element.get("tagName", "") != "embed":
            return None

        attributes: dict = element.get("attributes", {})
        if not attributes:
            return None

        type_attr: str | None = attributes.get("type")
        if not type_attr:
            return None

        if type_attr.lower() != "application/pdf":
            return None

        LOG.info("Found a PDF viewer page", element=element)
        return attributes.get("src", "")

    def support_economy_elements_tree(self) -> bool:
        return True

    def build_element_tree(
        self, fmt: ElementTreeFormat = ElementTreeFormat.HTML, html_need_skyvern_attrs: bool = True
    ) -> str:
        self.last_used_element_tree = self.element_tree_trimmed
        if fmt == ElementTreeFormat.JSON:
            return json.dumps(self.element_tree_trimmed)

        if fmt == ElementTreeFormat.HTML:
            return "".join(
                json_to_html(element, need_skyvern_attrs=html_need_skyvern_attrs)
                for element in self.element_tree_trimmed
            )

        raise UnknownElementTreeFormat(fmt=fmt)

    def build_economy_elements_tree(
        self,
        fmt: ElementTreeFormat = ElementTreeFormat.HTML,
        html_need_skyvern_attrs: bool = True,
        percent_to_keep: float = 1,
    ) -> str:
        """
        Economy elements tree doesn't include secondary elements like SVG, etc
        """
        if not self.economy_element_tree:
            economy_elements = []
            copied_element_tree_trimmed = copy.deepcopy(self.element_tree_trimmed)

            # Process each root element
            for root_element in copied_element_tree_trimmed:
                processed_element = self._process_element_for_economy_tree(root_element)
                if processed_element:
                    economy_elements.append(processed_element)

            self.economy_element_tree = economy_elements

        self.last_used_element_tree = self.economy_element_tree

        if fmt == ElementTreeFormat.JSON:
            element_str = json.dumps(self.economy_element_tree)
            return element_str[: int(len(element_str) * percent_to_keep)]

        if fmt == ElementTreeFormat.HTML:
            element_str = "".join(
                json_to_html(element, need_skyvern_attrs=html_need_skyvern_attrs)
                for element in self.economy_element_tree
            )
            return element_str[: int(len(element_str) * percent_to_keep)]

        raise UnknownElementTreeFormat(fmt=fmt)

    def _process_element_for_economy_tree(self, element: dict) -> dict | None:
        """
        Helper method to process an element for the economy tree using BFS.
        Removes SVG elements and their children.
        """
        # Skip SVG elements entirely
        if element.get("tagName", "").lower() == "svg":
            return None

        # Process children using BFS
        if "children" in element:
            new_children = []
            for child in element["children"]:
                processed_child = self._process_element_for_economy_tree(child)
                if processed_child:
                    new_children.append(processed_child)
            element["children"] = new_children
        return element

    async def refresh(self, draw_boxes: bool = True, scroll: bool = True, max_retries: int = 0) -> Self:
        refreshed_page = await self._browser_state.scrape_website(
            url=self.url,
            cleanup_element_tree=self._clean_up_func,
            max_retries=max_retries,
            scrape_exclude=self._scrape_exclude,
            draw_boxes=draw_boxes,
            scroll=scroll,
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

    async def generate_scraped_page(
        self,
        draw_boxes: bool = True,
        scroll: bool = True,
        take_screenshots: bool = True,
        max_retries: int = 0,
    ) -> Self:
        return await self._browser_state.scrape_website(
            url=self.url,
            cleanup_element_tree=self._clean_up_func,
            max_retries=max_retries,
            scrape_exclude=self._scrape_exclude,
            take_screenshots=take_screenshots,
            draw_boxes=draw_boxes,
            scroll=scroll,
        )

    async def generate_scraped_page_without_screenshots(self, max_retries: int = 0) -> Self:
        return await self.generate_scraped_page(take_screenshots=False, max_retries=max_retries)
