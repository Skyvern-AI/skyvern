"""
This channel exfiltrates all user activity in a browser.

What this channel looks like:

    [Skyvern App] <-- [API Server] <--> [Browser (CDP)]

Channel data:

    Raw JavaScript events (as JSON) over WebSockets.
"""

import asyncio
import dataclasses
import enum
import json
import time
import typing as t

import structlog
from playwright.async_api import CDPSession, ConsoleMessage, Page

from skyvern.forge.sdk.routes.streaming.channels.cdp import CdpChannel
from skyvern.forge.sdk.routes.streaming.channels.vnc import VncChannel

LOG = structlog.get_logger()


class ExfiltratedEventSource(enum.Enum):
    CONSOLE = "console"
    CDP = "cdp"
    NOT_SPECIFIED = "[not-specified]"


@dataclasses.dataclass
class ExfiltratedEvent:
    kind: t.Literal["exfiltrated-event"] = "exfiltrated-event"
    event_name: str = "[not-specified]"

    # TODO(jdo): improve typing for params
    params: dict = dataclasses.field(default_factory=dict)
    source: ExfiltratedEventSource = ExfiltratedEventSource.NOT_SPECIFIED
    timestamp: float = dataclasses.field(default_factory=lambda: time.time())  # seconds since epoch


OnExfiltrationEvent = t.Callable[[list[ExfiltratedEvent]], None]


class ExfiltrationChannel(CdpChannel):
    """
    ExfiltrationChannel.
    """

    def __init__(self, *, on_event: OnExfiltrationEvent, vnc_channel: VncChannel) -> None:
        self.cdp_session: CDPSession | None = None
        self.on_event = on_event

        super().__init__(vnc_channel=vnc_channel)

    def _handle_console_event(self, msg: ConsoleMessage) -> None:
        """Parse console messages for exfiltrated event data."""
        text = msg.text
        if text.startswith("[EXFIL]"):
            try:
                event_data = json.loads(text[7:])  # Strip '[EXFIL]' prefix

                messages = [
                    ExfiltratedEvent(
                        kind="exfiltrated-event",
                        event_name="user_interaction",
                        params=event_data,
                        source=ExfiltratedEventSource.CONSOLE,
                        timestamp=time.time(),
                    ),
                ]

                self.on_event(messages)
            except Exception:
                LOG.exception(f"{self.class_name} Failed to parse exfiltrated event", text=text)

    def _handle_cdp_event(self, event_name: str, params: dict) -> None:
        LOG.debug(f"{self.class_name} cdp event captured: {event_name}", params=params)

        messages = [
            ExfiltratedEvent(
                kind="exfiltrated-event",
                event_name=event_name,
                params=params,
                source=ExfiltratedEventSource.CDP,
                timestamp=time.time(),
            ),
        ]

        self.on_event(messages)

    async def adorn(self, page: Page) -> t.Self:
        """Add a mouse-following follower to the page."""
        if page.url.startswith("devtools:"):
            return self

        LOG.info(f"{self.class_name} adorning page.", url=page.url)

        (await page.evaluate(self.js("adorn")),)
        (await page.add_init_script(self.js("adorn")),)

        LOG.info(f"{self.class_name} adornment complete on page.", url=page.url)

        return self

    async def connect(self, cdp_url: str | None = None) -> t.Self:
        if self.browser and self.browser.is_connected() and self.cdp_session:
            return self

        await super().connect(cdp_url)

        # NOTE(jdo:streaming-local-dev)
        # from skyvern.config import settings
        # await super().connect(cdp_url or settings.BROWSER_REMOTE_DEBUGGING_URL)

        page = self.page

        if not page:
            raise RuntimeError(f"{self.class_name} No page available after connecting to browser.")

        self.cdp_session = await page.context.new_cdp_session(page)

        return self

    async def exfiltrate(self, page: Page) -> t.Self:
        """
        Track user interactions and send to console for CDP to capture.

        Uses add_init_script to ensure the exfiltration script is re-injected
        on every navigation (including address bar navigations).
        """
        if page.url.startswith("devtools:"):
            return self

        LOG.info(f"{self.class_name} setting up exfiltration on new page.", url=page.url)

        page.on("console", self._handle_console_event)

        await page.add_init_script(self.js("exfiltrate"))
        await page.evaluate(self.js("exfiltrate"))

        LOG.info(f"{self.class_name} setup complete on page.", url=page.url)

        return self

    async def decorate(self, page: Page) -> t.Self:
        """Add a mouse-following follower to the page."""
        if page.url.startswith("devtools:"):
            return self

        LOG.info(f"{self.class_name} adding decoration to page.", url=page.url)

        await page.add_init_script(self.js("decorate"))
        await page.evaluate(self.js("decorate"))

        LOG.info(f"{self.class_name} decoration setup complete on page.", url=page.url)

        return self

    async def undecorate(self, page: Page) -> t.Self:
        """Remove the mouse-following follower from the page."""
        if page.url.startswith("devtools:"):
            return self

        LOG.info(f"{self.class_name} removing decoration from page.", url=page.url)

        await page.add_init_script(self.js("undecorate"))
        await page.evaluate(self.js("undecorate"))

        LOG.info(f"{self.class_name} decoration removed from page.", url=page.url)

        return self

    async def enable_cdp_events(self) -> t.Self:
        await self.connect()

        cdp_session = self.cdp_session

        if not cdp_session:
            raise RuntimeError(f"{self.class_name} No CDP session available to enable events.")

        enables = [
            cdp_session.send("Runtime.enable"),
            cdp_session.send("DOM.enable"),
            cdp_session.send("Page.enable"),
            cdp_session.send("Target.setDiscoverTargets", {"discover": True}),
        ]

        await asyncio.gather(*enables)

        # listen to CDP events for tab management and navigation
        cdp_session.on("Target.targetCreated", lambda params: self._handle_cdp_event("target_created", params))
        cdp_session.on("Target.targetDestroyed", lambda params: self._handle_cdp_event("target_destroyed", params))
        cdp_session.on("Target.targetInfoChanged", lambda params: self._handle_cdp_event("target_info_changed", params))
        cdp_session.on(
            "Page.frameRequestedNavigation",
            lambda params: self._handle_cdp_event("nav:frame_requested_navigation", params),
        )
        cdp_session.on(
            "Page.frameStartedNavigating", lambda params: self._handle_cdp_event("nav:frame_started_navigating", params)
        )
        cdp_session.on("Page.frameNavigated", lambda params: self._handle_cdp_event("nav:frame_navigated", params))
        cdp_session.on(
            "Page.navigatedWithinDocument",
            lambda params: self._handle_cdp_event("nav:navigated_within_document", params),
        )

        return self

    async def enable_adornment(self) -> t.Self:
        browser_context = self.browser_context

        if not browser_context:
            LOG.error(f"{self.class_name} no browser context to enable adornment.")
            return self

        tasks: list[asyncio.Task] = []
        for page in browser_context.pages:
            tasks.append(asyncio.create_task(self.adorn(page)))

        await asyncio.gather(*tasks)

        browser_context.on("page", lambda page: asyncio.create_task(self.adorn(page)))

        return self

    def enable_console_events(self) -> t.Self:
        browser_context = self.browser_context

        if not browser_context:
            LOG.error(f"{self.class_name} no browser context to enable console events.")
            return self

        for page in browser_context.pages:
            asyncio.create_task(self.exfiltrate(page))

        browser_context.on("page", lambda page: asyncio.create_task(self.exfiltrate(page)))

        return self

    def enable_decoration(self) -> t.Self:
        browser_context = self.browser_context

        if not browser_context:
            LOG.error(f"{self.class_name} no browser context to enable decoration.")
            return self

        for page in browser_context.pages:
            asyncio.create_task(self.decorate(page))

        browser_context.on("page", lambda page: asyncio.create_task(self.decorate(page)))

        return self

    async def start(self) -> t.Self:
        LOG.info(f"{self.class_name} starting.")

        await self.enable_cdp_events()

        await self.enable_adornment()

        self.enable_console_events()

        self.enable_decoration()

        return self

    async def stop(self) -> t.Self:
        LOG.info(f"{self.class_name} stopping.")

        if not self.cdp_session:
            return self

        try:
            await self.cdp_session.detach()
        except Exception:
            pass

        self.cdp_session = None

        pages = self.browser_context.pages if self.browser_context else []

        for page in pages:
            try:
                page.remove_listener("console", self._handle_console_event)
            except KeyError:
                pass  # listener not found
            await self.undecorate(page)

        LOG.info(f"{self.class_name} stopped.")

        return self
