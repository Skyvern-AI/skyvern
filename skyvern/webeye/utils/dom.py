from __future__ import annotations

import asyncio
import copy
import typing
from enum import StrEnum
from random import uniform
from urllib.parse import urlparse

import structlog
from playwright.async_api import ElementHandle, FloatRect, Frame, FrameLocator, Locator, Page, TimeoutError

from skyvern.config import settings
from skyvern.constants import SKYVERN_ID_ATTR, TEXT_INPUT_DELAY
from skyvern.exceptions import (
    ElementIsNotLabel,
    ElementOutOfCurrentViewport,
    InteractWithDisabledElement,
    MissingElement,
    MissingElementDict,
    MissingElementInCSSMap,
    MissingElementInIframe,
    MultipleElementsFound,
    NoElementBoudingBox,
    NoneFrameError,
    SkyvernException,
)
from skyvern.experimentation.wait_utils import get_or_create_wait_config, get_wait_time, scroll_into_view_wait
from skyvern.webeye.actions import handler_utils
from skyvern.webeye.scraper.scraped_page import ScrapedPage, json_to_html
from skyvern.webeye.scraper.scraper import IncrementalScrapePage, trim_element
from skyvern.webeye.utils.page import SkyvernFrame

LOG = structlog.get_logger()
COMMON_INPUT_TAGS = {"input", "textarea", "select"}


async def resolve_locator(scrape_page: ScrapedPage, page: Page, frame: str, css: str) -> tuple[Locator, Page | Frame]:
    iframe_path: list[str] = []

    while frame != "main.frame":
        iframe_path.append(frame)

        frame_element = scrape_page.id_to_element_dict.get(frame)
        if frame_element is None:
            raise MissingElement(element_id=frame)

        parent_frame = frame_element.get("frame")
        if not parent_frame:
            raise SkyvernException(f"element without frame: {frame_element}")

        frame = parent_frame

    current_page: Page | FrameLocator = page
    current_frame: Page | Frame = page

    while len(iframe_path) > 0:
        child_frame = iframe_path.pop()

        frame_handler = await current_frame.query_selector(f"[{SKYVERN_ID_ATTR}='{child_frame}']")
        if frame_handler is None:
            raise NoneFrameError(frame_id=child_frame)

        content_frame = await frame_handler.content_frame()
        if content_frame is None:
            raise NoneFrameError(frame_id=child_frame)
        current_frame = content_frame

        current_page = current_page.frame_locator(f"[{SKYVERN_ID_ATTR}='{child_frame}']")

    return current_page.locator(css), current_frame


class InteractiveElement(StrEnum):
    A = "a"
    INPUT = "input"
    SELECT = "select"
    BUTTON = "button"


SELECTABLE_ELEMENT = [InteractiveElement.INPUT, InteractiveElement.SELECT]
RAW_INPUT_TYPE_VALUE = ["number", "url", "tel", "email", "username", "password"]
RAW_INPUT_NAME_VALUE = ["name", "email", "username", "password", "phone"]


class SkyvernOptionType(typing.TypedDict):
    optionIndex: int
    text: str
    value: str


class SkyvernElement:
    """
    SkyvernElement is a python interface to interact with js elements built during the scarping.
    When you try to interact with these elements by python, you are supposed to use this class as an interface.
    """

    # TODO: support to create SkyvernElement from incremental page by xpath
    @classmethod
    async def create_from_incremental(cls, incre_page: IncrementalScrapePage, element_id: str) -> SkyvernElement:
        element_dict = incre_page.id_to_element_dict.get(element_id)
        if element_dict is None:
            raise MissingElementDict(element_id)

        css_selector = incre_page.id_to_css_dict.get(element_id)
        if not css_selector:
            raise MissingElementInCSSMap(element_id)

        frame = incre_page.skyvern_frame.get_frame()
        locator = frame.locator(css_selector)

        num_elements = await locator.count()
        if num_elements < 1:
            LOG.debug("No elements found with css. Validation failed.", css=css_selector, element_id=element_id)
            raise MissingElement(selector=css_selector, element_id=element_id)

        elif num_elements > 1:
            LOG.debug(
                "Multiple elements found with css. Expected 1. Validation failed.",
                num_elements=num_elements,
                selector=css_selector,
                element_id=element_id,
            )
            raise MultipleElementsFound(num=num_elements, selector=css_selector, element_id=element_id)

        return cls(locator, frame, element_dict)

    def __init__(self, locator: Locator, frame: Page | Frame, static_element: dict, hash_value: str = "") -> None:
        self.__static_element = static_element
        self.__frame = frame
        self.locator = locator
        self.hash_value = hash_value
        self._id_cache = static_element.get("id", "")
        self._tag_name = static_element.get("tagName", "")
        self._selectable = static_element.get("isSelectable", False)
        self._hover_only = static_element.get("hoverOnly", False)
        self._frame_id = static_element.get("frame", "")
        self._attributes = static_element.get("attributes", {})
        self._rect: FloatRect | None = None

    def __repr__(self) -> str:
        return f"SkyvernElement({str(self.__static_element)})"

    def build_HTML(self, need_trim_element: bool = True, need_skyvern_attrs: bool = True) -> str:
        element_dict = self.get_element_dict()
        if need_trim_element:
            element_dict = trim_element(copy.deepcopy(element_dict))

        return json_to_html(element_dict, need_skyvern_attrs)

    async def is_auto_completion_input(self) -> bool:
        tag_name = self.get_tag_name()
        if tag_name != InteractiveElement.INPUT:
            return False

        data_bind: str | None = await self.get_attr("data-x-bind")
        if data_bind and "autocomplete" in data_bind.lower():
            return True

        autocomplete: str | None = await self.get_attr("aria-autocomplete")
        if autocomplete and autocomplete.lower() == "list":
            return True

        class_name: str | None = await self.get_attr("class")
        if class_name and "autocomplete-input" in class_name.lower():
            return True

        return False

    async def is_custom_option(self) -> bool:
        return self.get_tag_name() == "li" or await self.get_attr("role", mode="static") == "option"

    async def is_checkbox(self) -> bool:
        tag_name = self.get_tag_name()
        if tag_name != "input":
            return False

        button_type = await self.get_attr("type")
        return button_type == "checkbox"

    async def is_radio(self) -> bool:
        tag_name = self.get_tag_name()
        if tag_name != "input":
            return False

        button_type = await self.get_attr("type")
        return button_type == "radio"

    async def is_btn_input(self) -> bool:
        tag_name = self.get_tag_name()
        if tag_name != InteractiveElement.INPUT:
            return False

        input_type = await self.get_attr("type")
        return input_type == "button"

    async def is_raw_input(self) -> bool:
        if self.get_tag_name() != InteractiveElement.INPUT:
            return False

        if await self.is_spinbtn_input():
            return True

        input_type = str(await self.get_attr("type"))
        if input_type.lower() in RAW_INPUT_TYPE_VALUE:
            return True

        name = str(await self.get_attr("name"))
        if name.lower() in RAW_INPUT_NAME_VALUE:
            return True

        # if input has these attrs, it expects user to type and input sth
        if await self.get_attr("min") or await self.get_attr("max") or await self.get_attr("step"):
            return True

        # maxlength=6 or maxlength=1 usually means it's an OTP input field
        # already consider type="tel" or type="number" as raw_input in the previous logic, so need to confirm it for the OTP field
        max_length = str(await self.get_attr("maxlength", mode="static"))
        if input_type.lower() == "text" and max_length in ["1", "6"]:
            return True

        return False

    async def is_spinbtn_input(self) -> bool:
        """
        confirm the element is:
        1. <input> element
        2. role=spinbutton

        Usage of <input role="spinbutton">, https://developer.mozilla.org/en-US/docs/Web/Accessibility/ARIA/Roles/spinbutton_role
        """
        if self.get_tag_name() != InteractiveElement.INPUT:
            return False

        if await self.get_attr("role") == "spinbutton":
            return True

        return False

    async def is_file_input(self) -> bool:
        return self.get_tag_name() == InteractiveElement.INPUT and await self.get_attr("type") == "file"

    def is_interactable(self) -> bool:
        return self.__static_element.get("interactable", False)

    async def is_disabled(self, dynamic: bool = False) -> bool:
        # if attr not exist, return None
        # if attr is like 'disabled', return empty string or True
        # if attr is like `disabled=false`, return the value
        disabled = False
        aria_disabled = False

        disabled_attr: bool | str | None = None
        aria_disabled_attr: bool | str | None = None
        style_disabled: bool = False

        mode: typing.Literal["auto", "dynamic"] = "dynamic" if dynamic else "auto"
        try:
            disabled_attr = await self.get_attr("disabled", mode=mode)
            aria_disabled_attr = await self.get_attr("aria-disabled", mode=mode)
            skyvern_frame = await SkyvernFrame.create_instance(self.get_frame())
            style_disabled = await skyvern_frame.get_disabled_from_style(await self.get_element_handler())

        except Exception:
            # FIXME: maybe it should be considered as "disabled" element if failed to get the attributes?
            LOG.exception(
                "Failed to get the disabled attribute",
                element=self.__static_element,
                element_id=self.get_id(),
            )

        if disabled_attr is not None:
            # disabled_attr should be bool or str
            if isinstance(disabled_attr, bool):
                disabled = disabled_attr
            if isinstance(disabled_attr, str):
                disabled = disabled_attr.lower() != "false"

        if aria_disabled_attr is not None:
            # aria_disabled_attr should be bool or str
            if isinstance(aria_disabled_attr, bool):
                aria_disabled = aria_disabled_attr
            if isinstance(aria_disabled_attr, str):
                aria_disabled = aria_disabled_attr.lower() != "false"

        return disabled or aria_disabled or style_disabled

    async def is_readonly(self, dynamic: bool = False) -> bool:
        # if attr not exist, return None
        # if attr is like 'readonly', return empty string or True
        # if attr is like `readonly=false`, return the value
        readonly = False
        aria_readonly = False

        readonly_attr: bool | str | None = None
        aria_readonly_attr: bool | str | None = None
        mode: typing.Literal["auto", "dynamic"] = "dynamic" if dynamic else "auto"

        try:
            readonly_attr = await self.get_attr("readonly", mode=mode)
            aria_readonly_attr = await self.get_attr("aria-readonly", mode=mode)
        except Exception:
            LOG.exception(
                "Failed to get the readonly attribute",
                element=self.__static_element,
                element_id=self.get_id(),
            )

        if readonly_attr is not None:
            # readonly_attr should be bool or str
            if isinstance(readonly_attr, bool):
                readonly = readonly_attr
            if isinstance(readonly_attr, str):
                readonly = readonly_attr.lower() != "false"

        if aria_readonly_attr is not None:
            # aria_readonly_attr should be bool or str
            if isinstance(aria_readonly_attr, bool):
                aria_readonly = aria_readonly_attr
            if isinstance(aria_readonly_attr, str):
                aria_readonly = aria_readonly_attr.lower() != "false"

        return readonly or aria_readonly

    async def is_selectable(self) -> bool:
        return await self.get_selectable() or self.get_tag_name() in SELECTABLE_ELEMENT

    async def is_visible(self, must_visible_style: bool = True) -> bool:
        if not await self.get_locator().count():
            return False
        if not must_visible_style:
            return True
        skyvern_frame = await SkyvernFrame.create_instance(self.get_frame())
        return await skyvern_frame.get_element_visible(await self.get_element_handler())

    async def is_editable(self, timeout: float = settings.BROWSER_ACTION_TIMEOUT_MS) -> bool:
        try:
            return await self.get_locator().is_editable(timeout=timeout)
        except Exception:
            LOG.info(
                "Failed to check element editable, considering it's not editable",
                exc_info=True,
                element_id=self.get_id(),
            )
            return False

    async def is_child_of_pdf_object(self, timeout: float = settings.BROWSER_ACTION_TIMEOUT_MS) -> bool:
        parent_locator = self.get_locator().locator("..")
        tag_name: str | None = await parent_locator.evaluate("el => el.tagName", timeout=timeout)
        type_attr = await parent_locator.get_attribute("type", timeout=timeout)
        return tag_name is not None and tag_name.lower() == "object" and type_attr == "application/pdf"

    async def is_parent_of(self, target: ElementHandle) -> bool:
        skyvern_frame = await SkyvernFrame.create_instance(self.get_frame())
        return await skyvern_frame.is_parent(await self.get_element_handler(), target)

    async def is_child_of(self, target: ElementHandle) -> bool:
        skyvern_frame = await SkyvernFrame.create_instance(self.get_frame())
        return await skyvern_frame.is_parent(target, await self.get_element_handler())

    async def is_sibling_of(self, target: ElementHandle) -> bool:
        skyvern_frame = await SkyvernFrame.create_instance(self.get_frame())
        return await skyvern_frame.is_sibling(await self.get_element_handler(), target)

    async def has_hidden_attr(self) -> bool:
        hidden: str | None = await self.get_attr("hidden", mode="dynamic")
        aria_hidden: str | None = await self.get_attr("aria-hidden", mode="dynamic")
        if hidden is not None and hidden.lower() != "false":
            return True
        if aria_hidden is not None and aria_hidden.lower() != "false":
            return True
        return False

    async def has_attr(self, attr_name: str, mode: typing.Literal["auto", "dynamic", "static"] = "auto") -> bool:
        value = await self.get_attr(attr_name, mode=mode)
        # FIXME(maybe?): already parsed the value of "disabled", "readonly" into boolean.
        # so the empty string values should be considered as FALSE value?
        # maybe need to come back to change it?
        if value:
            return True
        return False

    def get_element_dict(self) -> dict:
        return self.__static_element

    async def get_selectable(self) -> bool:
        if self.get_tag_name() == InteractiveElement.INPUT:
            input_type = await self.get_attr("type", mode="static")
            if input_type == "select-one" or input_type == "select-multiple":
                return True
        return self._selectable

    def get_tag_name(self) -> str:
        return self._tag_name

    def get_id(self) -> str:
        return self._id_cache

    def get_frame_id(self) -> str:
        return self._frame_id

    def get_attributes(self) -> dict:
        return self._attributes

    def requires_hover(self) -> bool:
        return bool(self._hover_only)

    async def hover_to_reveal(
        self,
        max_depth: int = 4,
        timeout: float = settings.BROWSER_ACTION_TIMEOUT_MS,
        settle_delay_s: float = 0.15,
    ) -> bool:
        if not self.requires_hover():
            return False

        hover_target = self.get_locator()
        for depth in range(max_depth):
            try:
                await hover_target.scroll_into_view_if_needed()
                await hover_target.hover(timeout=timeout)
                await asyncio.sleep(settle_delay_s)
                if await self.get_locator().is_visible(timeout=timeout):
                    LOG.debug("Hover reveal succeeded", element_id=self.get_id(), depth=depth)
                    return True
            except Exception:
                LOG.debug(
                    "Hover attempt failed while trying to reveal element",
                    exc_info=True,
                    element_id=self.get_id(),
                    depth=depth,
                )

            parent_locator = hover_target.locator("..")
            try:
                if await parent_locator.count() != 1:
                    break
            except Exception:
                LOG.debug(
                    "Unable to evaluate parent locator during hover reveal", exc_info=True, element_id=self.get_id()
                )
                break
            hover_target = parent_locator

        LOG.debug("Hover reveal attempts exhausted", element_id=self.get_id())
        return False

    def get_options(self) -> list[SkyvernOptionType]:
        options = self.__static_element.get("options", None)
        if options is None:
            return []

        return typing.cast(typing.List[SkyvernOptionType], options)

    def get_frame(self) -> Page | Frame:
        return self.__frame

    def get_frame_index(self) -> int:
        return self.__static_element.get("frame_index", -1)

    def get_locator(self) -> Locator:
        return self.locator

    async def get_rect(self, timeout: float = settings.BROWSER_ACTION_TIMEOUT_MS) -> FloatRect | None:
        if self._rect is not None:
            return self._rect
        self._rect = await self.get_locator().bounding_box(timeout=timeout)
        return self._rect

    async def get_element_handler(self, timeout: float = settings.BROWSER_ACTION_TIMEOUT_MS) -> ElementHandle:
        handler = await self.locator.element_handle(timeout=timeout)
        assert handler is not None
        return handler

    async def should_use_navigation_instead_click(self, page: Page) -> str | None:
        if await self.get_attr("target", mode="static") != "_blank" and not await self.is_child_of_pdf_object():
            return None

        href: str | None = await self.get_attr("href", mode="static")
        if not href:
            return None

        href_url = urlparse(href)
        if href_url.scheme.lower() not in ["http", "https"]:
            return None

        if not href_url.netloc:
            return None

        cur_url = urlparse(page.url)
        if href_url.netloc.lower() == cur_url.netloc.lower():
            return None

        return href

    async def find_blocking_element(
        self, dom: DomUtil, incremental_page: IncrementalScrapePage | None = None
    ) -> tuple[SkyvernElement | None, bool]:
        skyvern_frame = await SkyvernFrame.create_instance(self.get_frame())
        blocking_element_id, blocked = await skyvern_frame.get_blocking_element_id(await self.get_element_handler())
        if not blocking_element_id:
            return None, blocked

        if await dom.check_id_in_dom(blocking_element_id):
            return await dom.get_skyvern_element_by_id(blocking_element_id), blocked

        if incremental_page and incremental_page.check_id_in_page(blocking_element_id):
            return await SkyvernElement.create_from_incremental(incremental_page, blocking_element_id), blocked

        return None, blocked

    async def find_element_in_label_children(
        self, dom: DomUtil, element_type: InteractiveElement
    ) -> SkyvernElement | None:
        element_id = self.find_element_id_in_label_children(element_type=element_type)
        if not element_id:
            return None
        return await dom.get_skyvern_element_by_id(element_id=element_id)

    def find_element_id_in_label_children(self, element_type: InteractiveElement) -> str | None:
        tag_name = self.get_tag_name()
        if tag_name != "label":
            raise ElementIsNotLabel(tag_name)

        children: list[dict] = self.__static_element.get("children", [])
        for child in children:
            if not child.get("interactable"):
                continue

            if child.get("tagName") == element_type:
                return child.get("id")

        return None

    async def find_children_element_id_by_callback(
        self, cb: typing.Callable[[dict], typing.Awaitable[bool]]
    ) -> str | None:
        index = 0
        queue = [self.get_element_dict()]
        while index < len(queue):
            item = queue[index]
            if await cb(item):
                return item.get("id", "")

            children: list[dict] = item.get("children", [])
            for child in children:
                queue.append(child)

            index += 1
        return None

    async def find_label_for(
        self, dom: DomUtil, timeout: float = settings.BROWSER_ACTION_TIMEOUT_MS
    ) -> SkyvernElement | None:
        if self.get_tag_name() != "label":
            return None

        for_id = await self.get_attr("for")
        if for_id == "":
            return None

        locator = self.get_frame().locator(f"[id='{for_id}']")
        # supposed to be only one element, since id is unique in the whole DOM
        if await locator.count() != 1:
            return None

        unique_id = await locator.get_attribute(SKYVERN_ID_ATTR, timeout=timeout)
        if unique_id is None:
            return None

        return await dom.get_skyvern_element_by_id(unique_id)

    async def find_bound_label_by_attr_id(self, timeout: float = settings.BROWSER_ACTION_TIMEOUT_MS) -> Locator | None:
        if self.get_tag_name() == "label":
            return None

        element_id: str = await self.get_attr("id", timeout=timeout)
        if not element_id:
            return None

        locator = self.get_frame().locator(f"label[for='{element_id}']")
        cnt = await locator.count()
        if cnt == 1:
            return locator

        return None

    async def find_bound_label_by_direct_parent(
        self, timeout: float = settings.BROWSER_ACTION_TIMEOUT_MS
    ) -> Locator | None:
        if self.get_tag_name() == "label":
            return None

        parent_locator = self.get_locator().locator("..")
        cnt = await parent_locator.count()
        if cnt != 1:
            return None

        timeout_sec = timeout / 1000
        async with asyncio.timeout(timeout_sec):
            tag_name: str | None = await parent_locator.evaluate("el => el.tagName")
            if not tag_name:
                return None

            if tag_name.lower() != "label":
                return None

            return parent_locator

    async def find_selectable_child(self, dom: DomUtil) -> SkyvernElement | None:
        # BFS to find the first selectable child
        index = 0
        queue = [self]
        while index < len(queue):
            item = queue[index]
            if item.is_interactable() and await item.is_selectable():
                return item

            try:
                for_element = await item.find_label_for(dom=dom)
                if for_element is not None and await for_element.is_selectable():
                    return for_element
            except Exception:
                LOG.error(
                    "Failed to find element by label-for",
                    element=item.__static_element,
                    exc_info=True,
                )

            children: list[dict] = item.__static_element.get("children", [])
            for child in children:
                child_id = child.get("id", "")
                child_element = await dom.get_skyvern_element_by_id(child_id)
                queue.append(child_element)

            index += 1
        return None

    async def find_interactable_anchor_child(
        self, dom: DomUtil, element_type: InteractiveElement
    ) -> SkyvernElement | None:
        index = 0
        queue = [self]
        while index < len(queue):
            item = queue[index]
            if item.is_interactable() and item.get_tag_name() == element_type:
                return item

            try:
                for_element = await item.find_label_for(dom=dom)
                if for_element is not None and for_element.get_tag_name() == element_type:
                    return for_element
            except Exception:
                LOG.error(
                    "Failed to find element by label-for",
                    element=item.__static_element,
                    exc_info=True,
                )

            children: list[dict] = item.__static_element.get("children", [])
            for child in children:
                child_id = child.get("id", "")
                child_element = await dom.get_skyvern_element_by_id(child_id)
                queue.append(child_element)

            index += 1
        return None

    async def find_file_input_in_children(self) -> Locator | None:
        """Sometime the file input is invisible on the page, so it won't exist in the element tree, but it can be found in the DOM."""
        locator = self.get_locator().locator('input[type="file"]')
        if await locator.count() != 1:
            return None
        return locator

    async def get_attr(
        self,
        attr_name: str,
        mode: typing.Literal["auto", "dynamic", "static"] = "auto",
        timeout: float = settings.BROWSER_ACTION_TIMEOUT_MS,
    ) -> typing.Any:
        """
        mode:
            auto: use value from the self.get_attributes() first. if empty, then try to get the value from the locator.get_attribute()
            dynamic: always use locator.get_attribute()
            static: always use self.get_attributes()
        """
        if mode != "dynamic":
            attr = self.get_attributes().get(attr_name)
            if attr is not None or mode == "static":
                return attr

        return await self.locator.get_attribute(attr_name, timeout=timeout)

    async def focus(self, timeout: float = settings.BROWSER_ACTION_TIMEOUT_MS) -> None:
        await self.get_locator().focus(timeout=timeout)

    async def input_sequentially(self, text: str, default_timeout: float = settings.BROWSER_ACTION_TIMEOUT_MS) -> None:
        await handler_utils.input_sequentially(self.get_locator(), text, timeout=default_timeout)

    async def press_key(self, key: str, timeout: float = settings.BROWSER_ACTION_TIMEOUT_MS) -> None:
        await self.get_locator().press(key=key, timeout=timeout)

    async def press_fill(self, text: str, timeout: float = settings.BROWSER_ACTION_TIMEOUT_MS) -> None:
        for char in text:
            await self.get_locator().type(char, delay=TEXT_INPUT_DELAY, timeout=timeout)

    async def input(self, text: str, timeout: float = settings.BROWSER_ACTION_TIMEOUT_MS) -> None:
        if self.get_tag_name().lower() not in COMMON_INPUT_TAGS:
            await self.input_fill(text, timeout=timeout)
            return
        await self.input_sequentially(text=text, default_timeout=timeout)

    async def input_fill(self, text: str, timeout: float = settings.BROWSER_ACTION_TIMEOUT_MS) -> None:
        await self.get_locator().fill(text, timeout=timeout)

    async def input_clear(self, timeout: float = settings.BROWSER_ACTION_TIMEOUT_MS) -> None:
        await self.get_locator().clear(timeout=timeout)

    async def check(
        self,
        timeout: float = settings.BROWSER_ACTION_TIMEOUT_MS,
        task_id: str | None = None,
        workflow_run_id: str | None = None,
        organization_id: str | None = None,
    ) -> None:
        # HACK: sometimes playwright will raise exception when checking the element.
        # we need to trigger the hack to check again in several seconds
        try:
            await self.get_locator().check(timeout=timeout)
        except Exception:
            LOG.info(
                "Failed to check the element at the first time, trigger the hack to check again",
                exc_info=True,
                element_id=self.get_id(),
            )
            wait_config = await get_or_create_wait_config(task_id, workflow_run_id, organization_id)
            await asyncio.sleep(get_wait_time(wait_config, "checkbox_retry_delay", default=2.0))
            if await self.get_locator().count() == 0:
                LOG.info("Element is not on the page, the checking should work", element_id=self.get_id())
                return
            await self.get_locator().check(timeout=timeout)

    async def uncheck(
        self,
        timeout: float = settings.BROWSER_ACTION_TIMEOUT_MS,
        task_id: str | None = None,
        workflow_run_id: str | None = None,
        organization_id: str | None = None,
    ) -> None:
        # HACK: sometimes playwright will raise exception when unchecking the element.
        # we need to trigger the hack to uncheck again in several seconds
        try:
            await self.get_locator().uncheck(timeout=timeout)
        except Exception:
            LOG.info(
                "Failed to uncheck the element at the first time, trigger the hack to uncheck again",
                exc_info=True,
                element_id=self.get_id(),
            )
            wait_config = await get_or_create_wait_config(task_id, workflow_run_id, organization_id)
            await asyncio.sleep(get_wait_time(wait_config, "checkbox_retry_delay", default=2.0))
            if await self.get_locator().count() == 0:
                LOG.info("Element is not on the page, the unchecking should work", element_id=self.get_id())
                return
            await self.get_locator().uncheck(timeout=timeout)

    async def move_mouse_to_safe(
        self,
        page: Page,
        task_id: str | None = None,
        step_id: str | None = None,
        timeout: float = settings.BROWSER_ACTION_TIMEOUT_MS,
    ) -> tuple[float, float] | tuple[None, None]:
        element_id = self.get_id()
        try:
            return await self.move_mouse_to(page, timeout=timeout)
        except NoElementBoudingBox:
            LOG.warning(
                "Failed to move mouse to the element - NoElementBoudingBox",
                task_id=task_id,
                step_id=step_id,
                element_id=element_id,
                exc_info=True,
            )
        except ElementOutOfCurrentViewport:
            LOG.warning(
                "Failed to move mouse to the element - ElementOutOfCurrentViewport",
                task_id=task_id,
                step_id=step_id,
                element_id=element_id,
                exc_info=True,
            )
        except Exception:
            LOG.warning(
                "Failed to move mouse to the element - unexpectd exception",
                task_id=task_id,
                step_id=step_id,
                element_id=element_id,
                exc_info=True,
            )
        return None, None

    async def move_mouse_to(
        self, page: Page, timeout: float = settings.BROWSER_ACTION_TIMEOUT_MS
    ) -> tuple[float, float]:
        bounding_box = await self.get_locator().bounding_box(timeout=timeout)
        if not bounding_box:
            raise NoElementBoudingBox(element_id=self.get_id())
        x, y, width, height = bounding_box["x"], bounding_box["y"], bounding_box["width"], bounding_box["height"]

        # calculate the click point, use open interval to avoid clicking on the border
        epsilon = 0.01
        dest_x = uniform(x + epsilon, x + width - epsilon) if width > 2 * epsilon else (x + width) / 2
        dest_y = uniform(y + epsilon, y + height - epsilon) if height > 2 * epsilon else (y + height) / 2

        # TODO: a better way to check if the element is out of current viewport
        # eg: x > window.innerWidth or y > window.innerHeight; part of the element is out of the viewport
        if dest_x < 0 or dest_y < 0:
            raise ElementOutOfCurrentViewport(element_id=self.get_id())

        await page.mouse.move(dest_x, dest_y)

        return dest_x, dest_y

    async def click(
        self,
        page: Page,
        dom: DomUtil | None = None,
        incremental_page: IncrementalScrapePage | None = None,
        timeout: float = settings.BROWSER_ACTION_TIMEOUT_MS,
    ) -> None:
        if await self.is_disabled(dynamic=True):
            raise InteractWithDisabledElement(element_id=self.get_id())

        try:
            await self.get_locator().click(timeout=timeout)
            return
        except Exception:
            LOG.info("Failed to click by playwright", exc_info=True, element_id=self.get_id())

        if dom is not None:
            # try to click on the blocking element
            try:
                await self.scroll_into_view(timeout=timeout)
                blocking_element, _ = await self.find_blocking_element(dom=dom, incremental_page=incremental_page)
                if blocking_element:
                    LOG.debug("Find the blocking element", element_id=blocking_element.get_id())
                    await blocking_element.get_locator().click(timeout=timeout)
                    return
            except Exception:
                LOG.info("Failed to click on the blocking element", exc_info=True, element_id=self.get_id())

        try:
            await self.scroll_into_view(timeout=timeout)
            await self.coordinate_click(page=page, timeout=timeout)
            return
        except Exception:
            LOG.info("Failed to click by coordinate", exc_info=True, element_id=self.get_id())

        await self.scroll_into_view(timeout=timeout)
        await self.click_in_javascript()
        return

    async def click_in_javascript(self) -> None:
        skyvern_frame = await SkyvernFrame.create_instance(self.get_frame())
        await skyvern_frame.click_element_in_javascript(await self.get_element_handler())

    async def coordinate_click(self, page: Page, timeout: float = settings.BROWSER_ACTION_TIMEOUT_MS) -> None:
        click_x, click_y = await self.move_mouse_to(page=page, timeout=timeout)
        await page.mouse.click(click_x, click_y)

    async def blur(self) -> None:
        if not await self.is_visible():
            return
        await SkyvernFrame.evaluate(
            frame=self.get_frame(), expression="(element) => element.blur()", arg=await self.get_element_handler()
        )

    async def scroll_into_view(self, timeout: float = settings.BROWSER_ACTION_TIMEOUT_MS) -> None:
        if not await self.is_visible():
            return

        try:
            target_x: int | None = None
            target_y: int | None = None

            rect = await self.get_rect(timeout=timeout)
            if rect is not None:
                element_x = rect["x"] if rect["x"] > 0 else None
                element_y = rect["y"] if rect["y"] > 0 else None

            # calculating y to move the element to the middle of the viewport
            if element_y is not None:
                target_y = max(int(element_y - (settings.BROWSER_HEIGHT / 2)), 0)

            if element_x is not None:
                target_x = max(int(element_x - (settings.BROWSER_WIDTH / 2)), 0)

            skyvern_frame = await SkyvernFrame.create_instance(self.get_frame())
            if target_x is not None and target_y is not None:
                await skyvern_frame.safe_scroll_to_x_y(target_x, target_y)
        except Exception:
            LOG.info(
                "Failed to calculate the y to move the element to the middle of the viewport, ignore it",
                exc_info=True,
                element_id=self.get_id(),
            )

        try:
            element_handler = await self.get_element_handler(timeout=timeout)
            await element_handler.scroll_into_view_if_needed(timeout=timeout)
        except TimeoutError:
            LOG.info(
                "Timeout to execute scrolling into view, try to re-focus to locate the element",
                element_id=self.get_id(),
            )
            await self.blur()
            await self.focus(timeout=timeout)

        # Wait for scrolling to complete
        await scroll_into_view_wait()

    async def calculate_min_y_distance_to(
        self,
        target_locator: Locator,
        timeout: float = settings.BROWSER_ACTION_TIMEOUT_MS,
    ) -> float:
        self_rect = await self.get_locator().bounding_box(timeout=timeout)
        target_rect = await target_locator.bounding_box(timeout=timeout)
        if self_rect is None or target_rect is None:
            return float("inf")  # Return infinity as the distance when element rect is not available

        y_1 = self_rect["y"] + self_rect["height"] - target_rect["y"]
        y_2 = self_rect["y"] - (target_rect["y"] + target_rect["height"])

        # if y1 * y2 <= 0, it means the two elements are overlapping
        if y_1 * y_2 <= 0:
            return 0

        return min(
            abs(y_1),
            abs(y_2),
        )

    async def calculate_min_x_distance_to(
        self,
        target_locator: Locator,
        timeout: float = settings.BROWSER_ACTION_TIMEOUT_MS,
    ) -> float:
        self_rect = await self.get_locator().bounding_box(timeout=timeout)
        target_rect = await target_locator.bounding_box(timeout=timeout)
        if self_rect is None or target_rect is None:
            return float("inf")  # Return infinity as the distance when element rect is not available

        x_1 = self_rect["x"] + self_rect["width"] - target_rect["x"]
        x_2 = self_rect["x"] - (target_rect["x"] + target_rect["width"])

        # if x1 * x2 <= 0, it means the two elements are overlapping
        if x_1 * x_2 <= 0:
            return 0

        return min(
            abs(x_1),
            abs(x_2),
        )

    async def is_next_to_element(
        self,
        target_locator: Locator,
        max_x_distance: float = 0,
        max_y_distance: float = 0,
        timeout: float = settings.BROWSER_ACTION_TIMEOUT_MS,
    ) -> bool:
        if max_x_distance > 0 and await self.calculate_min_x_distance_to(target_locator, timeout) > max_x_distance:
            return False

        if max_y_distance > 0 and await self.calculate_min_y_distance_to(target_locator, timeout) > max_y_distance:
            return False

        return True

    async def navigate_to_a_href(self, page: Page) -> str | None:
        if self.get_tag_name() != InteractiveElement.A:
            return None

        href = await self.should_use_navigation_instead_click(page)
        if not href:
            return None

        LOG.info(
            "Trying to navigate to the <a> href link instead of clicking",
            href=href,
            current_url=page.url,
        )
        try:
            await page.goto(href, timeout=settings.BROWSER_LOADING_TIMEOUT_MS)
            return href
        except Exception as e:
            # some cases use this method to download a file. but it will be redirected away soon
            # and agent will run into ABORTED error.
            error = str(e)
            if "net::ERR_ABORTED" in error:
                return href

            # some cases playwright will raise error like "Page.goto: Download is starting"
            if "Page.goto: Download is starting" in error:
                return href

            LOG.warning("Failed to navigate to the <a> href link", exc_info=True, href=href, current_url=page.url)
            raise

    async def refresh_select_options(self) -> tuple[list, str] | None:
        if self.get_tag_name() != InteractiveElement.SELECT:
            return None

        frame = await SkyvernFrame.create_instance(self.get_frame())
        options, selected_value = await frame.get_select_options(await self.get_element_handler())
        self.__static_element["options"] = options
        if "attributes" in self.__static_element:
            self.__static_element["attributes"]["selected"] = selected_value
            self._attributes = self.__static_element["attributes"]
        return options, selected_value


class DomUtil:
    """
    DomUtil is a python interface to interact with the DOM.
    The ultimate goal here is to provide a full python-js interaction.
    Some functions like wait_for_xxx should be supposed to define here.
    """

    def __init__(self, scraped_page: ScrapedPage, page: Page) -> None:
        self.scraped_page = scraped_page
        self.page = page

    async def check_id_in_dom(self, element_id: str) -> bool:
        css_selector = self.scraped_page.id_to_css_dict.get(element_id, "")
        if css_selector:
            return True
        return False

    async def get_skyvern_element_by_id(self, element_id: str) -> SkyvernElement:
        element = self.scraped_page.id_to_element_dict.get(element_id)
        if not element:
            raise MissingElementDict(element_id)

        frame = self.scraped_page.id_to_frame_dict.get(element_id)
        if not frame:
            raise MissingElementInIframe(element_id)

        css = self.scraped_page.id_to_css_dict.get(element_id)
        if not css:
            raise MissingElementInCSSMap(element_id)

        locator, frame_content = await resolve_locator(self.scraped_page, self.page, frame, css)

        num_elements = await locator.count()
        if num_elements < 1:
            xpath: str | None = element.get("xpath")
            if not xpath:
                LOG.warning("No elements found with css. Validation failed.", css=css, element_id=element_id)
                raise MissingElement(selector=css, element_id=element_id)
            else:
                # WARNING: current xpath is based on the tag name.
                # It can only represent the element position in the DOM tree with tag name, it's not 100% reliable.
                # As long as the current position has the same element with the tag name, the locator can be found.
                # (maybe) we should validate the element hash to make sure the element is the same?
                LOG.warning("Fallback to locator element by xpath.", xpath=xpath, element_id=element_id)
                locator = frame_content.locator(f"xpath={xpath}")
                num_elements = await locator.count()
                if num_elements < 1:
                    raise MissingElement(selector=xpath, element_id=element_id)

        elif num_elements > 1:
            LOG.warning(
                "Multiple elements found with css. Expected 1. Validation failed.",
                num_elements=num_elements,
                selector=css,
                element_id=element_id,
            )
            raise MultipleElementsFound(num=num_elements, selector=css, element_id=element_id)

        hash_value = self.scraped_page.id_to_element_hash.get(element_id, "")

        return SkyvernElement(locator, frame_content, element, hash_value)

    async def safe_get_skyvern_element_by_id(self, element_id: str) -> SkyvernElement | None:
        try:
            return await self.get_skyvern_element_by_id(element_id)
        except Exception:
            LOG.warning("Failed to get skyvern element by id", element_id=element_id, exc_info=True)
            return None
