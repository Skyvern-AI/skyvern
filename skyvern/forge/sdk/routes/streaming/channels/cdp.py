"""
A channel for connecting to a persistent browser instance.

What this channel looks like:

    [API Server] <--> [Browser (CDP)]

Channel data:

    CDP protocol data, with Playwright thrown in.
"""

from __future__ import annotations

import asyncio
import functools
import pathlib
import typing as t

import structlog
from playwright.async_api import Browser, BrowserContext, Page, Playwright, async_playwright

from skyvern.config import settings

if t.TYPE_CHECKING:
    from skyvern.forge.sdk.routes.streaming.channels.vnc import VncChannel

LOG = structlog.get_logger()


class CdpChannel:
    """
    CdpChannel. Relies on a VncChannel - without one, a CdpChannel has no
    r'aison d'etre.
    """

    def __new__(cls, *_: t.Iterable[t.Any], **__: t.Mapping[str, t.Any]) -> t.Self:  # noqa: N805
        if cls is CdpChannel:
            raise TypeError("CdpChannel class cannot be instantiated directly.")

        return super().__new__(cls)

    def __init__(self, *, vnc_channel: VncChannel) -> None:
        self.vnc_channel = vnc_channel
        # --
        self.browser: Browser | None = None
        self.browser_context: BrowserContext | None = None
        self.page: Page | None = None
        self.pw: Playwright | None = None
        self.url: str | None = None

    @property
    def class_name(self) -> str:
        return self.__class__.__name__

    @property
    def identity(self) -> t.Dict[str, t.Any]:
        base = self.vnc_channel.identity

        return base | {"url": self.url}

    async def connect(self, cdp_url: str | None = None) -> t.Self:
        """
        Idempotent.
        """

        if self.browser and self.browser.is_connected():
            return self

        await self.close()

        if cdp_url:
            url = cdp_url
        elif self.vnc_channel.browser_session and self.vnc_channel.browser_session.browser_address:
            url = self.vnc_channel.browser_session.browser_address
        else:
            url = settings.BROWSER_REMOTE_DEBUGGING_URL

        self.url = url

        LOG.info(f"{self.class_name} connecting to CDP", **self.identity)

        pw = self.pw or await async_playwright().start()

        self.pw = pw

        headers = (
            {
                "x-api-key": self.vnc_channel.x_api_key,
            }
            if self.vnc_channel.x_api_key
            else None
        )

        def on_close() -> None:
            LOG.warning(
                f"{self.class_name} closing because the persistent browser disconnected itself.", **self.identity
            )
            close_task = asyncio.create_task(self.close())
            close_task.add_done_callback(lambda _: asyncio.create_task(self.connect()))  # TODO: avoid blind reconnect

        self.browser = await pw.chromium.connect_over_cdp(url, headers=headers)
        self.browser.on("disconnected", on_close)

        await self.apply_download_behavior(self.browser)

        contexts = self.browser.contexts
        if contexts:
            LOG.info(f"{self.class_name} using existing browser context", **self.identity)
            self.browser_context = contexts[0]
        else:
            LOG.warning(f"{self.class_name} No existing browser context found, creating new one", **self.identity)
            self.browser_context = await self.browser.new_context()

        pages = self.browser_context.pages
        if pages:
            self.page = pages[0]
            LOG.info(f"{self.class_name} connected to page", **self.identity)
        else:
            LOG.warning(f"{self.class_name} No pages found in browser context", **self.identity)

        LOG.info(f"{self.class_name} connected successfully", **self.identity)

        return self

    async def apply_download_behavior(self, browser: Browser) -> t.Self:
        org_id = self.vnc_channel.organization_id

        browser_session_id = (
            self.vnc_channel.browser_session.persistent_browser_session_id if self.vnc_channel.browser_session else None
        )

        download_path = f"/app/downloads/{org_id}/{browser_session_id}" if browser_session_id else "/app/downloads/"

        cdp_session = await browser.new_browser_cdp_session()

        await cdp_session.send(
            "Browser.setDownloadBehavior",
            {
                "behavior": "allow",
                "downloadPath": download_path,
                "eventsEnabled": True,
            },
        )

        await cdp_session.detach()

        return self

    async def close(self) -> None:
        LOG.info(f"{self.class_name} closing connection", **self.identity)

        if self.browser:
            try:
                await self.browser.close()
            except Exception:
                pass
            self.browser = None

        if self.pw:
            await self.pw.stop()
            self.pw = None

        self.browser_context = None
        self.page = None

        LOG.info(f"{self.class_name} closed", **self.identity)

    async def evaluate_js(
        self,
        expression: str,
        arg: str | int | float | bool | list | dict | None = None,
    ) -> str | int | float | bool | list | dict | None:
        await self.connect()

        if not self.page:
            raise RuntimeError(f"{self.class_name} evaluate_js: not connected to a page. Call connect() first.")

        LOG.info(f"{self.class_name} evaluating js", expression=expression[:100], **self.identity)

        try:
            result = await self.page.evaluate(expression, arg)
            LOG.info(f"{self.class_name} evaluated js successfully", **self.identity)
            return result
        except Exception:
            LOG.exception(f"{self.class_name} failed to evaluate js", expression=expression, **self.identity)
            raise

    @functools.lru_cache(maxsize=None)
    def js(self, file_name: str) -> str:
        base_path = pathlib.Path(__file__).parent / "js"
        file_name = file_name.lstrip("/")

        if not file_name.endswith(".js"):
            file_name += ".js"

        relative_path = pathlib.Path(file_name)
        full_path = base_path / relative_path

        with open(full_path, encoding="utf-8") as f:
            return f.read()
