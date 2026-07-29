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
from skyvern.webeye.cdp_connection import (
    connect_over_cdp_with_diagnostics,
    is_local_pbs_cdp_url,
    resolve_local_pbs_cdp_url,
)
from skyvern.webeye.main_world_eval import evaluate_in_main_world

if t.TYPE_CHECKING:
    from skyvern.forge.sdk.routes.streaming.channels.vnc import VncChannel

LOG = structlog.get_logger()


@functools.lru_cache(maxsize=None)
def _load_js_asset(file_name: str) -> str:
    # Module-level so the cache key is the asset name only. A method-level lru_cache
    # keys on `self`, pinning every channel instance (and its Playwright driver) for
    # the lifetime of the process.
    base_path = pathlib.Path(__file__).parent / "js"
    file_name = file_name.lstrip("/")

    if not file_name.endswith(".js"):
        file_name += ".js"

    full_path = base_path / pathlib.Path(file_name)

    with open(full_path, encoding="utf-8") as f:
        return f.read()


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
        # Set True by a terminal stop() so the browser "disconnected" callback (registered
        # in connect()) does not resurrect the connection during teardown. close() stays
        # reconnect-neutral because connect() calls it to recycle a dropped connection.
        self._closing = False

    @property
    def class_name(self) -> str:
        return self.__class__.__name__

    @property
    def identity(self) -> t.Dict[str, t.Any]:
        base = self.vnc_channel.identity

        return base | {"cdp_url": self.url}

    async def connect(self, cdp_url: str | None = None) -> t.Self:
        """
        Idempotent.
        """

        # A channel torn down by stop() never reconnects: this also neutralizes a
        # reconnect task the "disconnected" callback may have scheduled just before teardown.
        if self._closing:
            return self

        if self.browser and self.browser.is_connected():
            return self

        await self.close()

        if cdp_url:
            url = cdp_url
        elif self.vnc_channel.browser_session and self.vnc_channel.browser_session.browser_address:
            url = self.vnc_channel.browser_session.browser_address
        else:
            url = settings.BROWSER_REMOTE_DEBUGGING_URL

        url = resolve_local_pbs_cdp_url(url)

        self.url = url

        LOG.info(f"{self.class_name} connecting to CDP", **self.identity)

        pw = self.pw or await async_playwright().start()

        self.pw = pw

        headers: dict[str, str] | None = None
        if self.vnc_channel.x_api_key:
            headers = {"x-api-key": self.vnc_channel.x_api_key}
        if self.vnc_channel.browser_session and is_local_pbs_cdp_url(url):
            headers = headers or {}
            headers["X-Session-Id"] = self.vnc_channel.browser_session.persistent_browser_session_id

        def on_close() -> None:
            if self._closing:
                return
            LOG.warning(
                f"{self.class_name} closing because the persistent browser disconnected itself.", **self.identity
            )
            close_task = asyncio.create_task(self.close())
            close_task.add_done_callback(lambda _: asyncio.create_task(self.connect()))  # TODO: avoid blind reconnect

        self.browser = await connect_over_cdp_with_diagnostics(pw, url, headers=headers if headers else None)
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

        try:
            if self.browser:
                try:
                    await self.browser.close()
                except Exception:
                    pass
                self.browser = None
        finally:
            # Release the driver even when browser.close() raised on a dead target;
            # a skipped stop() orphans the node subprocess for the process lifetime.
            if self.pw:
                try:
                    await self.pw.stop()
                except Exception:
                    LOG.warning(f"{self.class_name} failed to stop playwright driver", **self.identity, exc_info=True)
                self.pw = None

            self.browser_context = None
            self.page = None

        LOG.info(f"{self.class_name} closed", **self.identity)

    async def stop(self) -> t.Self:
        """Terminal close: unlike close() (which connect() reuses to recycle a dropped
        connection), stop() marks the channel closing so the browser "disconnected"
        callback registered in connect() cannot resurrect a fresh driver."""
        self._closing = True
        await self.close()
        return self

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
            result = await evaluate_in_main_world(self.page, expression, arg)
            LOG.info(f"{self.class_name} evaluated js successfully", **self.identity)
            return result
        except Exception:
            LOG.exception(f"{self.class_name} failed to evaluate js", expression=expression, **self.identity)
            raise

    def js(self, file_name: str) -> str:
        return _load_js_asset(file_name)
