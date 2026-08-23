"""Raw Chrome DevTools Protocol browser engine.

A Playwright-shaped facade over a direct CDP websocket. It attaches to browsers it did not start:
there is no launcher, no driver subprocess, and no bundled browser binary.
"""

from skyvern.webeye.skycdp.errors import (
    CdpConnectionError,
    CdpError,
    CdpProtocolError,
    CdpTargetClosedError,
    CdpTimeoutError,
    CdpUnsupportedOperation,
)
from skyvern.webeye.skycdp.facade.browser import Browser, BrowserContext, BrowserType, Skycdp, async_skycdp
from skyvern.webeye.skycdp.facade.elements import ElementHandle, JSHandle
from skyvern.webeye.skycdp.facade.locator import Locator
from skyvern.webeye.skycdp.facade.page import Frame, Page

__all__ = [
    "async_skycdp",
    "Skycdp",
    "BrowserType",
    "Browser",
    "BrowserContext",
    "Page",
    "Frame",
    "Locator",
    "ElementHandle",
    "JSHandle",
    "CdpError",
    "CdpTimeoutError",
    "CdpTargetClosedError",
    "CdpConnectionError",
    "CdpProtocolError",
    "CdpUnsupportedOperation",
]
