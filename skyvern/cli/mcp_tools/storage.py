"""MCP tools for web storage management (sessionStorage + localStorage clear).

Inline pattern — trivial page.evaluate wrappers, no do_* functions.
"""

from __future__ import annotations

import json
from typing import Annotated, Any

import structlog
from pydantic import Field

from ._common import ErrorCode, Timer, make_error, make_result
from ._session import BrowserNotAvailableError, get_page, no_browser_error

LOG = structlog.get_logger(__name__)


async def skyvern_get_session_storage(
    keys: Annotated[list[str] | None, Field(description="Specific keys to retrieve. Omit to get all.")] = None,
    session_id: Annotated[str | None, Field(description="Browser session ID (pbs_...).")] = None,
    cdp_url: Annotated[str | None, Field(description="CDP WebSocket URL.")] = None,
) -> dict[str, Any]:
    """Read sessionStorage values from the current page.

    Returns all key-value pairs, or specific keys if provided.
    Useful for reading auth tokens, user preferences, or temporary state stored by web apps.
    """
    try:
        page, ctx = await get_page(session_id=session_id, cdp_url=cdp_url)
    except BrowserNotAvailableError:
        return make_result("get_session_storage", ok=False, error=no_browser_error())

    with Timer() as timer:
        try:
            if keys:
                items = {}
                for key in keys:
                    val = await page.evaluate(f"() => window.sessionStorage.getItem({json.dumps(key)})")
                    if val is not None:
                        items[key] = val
            else:
                items = await page.evaluate("() => Object.fromEntries(Object.entries(window.sessionStorage))")
            timer.mark("sdk")
            return make_result(
                "get_session_storage",
                browser_context=ctx,
                data={"items": items, "count": len(items)},
                timing_ms=timer.timing_ms,
            )
        except Exception as e:
            return make_result(
                "get_session_storage",
                ok=False,
                browser_context=ctx,
                timing_ms=timer.timing_ms,
                error=make_error(ErrorCode.ACTION_FAILED, str(e), "Check page has loaded"),
            )


async def skyvern_set_session_storage(
    key: Annotated[str, Field(description="The key to set.")],
    value: Annotated[str, Field(description="The value to store.")],
    session_id: Annotated[str | None, Field(description="Browser session ID (pbs_...).")] = None,
    cdp_url: Annotated[str | None, Field(description="CDP WebSocket URL.")] = None,
) -> dict[str, Any]:
    """Set a sessionStorage key-value pair on the current page.

    sessionStorage persists only for the current tab/session and is cleared when the tab closes.
    """
    try:
        page, ctx = await get_page(session_id=session_id, cdp_url=cdp_url)
    except BrowserNotAvailableError:
        return make_result("set_session_storage", ok=False, error=no_browser_error())

    with Timer() as timer:
        try:
            await page.evaluate(
                "(args) => window.sessionStorage.setItem(args[0], args[1])",
                [key, value],
            )
            timer.mark("sdk")
            return make_result(
                "set_session_storage",
                browser_context=ctx,
                data={"key": key, "value_length": len(value)},
                timing_ms=timer.timing_ms,
            )
        except Exception as e:
            return make_result(
                "set_session_storage",
                ok=False,
                browser_context=ctx,
                timing_ms=timer.timing_ms,
                error=make_error(ErrorCode.ACTION_FAILED, str(e), "Check page has loaded"),
            )


async def skyvern_clear_session_storage(
    session_id: Annotated[str | None, Field(description="Browser session ID (pbs_...).")] = None,
    cdp_url: Annotated[str | None, Field(description="CDP WebSocket URL.")] = None,
) -> dict[str, Any]:
    """Clear all sessionStorage entries on the current page.

    This removes all key-value pairs from sessionStorage. Cannot be undone.
    """
    try:
        page, ctx = await get_page(session_id=session_id, cdp_url=cdp_url)
    except BrowserNotAvailableError:
        return make_result("clear_session_storage", ok=False, error=no_browser_error())

    with Timer() as timer:
        try:
            count = await page.evaluate(
                "() => { const n = window.sessionStorage.length; window.sessionStorage.clear(); return n; }"
            )
            timer.mark("sdk")
            return make_result(
                "clear_session_storage",
                browser_context=ctx,
                data={"cleared_count": count},
                timing_ms=timer.timing_ms,
            )
        except Exception as e:
            return make_result(
                "clear_session_storage",
                ok=False,
                browser_context=ctx,
                timing_ms=timer.timing_ms,
                error=make_error(ErrorCode.ACTION_FAILED, str(e), "Check page has loaded"),
            )


async def skyvern_clear_local_storage(
    session_id: Annotated[str | None, Field(description="Browser session ID (pbs_...).")] = None,
    cdp_url: Annotated[str | None, Field(description="CDP WebSocket URL.")] = None,
) -> dict[str, Any]:
    """Clear all localStorage entries on the current page.

    This removes all key-value pairs from localStorage. Cannot be undone.
    Use with caution — localStorage often contains login tokens and user preferences.
    """
    try:
        page, ctx = await get_page(session_id=session_id, cdp_url=cdp_url)
    except BrowserNotAvailableError:
        return make_result("clear_local_storage", ok=False, error=no_browser_error())

    with Timer() as timer:
        try:
            count = await page.evaluate(
                "() => { const n = window.localStorage.length; window.localStorage.clear(); return n; }"
            )
            timer.mark("sdk")
            return make_result(
                "clear_local_storage",
                browser_context=ctx,
                data={"cleared_count": count},
                timing_ms=timer.timing_ms,
            )
        except Exception as e:
            return make_result(
                "clear_local_storage",
                ok=False,
                browser_context=ctx,
                timing_ms=timer.timing_ms,
                error=make_error(ErrorCode.ACTION_FAILED, str(e), "Check page has loaded"),
            )
