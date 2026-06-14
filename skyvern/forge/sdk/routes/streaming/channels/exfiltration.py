"""
This channel exfiltrates all user activity in a browser.

What this channel looks like:

    [Skyvern App] <-- [API Server] <--> [Browser (CDP)]

Channel data:

    Raw JavaScript events (as JSON) over WebSockets.
"""

import asyncio
import contextlib
import dataclasses
import enum
import json
import time
import typing as t
import weakref

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


@dataclasses.dataclass
class PageConsoleCapture:
    console_listener: t.Callable[[ConsoleMessage], object]
    cdp_session: CDPSession | None = None


class ExfiltrationChannel(CdpChannel):
    """
    ExfiltrationChannel.
    """

    BINDING_NAME: t.ClassVar[str] = "__skyvern_exfiltrate_event"
    CONSOLE_DEDUP_TTL_SECONDS: t.ClassVar[float] = 5.0
    REFRESH_INTERVAL_SECONDS: t.ClassVar[float] = 1.0
    NETWORK_ACTIVITY_THROTTLE_SECONDS: t.ClassVar[float] = 1.0
    _active_binding_channels: t.ClassVar[weakref.WeakKeyDictionary[Page, "ExfiltrationChannel"]] = (
        weakref.WeakKeyDictionary()
    )
    _binding_registered_pages: t.ClassVar[weakref.WeakSet[Page]] = weakref.WeakSet()
    _adorn_init_script_pages: t.ClassVar[weakref.WeakSet[Page]] = weakref.WeakSet()
    _rearm_in_flight_pages: t.ClassVar[weakref.WeakSet[Page]] = weakref.WeakSet()

    def __init__(self, *, on_event: OnExfiltrationEvent, vnc_channel: VncChannel) -> None:
        self.cdp_session: CDPSession | None = None
        self.on_event = on_event
        self._page_console_captures: weakref.WeakKeyDictionary[Page, PageConsoleCapture] = weakref.WeakKeyDictionary()
        self._recent_console_event_fingerprints: dict[str, float] = {}
        self._pending_event_tasks: set[asyncio.Task[None]] = set()
        self._refresh_task: asyncio.Task | None = None
        self._network_activity_count = 0
        self._last_network_activity_emit = 0.0
        self._network_activity_flush_task: asyncio.Task[None] | None = None
        self._capture_paused = False

        super().__init__(vnc_channel=vnc_channel)

    def pause_capture(self) -> None:
        self._capture_paused = True

    def resume_capture(self) -> None:
        self._capture_paused = False

    def _emit_events(self, messages: list[ExfiltratedEvent]) -> None:
        if self._capture_paused:
            return
        self.on_event(messages)

    def _track_event_task(self, coro: t.Coroutine[t.Any, t.Any, None]) -> None:
        task = asyncio.create_task(coro)
        self._pending_event_tasks.add(task)
        task.add_done_callback(self._on_event_task_done)

    def _on_event_task_done(self, task: asyncio.Task[None]) -> None:
        self._pending_event_tasks.discard(task)
        if task.cancelled():
            return

        try:
            task.result()
        except Exception:
            LOG.exception(f"{self.class_name} async exfiltration event task failed")

    def _parse_exfil_payload(self, payload: object) -> dict[str, t.Any] | None:
        if isinstance(payload, str):
            try:
                payload = json.loads(payload)
            except json.JSONDecodeError:
                return None

        if isinstance(payload, dict):
            return t.cast(dict[str, t.Any], payload)

        return None

    def _parse_exfil_text(self, text: str) -> dict[str, t.Any] | None:
        if not text.startswith("[EXFIL]"):
            return None

        return self._parse_exfil_payload(text[7:].strip())

    def _parse_exfil_args(self, args: list[object]) -> dict[str, t.Any] | None:
        if len(args) < 2 or args[0] != "[EXFIL]":
            return None

        return self._parse_exfil_payload(args[1])

    def _extract_cdp_remote_object_value(self, arg: object) -> object:
        if not isinstance(arg, dict):
            return arg

        if "value" in arg:
            return arg["value"]

        if arg.get("type") == "string" and "description" in arg:
            return arg["description"]

        preview = arg.get("preview")
        if isinstance(preview, dict):
            properties = preview.get("properties")
            if isinstance(properties, list):
                materialized: dict[str, object] = {}
                for prop in properties:
                    if not isinstance(prop, dict):
                        continue
                    name = prop.get("name")
                    if not isinstance(name, str):
                        continue
                    if "value" in prop:
                        materialized[name] = prop["value"]
                    elif isinstance(prop.get("valuePreview"), dict) and "value" in prop["valuePreview"]:
                        materialized[name] = prop["valuePreview"]["value"]
                if materialized:
                    return materialized

        return arg

    def _prune_console_dedup_cache(self, now: float) -> None:
        expired = [
            fingerprint
            for fingerprint, emitted_at in self._recent_console_event_fingerprints.items()
            if now - emitted_at > self.CONSOLE_DEDUP_TTL_SECONDS
        ]
        for fingerprint in expired:
            self._recent_console_event_fingerprints.pop(fingerprint, None)

    def _should_emit_console_event(self, event_data: dict[str, t.Any]) -> bool:
        try:
            fingerprint = json.dumps(event_data, sort_keys=True, separators=(",", ":"))
        except TypeError:
            fingerprint = json.dumps(event_data, sort_keys=True, separators=(",", ":"), default=str)

        now = time.monotonic()
        self._prune_console_dedup_cache(now)
        previous = self._recent_console_event_fingerprints.get(fingerprint)
        if previous is not None and now - previous <= self.CONSOLE_DEDUP_TTL_SECONDS:
            return False

        self._recent_console_event_fingerprints[fingerprint] = now
        return True

    def _emit_console_event(self, event_data: dict[str, t.Any]) -> None:
        if not self._should_emit_console_event(event_data):
            return

        self._emit_events(
            [
                ExfiltratedEvent(
                    kind="exfiltrated-event",
                    event_name="user_interaction",
                    params=event_data,
                    source=ExfiltratedEventSource.CONSOLE,
                    timestamp=time.time(),
                )
            ]
        )

    def _handle_binding_event(self, source: dict[str, t.Any], payload: object) -> None:
        page = source.get("page") if isinstance(source, dict) else None
        active_channel = self._active_binding_channels.get(page, self) if page else self
        event_data = active_channel._parse_exfil_payload(payload)
        if event_data is None:
            return

        active_channel._emit_console_event(event_data)

    async def _handle_console_event_async(self, msg: ConsoleMessage) -> None:
        """Parse Playwright console messages for exfiltrated event data."""
        event_data: dict[str, t.Any] | None = None
        try:
            args = []
            for arg in msg.args[:2]:
                args.append(await arg.json_value())
            event_data = self._parse_exfil_args(args)
        except Exception:
            LOG.debug(f"{self.class_name} Failed to inspect console args for EXFIL event", exc_info=True)

        text = msg.text
        if event_data is None:
            event_data = self._parse_exfil_text(text)

        if event_data is None:
            return

        self._emit_console_event(event_data)

    def _handle_console_event(self, msg: ConsoleMessage) -> None:
        self._track_event_task(self._handle_console_event_async(msg))

    async def _handle_runtime_console_event_async(self, params: dict[str, t.Any]) -> None:
        raw_args = params.get("args")
        if not isinstance(raw_args, list):
            return

        event_data = self._parse_exfil_args([self._extract_cdp_remote_object_value(arg) for arg in raw_args[:2]])
        if event_data is None:
            return

        self._emit_console_event(event_data)

    async def _attach_page_cdp_console_capture(self, page: Page) -> CDPSession | None:
        cdp_session = await page.context.new_cdp_session(page)
        await cdp_session.send("Runtime.enable")
        cdp_session.on(
            "Runtime.consoleAPICalled",
            lambda params: self._track_event_task(self._handle_runtime_console_event_async(params)),
        )
        return cdp_session

    async def _ensure_binding(self, page: Page) -> None:
        self._active_binding_channels[page] = self

        if page in self._binding_registered_pages:
            return

        try:
            await page.expose_binding(self.BINDING_NAME, self._handle_binding_event)
            self._binding_registered_pages.add(page)
        except Exception:
            LOG.debug(f"{self.class_name} failed to expose exfiltration binding", page_url=page.url, exc_info=True)

    async def _install_exfiltration_script(self, page: Page, *, add_init_script: bool) -> None:
        binding_script = f"window.__skyvern_exfiltration_binding_name = {json.dumps(self.BINDING_NAME)};"

        if add_init_script:
            await page.add_init_script(binding_script)
            await page.add_init_script(self.js("exfiltrate"))

        await page.evaluate(binding_script)
        await page.evaluate(self.js("exfiltrate"))

    async def _refresh_exfiltration_loop(self) -> None:
        while True:
            await asyncio.sleep(self.REFRESH_INTERVAL_SECONDS)

            browser_context = self.browser_context
            if not browser_context:
                continue

            for page in list(browser_context.pages):
                if page.url.startswith("devtools:"):
                    continue
                try:
                    await self.exfiltrate(page)
                except Exception:
                    LOG.debug(
                        f"{self.class_name} failed to refresh exfiltration on page",
                        url=page.url,
                        exc_info=True,
                    )

    def _handle_network_activity(self) -> None:
        self._network_activity_count += 1

        now = time.monotonic()
        elapsed = now - self._last_network_activity_emit
        if elapsed < self.NETWORK_ACTIVITY_THROTTLE_SECONDS:
            if not self._network_activity_flush_task or self._network_activity_flush_task.done():
                delay = self.NETWORK_ACTIVITY_THROTTLE_SECONDS - elapsed
                self._network_activity_flush_task = asyncio.create_task(self._flush_network_activity_after(delay))
            return

        self._emit_network_activity(now)

    async def _flush_network_activity_after(self, delay: float) -> None:
        await asyncio.sleep(delay)
        self._emit_network_activity(time.monotonic())

    def _emit_network_activity(self, now: float) -> None:
        if self._network_activity_count == 0:
            return

        self._last_network_activity_emit = now
        count = self._network_activity_count
        self._network_activity_count = 0
        self._handle_cdp_event("net:activity", {"count": count})

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

        self._emit_events(messages)

        if event_name == "nav:frame_navigated":
            page = self.page
            if not page or page.url.startswith("devtools:"):
                return

            if page in self._rearm_in_flight_pages:
                return

            self._track_event_task(self._rearm_page_after_navigation(page, event_name=event_name))

    async def _rearm_page_after_navigation(self, page: Page, *, event_name: str) -> None:
        if page.url.startswith("devtools:"):
            return

        self._rearm_in_flight_pages.add(page)
        try:
            LOG.info(
                "re-applying exfiltration and adornment after navigation",
                class_name=self.class_name,
                event_name=event_name,
                url=page.url,
            )

            try:
                await page.wait_for_load_state("domcontentloaded", timeout=10_000)
            except Exception:
                LOG.warning(
                    "navigation re-arm timed out waiting for domcontentloaded",
                    class_name=self.class_name,
                    event_name=event_name,
                    url=page.url,
                    exc_info=True,
                )

            try:
                await self._ensure_binding(page)
                await self.exfiltrate(page)
                await self.adorn(page)
            except Exception:
                LOG.warning(
                    "failed to re-arm exfiltration after navigation",
                    class_name=self.class_name,
                    event_name=event_name,
                    url=page.url,
                    exc_info=True,
                )
        finally:
            self._rearm_in_flight_pages.discard(page)

    async def adorn(self, page: Page) -> t.Self:
        """Add a mouse-following follower to the page."""
        if page.url.startswith("devtools:"):
            return self

        LOG.info(f"{self.class_name} adorning page.", url=page.url)

        await page.evaluate(self.js("adorn"))
        if page not in self._adorn_init_script_pages:
            await page.add_init_script(self.js("adorn"))
            self._adorn_init_script_pages.add(page)

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

        existing_capture = self._page_console_captures.get(page)
        if existing_capture:
            self._active_binding_channels[page] = self
            await self._install_exfiltration_script(page, add_init_script=False)
            return self

        LOG.info(f"{self.class_name} setting up exfiltration on new page.", url=page.url)

        await self._ensure_binding(page)

        def console_listener(msg: ConsoleMessage) -> None:
            self._handle_console_event(msg)

        page.on("console", console_listener)

        await self._install_exfiltration_script(page, add_init_script=True)

        capture = PageConsoleCapture(console_listener=console_listener)
        self._page_console_captures[page] = capture
        try:
            capture.cdp_session = await self._attach_page_cdp_console_capture(page)
        except Exception:
            LOG.debug(f"{self.class_name} failed to attach page CDP EXFIL listener", page_url=page.url, exc_info=True)

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
            cdp_session.send("Network.enable"),
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
        cdp_session.on("Network.requestWillBeSent", lambda params: self._handle_network_activity())
        cdp_session.on("Network.loadingFinished", lambda params: self._handle_network_activity())
        cdp_session.on("Network.loadingFailed", lambda params: self._handle_network_activity())

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

        if self._refresh_task is None or self._refresh_task.done():
            self._refresh_task = asyncio.create_task(self._refresh_exfiltration_loop())

        return self

    async def stop(self) -> t.Self:
        LOG.info(f"{self.class_name} stopping.")

        if self._refresh_task:
            self._refresh_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._refresh_task
            self._refresh_task = None

        if self._network_activity_flush_task and not self._network_activity_flush_task.done():
            self._network_activity_flush_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._network_activity_flush_task
            self._network_activity_flush_task = None

        pending_event_tasks = list(self._pending_event_tasks)
        for task in pending_event_tasks:
            task.cancel()
        if pending_event_tasks:
            await asyncio.gather(*pending_event_tasks, return_exceptions=True)
        self._pending_event_tasks.clear()

        if self.cdp_session:
            try:
                await self.cdp_session.detach()
            except Exception:
                pass

        self.cdp_session = None

        captures = list(self._page_console_captures.items())
        self._page_console_captures.clear()
        pages = [page for page, _ in captures]

        if self.browser_context:
            for page in self.browser_context.pages:
                if all(existing is not page for existing in pages):
                    pages.append(page)

        for page, capture in captures:
            try:
                page.remove_listener("console", capture.console_listener)
            except KeyError:
                pass

            if capture.cdp_session:
                try:
                    await capture.cdp_session.detach()
                except Exception:
                    pass

        for page in pages:
            if self._active_binding_channels.get(page) is self:
                self._active_binding_channels.pop(page, None)
            self._binding_registered_pages.discard(page)

            try:
                await page.evaluate("window.__skyvern_exfiltration_binding_name = null;")
            except Exception:
                LOG.debug(f"{self.class_name} failed to clear exfiltration binding name", url=page.url, exc_info=True)

            await self.undecorate(page)

        self._recent_console_event_fingerprints.clear()

        LOG.info(f"{self.class_name} stopped.")

        return self
