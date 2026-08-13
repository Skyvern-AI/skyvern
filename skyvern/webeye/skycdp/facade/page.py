"""Page and Frame.

A Page owns one target's session; a Frame is an addressable execution context inside it. Same-process
iframes share the page's session and are reached by execution-context id; out-of-process iframes get
their own session through the auto-attach cascade and are reached the same way from the caller's side.
"""

from __future__ import annotations

import asyncio
import base64
import json
from collections.abc import Callable
from contextlib import suppress
from functools import partial
from pathlib import Path
from typing import TYPE_CHECKING, Any

import structlog

from skyvern.webeye.attach_only import forbid
from skyvern.webeye.skycdp.connection import CdpSession
from skyvern.webeye.skycdp.errors import (
    CdpError,
    CdpTargetClosedError,
    CdpTimeoutError,
    CdpUnsupportedOperation,
)
from skyvern.webeye.skycdp.facade.artifacts import ConsoleMessage, FileChooser
from skyvern.webeye.skycdp.facade.dialogs import Dialog
from skyvern.webeye.skycdp.facade.elements import ElementHandle, JSHandle, wait_for
from skyvern.webeye.skycdp.facade.evaluation import RemoteHandle, evaluate
from skyvern.webeye.skycdp.facade.input import Keyboard, Mouse
from skyvern.webeye.skycdp.facade.locator import FrameLocator, Locator
from skyvern.webeye.skycdp.facade.network import dispatch
from skyvern.webeye.skycdp.facade.network_events import NetworkObserver
from skyvern.webeye.skycdp.facade.routing import RouteHandler, RouteTable, URLMatcher
from skyvern.webeye.skycdp.facade.timeouts import DEFAULT_NAVIGATION_TIMEOUT_MS, seconds_from_ms

if TYPE_CHECKING:
    from skyvern.webeye.skycdp.facade.browser import BrowserContext

LOG = structlog.get_logger()

# Playwright names a paper size; CDP takes inches. Only the sizes PDFBlock.VALID_FORMATS offers are
# listed, so an unknown one raises rather than silently printing US Letter.
_PAPER_SIZES_INCHES: dict[str, tuple[float, float]] = {
    "A4": (8.27, 11.69),
    "Letter": (8.5, 11.0),
    "Legal": (8.5, 14.0),
    "Tabloid": (11.0, 17.0),
}

# Playwright's snake_case to CDP's camelCase. `landscape` and `scale` are spelled the same in both.
_PDF_OPTION_NAMES: dict[str, str] = {
    "landscape": "landscape",
    "scale": "scale",
    "print_background": "printBackground",
    "display_header_footer": "displayHeaderFooter",
    "header_template": "headerTemplate",
    "footer_template": "footerTemplate",
    "page_ranges": "pageRanges",
    "prefer_css_page_size": "preferCSSPageSize",
}


class Frame:
    def __init__(self, page: Page, frame_id: str, *, parent: Frame | None = None, url: str = "") -> None:
        self.page = page
        self.frame_id = frame_id
        self._parent = parent
        self._url = url
        self._name = ""
        self._detached = False

    def __repr__(self) -> str:
        return f"<Frame {self.frame_id} {self._url}>"

    @property
    def url(self) -> str:
        return self._url

    @property
    def name(self) -> str:
        return self._name

    @property
    def session(self) -> CdpSession:
        """The session this frame's execution contexts actually arrive on.

        A same-process iframe shares the page's session. A cross-origin one is moved to its own
        target by site isolation, and its contexts arrive on that target's auto-attached session --
        so a frame bound permanently to the page session could never be evaluated in. Captcha
        widgets, payment forms and embedded logins are all cross-origin iframes, which makes this
        the difference between the engine working on real sites and not.
        """
        return self.page.session_for_frame(self.frame_id)

    def is_detached(self) -> bool:
        return self._detached

    @property
    def parent_frame(self) -> Frame | None:
        return self._parent

    @property
    def child_frames(self) -> list[Frame]:
        return [frame for frame in self.page.frames if frame._parent is self]

    async def _context_id(self, timeout: float = 5.0) -> int:
        context_id = self.session.context_for_frame(self.frame_id)
        if context_id is not None:
            return context_id
        await self.session.wait_for_any_context(timeout=timeout)
        context_id = self.session.context_for_frame(self.frame_id)
        if context_id is None:
            raise CdpError(f"no execution context for frame {self.frame_id}")
        return context_id

    async def evaluate(self, expression: str, arg: Any = None) -> Any:
        return await evaluate(self.session, expression, arg, context_id=await self._context_id())

    async def evaluate_handle(self, expression: str, arg: Any = None) -> JSHandle:
        handle = await evaluate(self.session, expression, arg, context_id=await self._context_id(), by_value=False)
        return _wrap_handle(self, handle)

    async def query_selector(self, selector: str) -> ElementHandle | None:
        return await self.query_selector_indexed(selector, 0)

    async def query_selector_indexed(self, selector: str, index: int) -> ElementHandle | None:
        handle = await evaluate(
            self.session,
            "(spec) => document.querySelectorAll(spec.selector)[spec.index] || null",
            {"selector": selector, "index": index},
            context_id=await self._context_id(),
            by_value=False,
        )
        if not isinstance(handle, RemoteHandle) or handle.subtype != "node":
            if isinstance(handle, RemoteHandle):
                await handle.dispose()
            return None
        return ElementHandle(self, handle)

    async def resolve_locator_chain(self, steps: list[dict[str, Any]], index: int) -> ElementHandle | None:
        """Resolve a Locator's selector chain to one element handle, or None."""
        from skyvern.webeye.skycdp.facade.locator import _RESOLVE_JS

        handle = await evaluate(
            self.session,
            _RESOLVE_JS,
            {"steps": steps, "index": index, "mode": "one"},
            context_id=await self._context_id(),
            by_value=False,
        )
        if not isinstance(handle, RemoteHandle) or handle.subtype != "node":
            if isinstance(handle, RemoteHandle):
                await handle.dispose()
            return None
        return ElementHandle(self, handle)

    async def query_selector_all(self, selector: str) -> list[ElementHandle]:
        total = int(await self.evaluate("(s) => document.querySelectorAll(s).length", selector))
        found = []
        for index in range(total):
            handle = await self.query_selector_indexed(selector, index)
            if handle is not None:
                found.append(handle)
        return found

    def locator(self, selector: str) -> Locator:
        return Locator(self, selector)

    def frame_locator(self, selector: str) -> FrameLocator:
        return FrameLocator(self, selector)

    async def content(self) -> str:
        return await self.evaluate(
            """() => {
                const doctype = document.doctype;
                const prefix = doctype
                    ? '<!DOCTYPE ' + doctype.name +
                      (doctype.publicId ? ' PUBLIC "' + doctype.publicId + '"' : '') +
                      (!doctype.publicId && doctype.systemId ? ' SYSTEM' : '') +
                      (doctype.systemId ? ' "' + doctype.systemId + '"' : '') + '>\\n'
                    : '';
                return prefix + document.documentElement.outerHTML;
            }"""
        )

    async def title(self) -> str:
        return await self.evaluate("() => document.title")

    async def wait_for_load_state(self, state: str = "load", timeout: float | None = None) -> None:
        """Reached during JS-context-lost recovery, when the page is already unstable.

        A frame's readiness is asked of the frame's own document; the page-level wait is the same
        question for the main frame, so this delegates rather than duplicating the lifecycle logic.
        """
        if self._parent is None:
            await self.page.wait_for_load_state(state, timeout=timeout)
            return
        wanted = {"domcontentloaded": ("interactive", "complete")}.get(state, ("complete",))

        async def ready() -> bool:
            try:
                return await self.evaluate("() => document.readyState") in wanted
            except CdpError:
                return False

        with suppress(CdpTimeoutError):
            await wait_for(ready, timeout=seconds_from_ms(timeout), description=f"frame load state {state!r}")

    async def frame_element(self) -> ElementHandle:
        """The element in the parent document that hosts this frame."""
        owner = await self.page.element_for_frame(self)
        if owner is None:
            raise CdpError(f"frame {self.frame_id} has no owner element (it may be the main frame or detached)")
        return owner

    async def viewport_offset(self) -> tuple[float, float]:
        """Where this frame's viewport sits inside the top-level one, in CSS pixels.

        The Input domain addresses the top-level viewport, so an element's own
        ``getBoundingClientRect`` is only usable after this offset is added.
        """
        if self._parent is None:
            return 0.0, 0.0
        owner = await self.page.element_for_frame(self)
        if owner is None:
            return 0.0, 0.0
        box = await owner.bounding_box()
        if box is None:
            return 0.0, 0.0
        parent_x, parent_y = await self._parent.viewport_offset()
        return box["x"] + parent_x, box["y"] + parent_y


class EventInfo:
    """The handle an `expect_*` block yields. `value` is awaited, matching Playwright."""

    def __init__(self, waiter: asyncio.Future[Any]) -> None:
        self._waiter = waiter

    @property
    async def value(self) -> Any:
        return await self._waiter

    def is_done(self) -> bool:
        return self._waiter.done()

    def _cancel(self) -> None:
        self._waiter.cancel()


class EventContextManager:
    """Playwright's `expect_*` protocol, including the part that is easy to get wrong.

    On the way out of the block the waiter is awaited, so the timeout belongs to the block rather
    than to the later `await info.value`. But if the BODY raised, the waiter is cancelled and the
    body's exception is left alone: the body failing is the diagnosis, and awaiting anyway would
    spend the whole budget and then report a download timeout instead of the click error that caused
    it. `click_and_claim_download` in `workflow/models/block.py` depends on this.
    """

    def __init__(self, waiter: asyncio.Future[Any]) -> None:
        self._info = EventInfo(waiter)

    async def __aenter__(self) -> EventInfo:
        return self._info

    async def __aexit__(self, exc_type: Any, exc_value: Any, traceback: Any) -> None:
        if exc_value is not None:
            self._info._cancel()
            return
        await self._info.value


class Page:
    def __init__(self, context: BrowserContext, session: CdpSession) -> None:
        self._context = context
        self._session = session
        self._closed = False
        self._frames: dict[str, Frame] = {}
        # Frames whose execution contexts live on a different target's session (out-of-process
        # iframes). Absent means "the page's own session", which covers the common case.
        self._frame_sessions: dict[str, CdpSession] = {}
        self._main_frame_id: str | None = None
        self._url = ""
        self._listeners: dict[str, list[Callable[..., Any]]] = {}
        self.mouse = Mouse(session)
        self.keyboard = Keyboard(session)
        self._viewport_size: dict[str, int] | None = None
        self._routes = RouteTable()
        self._interception_enabled = False
        self._network = NetworkObserver(self)

    def __repr__(self) -> str:
        return f"<Page {self._url}>"

    # -- identity -----------------------------------------------------------

    @property
    def session(self) -> CdpSession:
        return self._session

    @property
    def context(self) -> BrowserContext:
        return self._context

    @property
    def url(self) -> str:
        return self._url

    @property
    def main_frame(self) -> Frame:
        if self._main_frame_id is None or self._main_frame_id not in self._frames:
            raise CdpError("page has no main frame yet")
        return self._frames[self._main_frame_id]

    @property
    def frames(self) -> list[Frame]:
        main = self._frames.get(self._main_frame_id or "")
        ordered = [main] if main else []
        ordered.extend(frame for frame in self._frames.values() if frame is not main)
        return ordered

    def is_closed(self) -> bool:
        return self._closed or self._session.detached

    # -- setup --------------------------------------------------------------

    async def _bootstrap(self) -> None:
        self._bind_session_events(self._session)
        await self._refresh_frame_tree()

    def _bind_session_events(self, session: CdpSession) -> None:
        session.on("Page.frameNavigated", self._on_frame_navigated)
        session.on("Page.frameAttached", self._on_frame_attached)
        session.on("Page.frameDetached", self._on_frame_detached)
        session.on("Page.javascriptDialogOpening", self._on_dialog)
        session.on("Page.fileChooserOpened", partial(self._on_file_chooser, session))
        session.on("Runtime.consoleAPICalled", self._on_console)
        self._network.bind(session)

    def _on_file_chooser(self, session: CdpSession, params: dict) -> None:
        backend_node_id = params.get("backendNodeId")
        if backend_node_id is None:
            # Chrome omits it unless DOM is enabled on the session that owns the input, and without
            # it there is no node to set files on. Say so rather than emitting a chooser that cannot
            # accept anything.
            LOG.warning("skycdp file chooser arrived with no backendNodeId; cannot bind it to an input")
            return
        chooser = FileChooser(
            session,
            self,
            int(backend_node_id),
            multiple=params.get("mode") == "selectMultiple",
        )
        self._emit("filechooser", chooser)

    def _on_console(self, params: dict) -> None:
        message = ConsoleMessage(params)
        self._emit("console", message)
        self._context._emit("console", message)

    def owns_frame(self, frame_id: str) -> bool:
        return frame_id in self._frames

    def note_download(self, download: Any) -> None:
        self._emit("download", download)
        self._context._emit("download", download)

    def _on_dialog(self, params: dict) -> None:
        dialog = Dialog(self._session, params)
        if not self._listeners.get("dialog"):
            # Nobody is listening, so nothing else will answer it. Dismissing is the only option that
            # leaves the page usable.
            asyncio.ensure_future(self._dismiss_unhandled(dialog))
            return
        self._emit("dialog", dialog)
        asyncio.ensure_future(self._dismiss_if_abandoned(dialog))

    async def _dismiss_unhandled(self, dialog: Dialog) -> None:
        with suppress(CdpError):
            await dialog.dismiss()

    async def _dismiss_if_abandoned(self, dialog: Dialog, grace_seconds: float = 30.0) -> None:
        """A listener that never answers is a wedged page; dismiss rather than block forever."""
        deadline = asyncio.get_running_loop().time() + grace_seconds
        while asyncio.get_running_loop().time() < deadline:
            if dialog.answered:
                return
            await asyncio.sleep(0.05)
        LOG.warning("skycdp dialog listener never answered; dismissing to keep the page usable")
        with suppress(CdpError):
            await dialog.dismiss()

    def session_for_frame(self, frame_id: str) -> CdpSession:
        """The session a frame's commands belong on, walking up to the nearest bound ancestor.

        An iframe appended inside a live cross-origin frame after adoption has no binding of its own,
        and a whole out-of-process subtree shares one renderer, so an ancestor's session is its
        session. Mirrors Playwright's ``_sessionForFrame``.
        """
        # Keyed before the walk so a frame id with a binding but no Frame object still resolves.
        session = self._frame_sessions.get(frame_id)
        if session is not None and not session.detached:
            return session
        frame = self._frames.get(frame_id)
        while frame is not None:
            frame = frame._parent
            if frame is None:
                break
            session = self._frame_sessions.get(frame.frame_id)
            if session is not None and not session.detached:
                return session
        return self._session

    async def adopt_oopif_session(self, session: CdpSession) -> None:
        """Bind a cross-origin iframe's own target session to the frames it owns."""
        self._bind_session_events(session)
        try:
            tree = await session.send("Page.getFrameTree")
        except CdpError:
            return
        self._absorb_frame_tree(tree.get("frameTree") or {}, parent=None, session=session)

    def drop_frame_session(self, session: CdpSession) -> None:
        for frame_id in [fid for fid, bound in self._frame_sessions.items() if bound is session]:
            self._frame_sessions.pop(frame_id, None)

    async def _refresh_frame_tree(self) -> None:
        try:
            tree = await self._session.send("Page.getFrameTree")
        except (CdpTargetClosedError, CdpError):
            return
        self._absorb_frame_tree(tree.get("frameTree") or {}, parent=None)

    def _absorb_frame_tree(self, node: dict, parent: Frame | None, session: CdpSession | None = None) -> None:
        raw = node.get("frame") or {}
        frame_id = raw.get("id")
        if not frame_id:
            return
        frame = self._frames.get(frame_id)
        if frame is None:
            # An OOPIF root reports its parent by id even though that parent lives in another target.
            resolved_parent = parent or self._frames.get(raw.get("parentId") or "")
            frame = Frame(self, frame_id, parent=resolved_parent, url=raw.get("url", ""))
            self._frames[frame_id] = frame
        frame._url = raw.get("url", frame._url)
        frame._name = raw.get("name", frame._name)
        if session is not None:
            self._frame_sessions[frame_id] = session
        elif parent is None:
            self._main_frame_id = frame_id
            self._url = frame._url
        for child in node.get("childFrames") or []:
            self._absorb_frame_tree(child, parent=frame, session=session)

    def _on_frame_navigated(self, params: dict) -> None:
        raw = params.get("frame") or {}
        frame_id = raw.get("id")
        if not frame_id:
            return
        parent_id = raw.get("parentId")
        frame = self._frames.get(frame_id)
        if frame is None:
            frame = Frame(self, frame_id, parent=self._frames.get(parent_id or ""), url=raw.get("url", ""))
            self._frames[frame_id] = frame
        frame._url = raw.get("url", "")
        frame._name = raw.get("name", "")
        if parent_id is None:
            self._main_frame_id = frame_id
            self._url = frame._url
        self._emit("framenavigated", frame)

    def _on_frame_attached(self, params: dict) -> None:
        frame_id = params.get("frameId")
        if not frame_id or frame_id in self._frames:
            return
        self._frames[frame_id] = Frame(self, frame_id, parent=self._frames.get(params.get("parentFrameId") or ""))

    def _on_frame_detached(self, params: dict) -> None:
        # reason="swap" means the frame moved to another process, not that it went away. Dropping it
        # here is what makes every cross-origin iframe look detached the moment site isolation kicks
        # in; Playwright keeps the frame for exactly this reason.
        if params.get("reason") == "swap":
            return
        frame = self._frames.pop(params.get("frameId") or "", None)
        if frame is not None:
            frame._detached = True

    # -- events -------------------------------------------------------------

    NETWORK_EVENTS = frozenset({"request", "response", "requestfinished", "requestfailed"})

    def on(self, event: str, handler: Callable[..., Any]) -> None:
        if event in self.NETWORK_EVENTS:
            self._network.ensure_enabled()
        self._listeners.setdefault(event, []).append(handler)

    def once(self, event: str, handler: Callable[..., Any]) -> None:
        def wrapper(*args: Any) -> Any:
            self.remove_listener(event, wrapper)
            return handler(*args)

        self.on(event, wrapper)

    def remove_listener(self, event: str, handler: Callable[..., Any]) -> None:
        handlers = self._listeners.get(event)
        if not handlers:
            return
        try:
            handlers.remove(handler)
        except ValueError:
            return

    off = remove_listener

    def expect_download(self, *, timeout: float | None = None) -> EventContextManager:
        """Arm a download waiter, then run the caller's body inside the `async with`.

        The arming has to happen here rather than at `__aenter__`, because a download that fires
        while the click is still in flight is the case the idiom exists to catch -- that is the whole
        difference between this and `page.on("download")`.
        """
        loop = asyncio.get_event_loop()
        future: asyncio.Future[Any] = loop.create_future()

        def capture(download: Any) -> None:
            if not future.done():
                future.set_result(download)

        self.on("download", capture)
        seconds = seconds_from_ms(timeout)

        async def settle() -> Any:
            try:
                return await asyncio.wait_for(future, seconds)
            except asyncio.TimeoutError as exc:
                raise CdpTimeoutError(f"no download fired within {seconds}s") from exc
            finally:
                self.remove_listener("download", capture)

        return EventContextManager(asyncio.ensure_future(settle()))

    def _emit(self, event: str, *args: Any) -> None:
        for handler in list(self._listeners.get(event, ())):
            try:
                result = handler(*args)
                if asyncio.iscoroutine(result):
                    asyncio.ensure_future(result)
            except Exception:
                LOG.warning("skycdp page listener raised", page_event=event, exc_info=True)

    # -- navigation ---------------------------------------------------------

    async def goto(
        self,
        url: str,
        *,
        timeout: float | None = None,
        wait_until: str = "load",
        referer: str | None = None,
    ) -> Any:
        """Navigate, and return the main frame's document response the way Playwright does.

        The return value is not cosmetic. `navigate_with_retry` feeds it straight into
        `_revalidate_navigation_response`, which walks `response.request.redirected_from` to re-check
        every hop a redirect followed -- the SSRF guard for a public entry point that lands on an
        internal host (SKY-13112). Returning None does not fail that check, it EMPTIES it:
        `_navigation_hop_urls` breaks on the first None and the guard validates zero hops, passing
        without having looked. So this enables the Network domain for the navigation whether or not
        anyone subscribed, which is the one case where the cost of enabling is worth paying.
        """
        self._network.ensure_enabled()
        # A subscription made on the line above must be live before the first request goes out.
        await self._network.settled()
        seconds = seconds_from_ms(timeout, DEFAULT_NAVIGATION_TIMEOUT_MS)
        params: dict[str, Any] = {"url": url}
        if referer:
            params["referrer"] = referer
        result = await self._session.send("Page.navigate", params, timeout=seconds)
        if result.get("errorText"):
            raise CdpError(f"navigation to {url} failed: {result['errorText']}")
        # wait_for_load_state takes milliseconds like every other public method, so it gets the
        # caller's original budget rather than the seconds already converted for the CDP send.
        await self.wait_for_load_state(wait_until, timeout=timeout)
        await self._refresh_frame_tree()
        return self._network.take_document_response()

    async def reload(self, *, timeout: float | None = None, wait_until: str = "load") -> None:
        await self._session.send("Page.reload", {})
        await self.wait_for_load_state("load", timeout=timeout)

    async def go_back(self, *, timeout: float | None = None, wait_until: str = "load") -> None:
        await self._navigate_history(-1)

    async def go_forward(self, *, timeout: float | None = None, wait_until: str = "load") -> None:
        await self._navigate_history(1)

    async def _navigate_history(self, delta: int) -> None:
        history = await self._session.send("Page.getNavigationHistory")
        entries = history.get("entries", [])
        index = int(history.get("currentIndex", 0)) + delta
        if not 0 <= index < len(entries):
            return
        await self._session.send("Page.navigateToHistoryEntry", {"entryId": entries[index]["id"]})
        await self.wait_for_load_state("load")

    async def wait_for_load_state(self, state: str = "load", timeout: float | None = None) -> None:
        """Wait on Chrome's own lifecycle event, falling back to polling readiness.

        The event is what makes this cheap: polling ``document.readyState`` costs a round trip per
        attempt and, worse, quantises every navigation to the poll interval -- a page that loads in
        5 ms still reports back at the next tick. Both paths run together because the event only
        fires for a load that is still in flight; a page that finished before the wait began has
        nothing left to emit, and only the readiness check can see that.
        """
        seconds = seconds_from_ms(timeout, DEFAULT_NAVIGATION_TIMEOUT_MS)
        wanted = {"domcontentloaded": ("interactive", "complete"), "load": ("complete",)}.get(state, ("complete",))
        cdp_event = "Page.domContentEventFired" if state == "domcontentloaded" else "Page.loadEventFired"

        fired: asyncio.Future[None] = asyncio.get_running_loop().create_future()

        def on_fired(_: dict) -> None:
            if not fired.done():
                fired.set_result(None)

        async def ready() -> bool:
            try:
                return await self.evaluate("() => document.readyState") in wanted
            except CdpError:
                # A context torn down mid-navigation is the normal case, not a failure.
                return False

        self._session.on(cdp_event, on_fired)
        polling = asyncio.ensure_future(wait_for(ready, timeout=seconds, description=f"load state {state!r}"))
        try:
            done, _ = await asyncio.wait({polling, fired}, timeout=seconds, return_when=asyncio.FIRST_COMPLETED)
            if not done:
                LOG.debug("skycdp load state not reached; continuing", state=state)
            elif polling in done:
                # Surface a genuine polling failure, but a load-state timeout is advisory: callers
                # navigate to pages that never go idle and still expect to work with them.
                with suppress(CdpTimeoutError, CdpError):
                    polling.result()
        finally:
            self._session.off(cdp_event, on_fired)
            polling.cancel()
            with suppress(asyncio.CancelledError, CdpTimeoutError, CdpError):
                await polling

    async def wait_for_timeout(self, milliseconds: float) -> None:
        await asyncio.sleep(milliseconds / 1000)

    async def wait_for_selector(
        self, selector: str, timeout: float | None = None, *, state: str = "visible", **_: Any
    ) -> ElementHandle:
        return await self.locator(selector).element_handle(timeout=timeout)

    # -- main-frame delegation ----------------------------------------------

    async def evaluate(self, expression: str, arg: Any = None) -> Any:
        # Evaluated JS is one of the two ways a caller triggers a request right after subscribing.
        await self._network.settled()
        return await self.main_frame.evaluate(expression, arg)

    async def evaluate_handle(self, expression: str, arg: Any = None) -> JSHandle:
        return await self.main_frame.evaluate_handle(expression, arg)

    async def query_selector(self, selector: str) -> ElementHandle | None:
        return await self.main_frame.query_selector(selector)

    async def resolve_locator_chain(self, steps: list[dict[str, Any]], index: int) -> ElementHandle | None:
        return await self.main_frame.resolve_locator_chain(steps, index)

    async def query_selector_all(self, selector: str) -> list[ElementHandle]:
        return await self.main_frame.query_selector_all(selector)

    def locator(self, selector: str) -> Locator:
        return self.main_frame.locator(selector)

    def frame_locator(self, selector: str) -> FrameLocator:
        return self.main_frame.frame_locator(selector)

    async def content(self) -> str:
        return await self.main_frame.content()

    async def title(self) -> str:
        return await self.main_frame.title()

    def frame(self, name: str) -> Frame | None:
        for frame in self.frames:
            if frame.name == name:
                return frame
        return None

    # -- frame <-> element bridging -----------------------------------------

    async def element_for_frame(self, frame: Frame) -> ElementHandle | None:
        """The ``<iframe>`` element in the parent document that hosts ``frame``.

        Sent on the parent's session: the owner element lives in the parent's document, and a
        renderer only answers for frames it hosts. All three sends move together because
        backendNodeId, executionContextId and objectId are session-local -- resolving a parent-minted
        id on another session returns a DIFFERENT element rather than failing.
        """
        if frame.frame_id == self._main_frame_id:
            # Hosted by no element at all; asking would report a protocol error as "no owner".
            return None
        # _on_frame_attached can arrive before its parent is known and never repairs _parent, so a
        # parentless non-main frame is an orphan, not the document root -- still worth asking the page
        # session about, which is what it got before this became parent-routed.
        parent = frame.parent_frame or self.main_frame
        session = self.session_for_frame(parent.frame_id)
        try:
            result = await session.send("DOM.getFrameOwner", {"frameId": frame.frame_id})
        except CdpError:
            return None
        backend_node_id = result.get("backendNodeId")
        if backend_node_id is None:
            return None
        try:
            resolved = await session.send(
                "DOM.resolveNode",
                {"backendNodeId": backend_node_id, "executionContextId": await parent._context_id()},
            )
        except CdpError:
            return None
        remote = resolved.get("object") or {}
        if not remote.get("objectId"):
            return None
        return ElementHandle(parent, RemoteHandle(session, remote))

    async def frame_for_element(self, element: ElementHandle) -> Frame | None:
        """The frame an ``<iframe>`` or ``<frame>`` element hosts, or None for any other element.

        ``DOM.describeNode`` reports the owned frame id directly, so this needs no search and no
        guesswork about which frame in the tree belongs to which element.

        Sent on the handle's own session, not the page's: an objectId is only meaningful to the
        session that minted it, so a handle obtained inside a cross-origin iframe cannot be described
        by the page session -- which is every ``content_frame()`` call that crosses into one.
        """
        try:
            described = await element._session.send("DOM.describeNode", {"objectId": element._object_id})
        except CdpError:
            return None
        frame_id = (described.get("node") or {}).get("frameId")
        if not frame_id:
            return None
        frame = self._frames.get(frame_id)
        if frame is None:
            # The tree can lag a freshly attached iframe; one refresh is enough because the frame
            # already exists as far as the DOM is concerned.
            await self._refresh_frame_tree()
            frame = self._frames.get(frame_id)
        return frame

    # -- capture ------------------------------------------------------------

    async def screenshot(
        self,
        *,
        full_page: bool = False,
        element: ElementHandle | None = None,
        type: str = "png",
        timeout: float | None = None,
        path: str | None = None,
        clip: dict[str, float] | None = None,
        animations: str | None = None,
        **unsupported: Any,
    ) -> bytes:
        """Capture the page, optionally writing it to ``path`` as Playwright does.

        ``animations="disabled"`` is accepted and honoured by freezing CSS animations for the
        capture, because callers pass it to stop a spinner from making two screenshots of the same
        page differ. Anything else unrecognised raises rather than being dropped.
        """
        if unsupported:
            raise CdpUnsupportedOperation(
                f"skycdp screenshot does not support {sorted(unsupported)}; it would be ignored silently"
            )

        seconds = None if timeout is None else seconds_from_ms(timeout)
        if animations == "disabled":
            with suppress(CdpError):
                await self._session.send("Animation.enable")
                await self._session.send("Animation.setPlaybackRate", {"playbackRate": 0})

        params: dict[str, Any] = {"format": type, "captureBeyondViewport": full_page}
        if clip is not None:
            params["clip"] = {**clip, "scale": clip.get("scale", 1)}
        if element is not None:
            box = await element.bounding_box()
            if box:
                params["clip"] = {
                    "x": box["x"],
                    "y": box["y"],
                    "width": box["width"],
                    "height": box["height"],
                    "scale": 1,
                }
        result = await self._session.send("Page.captureScreenshot", params, timeout=seconds)
        image = base64.b64decode(result["data"])
        if path:
            Path(path).parent.mkdir(parents=True, exist_ok=True)
            Path(path).write_bytes(image)
        return image

    async def pdf(self, **kwargs: Any) -> bytes:
        """Translate Playwright's option names into CDP's before printing.

        Forwarding them raw looks like it works and silently does not: CDP has no `format` at all --
        it takes `paperWidth`/`paperHeight` in inches -- and spells the rest in camelCase. Chrome
        ignores keys it does not recognise instead of erroring, so every PDF came out at the default
        US Letter no matter what the caller asked for. Measured before this: `format="A4"` produced
        596x843pt on Playwright and 612x792pt here.

        `PDFBlock._build_pdf_options` sends format, landscape, print_background and, when a timestamp
        is wanted, display_header_footer with header/footer templates.
        """
        params: dict[str, Any] = {"printBackground": True}
        for key, value in kwargs.items():
            if key == "format":
                size = _PAPER_SIZES_INCHES.get(str(value))
                if size is None:
                    raise CdpUnsupportedOperation(
                        f"unsupported PDF format {value!r}; known: {sorted(_PAPER_SIZES_INCHES)}"
                    )
                params["paperWidth"], params["paperHeight"] = size
            elif key in _PDF_OPTION_NAMES:
                params[_PDF_OPTION_NAMES[key]] = value
            else:
                # Fail loud rather than print something the caller did not ask for -- silently
                # dropping an option is exactly the defect this method used to have.
                raise CdpUnsupportedOperation(f"unsupported PDF option {key!r}")
        result = await self._session.send("Page.printToPDF", params)
        return base64.b64decode(result["data"])

    async def set_content(self, html: str, *, wait_until: str = "load") -> None:
        await self.evaluate(
            """(markup) => {
                document.open();
                document.write(markup);
                document.close();
            }""",
            html,
        )
        await self.wait_for_load_state(wait_until)

    @property
    def video(self) -> None:
        """None, as Playwright reports when recording is off -- unless this is an attach-only worker.

        A page with no recording configured genuinely has no video, so None is the accurate answer
        and keeps callers that merely check for it working. But an attach-only worker can never have
        configured recording, so reaching for a video there means an assumption broke, and it fails
        the run with a named cause rather than handing back a plausible nothing.
        """
        forbid("Page.video", "Recording is configured at launch, so an attached browser never has it.")
        return None

    @property
    def viewport_size(self) -> dict[str, int] | None:
        """The viewport last set through this engine, or None when it follows the window.

        Playwright exposes this synchronously, so it cannot be queried from the page on demand; it
        reports what was configured, which is the same contract.
        """
        return self._viewport_size

    async def set_viewport_size(self, viewport: dict[str, int]) -> None:
        self._viewport_size = {"width": int(viewport["width"]), "height": int(viewport["height"])}
        await self._session.send(
            "Emulation.setDeviceMetricsOverride",
            {
                "width": int(viewport["width"]),
                "height": int(viewport["height"]),
                "deviceScaleFactor": 0,
                "mobile": False,
            },
        )

    async def route(self, url: URLMatcher, handler: RouteHandler) -> None:
        self._routes.add(url, handler)
        await self._enable_interception()

    async def unroute(self, url: URLMatcher, handler: RouteHandler | None = None) -> None:
        self._routes.remove(url, handler)
        await self._disable_interception_if_idle()

    async def _enable_interception(self) -> None:
        """Pause every request and decide locally.

        Chrome is given a catch-all pattern rather than the caller's globs: its matcher cannot
        express Playwright's semantics, so filtering at the browser would silently change which
        requests a handler sees.
        """
        if self._interception_enabled:
            return
        self._session.on("Fetch.requestPaused", self._on_request_paused)
        with suppress(CdpError):
            await self._session.send("Fetch.enable", {"patterns": [{"urlPattern": "*"}], "handleAuthRequests": False})
        self._interception_enabled = True

    async def _disable_interception_if_idle(self) -> None:
        if not self._interception_enabled:
            return
        if len(self._routes) or len(self._context.routes):
            return
        self._session.off("Fetch.requestPaused", self._on_request_paused)
        with suppress(CdpError):
            await self._session.send("Fetch.disable")
        self._interception_enabled = False

    def _on_request_paused(self, params: dict) -> None:
        # Page routes are consulted before context routes, and an unhandled page route falls through.
        asyncio.ensure_future(dispatch(self._session, [self._routes, self._context.routes], params))

    async def add_init_script(self, script: str) -> None:
        await self._session.send("Page.addScriptToEvaluateOnNewDocument", {"source": script})

    async def set_extra_http_headers(self, headers: dict[str, str]) -> None:
        # Network.enable first: setExtraHTTPHeaders is rejected while the domain is disabled, and a
        # page session does not enable Network on its own. BrowserContext does the same per page.
        await self._session.send("Network.enable", {})
        await self._session.send("Network.setExtraHTTPHeaders", {"headers": headers})

    async def expose_binding(self, name: str, callback: Callable[..., Any]) -> None:
        """Install `window[name]` and route its calls to `callback(source, payload)`.

        Two production callers depend on this -- the transient page-text observer and the
        exfiltration channel -- and both call it with an OBJECT from page JS while CDP's
        `Runtime.addBinding` only accepts a string and only reports it back as one. So the raw
        binding is registered under an internal name and a shim over it does the JSON, which is the
        same shape Playwright uses.

        The shim is installed twice on purpose: as an init script for documents that do not exist
        yet, and evaluated once now, because a caller that binds after the page has already loaded
        would otherwise see nothing until the next navigation.

        Limit worth stating: the shim resolves immediately rather than waiting for the Python
        callback's return value. Both production callers are synchronous and return None, and the
        page side does `Promise.resolve(window[name](...))` without reading the result. A caller that
        needs a value back would need the reply plumbing this deliberately does not have.
        """
        raw_name = f"__skycdp_binding_{name}"
        shim = (
            f"(() => {{ const raw = () => window[{raw_name!r}];"
            f" window[{name!r}] = (...args) => {{ const send = raw();"
            f" if (send) send(JSON.stringify(args.length > 1 ? args : args[0]));"
            f" return Promise.resolve(); }}; }})()"
        )

        def _on_binding_called(params: dict) -> None:
            if params.get("name") != raw_name:
                return
            try:
                payload = json.loads(params.get("payload") or "null")
            except ValueError:
                payload = params.get("payload")
            try:
                callback({"page": self, "frame": self.main_frame}, payload)
            except Exception:
                # A raising callback must not kill the CDP read loop; the two production consumers
                # record events, and losing one is better than losing the connection.
                LOG.warning("skycdp exposed binding callback raised", binding=name, exc_info=True)

        self._session.on("Runtime.bindingCalled", _on_binding_called)
        await self._session.send("Runtime.addBinding", {"name": raw_name})
        await self._session.send("Page.addScriptToEvaluateOnNewDocument", {"source": shim})
        with suppress(CdpError):
            await self.evaluate(f"() => {{ {shim} }}")

    async def bring_to_front(self) -> None:
        await self._session.send("Page.bringToFront")

    # -- teardown -----------------------------------------------------------

    async def close(self, **_: Any) -> None:
        if self._closed:
            return
        self._closed = True
        self._network.clear()
        await self._context._detach_page(self)
        try:
            await self._context.browser.connection.close_target(self._session.target.target_id)
        except CdpError:
            pass
        self._emit("close", self)


def _wrap_handle(frame: Frame, handle: Any) -> JSHandle:
    if isinstance(handle, RemoteHandle) and handle.subtype == "node":
        return ElementHandle(frame, handle)
    if isinstance(handle, RemoteHandle):
        return JSHandle(frame, handle)
    raise CdpError("expected a remote handle")
