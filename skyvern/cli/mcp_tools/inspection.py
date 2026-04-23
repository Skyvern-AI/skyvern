from __future__ import annotations

import asyncio
import time
from datetime import datetime, timedelta, timezone
from typing import Annotated, Any, Literal
from urllib.parse import parse_qs, parse_qsl, urlencode, urlparse, urlsplit, urlunsplit

import structlog
from pydantic import Field

from skyvern.cli.core.browser_ops import (
    do_network_request_detail,
    do_network_requests,
    do_network_route,
    do_network_unroute,
)

from ._common import ErrorCode, make_error, make_result
from ._session import BrowserNotAvailableError, get_current_session, get_page, no_browser_error

# Query param keys whose values are redacted from captured URLs.
_SECRET_QUERY_PARAMS = frozenset(
    {
        "token",
        "api_key",
        "apikey",
        "api-key",
        "access_token",
        "secret",
        "password",
        "key",
        "x-amz-signature",
        "x-amz-credential",
        "x-amz-security-token",
        "sig",
        "signature",
        "authorization",
        "auth",
    }
)

_REDACTED_HEADERS = frozenset({"authorization", "cookie", "set-cookie", "proxy-authorization"})
_SECRET_QS_NAMES = frozenset(p.lower() for p in _SECRET_QUERY_PARAMS)

_STATELESS_ERROR_MSG = (
    "Inspection tools (console_messages, network_requests, handle_dialog, get_errors, har_*) "
    "are not available in stateless HTTP mode because event buffers are not persisted between requests. "
    "Use skyvern_evaluate to read equivalent state directly from the page "
    "(e.g., `performance.getEntriesByType('resource')` for network, captured console output, or DOM state)."
)
_STATELESS_HINT = (
    "Call skyvern_evaluate with JavaScript that reads the page state you need. "
    "Cloud-hosted inspection-buffer support is not yet available in this transport mode."
)

LOG = structlog.get_logger(__name__)

# Response headers that are safe to expose (no credentials, no auth tokens).
_SAFE_RESPONSE_HEADERS = frozenset(
    {
        "content-type",
        "content-length",
        "content-encoding",
        "cache-control",
        "etag",
        "last-modified",
        "date",
        "server",
        "x-request-id",
        "x-correlation-id",
        "access-control-allow-origin",
        "access-control-allow-methods",
        "vary",
        "x-powered-by",
        "x-frame-options",
        "content-security-policy",
        "strict-transport-security",
    }
)

# Content types worth capturing bodies for (debugging value). Binary types skipped.
# Standard text-based content types worth capturing. Vendor-specific variants
# (e.g. application/vnd.api+json, application/hal+json) are intentionally
# excluded to keep the allowlist tight — add them here if needed.
_CAPTURABLE_CONTENT_TYPES = frozenset(
    {
        "application/json",
        "text/plain",
        "text/html",
        "text/xml",
        "application/xml",
        "text/javascript",
        "application/javascript",
        "application/x-www-form-urlencoded",
        "application/ld+json",
        "application/graphql-response+json",
    }
)

_MAX_BODY_BYTES = 256 * 1024  # 256 KB per body
_BODY_STORE_MAX = 100  # max bodies in memory


def _redact_url(url: str) -> str:
    """Strip secret values from URL query parameters.

    Params like ?token=xxx, ?api_key=xxx, and AWS signed URL params are replaced
    with ?token=REDACTED. Non-secret params are left intact.
    """
    parts = urlsplit(url)
    if not parts.query:
        return url
    params = parse_qs(parts.query, keep_blank_values=True)
    redacted = False
    for key in params:
        if key.lower() in _SECRET_QUERY_PARAMS:
            params[key] = ["REDACTED"]
            redacted = True
    if not redacted:
        return url
    new_query = urlencode(params, doseq=True)
    return urlunsplit((parts.scheme, parts.netloc, parts.path, new_query, parts.fragment))


def _safe_headers(headers: dict[str, str]) -> dict[str, str]:
    """Filter response headers to an allowlist — never expose auth/cookie headers."""
    return {k: v for k, v in headers.items() if k.lower() in _SAFE_RESPONSE_HEADERS}


def _should_capture_body(content_type: str, content_length: str | None) -> bool:
    """Decide whether a response body is worth capturing based on content type and size."""
    if not content_type:
        return False
    ct_base = content_type.lower().split(";")[0].strip()
    if ct_base not in _CAPTURABLE_CONTENT_TYPES:
        # Also match vendor-specific suffixes like application/vnd.api+json, application/hal+json
        if not (ct_base.endswith("+json") or ct_base.endswith("+xml")):
            return False
    if content_length is not None:
        try:
            if int(content_length) > _MAX_BODY_BYTES:
                return False
        except ValueError:
            pass
    return True


async def _capture_body(response: Any, request_id: int, state: Any) -> None:
    """Download response body via CDP and store in state._body_store with FIFO eviction."""
    async with state._body_semaphore:
        body_bytes = await asyncio.wait_for(response.body(), timeout=10.0)
        was_truncated = len(body_bytes) > _MAX_BODY_BYTES
        if was_truncated:
            body_bytes = body_bytes[:_MAX_BODY_BYTES]
        body_text = body_bytes.decode("utf-8", errors="replace")
        if was_truncated:
            body_text += "...[truncated]"
        # FIFO eviction — earliest-captured entries removed first (limit is approximate
        # under concurrency: up to _BODY_SEMAPHORE_LIMIT extra entries may exist momentarily)
        while len(state._body_store) >= _BODY_STORE_MAX:
            try:
                oldest_key = next(iter(state._body_store))
                del state._body_store[oldest_key]
            except (StopIteration, KeyError):
                break
        # Guard against stale writes: if clear was called while this task was
        # in-flight, the request_id is no longer in network_requests — skip the write.
        if not any(e.get("request_id") == request_id for e in state.network_requests):
            return
        state._body_store[request_id] = body_text


def _make_page_handlers(state: Any, raw_page: Any) -> dict[str, Any]:
    """Create console/network/dialog/pageerror handlers bound to a specific page."""

    def _on_console(msg: Any) -> None:
        try:
            state.console_messages.append(
                {
                    "level": msg.type,
                    "text": msg.text,
                    "timestamp": time.time(),
                    "page_url": raw_page.url,
                    "tab_id": str(id(raw_page)),
                    "source_url": msg.location.get("url", "") if hasattr(msg, "location") and msg.location else "",
                    "line_number": msg.location.get("lineNumber", 0)
                    if hasattr(msg, "location") and msg.location
                    else 0,
                }
            )
        except Exception:
            pass  # Never let a listener error crash the tool pipeline

    def _on_response(response: Any) -> None:
        try:
            timing = 0.0
            try:
                timing_obj = response.request.timing
                if isinstance(timing_obj, dict):
                    timing = timing_obj.get("responseEnd", 0)
            except Exception:
                pass

            request_id = next(state._request_id_counter)

            content_type = response.headers.get("content-type", "")
            content_length = response.headers.get("content-length")

            resource_type = ""
            try:
                resource_type = response.request.resource_type
            except Exception:
                pass

            try:
                response_size = int(content_length) if content_length is not None else None
            except (ValueError, TypeError):
                response_size = None
            state.network_requests.append(
                {
                    "request_id": request_id,
                    "url": _redact_url(response.url),
                    "method": response.request.method,
                    "status": response.status,
                    "content_type": content_type,
                    "resource_type": resource_type,
                    "timing_ms": round(timing, 1),
                    "response_size": response_size,
                    "response_headers": _safe_headers(response.headers),
                    "page_url": raw_page.url,
                    "tab_id": str(id(raw_page)),
                }
            )

            if _should_capture_body(content_type, content_length):
                try:
                    task = asyncio.create_task(_capture_body(response, request_id, state))
                    state._pending_tasks.add(task)
                    task.add_done_callback(state._pending_tasks.discard)
                    redacted_url = _redact_url(response.url)
                    task.add_done_callback(lambda t: _body_capture_done(t, request_id, redacted_url))
                except Exception:
                    LOG.warning("Body capture task creation failed", request_id=request_id, exc_info=True)

            # HAR recording: capture enhanced entry when enabled
            if state.har_enabled:
                req_headers = []
                try:
                    for k, v in response.request.headers.items():
                        if k.lower() not in _REDACTED_HEADERS:
                            req_headers.append({"name": k, "value": v})
                except Exception:
                    pass

                resp_headers = []
                try:
                    for k, v in response.headers.items():
                        if k.lower() not in _REDACTED_HEADERS:
                            resp_headers.append({"name": k, "value": v})
                except Exception:
                    pass

                # Approximate request start from response time minus elapsed
                started = datetime.now(timezone.utc) - timedelta(milliseconds=timing)
                qs = [
                    {"name": n, "value": "REDACTED" if n.lower() in _SECRET_QS_NAMES else v}
                    for n, v in parse_qsl(urlparse(response.url).query)
                ]

                state._har_entries.append(
                    {
                        "startedDateTime": started.isoformat(),
                        "time": round(timing, 1),
                        "request": {
                            "method": response.request.method,
                            "url": _redact_url(response.url),
                            "httpVersion": "HTTP/1.1",
                            "headers": req_headers,
                            "queryString": qs,
                            "cookies": [],
                            "headersSize": -1,
                            "bodySize": -1,
                        },
                        "response": {
                            "status": response.status,
                            "statusText": response.status_text if hasattr(response, "status_text") else "",
                            "httpVersion": "HTTP/1.1",
                            "headers": resp_headers,
                            "content": {
                                "size": response_size if response_size is not None else -1,
                                "mimeType": response.headers.get("content-type", ""),
                            },
                            "redirectURL": "",
                            "headersSize": -1,
                            "bodySize": -1,
                            "cookies": [],
                        },
                        "timings": {
                            "send": -1,
                            "wait": round(timing, 1),
                            "receive": -1,
                        },
                    }
                )
        except Exception:
            pass

    def _body_capture_done(task: asyncio.Task[None], request_id: int, url: str) -> None:
        if not task.cancelled() and task.exception() is not None:
            LOG.debug("Body capture failed", request_id=request_id, url=url, error=str(task.exception()))

    def _on_dialog(dialog: Any) -> None:
        try:
            event_record: dict[str, Any] = {
                "type": dialog.type,
                "message": dialog.message,
                "default_value": dialog.default_value if hasattr(dialog, "default_value") else None,
                "action_taken": "dismiss_pending",
                "timestamp": time.time(),
                "page_url": raw_page.url,
                "tab_id": str(id(raw_page)),
            }
            state.dialog_events.append(event_record)
            task = asyncio.create_task(dialog.dismiss())
            state._pending_tasks.add(task)
            task.add_done_callback(state._pending_tasks.discard)
            task.add_done_callback(lambda t: _dismiss_done(t, event_record))
        except Exception:
            pass

    def _dismiss_done(task: asyncio.Task[None], event_record: dict[str, Any]) -> None:
        if task.cancelled():
            event_record["action_taken"] = "dismiss_cancelled"
        elif task.exception() is not None:
            event_record["action_taken"] = "dismiss_failed"
            LOG.warning("Dialog dismiss failed", error=str(task.exception()))
        else:
            event_record["action_taken"] = "dismissed"

    def _on_pageerror(error: Any) -> None:
        try:
            try:
                message = str(error)
            except Exception:
                message = "<unserializable error>"
            state.page_errors.append(
                {
                    "message": message,
                    "timestamp": time.time(),
                    "page_url": raw_page.url,
                    "tab_id": str(id(raw_page)),
                }
            )
        except Exception:
            pass

    return {"console": _on_console, "response": _on_response, "dialog": _on_dialog, "pageerror": _on_pageerror}


def _register_hooks_on_page(state: Any, raw_page: Any) -> None:
    """Register event listeners on a single page. Idempotent per page id."""
    page_id = id(raw_page)
    if page_id in state._hooked_page_ids:
        return

    handlers = _make_page_handlers(state, raw_page)
    raw_page.on("console", handlers["console"])
    raw_page.on("response", handlers["response"])
    raw_page.on("dialog", handlers["dialog"])
    raw_page.on("pageerror", handlers["pageerror"])
    state._hooked_page_ids.add(page_id)
    state._hooked_handlers_map[page_id] = handlers


def ensure_hooks_on_all_pages(state: Any, all_pages: list[Any]) -> None:
    """Register inspection hooks on ALL open pages.

    Idempotent per page — only registers on pages not yet hooked. This ensures
    console/network/dialog events from background tabs (popups, target=_blank)
    are captured alongside the active tab. Each event includes a tab_id field
    for attribution.
    """
    for raw_page in all_pages:
        try:
            if not raw_page.is_closed():
                _register_hooks_on_page(state, raw_page)
        except Exception:
            LOG.debug("Failed to register hooks on page", exc_info=True)

    # Prune stale entries for closed pages
    live_ids = {id(p) for p in all_pages}
    stale = state._hooked_page_ids - live_ids
    for pid in stale:
        state._hooked_page_ids.discard(pid)
        state._hooked_handlers_map.pop(pid, None)


async def skyvern_console_messages(
    session_id: Annotated[str | None, Field(description="Browser session ID (pbs_...)")] = None,
    cdp_url: Annotated[str | None, Field(description="CDP WebSocket URL")] = None,
    level: Annotated[
        str | None,
        Field(description="Filter by level: log, info, warning, error, debug. Omit for all."),
    ] = None,
    text: Annotated[
        str | None,
        Field(description="Filter by substring match in message text. Case-insensitive."),
    ] = None,
    clear: Annotated[
        bool,
        Field(description="Clear the buffer after reading. Default false."),
    ] = False,
) -> dict[str, Any]:
    """Read console log messages from the browser. Filter by level ('error') or text substring."""
    # Inline import: session_manager → inspection (ensure_hooks_on_all_pages) creates a
    # circular import if these are at module level. See session_manager.py:get_page().
    from skyvern.cli.core.session_manager import is_stateless_http_mode

    if is_stateless_http_mode():
        return make_result(
            "skyvern_console_messages",
            ok=False,
            error=make_error(ErrorCode.ACTION_FAILED, _STATELESS_ERROR_MSG, _STATELESS_HINT),
        )

    try:
        page, ctx = await get_page(session_id=session_id, cdp_url=cdp_url)
    except BrowserNotAvailableError:
        return make_result("skyvern_console_messages", ok=False, error=no_browser_error())

    state = get_current_session()
    has_filter = level is not None or text is not None
    entries = list(state.console_messages)

    if level:
        entries = [e for e in entries if e.get("level") == level]
    if text:
        text_lower = text.lower()
        entries = [e for e in entries if text_lower in e.get("text", "").lower()]

    if clear:
        if has_filter:
            # Only remove matched entries — keep unmatched ones in the buffer
            matched = {id(e) for e in entries}
            state.console_messages = type(state.console_messages)(
                (e for e in state.console_messages if id(e) not in matched),
                maxlen=state.console_messages.maxlen,
            )
        else:
            state.console_messages.clear()

    return make_result(
        "skyvern_console_messages",
        browser_context=ctx,
        data={
            "messages": entries,
            "count": len(entries),
            "buffer_size": len(state.console_messages),
        },
    )


async def skyvern_network_requests(
    session_id: Annotated[str | None, Field(description="Browser session ID (pbs_...)")] = None,
    cdp_url: Annotated[str | None, Field(description="CDP WebSocket URL")] = None,
    url_pattern: Annotated[
        str | None,
        Field(description="Filter by URL regex pattern. Example: 'api/v1' or '\\.json$'"),
    ] = None,
    status_code: Annotated[
        int | None,
        Field(description="Filter by exact HTTP status code. Example: 404"),
    ] = None,
    method: Annotated[
        str | None,
        Field(description="Filter by HTTP method: GET, POST, PUT, DELETE, etc."),
    ] = None,
    resource_type: Annotated[
        str | None,
        Field(
            description="Filter by resource type: document, stylesheet, image, media, font, "
            "script, xhr, fetch, websocket, manifest, other."
        ),
    ] = None,
    clear: Annotated[
        bool,
        Field(description="Clear the buffer after reading. Default false."),
    ] = False,
) -> dict[str, Any]:
    """Read captured network requests/responses. Use request_id with skyvern_network_request_detail for headers and body."""
    # Inline import: session_manager → inspection (ensure_hooks_on_all_pages) creates a
    # circular import if these are at module level. See session_manager.py:get_page().
    from skyvern.cli.core.session_manager import is_stateless_http_mode

    if is_stateless_http_mode():
        return make_result(
            "skyvern_network_requests",
            ok=False,
            error=make_error(ErrorCode.ACTION_FAILED, _STATELESS_ERROR_MSG, _STATELESS_HINT),
        )

    try:
        page, ctx = await get_page(session_id=session_id, cdp_url=cdp_url)
    except BrowserNotAvailableError:
        return make_result("skyvern_network_requests", ok=False, error=no_browser_error())

    state = get_current_session()
    result = do_network_requests(
        state,
        url_pattern=url_pattern,
        status_code=status_code,
        method=method,
        resource_type=resource_type,
    )
    if result.error:
        return make_result(
            "skyvern_network_requests",
            ok=False,
            browser_context=ctx,
            error=result.error,
        )

    if clear:
        has_filter = (
            url_pattern is not None or status_code is not None or method is not None or resource_type is not None
        )
        if has_filter:
            matched_ids = {e.get("request_id") for e in result.requests}
            state.network_requests = type(state.network_requests)(
                (e for e in state.network_requests if e.get("request_id") not in matched_ids),
                maxlen=state.network_requests.maxlen,
            )
            # Prune orphaned bodies for cleared requests
            for rid in matched_ids:
                if rid is not None:
                    state._body_store.pop(rid, None)
        else:
            state.network_requests.clear()
            state._body_store.clear()

    return make_result(
        "skyvern_network_requests",
        browser_context=ctx,
        data={
            "requests": result.requests,
            "count": result.count,
            "buffer_size": len(state.network_requests),
        },
    )


async def skyvern_handle_dialog(
    session_id: Annotated[str | None, Field(description="Browser session ID (pbs_...)")] = None,
    cdp_url: Annotated[str | None, Field(description="CDP WebSocket URL")] = None,
    clear: Annotated[
        bool,
        Field(description="Clear the dialog history after reading. Default false."),
    ] = False,
) -> dict[str, Any]:
    """Read the history of JavaScript dialogs (alert, confirm, prompt). Auto-dismissed by default."""
    # Inline import: session_manager → inspection (ensure_hooks_on_all_pages) creates a
    # circular import if these are at module level. See session_manager.py:get_page().
    from skyvern.cli.core.session_manager import is_stateless_http_mode

    if is_stateless_http_mode():
        return make_result(
            "skyvern_handle_dialog",
            ok=False,
            error=make_error(ErrorCode.ACTION_FAILED, _STATELESS_ERROR_MSG, _STATELESS_HINT),
        )

    try:
        page, ctx = await get_page(session_id=session_id, cdp_url=cdp_url)
    except BrowserNotAvailableError:
        return make_result("skyvern_handle_dialog", ok=False, error=no_browser_error())

    state = get_current_session()
    entries = list(state.dialog_events)

    if clear:
        state.dialog_events.clear()

    return make_result(
        "skyvern_handle_dialog",
        browser_context=ctx,
        data={
            "dialogs": entries,
            "count": len(entries),
        },
    )


# -- Page JS error tool --


async def skyvern_get_errors(
    session_id: Annotated[str | None, Field(description="Browser session ID (pbs_...)")] = None,
    cdp_url: Annotated[str | None, Field(description="CDP WebSocket URL")] = None,
    text: Annotated[
        str | None,
        Field(description="Filter by substring match in error message. Case-insensitive."),
    ] = None,
    clear: Annotated[
        bool,
        Field(description="Clear the buffer after reading. Default false."),
    ] = False,
) -> dict[str, Any]:
    """Read uncaught JavaScript exceptions. Distinct from console.error — use skyvern_console_messages for those."""
    from skyvern.cli.core.session_manager import is_stateless_http_mode

    if is_stateless_http_mode():
        return make_result(
            "skyvern_get_errors",
            ok=False,
            error=make_error(ErrorCode.ACTION_FAILED, _STATELESS_ERROR_MSG, _STATELESS_HINT),
        )

    try:
        page, ctx = await get_page(session_id=session_id, cdp_url=cdp_url)
    except BrowserNotAvailableError:
        return make_result("skyvern_get_errors", ok=False, error=no_browser_error())

    state = get_current_session()
    has_filter = text is not None
    entries = list(state.page_errors)

    if text:
        text_lower = text.lower()
        entries = [e for e in entries if text_lower in e.get("message", "").lower()]

    if clear:
        if has_filter:
            matched = {id(e) for e in entries}
            state.page_errors = type(state.page_errors)(
                (e for e in state.page_errors if id(e) not in matched),
                maxlen=state.page_errors.maxlen,
            )
        else:
            state.page_errors.clear()

    return make_result(
        "skyvern_get_errors",
        browser_context=ctx,
        data={
            "errors": entries,
            "count": len(entries),
            "buffer_size": len(state.page_errors),
        },
    )


# -- HAR recording tools --


async def skyvern_har_start(
    session_id: Annotated[str | None, Field(description="Browser session ID (pbs_...)")] = None,
    cdp_url: Annotated[str | None, Field(description="CDP WebSocket URL")] = None,
) -> dict[str, Any]:
    """Start recording network traffic in HAR format. Call skyvern_har_stop to retrieve the data."""
    from skyvern.cli.core.session_manager import is_stateless_http_mode

    if is_stateless_http_mode():
        return make_result(
            "skyvern_har_start",
            ok=False,
            error=make_error(ErrorCode.ACTION_FAILED, _STATELESS_ERROR_MSG, _STATELESS_HINT),
        )

    try:
        _, ctx = await get_page(session_id=session_id, cdp_url=cdp_url)
    except BrowserNotAvailableError:
        return make_result("skyvern_har_start", ok=False, error=no_browser_error())

    state = get_current_session()

    if state.har_enabled:
        return make_result(
            "skyvern_har_start",
            ok=False,
            browser_context=ctx,
            error=make_error(
                ErrorCode.ACTION_FAILED,
                "HAR recording is already active",
                "Call skyvern_har_stop first to stop the current recording",
            ),
        )

    state._har_entries.clear()
    state.har_enabled = True

    return make_result(
        "skyvern_har_start",
        browser_context=ctx,
        data={
            "recording": True,
            "message": "HAR recording started. Network traffic is being captured.",
        },
    )


async def skyvern_har_stop(
    session_id: Annotated[str | None, Field(description="Browser session ID (pbs_...)")] = None,
    cdp_url: Annotated[str | None, Field(description="CDP WebSocket URL")] = None,
) -> dict[str, Any]:
    """Stop HAR recording and return captured traffic as HAR 1.2 JSON."""
    from skyvern.cli.core.session_manager import is_stateless_http_mode

    if is_stateless_http_mode():
        return make_result(
            "skyvern_har_stop",
            ok=False,
            error=make_error(ErrorCode.ACTION_FAILED, _STATELESS_ERROR_MSG, _STATELESS_HINT),
        )

    try:
        _, ctx = await get_page(session_id=session_id, cdp_url=cdp_url)
    except BrowserNotAvailableError:
        return make_result("skyvern_har_stop", ok=False, error=no_browser_error())

    state = get_current_session()

    if not state.har_enabled:
        return make_result(
            "skyvern_har_stop",
            ok=False,
            browser_context=ctx,
            error=make_error(
                ErrorCode.ACTION_FAILED,
                "No active HAR recording",
                "Call skyvern_har_start first to begin recording",
            ),
        )

    entries = list(state._har_entries)
    state.har_enabled = False
    state._har_entries.clear()

    har = {
        "log": {
            "version": "1.2",
            "creator": {"name": "Skyvern", "version": "1.0"},
            "pages": [],
            "entries": entries,
        },
    }

    return make_result(
        "skyvern_har_stop",
        browser_context=ctx,
        data={
            "har": har,
            "entry_count": len(entries),
        },
    )


# -- DOM inspection tools --


async def skyvern_get_html(
    selector: Annotated[str, Field(description="CSS or XPath selector for the element.")],
    outer: Annotated[
        bool,
        Field(description="If true, return outerHTML (includes the element itself). Default false (innerHTML)."),
    ] = False,
    session_id: Annotated[str | None, Field(description="Browser session ID (pbs_...)")] = None,
    cdp_url: Annotated[str | None, Field(description="CDP WebSocket URL")] = None,
) -> dict[str, Any]:
    """Get HTML content of a DOM element. Returns innerHTML by default; set outer=true for outerHTML."""
    from skyvern.cli.core.browser_ops import do_get_html

    try:
        page, ctx = await get_page(session_id=session_id, cdp_url=cdp_url)
    except BrowserNotAvailableError:
        return make_result("skyvern_get_html", ok=False, error=no_browser_error())

    try:
        html = await do_get_html(page, selector, outer=outer)
        return make_result(
            "skyvern_get_html",
            browser_context=ctx,
            data={
                "html": html,
                "selector": selector,
                "outer": outer,
                "length": len(html),
            },
        )
    except Exception as e:
        return make_result(
            "skyvern_get_html",
            ok=False,
            browser_context=ctx,
            error=make_error(ErrorCode.ACTION_FAILED, str(e), "Check that the selector matches an element on the page"),
        )


async def skyvern_get_value(
    selector: Annotated[str, Field(description="CSS or XPath selector for the input element.")],
    session_id: Annotated[str | None, Field(description="Browser session ID (pbs_...)")] = None,
    cdp_url: Annotated[str | None, Field(description="CDP WebSocket URL")] = None,
) -> dict[str, Any]:
    """Get the current value of a form input element (<input>, <textarea>, <select>)."""
    from skyvern.cli.core.browser_ops import do_get_value

    try:
        page, ctx = await get_page(session_id=session_id, cdp_url=cdp_url)
    except BrowserNotAvailableError:
        return make_result("skyvern_get_value", ok=False, error=no_browser_error())

    try:
        value = await do_get_value(page, selector)
        return make_result(
            "skyvern_get_value",
            browser_context=ctx,
            data={
                "value": value,
                "selector": selector,
            },
        )
    except Exception as e:
        return make_result(
            "skyvern_get_value",
            ok=False,
            browser_context=ctx,
            error=make_error(
                ErrorCode.ACTION_FAILED, str(e), "Check that the selector matches an input/textarea/select element"
            ),
        )


async def skyvern_get_styles(
    selector: Annotated[str, Field(description="CSS or XPath selector for the element.")],
    properties: Annotated[
        list[str] | None,
        Field(description="Specific CSS properties to retrieve (e.g. ['color', 'font-size']). Omit for all (max 100)."),
    ] = None,
    session_id: Annotated[str | None, Field(description="Browser session ID (pbs_...)")] = None,
    cdp_url: Annotated[str | None, Field(description="CDP WebSocket URL")] = None,
) -> dict[str, Any]:
    """Get computed CSS styles from a DOM element. Specify properties for targeted lookup or omit for all."""
    from skyvern.cli.core.browser_ops import do_get_styles

    try:
        page, ctx = await get_page(session_id=session_id, cdp_url=cdp_url)
    except BrowserNotAvailableError:
        return make_result("skyvern_get_styles", ok=False, error=no_browser_error())

    try:
        styles = await do_get_styles(page, selector, properties=properties)
        return make_result(
            "skyvern_get_styles",
            browser_context=ctx,
            data={
                "styles": styles,
                "selector": selector,
                "count": len(styles),
            },
        )
    except Exception as e:
        return make_result(
            "skyvern_get_styles",
            ok=False,
            browser_context=ctx,
            error=make_error(ErrorCode.ACTION_FAILED, str(e), "Check that the selector matches an element on the page"),
        )


async def skyvern_network_request_detail(
    request_id: Annotated[int, Field(description="The request_id from skyvern_network_requests output.")],
    session_id: Annotated[str | None, Field(description="Browser session ID (pbs_...)")] = None,
    cdp_url: Annotated[str | None, Field(description="CDP WebSocket URL")] = None,
) -> dict[str, Any]:
    """Get full details for a network request by request_id: response headers and captured body."""
    from skyvern.cli.core.session_manager import is_stateless_http_mode

    if is_stateless_http_mode():
        return make_result(
            "skyvern_network_request_detail",
            ok=False,
            error=make_error(ErrorCode.ACTION_FAILED, _STATELESS_ERROR_MSG, _STATELESS_HINT),
        )

    try:
        page, ctx = await get_page(session_id=session_id, cdp_url=cdp_url)
    except BrowserNotAvailableError:
        return make_result("skyvern_network_request_detail", ok=False, error=no_browser_error())

    state = get_current_session()
    result = do_network_request_detail(state, request_id)
    if not result.found:
        return make_result(
            "skyvern_network_request_detail",
            ok=False,
            browser_context=ctx,
            error=make_error(
                ErrorCode.INVALID_INPUT,
                f"Request ID {request_id} not found in buffer",
                "Call skyvern_network_requests first to see available request_ids",
            ),
        )

    return make_result(
        "skyvern_network_request_detail",
        browser_context=ctx,
        data={
            "request": result.request,
            "body": result.body,
            "body_available": result.body is not None,
        },
    )


async def skyvern_network_route(
    url_pattern: Annotated[str, Field(description="URL glob pattern to intercept. Example: '**/api/*' or '*.png'")],
    action: Annotated[
        Literal["abort", "mock"],
        Field(description="Action: 'abort' blocks matched requests, 'mock' returns a fake response."),
    ] = "abort",
    mock_status: Annotated[int, Field(description="HTTP status for mock responses. Default 200.")] = 200,
    mock_body: Annotated[str | None, Field(description="Response body for mock action.")] = None,
    mock_content_type: Annotated[
        str | None,
        Field(
            description="Content-Type header for mock responses. Defaults to 'application/json' when mock_body is provided."
        ),
    ] = None,
    session_id: Annotated[str | None, Field(description="Browser session ID (pbs_...)")] = None,
    cdp_url: Annotated[str | None, Field(description="CDP WebSocket URL")] = None,
) -> dict[str, Any]:
    """Intercept network requests matching a URL glob pattern. Use 'abort' to block or 'mock' to return fake data.
    Call skyvern_network_unroute to remove.
    """
    from skyvern.cli.core.session_manager import is_stateless_http_mode

    if is_stateless_http_mode():
        return make_result(
            "skyvern_network_route",
            ok=False,
            error=make_error(ErrorCode.ACTION_FAILED, _STATELESS_ERROR_MSG, _STATELESS_HINT),
        )

    try:
        page, ctx = await get_page(session_id=session_id, cdp_url=cdp_url)
    except BrowserNotAvailableError:
        return make_result("skyvern_network_route", ok=False, error=no_browser_error())

    state = get_current_session()
    raw_page = page.page
    try:
        result = await do_network_route(
            raw_page,
            state,
            url_pattern=url_pattern,
            action=action,
            mock_status=mock_status,
            mock_body=mock_body,
            mock_content_type=mock_content_type,
        )
    except Exception as exc:
        return make_result(
            "skyvern_network_route",
            ok=False,
            browser_context=ctx,
            error=make_error(ErrorCode.ACTION_FAILED, str(exc), "Check the URL pattern syntax"),
        )

    return make_result(
        "skyvern_network_route",
        browser_context=ctx,
        data={
            "url_pattern": result.url_pattern,
            "action": result.action,
            "active_routes": result.active_routes,
        },
    )


async def skyvern_network_unroute(
    url_pattern: Annotated[str, Field(description="URL pattern to stop intercepting. Must match a previous route.")],
    session_id: Annotated[str | None, Field(description="Browser session ID (pbs_...)")] = None,
    cdp_url: Annotated[str | None, Field(description="CDP WebSocket URL")] = None,
) -> dict[str, Any]:
    """Remove a network interception rule. Requests matching the pattern will flow normally again."""
    from skyvern.cli.core.session_manager import is_stateless_http_mode

    if is_stateless_http_mode():
        return make_result(
            "skyvern_network_unroute",
            ok=False,
            error=make_error(ErrorCode.ACTION_FAILED, _STATELESS_ERROR_MSG, _STATELESS_HINT),
        )

    try:
        page, ctx = await get_page(session_id=session_id, cdp_url=cdp_url)
    except BrowserNotAvailableError:
        return make_result("skyvern_network_unroute", ok=False, error=no_browser_error())

    state = get_current_session()
    raw_page = page.page
    try:
        result = await do_network_unroute(raw_page, state, url_pattern)
    except Exception as exc:
        return make_result(
            "skyvern_network_unroute",
            ok=False,
            browser_context=ctx,
            error=make_error(ErrorCode.ACTION_FAILED, str(exc), "Check the URL pattern"),
        )

    return make_result(
        "skyvern_network_unroute",
        browser_context=ctx,
        data={
            "url_pattern": result.url_pattern,
            "removed": result.removed,
            "active_routes": result.active_routes,
        },
    )
