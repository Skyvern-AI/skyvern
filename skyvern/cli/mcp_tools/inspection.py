from __future__ import annotations

import asyncio
import re
import time
from typing import Annotated, Any

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


def ensure_hooks_registered(state: Any, page: Any) -> None:
    """Register console/network/dialog event listeners on the Playwright page.

    Idempotent: only registers when the underlying Playwright Page object changes
    (tracked by id). On page switch, removes listeners from the old page to prevent
    listener leaks and stale events mixing into the buffers.
    """
    raw_page = page.page  # SkyvernPage stores raw Playwright Page as self.page
    page_id = id(raw_page)

    if state._hooked_page_id == page_id:
        return

    # Remove listeners from the old page to prevent leaks
    old_page = state._hooked_raw_page
    old_handlers = state._hooked_handlers
    if old_page is not None and old_handlers:
        for event_name, handler in old_handlers.items():
            try:
                old_page.remove_listener(event_name, handler)
            except Exception:
                pass  # Old page may already be closed
        state._hooked_raw_page = None
        state._hooked_handlers = {}

    def _on_console(msg: Any) -> None:
        try:
            state.console_messages.append(
                {
                    "level": msg.type,
                    "text": msg.text,
                    "timestamp": time.time(),
                    "page_url": raw_page.url,
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
            state.network_requests.append(
                {
                    "url": _redact_url(response.url),
                    "method": response.request.method,
                    "status": response.status,
                    "content_type": response.headers.get("content-type", ""),
                    "timing_ms": round(timing, 1),
                    "response_size": int(content_length) if content_length is not None else None,
                    "page_url": raw_page.url,
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
            }
            state.dialog_events.append(event_record)
            # Auto-dismiss to match Playwright defaults and prevent page lockup.
            # dialog.dismiss() is async — schedule it and track the outcome.
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

    raw_page.on("console", _on_console)
    raw_page.on("response", _on_response)
    raw_page.on("dialog", _on_dialog)
    state._hooked_page_id = page_id
    state._hooked_raw_page = raw_page
    state._hooked_handlers = {"console": _on_console, "response": _on_response, "dialog": _on_dialog}


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
    # Inline import: session_manager → inspection (ensure_hooks_registered) creates a
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
    # Inline import: session_manager → inspection (ensure_hooks_registered) creates a
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
    # Inline import: session_manager → inspection (ensure_hooks_registered) creates a
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
