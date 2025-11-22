"""
A channel for executing JavaScript against a persistent browser instance.

What this channel looks like:

    [API Server] <--> [Browser (CDP)]

Channel data:

    Chrome DevTools Protocol (CDP) over WebSockets. We cheat and use Playwright.
"""

from __future__ import annotations

import typing as t
from contextlib import asynccontextmanager

import structlog
from playwright.async_api import Browser, BrowserContext, Page, Playwright

from skyvern.forge.sdk.routes.streaming.channels.cdp import CdpChannel

if t.TYPE_CHECKING:
    from skyvern.forge.sdk.routes.streaming.channels.vnc import VncChannel

LOG = structlog.get_logger()


class ExecutionChannel(CdpChannel):
    # Explicitly declare inherited attributes for mypy when follow_imports = skip
    browser: Browser | None
    browser_context: BrowserContext | None
    page: Page | None
    pw: Playwright | None
    """
    ExecutionChannel.
    """

    @property
    def class_name(self) -> str:
        return self.__class__.__name__

    async def get_selected_text(self) -> str:
        LOG.info(f"{self.class_name} getting selected text", **self.identity)

        js_expression = """
        () => {
            const selection = window.getSelection();
            return selection ? selection.toString() : '';
        }
        """

        selected_text = await self.evaluate_js(js_expression)

        if isinstance(selected_text, str) or selected_text is None:
            LOG.info(
                f"{self.class_name} got selected text",
                length=len(selected_text) if selected_text else 0,
                **self.identity,
            )
            return selected_text or ""

        raise RuntimeError(f"{self.class_name} selected text is not a string, but a(n) '{type(selected_text)}'")

    async def paste_text(self, text: str) -> None:
        LOG.info(f"{self.class_name} pasting text", **self.identity)

        js_expression = """
        (text) => {
            const activeElement = document.activeElement;
            if (activeElement && (activeElement.tagName === 'INPUT' || activeElement.tagName === 'TEXTAREA' || activeElement.isContentEditable)) {
                const start = activeElement.selectionStart || 0;
                const end = activeElement.selectionEnd || 0;
                const value = activeElement.value || '';
                activeElement.value = value.slice(0, start) + text + value.slice(end);
                const newCursorPos = start + text.length;
                activeElement.setSelectionRange(newCursorPos, newCursorPos);
            }
        }
        """

        await self.evaluate_js(js_expression, text)

        LOG.info(f"{self.class_name} pasted text successfully", **self.identity)

    async def close(self) -> None:
        LOG.info(f"{self.class_name} closing connection", **self.identity)

        if self.browser:
            await self.browser.close()
            self.browser = None
            self.browser_context = None
            self.page = None

        if self.pw:
            await self.pw.stop()
            self.pw = None

        LOG.info(f"{self.class_name} closed", **self.identity)


@asynccontextmanager
async def execution_channel(vnc_channel: VncChannel) -> t.AsyncIterator[ExecutionChannel]:
    """
    The first pass at this has us doing the following for every operation:
      - creating a new channel
      - connecting
      - [doing smth]
      - closing the channel

    This may add latency, but locally it is pretty fast. This keeps things stateless for now.

    If it turns out it's too slow, we can refactor to keep a persistent channel per vnc client.
    """

    channel = ExecutionChannel(vnc_channel=vnc_channel)

    try:
        await channel.connect()

        # NOTE(jdo:streaming-local-dev)
        # from skyvern.config import settings
        # await channel.connect(settings.BROWSER_REMOTE_DEBUGGING_URL)

        yield channel
    finally:
        await channel.close()
