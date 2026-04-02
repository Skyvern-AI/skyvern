from __future__ import annotations

import asyncio
import re
import time
from datetime import datetime, timedelta, timezone
from typing import Annotated, Any
from urllib.parse import parse_qsl, urlparse

import structlog
from pydantic import Field

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
    "Inspection tools are not supported in stateless HTTP mode. "
    "Event buffers are not persisted across requests in this transport. "
    "Use stdio transport (Claude Code, gstack) for browser inspection, "
    "or use skyvern_evaluate to run JavaScript that reads console/network state directly."
)
_STATELESS_HINT = (
    "Connect via stdio transport: `skyvern mcp` (default). "
    "Cloud-hosted inspection support is planned — see cloud_docs/mcp-inspection/TODOS.md"
)

LOG = structlog.get_logger(__name__)

# Network entries only capture content-type and content-length inline (not a full
# headers dict), so credential headers (Authorization, Cookie, Set-Cookie) are
# never included. If headers are added later, use an allowlist approach.


def _redact_url(url: str) -> str:
    """Strip secret values from URL query parameters.

    Params like ?token=xxx, ?api_key=xxx, and AWS signed URL params are replaced
    with ?token=REDACTED. Non-secret params are left intact.
    """
    from urllib.parse import parse_qs, urlencode, urlsplit, urlunsplit

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

            content_length = response.headers.get("content-length")
            try:
                response_size = int(content_length) if content_length is not None else None
            except (ValueError, TypeError):
                response_size = None
            state.network_requests.append(
                {
                    "url": _redact_url(response.url),
                    "method": response.request.method,
                    "status": response.status,
                    "content_type": response.headers.get("content-type", ""),
                    "timing_ms": round(timing, 1),
                    "response_size": response_size,
                    "page_url": raw_page.url,
                    "tab_id": str(id(raw_page)),
                }
            )

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
    """Read console log messages from the browser. Captures console.log, console.error, console.warn, etc.

    Messages are buffered automatically — call this anytime to see what the page has logged.
    Use level='error' to find JavaScript errors. Use text='...' to search for specific messages.
    """
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
    clear: Annotated[
        bool,
        Field(description="Clear the buffer after reading. Default false."),
    ] = False,
) -> dict[str, Any]:
    """Read network requests/responses from the browser. Captures all HTTP traffic the page generates.

    Each entry includes: url, method, status, content_type, timing_ms, response_size, and page_url.
    No response headers dict or response bodies are captured — credential headers (Authorization,
    Cookie, Set-Cookie) are never exposed. Use skyvern_evaluate with fetch() if you need body content.
    """
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
    has_filter = url_pattern is not None or status_code is not None or method is not None
    entries = list(state.network_requests)

    if url_pattern:
        try:
            pattern = re.compile(url_pattern)
            entries = [e for e in entries if pattern.search(e.get("url", ""))]
        except re.error:
            return make_result(
                "skyvern_network_requests",
                ok=False,
                browser_context=ctx,
                error=make_error(
                    ErrorCode.INVALID_INPUT,
                    f"Invalid regex pattern: {url_pattern}",
                    "Provide a valid Python regex pattern",
                ),
            )
    if status_code is not None:
        entries = [e for e in entries if e.get("status") == status_code]
    if method:
        method_upper = method.upper()
        entries = [e for e in entries if e.get("method") == method_upper]

    if clear:
        if has_filter:
            matched = {id(e) for e in entries}
            state.network_requests = type(state.network_requests)(
                (e for e in state.network_requests if id(e) not in matched),
                maxlen=state.network_requests.maxlen,
            )
        else:
            state.network_requests.clear()

    return make_result(
        "skyvern_network_requests",
        browser_context=ctx,
        data={
            "requests": entries,
            "count": len(entries),
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
    """Read the history of JavaScript dialogs (alert, confirm, prompt) that appeared on the page.

    Dialogs are automatically dismissed by default to prevent page lockup.
    This tool lets you see what dialogs appeared and what action was taken.
    """
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
    """Read uncaught JavaScript errors (exceptions) from the browser page.

    Captures unhandled errors thrown by page scripts (window onerror / unhandledrejection).
    These are distinct from console.error() messages — use skyvern_console_messages(level='error') for those.
    Use text='...' to search for specific error messages.
    """
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
    """Start recording network traffic in HAR format.

    All HTTP requests/responses will be captured until skyvern_har_stop is called.
    The HAR buffer is cleared on start. Only one recording can be active at a time.
    Use skyvern_har_stop to retrieve the HAR data.
    """
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
    """Stop HAR recording and return the captured traffic as HAR 1.2 JSON.

    Returns a complete HAR archive with all HTTP requests/responses captured since skyvern_har_start.
    The HAR data can be imported into browser DevTools, Charles Proxy, or other HTTP analysis tools.
    """
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
    """Get the HTML content of a DOM element.

    Returns innerHTML by default (children only). Set outer=true for outerHTML (includes the element tag).
    Useful for inspecting page structure, checking rendered content, or debugging element contents.
    """
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
    """Get the current value of a form input element.

    Works with <input>, <textarea>, and <select> elements.
    Returns the current value (what the user typed or selected), not the placeholder or label.
    """
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
    """Get computed CSS styles from a DOM element.

    Returns the browser's computed style values (after CSS cascade + inheritance).
    Specify properties for targeted lookup, or omit to get the first 100 computed properties.
    Useful for verifying visual styling, checking visibility, or debugging layout issues.
    """
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
