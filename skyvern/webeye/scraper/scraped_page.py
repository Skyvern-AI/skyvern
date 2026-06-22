import copy
import json
import re
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

_PUA_PATTERN = re.compile(r"[\uE000-\uF8FF\U000F0000-\U000FFFFD\U00100000-\U0010FFFD]+")


def _replace_pua_with_marker(text: str | None) -> str:
    if not text:
        return ""
    return _PUA_PATTERN.sub("[icon]", text)


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

    before_pseudo_text = _replace_pua_with_marker(element.get("beforePseudoText"))
    after_pseudo_text = _replace_pua_with_marker(element.get("afterPseudoText"))

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
    def support_lean_elements_tree(self) -> bool:
        """SKY-9718 Layer 1 — whether this builder implements build_lean_elements_tree.

        Mirrors `support_economy_elements_tree`. Callers of `load_prompt_with_elements`
        check this before passing lean flags so builders that only implement the
        plain `build_element_tree` (e.g. `IncrementalScrapePage`) don't crash.
        """

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

    @abstractmethod
    def build_lean_elements_tree(
        self,
        fmt: ElementTreeFormat = ElementTreeFormat.HTML,
        html_need_skyvern_attrs: bool = True,
        *,
        compress_long_href: bool = False,
        compress_image_src: bool = False,
        strip_url_query_strings: bool = False,
        compress_nonnavigable_href: bool = False,
    ) -> str:
        pass

    # Sanitized HTML of the last element tree built for the LLM; None when the
    # last build was JSON or none has run yet. Builders that never render HTML
    # (e.g. IncrementalScrapePage) leave it None.
    last_used_element_tree_html: str | None


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
    # SKY-9718 Layer 1: lazy cache for lean trees, keyed by the 4-flag combo
    # the caller asked for. Two call sites in one prompt build asking for
    # different combos each pay the walk cost once.
    lean_element_tree_cache: dict[tuple[bool, bool, bool, bool], list[dict]] = {}
    last_used_element_tree: list[dict] | None = None
    last_used_element_tree_html: str | None = None
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

    @staticmethod
    def _is_pdf_embed(element: dict) -> str | None:
        if element.get("tagName", "") != "embed":
            return None
        attributes: dict = element.get("attributes", {})
        if not attributes:
            return None
        type_attr: str | None = attributes.get("type")
        if not type_attr or type_attr.lower() != "application/pdf":
            return None
        return attributes.get("src", "")

    def check_pdf_viewer_embed(self) -> str | None:
        """
        Check if the page contains a PDF viewer embed.
        If found, return the src attribute of the embed.

        Detection works at two levels:
        1. Whole-page: entire page has exactly one element and it is a PDF embed.
        2. Per-frame: any child frame has exactly one element and it is a PDF embed.
           This covers multi-frame pages (e.g. framesets) where the PDF is loaded
           in one frame while other frames contain navigation elements.
        """
        if len(self.elements) == 1:
            pdf_src = self._is_pdf_embed(self.elements[0])
            if pdf_src is not None:
                LOG.info("Found a PDF viewer page", element=self.elements[0])
                return pdf_src

        if self.id_to_frame_dict:
            frame_elements: dict[str, list[dict]] = {}
            for element in self.elements:
                element_id = element.get("id", "")
                frame_id = self.id_to_frame_dict.get(element_id, "main.frame")
                if frame_id == "main.frame":
                    continue
                frame_elements.setdefault(frame_id, []).append(element)

            for frame_id, elements in frame_elements.items():
                if len(elements) == 1:
                    pdf_src = self._is_pdf_embed(elements[0])
                    if pdf_src is not None:
                        LOG.info(
                            "Found a PDF viewer embed in frame",
                            frame_id=frame_id,
                            element=elements[0],
                        )
                        return pdf_src

        return None

    async def check_pdf_iframe(self) -> str | None:
        """
        Check if the page has a child iframe with PDF data URI content.
        This handles Edge's PDF interstitial page where PDF links open in an iframe
        with src="data:application/pdf;base64,..." on an about:blank page.
        """
        page = await self._browser_state.get_working_page()
        if not page:
            return None

        for frame in page.main_frame.child_frames:
            if frame.url and frame.url.startswith("data:application/pdf"):
                LOG.info("Found a PDF iframe with data URI", frame_url_prefix=frame.url[:80])
                return frame.url

        return None

    def support_economy_elements_tree(self) -> bool:
        return True

    def support_lean_elements_tree(self) -> bool:
        return True

    def build_element_tree(
        self, fmt: ElementTreeFormat = ElementTreeFormat.HTML, html_need_skyvern_attrs: bool = True
    ) -> str:
        # Side effect: writes self.last_used_element_tree (always) and
        # self.last_used_element_tree_html (HTML → string, JSON → None). The
        # extraction cache reads the latter to hash the exact variant sent to
        # the LLM. Callers that don't want the write must snapshot first.
        self.last_used_element_tree = self.element_tree_trimmed
        if fmt == ElementTreeFormat.JSON:
            self.last_used_element_tree_html = None
            return json.dumps(self.element_tree_trimmed)

        if fmt == ElementTreeFormat.HTML:
            result = "".join(
                json_to_html(element, need_skyvern_attrs=html_need_skyvern_attrs)
                for element in self.element_tree_trimmed
            )
            self.last_used_element_tree_html = result
            return result

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
            self.last_used_element_tree_html = None
            element_str = json.dumps(self.economy_element_tree)
            return element_str[: int(len(element_str) * percent_to_keep)]

        if fmt == ElementTreeFormat.HTML:
            elements_to_render = self.economy_element_tree
            if percent_to_keep < 1:
                # Portals (popper menus, dialogs) are appended near the end of
                # <body> and appear last in the root-level list. A naïve front
                # slice drops them disproportionately. Move overlay root elements
                # to the front so they survive character-level truncation.
                overlay_roots = [e for e in self.economy_element_tree if self._element_subtree_has_overlay(e)]
                if overlay_roots:
                    regular_roots = [e for e in self.economy_element_tree if not self._element_subtree_has_overlay(e)]
                    elements_to_render = overlay_roots + regular_roots
                    LOG.info(
                        "economy_tree_portal_priority: moved overlay roots to front before truncation",
                        overlay_count=len(overlay_roots),
                        regular_count=len(regular_roots),
                        percent_to_keep=percent_to_keep,
                    )
            element_str = "".join(
                json_to_html(element, need_skyvern_attrs=html_need_skyvern_attrs) for element in elements_to_render
            )
            result = element_str[: int(len(element_str) * percent_to_keep)]
            self.last_used_element_tree_html = result
            return result

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

    @staticmethod
    def _element_subtree_has_overlay(element: dict, _depth: int = 0) -> bool:
        """Return True if this element or any descendant carries role=listbox/option.

        These roles are preserved by `_trimmed_attributes` and are reliable
        indicators of popper/dialog portal content.  Recursion is limited to
        avoid spending time walking deep table trees — portals are typically
        shallow (1-3 levels).
        """
        if _depth > 4:
            return False
        role = (element.get("attributes") or {}).get("role", "")
        if role in ("listbox", "option"):
            return True
        return any(ScrapedPage._element_subtree_has_overlay(child, _depth + 1) for child in element.get("children", []))

    def build_lean_elements_tree(
        self,
        fmt: ElementTreeFormat = ElementTreeFormat.HTML,
        html_need_skyvern_attrs: bool = True,
        *,
        compress_long_href: bool = False,
        compress_image_src: bool = False,
        strip_url_query_strings: bool = False,
        compress_nonnavigable_href: bool = False,
    ) -> str:
        """SKY-9718 Layer 1 — deterministic lean element tree.

        Same shape as `build_economy_elements_tree`: deep-copy the trimmed
        tree, walk it applying the lean recipe, cache by flag combo, then
        render.

        Each of the 3 transforms is independently gated by its kwarg. Callers
        of `load_prompt_with_elements` pick the right combo per template.

        Skyvern internal IDs are *not* a lean flag — drop them by passing
        `html_need_skyvern_attrs=False` (the existing mechanism). `json_to_html`
        only copies `element["id"]` into the rendered HTML when that flag is True.
        """
        from skyvern.utils.lean_html import apply_lean_to_tree

        cache_key = (
            compress_long_href,
            compress_image_src,
            strip_url_query_strings,
            compress_nonnavigable_href,
        )
        cached = self.lean_element_tree_cache.get(cache_key)
        if cached is None:
            cached = apply_lean_to_tree(
                self.element_tree_trimmed,
                compress_long_href=compress_long_href,
                compress_image_src=compress_image_src,
                strip_url_query_strings=strip_url_query_strings,
                compress_nonnavigable_href=compress_nonnavigable_href,
            )
            self.lean_element_tree_cache[cache_key] = cached

        self.last_used_element_tree = cached

        if fmt == ElementTreeFormat.JSON:
            self.last_used_element_tree_html = None
            return json.dumps(cached)

        if fmt == ElementTreeFormat.HTML:
            result = "".join(json_to_html(element, need_skyvern_attrs=html_need_skyvern_attrs) for element in cached)
            self.last_used_element_tree_html = result
            return result

        raise UnknownElementTreeFormat(fmt=fmt)

    async def refresh(self, draw_boxes: bool = False, scroll: bool = True, max_retries: int = 0) -> Self:
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
        # Defensive: callers today rebuild before reading, but future direct
        # reads of this field post-refresh would otherwise see stale HTML.
        self.last_used_element_tree_html = None
        # SKY-9718 Layer 1 + pre-existing bug for economy: derived element-tree
        # caches must be invalidated when the underlying `element_tree_trimmed`
        # is replaced. Without these resets, the next build_*_elements_tree
        # call returns a tree from the pre-refresh page state — particularly
        # bad on the `complete_verify` hot path, where the verifier would
        # reason about the pre-action page when checking if a post-action
        # goal was achieved.
        self.economy_element_tree = None
        self.lean_element_tree_cache = {}
        self.last_used_element_tree = None
        return self

    async def generate_scraped_page(
        self,
        # DEPRECATED: visual bounding box overlays are no longer rendered during scraping.
        # The parameter is retained for backwards compatibility and is scheduled for removal.
        # New call sites must not pass ``draw_boxes=True``.
        draw_boxes: bool = False,
        scroll: bool = True,
        take_screenshots: bool = True,
        max_retries: int = 0,
        must_included_tags: list[str] | None = None,
    ) -> Self:
        return await self._browser_state.scrape_website(
            url=self.url,
            cleanup_element_tree=self._clean_up_func,
            max_retries=max_retries,
            scrape_exclude=self._scrape_exclude,
            take_screenshots=take_screenshots,
            draw_boxes=draw_boxes,
            scroll=scroll,
            must_included_tags=must_included_tags,
        )

    async def generate_scraped_page_without_screenshots(self, max_retries: int = 0) -> Self:
        return await self.generate_scraped_page(take_screenshots=False, max_retries=max_retries)
