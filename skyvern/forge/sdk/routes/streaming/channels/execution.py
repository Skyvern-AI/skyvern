"""
A channel for executing JavaScript against a persistent browser instance.

What this channel looks like:

    [API Server] <--> [Browser (CDP)]

Channel data:

    Chrome DevTools Protocol (CDP) over WebSockets. We cheat and use Playwright.
"""

from __future__ import annotations

import base64
import typing as t
from contextlib import asynccontextmanager
from urllib.parse import urlparse

import structlog
from playwright.async_api import Browser, BrowserContext, Page, Playwright

from skyvern.config import settings
from skyvern.forge.sdk.routes.streaming.channels.cdp import CdpChannel
from skyvern.forge.sdk.routes.streaming.payload_limits import MAX_SCREENSHOT_BYTES
from skyvern.forge.sdk.routes.streaming.registries import get_vnc_channel
from skyvern.webeye.main_world_eval import evaluate_in_main_world

if t.TYPE_CHECKING:
    from skyvern.forge.sdk.routes.streaming.channels.message import MessageChannel
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

    async def get_current_url(self) -> str:
        LOG.debug(f"{self.class_name} getting current URL", **self.identity)

        if not self.page:
            raise RuntimeError(f"{self.class_name} get_current_url: not connected to a page.")

        url = self.page.url
        LOG.debug(f"{self.class_name} got current URL", url=url, **self.identity)
        return url or ""

    async def paste_text(self, text: str) -> None:
        LOG.info(f"{self.class_name} pasting text", **self.identity)

        if not self.page:
            raise RuntimeError(f"{self.class_name} paste_text: not connected to a page.")

        await self.page.keyboard.insert_text(text)

        LOG.info(f"{self.class_name} pasted text successfully", **self.identity)

    async def navigate(self, url: str) -> None:
        LOG.info(f"{self.class_name} navigating", target_url=url, **self.identity)

        if not self.page:
            raise RuntimeError(f"{self.class_name} navigate: not connected to a page.")

        normalized = self._normalize_url(url)

        await self.page.goto(
            normalized,
            wait_until="domcontentloaded",
            timeout=settings.BROWSER_ACTION_TIMEOUT_MS,
        )

        LOG.info(f"{self.class_name} navigated", target_url=normalized, **self.identity)

    async def reload(self, hard: bool = False) -> None:
        LOG.info(f"{self.class_name} reloading", hard=hard, **self.identity)

        if not self.page:
            raise RuntimeError(f"{self.class_name} reload: not connected to a page.")

        if hard:
            cdp_session = await self.page.context.new_cdp_session(self.page)
            try:
                await cdp_session.send("Network.clearBrowserCache")
            finally:
                await cdp_session.detach()

        await self.page.reload(
            wait_until="domcontentloaded",
            timeout=settings.BROWSER_ACTION_TIMEOUT_MS,
        )

    async def go_back(self) -> None:
        LOG.info(f"{self.class_name} going back", **self.identity)

        if not self.page:
            raise RuntimeError(f"{self.class_name} go_back: not connected to a page.")

        # Playwright returns None when there is no history to go back to; that is fine.
        await self.page.go_back(
            wait_until="domcontentloaded",
            timeout=settings.BROWSER_ACTION_TIMEOUT_MS,
        )

    async def go_forward(self) -> None:
        LOG.info(f"{self.class_name} going forward", **self.identity)

        if not self.page:
            raise RuntimeError(f"{self.class_name} go_forward: not connected to a page.")

        await self.page.go_forward(
            wait_until="domcontentloaded",
            timeout=settings.BROWSER_ACTION_TIMEOUT_MS,
        )

    async def take_screenshot(self) -> str:
        LOG.info(f"{self.class_name} taking screenshot", **self.identity)

        if not self.page:
            raise RuntimeError(f"{self.class_name} take_screenshot: not connected to a page.")

        png_bytes = await self.page.screenshot(type="png", full_page=False)

        if len(png_bytes) > MAX_SCREENSHOT_BYTES:
            raise RuntimeError(
                f"{self.class_name} screenshot too large: {len(png_bytes)} bytes (max {MAX_SCREENSHOT_BYTES})",
            )

        return base64.b64encode(png_bytes).decode("ascii")

    async def clear_cookies(self) -> None:
        LOG.info(f"{self.class_name} clearing cookies", **self.identity)

        if not self.browser_context:
            raise RuntimeError(f"{self.class_name} clear_cookies: no browser context.")

        await self.browser_context.clear_cookies()

    async def clear_storage(self) -> None:
        """Clear storage for the current page's origin: cookies, local/session storage, IndexedDB, caches, service workers."""

        LOG.info(f"{self.class_name} clearing all storage for origin", **self.identity)

        if not self.page or not self.browser_context:
            raise RuntimeError(f"{self.class_name} clear_storage: not connected.")

        origin = self._origin_of(self.page.url)

        cdp_session = await self.browser_context.new_cdp_session(self.page)
        try:
            await cdp_session.send(
                "Storage.clearDataForOrigin",
                {
                    "origin": origin,
                    "storageTypes": (
                        "cookies,local_storage,session_storage,indexeddb,websql,"
                        "service_workers,cache_storage,shader_cache,file_systems"
                    ),
                },
            )
        finally:
            await cdp_session.detach()

    async def clear_history(self) -> None:
        LOG.info(
            f"{self.class_name} clearing history (cache, SW/cache storage, navigation stack)",
            **self.identity,
        )

        if not self.page or not self.browser_context:
            raise RuntimeError(f"{self.class_name} clear_history: not connected.")

        origin = self._origin_of(self.page.url)

        cdp_session = await self.browser_context.new_cdp_session(self.page)
        try:
            await cdp_session.send("Network.clearBrowserCache")
            await cdp_session.send(
                "Storage.clearDataForOrigin",
                {"origin": origin, "storageTypes": "service_workers,cache_storage"},
            )
            await cdp_session.send("Page.resetNavigationHistory")
        finally:
            await cdp_session.detach()

    @staticmethod
    def _normalize_url(url: str) -> str:
        candidate = url.strip()
        if not candidate:
            raise ValueError("URL must not be empty")
        if candidate.startswith("//"):
            raise ValueError("URL must include an explicit http(s) scheme or host")

        parsed = urlparse(candidate)
        if parsed.scheme in ("http", "https"):
            return candidate
        if parsed.scheme:
            raise ValueError(f"refusing to navigate to non-http(s) scheme: {parsed.scheme}")
        # Bare host or path -- prepend https://.
        return f"https://{candidate}"

    @staticmethod
    def _origin_of(url: str) -> str:
        parsed = urlparse(url)
        if not parsed.scheme or not parsed.netloc:
            return ""
        return f"{parsed.scheme}://{parsed.netloc}"

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


class LocalExecutionChannel(ExecutionChannel):
    def __init__(self, *, page: Page) -> None:  # type: ignore[override]
        # bypass CdpChannel.__init__ which requires a VncChannel
        self.vnc_channel = None  # type: ignore[assignment]
        self.browser = None
        self.browser_context = page.context
        self.page = page
        self.pw = None
        self.url = None
        self._closing = False

    @property
    def identity(self) -> dict[str, t.Any]:
        # Use `page_url` (not `url`) to avoid colliding with the inherited
        # get_current_url log call which already passes `url=...` as a kwarg.
        return {"local_execution": True, "page_url": self.page.url if self.page else None}

    async def evaluate_js(
        self,
        expression: str,
        arg: str | int | float | bool | list | dict | None = None,
    ) -> str | int | float | bool | list | dict | None:
        # Skip the inherited connect() — it dereferences self.vnc_channel,
        # which LocalExecutionChannel intentionally leaves None.
        if not self.page:
            raise RuntimeError(f"{self.class_name} evaluate_js: no page available.")
        return await evaluate_in_main_world(self.page, expression, arg)

    async def close(self) -> None:
        # We don't own the page or context; do not close.
        return None


@asynccontextmanager
async def execution_for_message_channel(
    message_channel: MessageChannel,
) -> t.AsyncIterator[ExecutionChannel]:
    """
    Resolve an ExecutionChannel for a given message channel.

    Prefers the VNC pipeline (remote CDP via VncChannel.browser_session.browser_address) when a
    VncChannel is registered for this client. Falls back to the in-process Page held by
    PERSISTENT_SESSIONS_MANAGER, which is what local dev (and any single-process deployment) uses.
    """

    vnc_channel = get_vnc_channel(message_channel.client_id)

    if vnc_channel is not None:
        async with execution_channel(vnc_channel) as execute:
            yield execute
        return

    # Imports kept local to avoid a circular import (this module is imported by message.py,
    # and the persistent sessions manager pulls in app/cloud bootstrapping).
    from skyvern.forge import app
    from skyvern.forge.sdk.routes.streaming.screencast import wait_for_browser_state

    if message_channel.browser_session is None:
        raise RuntimeError("execution_for_message_channel: no browser session on message channel")

    browser_session_id = message_channel.browser_session.persistent_browser_session_id

    session = await app.PERSISTENT_SESSIONS_MANAGER.get_session(
        session_id=browser_session_id,
        organization_id=message_channel.organization_id,
    )
    if session is None:
        raise RuntimeError(f"execution_for_message_channel: session {browser_session_id} not found")

    browser_state = await wait_for_browser_state(
        browser_session_id,
        "browser_session",
        organization_id=message_channel.organization_id,
    )
    if browser_state is None:
        raise RuntimeError(f"execution_for_message_channel: browser state timeout for {browser_session_id}")

    page = await browser_state.get_working_page()
    if page is None:
        raise RuntimeError(f"execution_for_message_channel: no working page for {browser_session_id}")

    yield LocalExecutionChannel(page=page)


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

        yield channel
    finally:
        await channel.close()
