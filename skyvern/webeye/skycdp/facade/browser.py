"""Driver, browser, and context -- the attach-only entry point.

There is no ``launch``. The engine's whole premise is that something else provisions the browser: a
persistent-session pod, a vendor, or a local Chrome someone already started. That is what keeps every
stealth property -- the patched binary, the launch switches, the side-loaded captcha extension,
headful under Xvfb -- owned by the provisioner, where it already lives, and out of reach of this code.
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any
from urllib.parse import urlparse

import structlog

from skyvern.webeye.attach_only import forbid
from skyvern.webeye.skycdp.connection import CdpConnection, CdpSession
from skyvern.webeye.skycdp.errors import CdpConnectionError, CdpError, CdpUnsupportedOperation
from skyvern.webeye.skycdp.facade.artifacts import Download
from skyvern.webeye.skycdp.facade.page import Page
from skyvern.webeye.skycdp.facade.routing import RouteHandler, RouteTable, URLMatcher
from skyvern.webeye.skycdp.transport import CdpTransport

LOG = structlog.get_logger()

DEFAULT_CONNECT_TIMEOUT_MS = 30_000
# Screenshots and response bodies routinely exceed a websocket library's default frame cap.
MAX_WS_MESSAGE_BYTES = 100 * 1024 * 1024


class BrowserContext:
    """A browser context. Attach-only, so this usually wraps a context that already exists."""

    def __init__(self, browser: Browser, browser_context_id: str | None) -> None:
        self.browser = browser
        self.browser_context_id = browser_context_id
        self._pages: list[Page] = []
        self._listeners: dict[str, list[Callable[..., Any]]] = {}
        self._closed = False
        # A target reaches adoption from two directions at once: the caller that created it, and the
        # auto-attach event announcing it. Both must end at one Page and one "page" event, so
        # concurrent adopters share a single in-flight task rather than interleaving.
        self._adoptions: dict[str, asyncio.Task[Page]] = {}
        self._routes = RouteTable()

    @property
    def pages(self) -> list[Page]:
        return [page for page in self._pages if not page.is_closed()]

    def on(self, event: str, handler: Callable[..., Any]) -> None:
        self._listeners.setdefault(event, []).append(handler)

    def remove_listener(self, event: str, handler: Callable[..., Any]) -> None:
        handlers = self._listeners.get(event)
        if handlers and handler in handlers:
            handlers.remove(handler)

    off = remove_listener

    def _emit(self, event: str, *args: Any) -> None:
        for handler in list(self._listeners.get(event, ())):
            try:
                result = handler(*args)
                if asyncio.iscoroutine(result):
                    asyncio.ensure_future(result)
            except Exception:
                LOG.warning("skycdp context listener raised", context_event=event, exc_info=True)

    async def new_page(self) -> Page:
        session = await self.browser.connection.create_target("about:blank", browser_context_id=self.browser_context_id)
        return await self._adopt(session)

    async def _adopt(self, session: CdpSession) -> Page:
        existing = self._page_for(session)
        if existing is not None:
            return existing

        in_flight = self._adoptions.get(session.session_id)
        if in_flight is None:
            in_flight = asyncio.ensure_future(self._build_page(session))
            self._adoptions[session.session_id] = in_flight
        try:
            return await in_flight
        finally:
            self._adoptions.pop(session.session_id, None)

    def _page_for(self, session: CdpSession) -> Page | None:
        for page in self._pages:
            if page.session.session_id == session.session_id:
                return page
        return None

    async def route(self, url: URLMatcher, handler: RouteHandler) -> None:
        """Intercept matching requests on every page in this context, including ones opened later."""
        first = len(self._routes) == 0
        self._routes.add(url, handler)
        if first:
            for page in self.pages:
                await page._enable_interception()

    async def unroute(self, url: URLMatcher, handler: RouteHandler | None = None) -> None:
        self._routes.remove(url, handler)
        if len(self._routes) == 0:
            for page in self.pages:
                await page._disable_interception_if_idle()

    @property
    def routes(self) -> RouteTable:
        return self._routes

    async def _build_page(self, session: CdpSession) -> Page:
        await self.browser.connection.prepare_page_session(session)
        page = Page(self, session)
        await page._bootstrap()
        # A page adopted after routes were registered must inherit them, or a popup's requests
        # escape the very guards the context installed.
        if len(self._routes):
            await page._enable_interception()
        self._pages.append(page)
        self._emit("page", page)
        return page

    async def _detach_page(self, page: Page) -> None:
        if page in self._pages:
            self._pages.remove(page)

    async def new_cdp_session(self, page: Page) -> CdpSessionFacade:
        """Raw CDP access scoped to one page, matching `context.new_cdp_session(page)`.

        Unlike Playwright this opens no second connection: the page already owns a flat-mode session
        on the one websocket, and this hands back a view onto it.
        """
        return CdpSessionFacade(self.browser.connection, session=page.session)

    async def cookies(self, urls: str | list[str] | None = None) -> list[dict]:
        params: dict[str, Any] = {}
        if urls:
            params["urls"] = [urls] if isinstance(urls, str) else list(urls)
        result = await self.browser.connection.transport.send("Storage.getCookies", params)
        return result.get("cookies", [])

    async def add_cookies(self, cookies: list[dict]) -> None:
        await self.browser.connection.transport.send("Storage.setCookies", {"cookies": cookies})

    async def clear_cookies(self) -> None:
        await self.browser.connection.transport.send("Storage.clearCookies", {})

    async def add_init_script(self, script: str) -> None:
        for page in self.pages:
            await page.add_init_script(script)

    async def set_extra_http_headers(self, headers: dict[str, str]) -> None:
        for page in self.pages:
            await page.session.send("Network.enable", {})
            await page.session.send("Network.setExtraHTTPHeaders", {"headers": headers})

    async def grant_permissions(self, permissions: list[str], origin: str | None = None) -> None:
        params: dict[str, Any] = {"permissions": permissions}
        if origin:
            params["origin"] = origin
        if self.browser_context_id:
            params["browserContextId"] = self.browser_context_id
        await self.browser.connection.transport.send("Browser.grantPermissions", params)

    async def close(self, *, reason: str | None = None) -> None:
        if self._closed:
            return
        self._closed = True
        for page in list(self._pages):
            await page.close()
        if self.browser_context_id:
            try:
                await self.browser.connection.transport.send(
                    "Target.disposeBrowserContext", {"browserContextId": self.browser_context_id}
                )
            except CdpError:
                pass
        self._emit("close", self)


class Browser:
    def __init__(self, connection: CdpConnection) -> None:
        self.connection = connection
        self._contexts: dict[str | None, BrowserContext] = {}
        self._listeners: dict[str, list[Callable[..., Any]]] = {}
        self._closed = False
        connection.on_disconnected(self._on_connection_lost)
        # Targets the *site* opens -- window.open, target=_blank, and the separate targets site
        # isolation gives cross-origin iframes -- arrive only as auto-attach events. Without this
        # they would never become Pages or Frames: context.pages would miss the popup and every
        # download listener bound to context.on("page") would sit silent.
        connection.on_page_session(self._on_attached_session)
        connection.on_session_detached(self._on_session_detached)
        # Downloads are browser-level in CDP: these arrive with no session id, so they are subscribed
        # once here and routed to a page by the frame id they carry, rather than per session.
        self._downloads: dict[str, Download] = {}
        connection.transport.on("Browser.downloadWillBegin", self._on_download_will_begin)
        connection.transport.on("Browser.downloadProgress", self._on_download_progress)

    def _announce_popup(self, session: CdpSession, page: Page) -> None:
        """Tell the opener that it opened something.

        Only `openerId` distinguishes a popup from any other new page, and production listens for it
        on the opener, not on the context -- a download that arrives via target=_blank is reached
        this way and is otherwise lost.
        """
        opener_id = session.target.opener_id
        if not opener_id:
            return
        for context in self._contexts.values():
            for candidate in context.pages:
                if candidate is not page and candidate.session.target.target_id == opener_id:
                    candidate._emit("popup", page)
                    return

    def _page_for_frame(self, frame_id: str | None) -> Any:
        if not frame_id:
            return None
        for context in self._contexts.values():
            for page in context.pages:
                if page.owns_frame(frame_id):
                    return page
        return None

    def _on_download_will_begin(self, params: dict) -> None:
        guid = params.get("guid")
        if not guid:
            return
        page = self._page_for_frame(params.get("frameId"))
        download = Download(
            self.connection,
            guid=str(guid),
            url=str(params.get("url", "")),
            suggested_filename=str(params.get("suggestedFilename", "")),
            page=page,
        )
        self._downloads[str(guid)] = download
        if page is not None:
            page.note_download(download)
        else:
            # A download whose frame we do not know about still has to be reachable, or the file is
            # silently lost. Emitting at browser level is the honest fallback.
            self._emit("download", download)

    def _on_download_progress(self, params: dict) -> None:
        download = self._downloads.get(str(params.get("guid") or ""))
        if download is None:
            return
        download.note_progress(params)
        if params.get("state") in ("completed", "canceled"):
            self._downloads.pop(str(params.get("guid")), None)

    def _on_connection_lost(self) -> None:
        if not self._closed:
            self._closed = True
            self._emit("disconnected", self)

    def _on_attached_session(self, session: CdpSession) -> None:
        asyncio.ensure_future(self._absorb_session(session))

    async def _absorb_session(self, session: CdpSession) -> None:
        try:
            if session.target.type == "page":
                page = await self._context_for(session.target.browser_context_id)._adopt(session)
                self._announce_popup(session, page)
                return
            if session.target.type == "iframe":
                for context in self._contexts.values():
                    for page in context.pages:
                        await page.adopt_oopif_session(session)
        except Exception:
            LOG.warning("skycdp could not absorb an attached target", exc_info=True)

    def _on_session_detached(self, session: CdpSession) -> None:
        for context in self._contexts.values():
            for page in list(context._pages):
                page.drop_frame_session(session)

    @property
    def contexts(self) -> list[BrowserContext]:
        return list(self._contexts.values())

    def is_connected(self) -> bool:
        return not self._closed and not self.connection.is_closed

    def on(self, event: str, handler: Callable[..., Any]) -> None:
        self._listeners.setdefault(event, []).append(handler)

    def remove_listener(self, event: str, handler: Callable[..., Any]) -> None:
        handlers = self._listeners.get(event)
        if handlers and handler in handlers:
            handlers.remove(handler)

    def _emit(self, event: str, *args: Any) -> None:
        for handler in list(self._listeners.get(event, ())):
            try:
                handler(*args)
            except Exception:
                LOG.warning("skycdp browser listener raised", browser_event=event, exc_info=True)

    def _context_for(self, browser_context_id: str | None) -> BrowserContext:
        context = self._contexts.get(browser_context_id)
        if context is None:
            context = BrowserContext(self, browser_context_id)
            self._contexts[browser_context_id] = context
        return context

    async def _discover(self) -> None:
        """Adopt every page target that already exists on the browser we attached to."""
        for target in await self.connection.list_targets():
            if target.type != "page":
                continue
            try:
                session = await self.connection.attach(target.target_id)
            except CdpError:
                continue
            await self._context_for(target.browser_context_id)._adopt(session)

    async def new_context(self, **kwargs: Any) -> BrowserContext:
        # Only a value that actually asks for recording counts. Production passes record_video_dir
        # unconditionally and it is None whenever recording is off, so keying on the presence of the
        # argument rather than its value would reject every context creation in the fleet.
        recording = {"record_video_dir", "record_har_path", "record_video_size"}
        requested = sorted(key for key in recording if kwargs.get(key))
        if requested:
            # In the attach-only worker this is a broken assumption, not a preference: that worker
            # never launched the browser, so nothing configured recording and asking for it means a
            # caller believes something false. Fail the run and name it.
            forbid(
                f"browser context creation requesting {requested}",
                "Recording is configured when a browser is launched, and this worker launches none.",
            )
            # Everywhere else skycdp runs alongside the Playwright fleet for comparison. Recording is
            # architecturally impossible here -- there is no driver to record -- so refusing to build
            # the context at all would mean the engine can never run, rather than running without
            # video. Drop it, but say so at WARNING: a dropped capability nobody announces is exactly
            # the failure this engine was written to avoid.
            LOG.warning(
                "skycdp cannot record; creating the context without it",
                dropped=requested,
                detail="skycdp has no driver process, so video and HAR capture do not exist for it",
            )
        result = await self.connection.transport.send("Target.createBrowserContext", {})
        context = self._context_for(result["browserContextId"])
        viewport = kwargs.get("viewport")
        if viewport:
            context._default_viewport = viewport  # type: ignore[attr-defined]
        return context

    async def _enable_download_events(self) -> None:
        """Turn on download reporting without deciding where downloads go.

        ``behavior: "default"`` deliberately leaves Chrome's own destination alone: the download
        directory is run-scoped and owned by the caller, which rebinds it per run. All this asks for
        is the events, so a download that begins is observable even before anyone sets a path.
        """
        try:
            await self.connection.transport.send(
                "Browser.setDownloadBehavior", {"behavior": "default", "eventsEnabled": True}
            )
        except CdpError:
            LOG.warning("skycdp could not enable download events; page.on('download') will stay silent")

    async def new_browser_cdp_session(self) -> CdpSessionFacade:
        return CdpSessionFacade(self.connection, session=None)

    async def close(self, *, reason: str | None = None) -> None:
        if self._closed:
            return
        self._closed = True
        # The connection's own disconnect callback is suppressed by the flag above, so the event
        # fires exactly once whether the close came from here or from the socket dying.
        await self.connection.close()
        self._emit("disconnected", self)

    async def version(self) -> dict:
        return await self.connection.transport.send("Browser.getVersion")


class CdpSessionFacade:
    """The object Playwright hands back from ``new_cdp_session``: raw ``send`` plus events."""

    def __init__(self, connection: CdpConnection, session: CdpSession | None) -> None:
        self._connection = connection
        self._session = session

    async def send(self, method: str, params: dict | None = None) -> dict:
        if self._session is not None:
            return await self._session.send(method, params)
        return await self._connection.transport.send(method, params)

    def on(self, event: str, handler: Callable[[dict], None]) -> None:
        session_id = self._session.session_id if self._session else None
        self._connection.transport.on(event, handler, session_id=session_id)

    def off(self, event: str, handler: Callable[[dict], None]) -> None:
        session_id = self._session.session_id if self._session else None
        self._connection.transport.off(event, handler, session_id=session_id)

    # Playwright's CDPSession exposes both spellings, and production uses both: the download
    # interceptor subscribes with on() and unsubscribes with remove_listener(). Without this alias
    # that unsubscribe raises inside a suppressed block, the handler stays bound, and a re-enabled
    # interceptor sees every download twice.
    remove_listener = off

    async def detach(self) -> None:
        return None


class BrowserType:
    """``chromium``, restricted to the one operation this engine supports."""

    name = "chromium"

    async def connect_over_cdp(
        self,
        endpoint_url: str,
        *,
        timeout: float = DEFAULT_CONNECT_TIMEOUT_MS,
        headers: dict[str, str] | None = None,
        **_: Any,
    ) -> Browser:
        ws_url = await _resolve_websocket_url(endpoint_url, headers=headers, timeout_ms=timeout)
        socket = await _open_websocket(ws_url, headers=headers, timeout_ms=timeout)
        transport = CdpTransport(socket)
        await transport.start()
        connection = CdpConnection(transport)
        await connection.start()
        browser = Browser(connection)
        await browser._discover()
        await browser._enable_download_events()
        return browser

    async def launch(self, **_: Any) -> Browser:
        raise CdpUnsupportedOperation(
            "skycdp is attach-only: it never starts a browser. Provision one (persistent session, "
            "vendor, or local Chrome) and pass its CDP endpoint to connect_over_cdp."
        )

    async def launch_persistent_context(self, *_: Any, **__: Any) -> BrowserContext:
        raise CdpUnsupportedOperation(
            "skycdp is attach-only: it never starts a browser. Provision one and use connect_over_cdp."
        )


class Skycdp:
    """The ``Playwright``-shaped driver handle."""

    def __init__(self) -> None:
        self.chromium = BrowserType()

    async def start(self) -> Skycdp:
        return self

    async def stop(self) -> None:
        return None

    async def __aenter__(self) -> Skycdp:
        return await self.start()

    async def __aexit__(self, *_: Any) -> None:
        await self.stop()


def async_skycdp() -> Skycdp:
    """Mirrors ``async_playwright()``: returns a driver you ``start()``."""
    return Skycdp()


def _fetch_discovery(url: str, headers: dict[str, str] | None, timeout: float) -> dict[str, Any]:
    import json
    import urllib.request

    request = urllib.request.Request(url, headers=headers or {})
    # nosemgrep: python.lang.security.audit.dynamic-urllib-use-detected -- the URL is not attacker
    # reachable and its scheme is already constrained. `_resolve_websocket_url` raises
    # CdpConnectionError for anything that is not http/https before this is called, and the endpoint
    # comes from BROWSER_REMOTE_DEBUGGING_URL / the run's provisioned browser, which is operator
    # configuration rather than page or user input. Redirect following is irrelevant here: Chrome's
    # /json/version answers directly and the response is parsed as JSON, not followed.
    with urllib.request.urlopen(request, timeout=timeout) as response:
        return dict(json.loads(response.read()))


async def _resolve_websocket_url(
    endpoint_url: str,
    *,
    headers: dict[str, str] | None,
    timeout_ms: float,
    fetch: Callable[[str, dict[str, str] | None, float], dict[str, Any]] = _fetch_discovery,
) -> str:
    """Turn an http(s) DevTools endpoint into the websocket URL to dial.

    Only the *path* of Chrome's ``webSocketDebuggerUrl`` is used, because it carries the browser's
    UUID. The authority is taken from the endpoint the caller supplied instead: Chrome describes
    itself from its own vantage point, which against a remote or containerised browser is an address
    the caller cannot reach. Asked with a ``Host: localhost`` header -- the standard workaround for
    Chrome's own Host-header check -- it answers ``ws://localhost/devtools/browser/<uuid>``, with no
    port at all, and following that verbatim dials the caller's own machine on port 80.
    """
    parsed = urlparse(endpoint_url)
    if parsed.scheme in ("ws", "wss"):
        return endpoint_url
    if parsed.scheme not in ("http", "https"):
        raise CdpConnectionError(f"unsupported CDP endpoint scheme {parsed.scheme!r}")
    if not parsed.netloc:
        raise CdpConnectionError(f"CDP endpoint {endpoint_url!r} names no host")

    version_url = f"{parsed.scheme}://{parsed.netloc}{parsed.path.rstrip('/')}/json/version"
    seconds = max(timeout_ms / 1000, 1)

    try:
        payload = await asyncio.to_thread(fetch, version_url, headers, seconds)
    except Exception as exc:
        detail = str(exc) or type(exc).__name__
        raise CdpConnectionError(
            f"could not read {version_url}: {detail}. If the endpoint is a remote or containerised "
            "Chrome, note that it refuses DevTools HTTP requests whose Host header is neither "
            "localhost nor an IP literal -- pass headers={'Host': 'localhost'} to connect_over_cdp."
        ) from exc

    reported = payload.get("webSocketDebuggerUrl")
    if not reported:
        raise CdpConnectionError(f"{version_url} returned no webSocketDebuggerUrl")

    ws_scheme = "wss" if parsed.scheme == "https" else "ws"
    reported_path = urlparse(str(reported)).path or "/"
    return f"{ws_scheme}://{parsed.netloc}{reported_path}"


@dataclass(frozen=True)
class WebSocketDial:
    """How to open the websocket: what to say, and where to say it."""

    uri: str
    connect_host: str | None
    connect_port: int | None
    headers: dict[str, str] | None


def plan_websocket_dial(ws_url: str, headers: dict[str, str] | None) -> WebSocketDial:
    """Honour a caller-supplied ``Host`` by rewriting the URI and routing the connection separately.

    Chrome applies its Host check to the websocket upgrade as well as to ``/json/version``, so a
    remote browser addressed by service name refuses the upgrade with HTTP 500. The websockets client
    derives ``Host`` from the URI and ignores an override passed as an ordinary header, so the only
    way to send one is to put it in the URI -- and then tell the transport where to actually dial.
    """
    remaining = dict(headers or {})
    host_override = next((remaining.pop(key) for key in list(remaining) if key.lower() == "host"), None)
    if not host_override:
        return WebSocketDial(uri=ws_url, connect_host=None, connect_port=None, headers=headers)

    parsed = urlparse(ws_url)
    rewritten = parsed._replace(netloc=host_override).geturl()
    return WebSocketDial(
        uri=rewritten,
        connect_host=parsed.hostname,
        connect_port=parsed.port,
        headers=remaining or None,
    )


async def _open_websocket(ws_url: str, *, headers: dict[str, str] | None, timeout_ms: float) -> Any:
    try:
        from websockets.asyncio.client import connect
    except ImportError as exc:
        raise CdpConnectionError("the 'websockets' package is required by the skycdp engine") from exc

    dial = plan_websocket_dial(ws_url, headers)
    routing: dict[str, Any] = {}
    if dial.connect_host is not None:
        routing["host"] = dial.connect_host
        if dial.connect_port is not None:
            routing["port"] = dial.connect_port

    try:
        return await asyncio.wait_for(
            connect(
                dial.uri,
                additional_headers=dial.headers,
                max_size=MAX_WS_MESSAGE_BYTES,
                open_timeout=max(timeout_ms / 1000, 1),
                ping_interval=None,
                **routing,
            ),
            timeout=max(timeout_ms / 1000, 1) + 5,
        )
    except asyncio.TimeoutError as exc:
        raise CdpConnectionError(f"timed out connecting to {ws_url}") from exc
    except Exception as exc:
        raise CdpConnectionError(
            f"could not connect to {ws_url}: {exc}. A remote Chrome refuses the upgrade when the Host "
            "header is neither localhost nor an IP literal; pass headers={'Host': 'localhost'}."
        ) from exc
