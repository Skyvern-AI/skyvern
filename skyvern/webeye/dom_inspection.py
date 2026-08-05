from playwright.async_api import ElementHandle, Frame, Locator, Page

from skyvern.webeye.utils.page import SkyvernFrame

_READ_CURRENT_URL = "() => document.location.href"
_READ_LOCATOR_TAG_NAME = "element => element.tagName"
_READ_RESOLVED_ANCHOR_HREF = "(element) => element instanceof HTMLAnchorElement ? element.href : null"
_READ_WHETHER_LINK_OR_BUTTON = "(element) => element.matches('a[href], button')"


async def read_current_url(frame: Page | Frame) -> str | None:
    value = await SkyvernFrame.evaluate(frame=frame, expression=_READ_CURRENT_URL)
    return value if isinstance(value, str) else None


async def read_locator_tag_name(locator: Locator, *, timeout: float | None = None) -> str | None:
    value = await locator.evaluate(_READ_LOCATOR_TAG_NAME, timeout=timeout)
    return value if isinstance(value, str) else None


async def read_resolved_anchor_href(frame: Page | Frame, element: ElementHandle) -> str | None:
    value = await SkyvernFrame.evaluate(frame=frame, expression=_READ_RESOLVED_ANCHOR_HREF, arg=element)
    return value if isinstance(value, str) else None


async def read_whether_link_or_button(frame: Page | Frame, element: ElementHandle) -> bool | None:
    value = await SkyvernFrame.evaluate(frame=frame, expression=_READ_WHETHER_LINK_OR_BUTTON, arg=element)
    return value if isinstance(value, bool) else None
