"""Context-owned Fetch interception with ordered handlers and acknowledged request settlement."""

from __future__ import annotations

import asyncio
import base64
import inspect
from collections.abc import Callable, Coroutine
from contextvars import ContextVar
from enum import Enum, auto
from typing import Any, Protocol

import structlog

from skyvern.webeye.cdp_errors import CdpError, CdpProtocolError, CdpTargetClosedError
from skyvern.webeye.http_routing import RouteTable


class FetchSession(Protocol):
    async def send(self, method: str, params: dict[str, Any] | None = None) -> dict: ...

    def on(self, event: str, handler: Callable[[dict], None]) -> None: ...

    def off(self, event: str, handler: Callable[[dict], None]) -> None: ...


LOG = structlog.get_logger()
_current_dispatch: ContextVar[tuple[_FetchSession, asyncio.Task[None] | None] | None] = ContextVar(
    "fetch_dispatch", default=None
)


class Request:
    """The paused request, as a handler sees it, including any overrides applied so far."""

    def __init__(self, params: dict[str, Any]) -> None:
        raw = params.get("request") or {}
        self._url = str(raw.get("url", ""))
        self._method = str(raw.get("method", "GET"))
        self._headers = {str(k): str(v) for k, v in (raw.get("headers") or {}).items()}
        self._post_data = raw.get("postData")
        self.resource_type = str(params.get("resourceType", "Other")).lower()
        self.is_navigation_request_value = params.get("resourceType") == "Document"
        self.frame_id = params.get("frameId")
        self._overrides: dict[str, Any] = {}

    @property
    def url(self) -> str:
        return str(self._overrides.get("url", self._url))

    @property
    def original_url(self) -> str:
        return self._url

    @property
    def method(self) -> str:
        return str(self._overrides.get("method", self._method))

    @property
    def headers(self) -> dict[str, str]:
        return dict(self._overrides.get("headers", self._headers))

    @property
    def post_data(self) -> str | None:
        value = self.post_data_buffer
        return None if value is None else value.decode("utf-8", errors="replace")

    @property
    def post_data_buffer(self) -> bytes | None:
        value = self._overrides.get("post_data", self._post_data)
        return None if value is None else value if isinstance(value, bytes) else value.encode()

    def is_navigation_request(self) -> bool:
        return self.is_navigation_request_value

    def apply_overrides(self, overrides: dict[str, Any]) -> None:
        """Accumulate what a declining handler asked to change, for the handler that follows."""
        self._overrides.update({key: value for key, value in overrides.items() if value is not None})
        if "headers" in self._overrides:
            self._overrides["headers"] = dict(self._overrides["headers"])

    @property
    def overrides(self) -> dict[str, Any]:
        return dict(self._overrides)


class Settlement(Enum):
    PENDING = auto()
    SENDING = auto()
    ACKNOWLEDGED = auto()
    CLOSED = auto()
    UNKNOWN = auto()


class Route:
    """A paused request with one terminal command; fallback only delegates within Python."""

    def __init__(self, session: FetchSession, request_id: str, request: Request) -> None:
        self._session = session
        self._request_id = request_id
        self.request = request
        self._state = Settlement.PENDING
        self._fell_back = False

    @property
    def settled(self) -> bool:
        return self._state in (Settlement.ACKNOWLEDGED, Settlement.CLOSED)

    @property
    def can_settle(self) -> bool:
        return self._state is Settlement.PENDING

    @property
    def fell_back(self) -> bool:
        return self._fell_back

    async def continue_(
        self,
        *,
        url: str | None = None,
        method: str | None = None,
        headers: dict[str, str] | None = None,
        post_data: str | bytes | None = None,
    ) -> None:
        """Let the request proceed, ending the handler chain here."""
        merged = {**self.request.overrides}
        merged.update({k: v for k, v in {"url": url, "method": method, "headers": headers}.items() if v is not None})
        if post_data is not None:
            merged["post_data"] = post_data

        params: dict[str, Any] = {"requestId": self._request_id}
        if merged.get("url"):
            params["url"] = merged["url"]
        if merged.get("method"):
            params["method"] = merged["method"]
        if merged.get("headers") is not None:
            params["headers"] = [{"name": name, "value": value} for name, value in merged["headers"].items()]
        if merged.get("post_data") is not None:
            body = merged["post_data"]
            params["postData"] = base64.b64encode(body if isinstance(body, bytes) else body.encode()).decode()
        await self._send("Fetch.continueRequest", params)

    async def fulfill(
        self,
        *,
        status: int = 200,
        headers: dict[str, str] | None = None,
        body: str | bytes | None = None,
        content_type: str | None = None,
        response: Any = None,
    ) -> None:
        """Answer the request from here without it reaching the network."""
        resolved_headers = dict(headers or {})
        payload: str | bytes | None = body
        if response is not None and body is None:
            payload = getattr(response, "body", None) or getattr(response, "content", None)
            if headers is None:
                # Playwright replaces, rather than merges, when explicit headers are supplied.
                resolved_headers = dict(getattr(response, "headers", {}) or {})
            status = int(getattr(response, "status", status))
        if content_type:
            resolved_headers["content-type"] = content_type

        raw = payload if isinstance(payload, bytes) else (payload or "").encode()
        await self._send(
            "Fetch.fulfillRequest",
            {
                "requestId": self._request_id,
                "responseCode": status,
                "responseHeaders": [{"name": n, "value": v} for n, v in resolved_headers.items()],
                "body": base64.b64encode(raw).decode(),
            },
        )

    async def abort(self, error_code: str = "failed") -> None:
        reasons = {
            "failed": "Failed",
            "aborted": "Aborted",
            "timedout": "TimedOut",
            "accessdenied": "AccessDenied",
            "connectionrefused": "ConnectionRefused",
            "connectionreset": "ConnectionReset",
            "internetdisconnected": "InternetDisconnected",
            "addressunreachable": "AddressUnreachable",
            "blockedbyclient": "BlockedByClient",
            "blockedbyresponse": "BlockedByResponse",
        }
        reason = reasons.get(error_code.lower().replace("_", ""), "Failed")
        await self._send("Fetch.failRequest", {"requestId": self._request_id, "errorReason": reason})

    async def fallback(
        self,
        *,
        url: str | None = None,
        method: str | None = None,
        headers: dict[str, str] | None = None,
        post_data: str | bytes | None = None,
    ) -> None:
        """Decline, passing the request -- with any overrides -- to the next matching handler.

        This is the difference the egress guard is built on: `continue_` ends the chain, `fallback`
        does not, so a pass-through handler can rewrite headers without stopping a later guard from
        seeing the request.
        """
        if not self.can_settle:
            raise CdpError("The paused request already has a terminal command")
        self.request.apply_overrides({"url": url, "method": method, "headers": headers, "post_data": post_data})
        self._fell_back = True

    def reset_for_next_handler(self) -> None:
        self._fell_back = False

    async def _send(self, method: str, params: dict[str, Any]) -> None:
        if not self.can_settle:
            raise CdpError("The paused request already has a terminal command")
        self._state = Settlement.SENDING
        terminal = asyncio.create_task(self._settle(method, params))
        try:
            await asyncio.shield(terminal)
        except asyncio.CancelledError:
            # Cancellation cannot tell whether Chrome accepted the command. Retain the ACK owner.
            try:
                await asyncio.shield(terminal)
            except CdpError:
                pass
            raise

    async def _settle(self, method: str, params: dict[str, Any]) -> None:
        try:
            await self._session.send(method, params)
        except CdpTargetClosedError:
            self._state = Settlement.CLOSED
        except CdpProtocolError as error:
            disabled = False
            dispatch_owner = _current_dispatch.get()
            if (
                dispatch_owner is not None
                and dispatch_owner[0]._session is self._session
                and error.method == method
                and error.code in (None, -32000)
                and str(error) == f"{method}: Fetch domain is not enabled"
            ):
                # Wait for an in-flight owned disable ACK, never for unrelated dispatchers.
                async with dispatch_owner[0]._lock:
                    disabled = not dispatch_owner[0]._enabled
            if disabled or "invalid interceptionid" in str(error).lower():
                # Chrome discarded this interception; the owning target can still be healthy.
                self._state = Settlement.CLOSED
            else:
                self._state = Settlement.PENDING
                raise
        except BaseException:
            self._state = Settlement.UNKNOWN
            raise
        else:
            self._state = Settlement.ACKNOWLEDGED


async def dispatch(
    session: FetchSession,
    tables: list[RouteTable],
    params: dict[str, Any],
) -> None:
    """Answer one paused request by walking the matching handlers newest-first.

    Every exit path settles the request. An unmatched request continues; a handler that raises fails
    the request rather than letting it through, because the handlers that matter here are security
    guards and a guard that errors must not become a guard that permits.
    """
    request_id = str(params.get("requestId", ""))
    request = Request(params)
    route = Route(session, request_id, request)

    try:
        for table in tables:
            # Rewrites affect the request, never the URL used to choose subsequent handlers.
            for entry in table.matching(request.original_url):
                route.reset_for_next_handler()
                result = entry.handler(route, request) if _wants_request(entry.handler) else entry.handler(route)
                if inspect.isawaitable(result):
                    await result
                if route.settled:
                    return
                if not route.fell_back:
                    raise CdpError("Route handler returned without answering or falling back")
        await route.continue_()
    except asyncio.CancelledError:
        if route.can_settle:
            await route.abort("aborted")
        if not route.settled:
            raise CdpError("Cancelled request has an unacknowledged terminal command")
        raise
    except Exception:
        LOG.warning("CDP route dispatch failed closed", exc_info=True)
        if route.can_settle:
            await route.abort("failed")
        elif not route.settled:
            # A missing ACK is not permission to replay a potentially accepted network send.
            raise


def _wants_request(handler: Any) -> bool:
    """Whether a handler takes (route, request) rather than (route), as Playwright decides."""
    try:
        parameters = inspect.signature(handler).parameters
    except (TypeError, ValueError):
        return False
    return len([p for p in parameters.values() if p.kind in (p.POSITIONAL_ONLY, p.POSITIONAL_OR_KEYWORD)]) >= 2


class FetchRouter:
    """Context-owned request interception; raw CDP callers keep their own sessions and events."""

    def __init__(self, context_routes: RouteTable) -> None:
        self._context_routes = context_routes
        self._sessions: dict[FetchSession, _FetchSession] = {}

    async def enable(
        self, session: FetchSession, page_routes: RouteTable, on_failure: Callable[[], Coroutine[Any, Any, None]]
    ) -> None:
        owner = self._sessions.get(session)
        if owner is None:
            owner = _FetchSession(session, [page_routes, self._context_routes], on_failure)
            self._sessions[session] = owner
        await owner.enable()

    async def disable_if_idle(self, session: FetchSession) -> None:
        owner = self._sessions.get(session)
        if owner is not None:
            await owner.disable_if_idle()

    async def close(self, session: FetchSession) -> None:
        owner = self._sessions.pop(session, None)
        if owner is not None:
            await owner.close()

    async def prepare_close(self, session: FetchSession) -> None:
        owner = self._sessions.get(session)
        if owner is not None:
            await owner.prepare_close()

    def is_draining_current_dispatch(self, session: FetchSession) -> bool:
        dispatch = _current_dispatch.get()
        if dispatch is None:
            return False
        owner, current = dispatch
        return (
            owner._session is session
            and owner._closing
            and current in owner._tasks.values()
            and current is not owner._closing_dispatch
        )

    def resume(self, session: FetchSession) -> None:
        owner = self._sessions.get(session)
        if owner is not None:
            owner.resume()

    async def release(self, session: FetchSession, detach: Callable[[], Coroutine[Any, Any, None]]) -> None:
        owner = self._sessions.get(session)
        if owner is not None:
            await owner.prepare_close()
        try:
            await detach()
        except BaseException:
            if owner is not None:
                owner.resume()
            raise
        if owner is not None:
            await owner.close()
        self._sessions.pop(session, None)

    def detached(self, session: FetchSession) -> None:
        owner = self._sessions.pop(session, None)
        if owner is not None:
            owner.cancel()

    def disconnected(self) -> None:
        for session in list(self._sessions):
            self.detached(session)


class _FetchSession:
    def __init__(
        self, session: FetchSession, tables: list[RouteTable], on_failure: Callable[[], Coroutine[Any, Any, None]]
    ) -> None:
        self._session = session
        self._tables = tables
        self._on_failure = on_failure
        self._tasks: dict[str, asyncio.Task[None]] = {}
        self._lock = asyncio.Lock()
        self._enabled = False
        self._closing = False
        self._retiring: asyncio.Task[None] | None = None
        self._failure: asyncio.Task[None] | None = None
        self._closing_dispatch: asyncio.Task[None] | None = None
        self._drain_failed = False

    async def enable(self) -> None:
        async with self._lock:
            if self._closing:
                raise CdpTargetClosedError("Fetch owner is closing")
            if self._retiring is not None:
                self._retiring.cancel()
                self._retiring = None
            if self._enabled:
                return
            self._session.on("Fetch.requestPaused", self._paused)
            try:
                await self._session.send("Fetch.enable", {"patterns": [{"urlPattern": "*", "requestStage": "Request"}]})
            except BaseException:
                self._fail_target()
                raise
            self._enabled = True

    def _paused(self, params: dict[str, Any]) -> None:
        request_id = str(params["requestId"])
        if request_id in self._tasks:
            return
        task = asyncio.create_task(self._dispatch(params), name="cdp-fetch-dispatch")
        self._tasks[request_id] = task
        task.add_done_callback(lambda completed: self._finished(request_id, completed))

    async def _dispatch(self, params: dict[str, Any]) -> None:
        token = _current_dispatch.set((self, asyncio.current_task()))
        try:
            if self._closing:
                await Route(self._session, str(params["requestId"]), Request(params)).abort("aborted")
            else:
                await dispatch(self._session, self._tables, params)
        except Exception:
            self._drain_failed = True
            raise
        finally:
            _current_dispatch.reset(token)

    def _finished(self, request_id: str, task: asyncio.Task[None]) -> None:
        self._tasks.pop(request_id, None)
        if task.cancelled():
            return
        if task.exception() is not None:
            LOG.error("CDP Fetch settlement failed; closing affected target", exc_info=task.exception())
            self._fail_target()

    def _fail_target(self) -> None:
        if not self._closing and self._failure is None:
            self._failure = asyncio.create_task(self._on_failure(), name="cdp-fetch-close-target")
            self._failure.add_done_callback(self._observe_cleanup)

    @staticmethod
    def _observe_cleanup(task: asyncio.Task[None]) -> None:
        if not task.cancelled() and task.exception() is not None:
            LOG.error("CDP Fetch cleanup failed", exc_info=task.exception())

    async def disable_if_idle(self) -> None:
        if any(len(table) for table in self._tables):
            return
        if self._tasks:
            if self._retiring is None or self._retiring.done():
                self._retiring = asyncio.create_task(self._retire(list(self._tasks.values())), name="cdp-fetch-retire")
                self._retiring.add_done_callback(self._observe_cleanup)
            return
        await self._disable()

    async def _disable(self) -> None:
        async with self._lock:
            if self._enabled and not self._closing and not any(len(table) for table in self._tables):
                try:
                    await self._session.send("Fetch.disable")
                except CdpTargetClosedError:
                    pass
                except BaseException:
                    self._fail_target()
                    raise
                self._session.off("Fetch.requestPaused", self._paused)
                self._enabled = False

    async def _retire(self, cohort: list[asyncio.Task[None]]) -> None:
        await asyncio.gather(*(asyncio.shield(task) for task in cohort), return_exceptions=True)
        await self._disable()

    def _cancel_tasks(self) -> None:
        self._closing = True
        dispatch = _current_dispatch.get()
        initiator = dispatch[1] if dispatch is not None and dispatch[0] is self else None
        if initiator in self._tasks.values():
            self._closing_dispatch = initiator
        if self._retiring is not None:
            self._retiring.cancel()
        for task in self._tasks.values():
            if task not in (asyncio.current_task(), self._closing_dispatch):
                task.cancel()

    async def prepare_close(self) -> None:
        self._cancel_tasks()
        await asyncio.gather(
            *(task for task in self._tasks.values() if task not in (asyncio.current_task(), self._closing_dispatch)),
            return_exceptions=True,
        )
        if self._retiring is not None:
            await asyncio.gather(self._retiring, return_exceptions=True)

    def resume(self) -> None:
        if self._drain_failed:
            return
        self._closing = False
        self._closing_dispatch = None
        self._failure = None

    def cancel(self) -> None:
        self._cancel_tasks()
        self._session.off("Fetch.requestPaused", self._paused)

    async def close(self) -> None:
        self.cancel()
        await self.prepare_close()
        # Page.close destroys the target. Fetch.disable here could release a new, unhandled pause first.
