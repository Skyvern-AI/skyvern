"""
A lightweight "agent" for interacting with the streaming browser over CDP.
"""

import typing
from contextlib import asynccontextmanager

import structlog
from playwright.async_api import Browser, BrowserContext, Page, Playwright, async_playwright

import skyvern.forge.sdk.routes.streaming.clients as sc
from skyvern.config import settings

LOG = structlog.get_logger()


class StreamingAgent:
    """
    A minimal agent that can connect to a browser via CDP and execute JavaScript.

    Specifically for operations during streaming sessions (like copy/pasting selected text, etc.).
    """

    def __init__(self, streaming: sc.Streaming) -> None:
        self.streaming = streaming
        self.browser: Browser | None = None
        self.browser_context: BrowserContext | None = None
        self.page: Page | None = None
        self.pw: Playwright | None = None

    async def connect(self, cdp_url: str | None = None) -> None:
        url = cdp_url or settings.BROWSER_REMOTE_DEBUGGING_URL

        LOG.info("StreamingAgent connecting to CDP", cdp_url=url)

        pw = self.pw or await async_playwright().start()

        self.pw = pw

        headers = {
            "x-api-key": self.streaming.x_api_key,
        }

        self.browser = await pw.chromium.connect_over_cdp(url, headers=headers)

        org_id = self.streaming.organization_id
        browser_session_id = (
            self.streaming.browser_session.persistent_browser_session_id if self.streaming.browser_session else None
        )

        if browser_session_id:
            cdp_session = await self.browser.new_browser_cdp_session()
            await cdp_session.send(
                "Browser.setDownloadBehavior",
                {
                    "behavior": "allow",
                    "downloadPath": f"/app/downloads/{org_id}/{browser_session_id}",
                    "eventsEnabled": True,
                },
            )

        contexts = self.browser.contexts
        if contexts:
            LOG.info("StreamingAgent using existing browser context")
            self.browser_context = contexts[0]
        else:
            LOG.warning("No existing browser context found, creating new one")
            self.browser_context = await self.browser.new_context()

        pages = self.browser_context.pages
        if pages:
            self.page = pages[0]
            LOG.info("StreamingAgent connected to page", url=self.page.url)
        else:
            LOG.warning("No pages found in browser context")

        LOG.info("StreamingAgent connected successfully")

    async def evaluate_js(
        self, expression: str, arg: str | int | float | bool | list | dict | None = None
    ) -> str | int | float | bool | list | dict | None:
        if not self.page:
            raise RuntimeError("StreamingAgent is not connected to a page. Call connect() first.")

        LOG.info("StreamingAgent evaluating JS", expression=expression[:100])

        try:
            result = await self.page.evaluate(expression, arg)
            LOG.info("StreamingAgent JS evaluation successful")
            return result
        except Exception as ex:
            LOG.exception("StreamingAgent JS evaluation failed", expression=expression, ex=str(ex))
            raise

    async def get_selected_text(self) -> str:
        LOG.info("StreamingAgent getting selected text")

        js_expression = """
        () => {
            const selection = window.getSelection();
            return selection ? selection.toString() : '';
        }
        """

        selected_text = await self.evaluate_js(js_expression)

        if isinstance(selected_text, str) or selected_text is None:
            LOG.info("StreamingAgent got selected text", length=len(selected_text) if selected_text else 0)
            return selected_text or ""

        raise RuntimeError(f"StreamingAgent selected text is not a string, but a(n) '{type(selected_text)}'")

    async def paste_text(self, text: str) -> None:
        LOG.info("StreamingAgent pasting text")

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

        LOG.info("StreamingAgent pasted text successfully")

    async def close(self) -> None:
        LOG.info("StreamingAgent closing connection")

        if self.browser:
            await self.browser.close()
            self.browser = None
            self.browser_context = None
            self.page = None

        if self.pw:
            await self.pw.stop()
            self.pw = None

        LOG.info("StreamingAgent closed")


@asynccontextmanager
async def connected_agent(streaming: sc.Streaming | None) -> typing.AsyncIterator[StreamingAgent]:
    """
    The first pass at this has us doing the following for every operation:
      - creating a new agent
      - connecting
      - [doing smth]
      - closing the agent

    This may add latency, but locally it is pretty fast. This keeps things stateless for now.

    If it turns out it's too slow, we can refactor to keep a persistent agent per streaming client.
    """

    if not streaming:
        msg = "connected_agent: no streaming client provided."
        LOG.error(msg)

        raise Exception(msg)

    if not streaming.browser_session or not streaming.browser_session.browser_address:
        msg = "connected_agent: no browser session or browser address found for streaming client."

        LOG.error(
            msg,
            client_id=streaming.client_id,
            organization_id=streaming.organization_id,
        )

        raise Exception(msg)

    agent = StreamingAgent(streaming=streaming)

    try:
        await agent.connect(streaming.browser_session.browser_address)

        # NOTE(jdo:streaming-local-dev): use BROWSER_REMOTE_DEBUGGING_URL from settings
        # await agent.connect()

        yield agent
    finally:
        await agent.close()
