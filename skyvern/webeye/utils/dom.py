import typing
from enum import StrEnum

import structlog
from playwright.async_api import Locator, Page

from skyvern.exceptions import (
    ElementIsNotLabel,
    MissingElement,
    MissingElementDict,
    MissingElementInIframe,
    MultipleElementsFound,
)
from skyvern.forge.sdk.settings_manager import SettingsManager
from skyvern.webeye.actions.handler import resolve_locator
from skyvern.webeye.scraper.scraper import ScrapedPage

LOG = structlog.get_logger()
TEXT_INPUT_DELAY = 10


class InteractiveElement(StrEnum):
    INPUT = "input"
    SELECT = "select"
    BUTTON = "button"


class SkyvernElement:
    """
    SkyvernElement is a python interface to interact with js elements built during the scarping.
    When you try to interact with these elements by python, you are supposed to use this class as an interface.
    """

    def __init__(self, locator: Locator, static_element: dict) -> None:
        self.__static_element = static_element
        self.locator = locator

    def get_tag_name(self) -> str:
        return self.__static_element.get("tagName", "")

    def get_id(self) -> int | None:
        return self.__static_element.get("id")

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

    async def get_attr(
        self,
        attr_name: str,
        dynamic: bool = False,
        timeout: float = SettingsManager.get_settings().BROWSER_ACTION_TIMEOUT_MS,
    ) -> typing.Any:
        if not dynamic:
            if attr := self.__static_element.get("attributes", {}).get(attr_name):
                return attr

        return await self.locator.get_attribute(attr_name, timeout=timeout)

    async def input_sequentially(
        self, text: str, default_timeout: float = SettingsManager.get_settings().BROWSER_ACTION_TIMEOUT_MS
    ) -> None:
        total_timeout = max(len(text) * TEXT_INPUT_DELAY * 3, default_timeout)
        await self.locator.press_sequentially(text, timeout=total_timeout)


class DomUtil:
    """
    DomUtil is a python interface to interact with the DOM.
    The ultimate goal here is to provide a full python-js interaction.
    Some functions like wait_for_xxx should be supposed to define here.
    """

    def __init__(self, scraped_page: ScrapedPage, page: Page) -> None:
        self.scraped_page = scraped_page
        self.page = page

    async def get_skyvern_element_by_id(self, element_id: str) -> SkyvernElement:
        element = self.scraped_page.id_to_element_dict.get(element_id)
        if not element:
            raise MissingElementDict(element_id)

        frame = self.scraped_page.id_to_frame_dict.get(element_id)
        if not frame:
            raise MissingElementInIframe(element_id)

        xpath = self.scraped_page.id_to_xpath_dict[element_id]

        locator = resolve_locator(self.scraped_page, self.page, frame, xpath)

        num_elements = await locator.count()
        if num_elements < 1:
            LOG.warning("No elements found with xpath. Validation failed.", xpath=xpath)
            raise MissingElement(xpath=xpath, element_id=element_id)

        elif num_elements > 1:
            LOG.warning(
                "Multiple elements found with xpath. Expected 1. Validation failed.",
                num_elements=num_elements,
            )
            raise MultipleElementsFound(num=num_elements, xpath=xpath, element_id=element_id)

        return SkyvernElement(locator, element)
