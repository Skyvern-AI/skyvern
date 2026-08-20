"""Network observation: the `request` / `response` / `pageerror` events, as Playwright shapes them.

Distinct from ``facade/network.py``, which is the Fetch domain -- that layer *pauses* a request so a
handler can rewrite it. This one only watches, over ``Network.*``, and never blocks a request.

Two constraints from the consumer drive the design. ``ScopedXhrDownloadCapture`` keeps admitted
requests in a ``set`` and later asks ``response.request not in self._admitted_requests``, so a
request must be **the same object** across its ``request``, ``response`` and ``requestfinished``
events -- a fresh object per event would make every one of those checks miss. And it reads the body
of a response it likes, which Chrome will only hand over once the transfer has finished, so
``body()`` waits for that rather than racing it.
"""

from __future__ import annotations

import asyncio
import base64
from typing import TYPE_CHECKING, Any

import structlog

from skyvern.webeye.skycdp.errors import CdpError

if TYPE_CHECKING:
    from skyvern.webeye.skycdp.connection import CdpSession
    from skyvern.webeye.skycdp.facade.page import Page

LOG = structlog.get_logger()

# Chrome's resourceType is TitleCase ("XHR", "Fetch", "Document"); Playwright reports lowercase, and
# production filters on `resource_type not in ("xhr", "fetch")`, so the case matters.
_RESOURCE_TYPES = {"xhr": "xhr", "fetch": "fetch", "document": "document", "stylesheet": "stylesheet"}

# How many requests a page keeps addressable. Bounded rather than released on `loadingFinished`,
# because `response.body()` is lazy: the consumer decides it wants a body AFTER the transfer ends, so
# forgetting there would break the one caller this exists for. A page that lives through a long agent
# run issues thousands of requests, and holding every one forever is a leak with no upside -- Chrome
# discards the bodies itself long before then. Oldest-first eviction keeps the recent ones, which are
# the only ones anyone asks about.
_MAX_TRACKED_REQUESTS = 500


class NetworkRequest:
    """One request, carried across every event about it."""

    def __init__(self, page: Page, request_id: str, params: dict[str, Any]) -> None:
        self._page = page
        self.request_id = request_id
        raw = params.get("request") or {}
        self._url = str(raw.get("url", ""))
        self._method = str(raw.get("method", "GET"))
        self._headers = {str(k).lower(): str(v) for k, v in (raw.get("headers") or {}).items()}
        self._post_data = raw.get("postData")
        raw_type = str(params.get("type", "Other"))
        self.resource_type = _RESOURCE_TYPES.get(raw_type.lower(), raw_type.lower())
        self._frame_id = params.get("frameId")
        self.redirected_from: NetworkRequest | None = None
        self._response: NetworkResponse | None = None
        self._failure: str | None = None

    @property
    def url(self) -> str:
        return self._url

    @property
    def method(self) -> str:
        return self._method

    @property
    def headers(self) -> dict[str, str]:
        return dict(self._headers)

    @property
    def post_data(self) -> str | None:
        return None if self._post_data is None else str(self._post_data)

    @property
    def frame(self) -> Any:
        """The frame that issued this request.

        Production reaches `request.frame.page` to decide whether a child tab is in scope, so this
        falls back to the main frame rather than None -- a null here would turn a scope check into an
        AttributeError inside an event handler, where it would be swallowed and lose the download.
        """
        frames = self._page._frames
        if self._frame_id and self._frame_id in frames:
            return frames[self._frame_id]
        try:
            return self._page.main_frame
        except CdpError:
            return None

    def response(self) -> NetworkResponse | None:
        """A method, not a property -- Playwright's is a method."""
        return self._response

    def is_navigation_request(self) -> bool:
        return self.resource_type == "document"

    @property
    def failure(self) -> str | None:
        """A property, not a method -- Playwright's is a property. As a method, `if request.failure:`
        is truthy for a request that never failed, because a bound method is always truthy."""
        return self._failure

    def note_failure(self, error_text: str) -> None:
        self._failure = error_text

    def __repr__(self) -> str:
        return f"<Request {self.method} {self._url}>"


class NetworkResponse:
    def __init__(self, request: NetworkRequest, params: dict[str, Any]) -> None:
        self.request = request
        raw = params.get("response") or {}
        self._url = str(raw.get("url", ""))
        self._status = int(raw.get("status", 0))
        self._status_text = str(raw.get("statusText", ""))
        self._headers = {str(k).lower(): str(v) for k, v in (raw.get("headers") or {}).items()}

    @property
    def url(self) -> str:
        return self._url

    @property
    def status(self) -> int:
        return self._status

    @property
    def status_text(self) -> str:
        return self._status_text

    @property
    def headers(self) -> dict[str, str]:
        return dict(self._headers)

    @property
    def ok(self) -> bool:
        return 200 <= self._status < 300

    async def body(self) -> bytes:
        return await self.request._page._network.response_body(self.request.request_id)

    async def text(self) -> str:
        return (await self.body()).decode("utf-8", errors="replace")

    def __repr__(self) -> str:
        return f"<Response {self._status} {self._url}>"


class NetworkObserver:
    """Wires the CDP Network domain to one page's `request`/`response` listeners.

    Enabling the domain is not free: Chrome then streams three events per subresource, and measured
    against a real browser it cost +127 ms on `goto` (175 -> 302 ms) -- enough to make this engine's
    navigation slower than Playwright's. So it is enabled only for pages that actually subscribe.

    The obvious lazy implementation is wrong, and was the first thing tried: `page.on` is synchronous,
    so scheduling the enable from it loses the race against a `goto` on the next line and silently
    drops every request that beat it onto the wire. Instead the enable is *started* on subscription
    and *awaited* before the next navigation, which closes the race without paying for pages nobody
    watches.
    """

    def __init__(self, page: Page) -> None:
        self._page = page
        self._requests: dict[str, NetworkRequest] = {}
        self._finished: dict[str, asyncio.Event] = {}
        self._enable_task: asyncio.Task[None] | None = None

        self._last_document_response: NetworkResponse | None = None

    def ensure_enabled(self) -> None:
        """Start enabling the domain. Safe to call from synchronous `page.on`."""
        if self._enable_task is None:
            self._enable_task = asyncio.ensure_future(self._enable())

    async def settled(self) -> None:
        """Await a pending enable, so a navigation cannot outrun its own subscription."""
        if self._enable_task is not None and not self._enable_task.done():
            await self._enable_task

    async def _enable(self) -> None:
        try:
            await self._page.session.send("Network.enable", {})
        except CdpError:
            LOG.warning("skycdp could not enable the Network domain; network events will not fire")

    def bind(self, session: CdpSession) -> None:
        session.on("Network.requestWillBeSent", self._on_request_will_be_sent)
        session.on("Network.responseReceived", self._on_response_received)
        session.on("Network.loadingFinished", self._on_loading_finished)
        session.on("Network.loadingFailed", self._on_loading_failed)
        session.on("Runtime.exceptionThrown", self._on_exception_thrown)

    # -- events -------------------------------------------------------------

    def _on_request_will_be_sent(self, params: dict) -> None:
        request_id = params.get("requestId")
        if not request_id:
            return
        request = NetworkRequest(self._page, str(request_id), params)
        # A redirect reuses the requestId and carries the previous hop's response, so the request we
        # are replacing becomes this one's `redirected_from` rather than being forgotten.
        if params.get("redirectResponse") is not None:
            request.redirected_from = self._requests.get(str(request_id))
        self._remember(str(request_id), request)
        self._page._emit("request", request)

    def _on_response_received(self, params: dict) -> None:
        request = self._requests.get(str(params.get("requestId") or ""))
        if request is None:
            return
        response = NetworkResponse(request, params)
        request._response = response
        if str(params.get("type") or "") == "Document" and params.get("frameId") == self._page._main_frame_id:
            # The main frame's document response is what `goto` returns, and it is the only thing
            # carrying the redirect chain the SSRF revalidation walks. Keeping the LAST one is
            # deliberate: a redirect emits one of these per hop, and the final hop is the response
            # Playwright hands back, with the earlier hops reachable through `redirected_from`.
            self._last_document_response = response
        self._page._emit("response", response)

    def take_document_response(self) -> NetworkResponse | None:
        """The main frame's most recent document response, consumed once by `Page.goto`."""
        response, self._last_document_response = self._last_document_response, None
        return response

    def _on_loading_finished(self, params: dict) -> None:
        request_id = str(params.get("requestId") or "")
        event = self._finished.get(request_id)
        if event is not None:
            event.set()
        request = self._requests.get(request_id)
        if request is not None:
            self._page._emit("requestfinished", request)

    def _on_loading_failed(self, params: dict) -> None:
        request_id = str(params.get("requestId") or "")
        event = self._finished.get(request_id)
        if event is not None:
            event.set()
        request = self._requests.get(request_id)
        if request is not None:
            request.note_failure(str(params.get("errorText", "failed")))
            self._page._emit("requestfailed", request)

    def _on_exception_thrown(self, params: dict) -> None:
        details = params.get("exceptionDetails") or {}
        exception = details.get("exception") or {}
        message = exception.get("description") or details.get("text") or "page error"
        self._page._emit("pageerror", PageError(str(message)))

    # -- bodies ---------------------------------------------------------------

    async def response_body(self, request_id: str) -> bytes:
        """Chrome discards a response body it has not been asked for, and refuses one that has not
        finished arriving, so this waits for the transfer before asking."""
        event = self._finished.get(request_id)
        if event is None:
            # Evicted by the bound above, which means a body was asked for long after its request.
            # Saying so is better than the confusing empty body Chrome would return.
            raise CdpError(
                f"response body for {request_id} is no longer tracked; more than "
                f"{_MAX_TRACKED_REQUESTS} requests have been issued on this page since it finished"
            )
        if not event.is_set():
            try:
                await asyncio.wait_for(event.wait(), timeout=30)
            except asyncio.TimeoutError:
                raise CdpError(f"response body for {request_id} never finished arriving") from None
        result = await self._page.session.send("Network.getResponseBody", {"requestId": request_id})
        body = str(result.get("body", ""))
        return base64.b64decode(body) if result.get("base64Encoded") else body.encode("utf-8")

    def _remember(self, request_id: str, request: NetworkRequest) -> None:
        self._requests[request_id] = request
        self._finished[request_id] = asyncio.Event()
        while len(self._requests) > _MAX_TRACKED_REQUESTS:
            self.forget(next(iter(self._requests)))

    def forget(self, request_id: str) -> None:
        self._requests.pop(request_id, None)
        self._finished.pop(request_id, None)

    def clear(self) -> None:
        """Drop everything. Called when the page closes, so a closed page holds nothing."""
        self._requests.clear()
        self._finished.clear()


class PageError(Exception):
    """What `page.on("pageerror")` delivers: an uncaught exception from the page itself."""

    def __init__(self, message: str) -> None:
        super().__init__(message)
        self.message = message
