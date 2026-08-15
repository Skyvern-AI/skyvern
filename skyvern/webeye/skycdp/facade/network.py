"""Request interception over the CDP Fetch domain.

Every paused request must be answered exactly once. Chrome holds the request until it is continued,
fulfilled or failed, so a handler that raises, forgets, or answers twice does not produce an error --
it produces a page that stops loading. That makes the failure mode of this module silence, which is
why the unhandled paths below all end in an explicit answer.

The chain is Playwright's: handlers are consulted newest-first, and one that calls ``fallback()``
declines and passes the request to the next match, accumulating any overrides it supplied. A handler
that calls ``continue_()`` ends the chain there. Two production security guards are built on exactly
that distinction, so it is reproduced rather than approximated.
"""

from __future__ import annotations

import base64
import inspect
from typing import TYPE_CHECKING, Any

import structlog

from skyvern.webeye.skycdp.errors import CdpError, CdpTargetClosedError

if TYPE_CHECKING:
    from skyvern.webeye.skycdp.connection import CdpSession
    from skyvern.webeye.skycdp.facade.routing import RouteTable

LOG = structlog.get_logger()


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
    def method(self) -> str:
        return str(self._overrides.get("method", self._method))

    @property
    def headers(self) -> dict[str, str]:
        return dict(self._overrides.get("headers", self._headers))

    @property
    def post_data(self) -> str | None:
        value = self._overrides.get("post_data", self._post_data)
        return None if value is None else str(value)

    def is_navigation_request(self) -> bool:
        return self.is_navigation_request_value

    def apply_overrides(self, overrides: dict[str, Any]) -> None:
        """Accumulate what a declining handler asked to change, for the handler that follows."""
        self._overrides.update({key: value for key, value in overrides.items() if value is not None})

    @property
    def overrides(self) -> dict[str, Any]:
        return dict(self._overrides)


class Route:
    """One paused request. Exactly one of continue_/fulfill/abort/fallback settles it."""

    def __init__(self, session: CdpSession, request_id: str, request: Request) -> None:
        self._session = session
        self._request_id = request_id
        self.request = request
        self._settled = False
        self._fell_back = False

    @property
    def settled(self) -> bool:
        return self._settled

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
        if self._settled:
            return
        self._settled = True
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
        if self._settled:
            return
        self._settled = True

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
        if self._settled:
            return
        self._settled = True
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
        if self._settled:
            return
        self.request.apply_overrides({"url": url, "method": method, "headers": headers, "post_data": post_data})
        self._fell_back = True

    def reset_for_next_handler(self) -> None:
        self._fell_back = False

    async def _send(self, method: str, params: dict[str, Any]) -> None:
        try:
            await self._session.send(method, params)
        except CdpTargetClosedError:
            # The page went away while the request was paused; there is nothing left to answer.
            return
        except CdpError:
            LOG.debug("skycdp could not settle a paused request", cdp_method=method, exc_info=True)


async def dispatch(
    session: CdpSession,
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

    for table in tables:
        for entry in table.matching(request.url):
            route.reset_for_next_handler()
            try:
                result = entry.handler(route, request) if _wants_request(entry.handler) else entry.handler(route)
                if inspect.isawaitable(result):
                    await result
            except Exception:
                LOG.warning("skycdp route handler raised; failing the request closed", url=request.url, exc_info=True)
                await route.abort("failed")
                return
            if route.settled:
                return
            if not route.fell_back:
                # A handler that returned without settling or declining has abandoned the request;
                # treating that as a decline keeps the page alive and the chain moving.
                LOG.debug("skycdp route handler returned without settling; treating as fallback")
    await route.continue_()


def _wants_request(handler: Any) -> bool:
    """Whether a handler takes (route, request) rather than (route), as Playwright decides."""
    try:
        parameters = inspect.signature(handler).parameters
    except (TypeError, ValueError):
        return False
    return len([p for p in parameters.values() if p.kind in (p.POSITIONAL_ONLY, p.POSITIONAL_OR_KEYWORD)]) >= 2
