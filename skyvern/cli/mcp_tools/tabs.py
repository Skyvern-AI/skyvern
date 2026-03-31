"""MCP tools for browser tab management.

Provides tools to list, create, switch, close, and wait for browser tabs.
"""

from __future__ import annotations

import asyncio
from collections import deque
from typing import Annotated, Any

import structlog
from pydantic import BaseModel, Field

from ._common import ErrorCode, Timer, make_error, make_result
from ._session import BrowserNotAvailableError, get_current_session, get_page, no_browser_error

LOG = structlog.get_logger(__name__)

_STATELESS_TAB_MSG = (
    "Tab management tools that rely on persisted state (switch, close, wait_for_new) "
    "are not supported in stateless HTTP mode. Use stdio transport (Claude Code, gstack)."
)
_STATELESS_TAB_HINT = "Connect via stdio transport: `skyvern mcp` (default)."


class TabInfo(BaseModel):
    """Typed descriptor for a browser tab.

    NOTE: tab_id uses id(page) which can be reused after GC. A UUID-based
    tab ID scheme is planned as a follow-up to eliminate this class of issue.
    """

    tab_id: str
    index: int
    url: str
    title: str = ""
    is_active: bool


def _tab_info(page: Any, *, index: int, is_active: bool) -> TabInfo:
    """Build a TabInfo from a raw Playwright Page (sync — title left empty)."""
    return TabInfo(
        tab_id=str(id(page)),
        index=index,
        url=page.url,
        is_active=is_active,
    )


async def _tab_info_with_title(page: Any, *, index: int, is_active: bool) -> TabInfo:
    info = _tab_info(page, index=index, is_active=is_active)
    try:
        info.title = await page.title()
    except Exception:
        pass  # title defaults to ""
    return info


def _resolve_tab(
    pages: list[Any],
    *,
    tab_id: str | None = None,
    index: int | None = None,
) -> Any | None:
    """Find a page by tab_id (id(page)) or index. Returns None if not found or closed."""
    if tab_id is not None:
        for p in pages:
            if str(id(p)) == tab_id:
                return None if p.is_closed() else p
        return None
    if index is not None:
        if 0 <= index < len(pages):
            p = pages[index]
            return None if p.is_closed() else p
        return None
    return None


async def skyvern_tab_list(
    session_id: Annotated[str | None, Field(description="Browser session ID (pbs_...)")] = None,
    cdp_url: Annotated[str | None, Field(description="CDP WebSocket URL")] = None,
) -> dict[str, Any]:
    """List all open browser tabs with their URLs, titles, and active status.

    Returns an array of tabs, each with tab_id (session-scoped identifier for switching),
    index (position), url, title, and is_active flag.
    """
    try:
        page, ctx = await get_page(session_id=session_id, cdp_url=cdp_url)
    except BrowserNotAvailableError:
        return make_result("skyvern_tab_list", ok=False, error=no_browser_error())

    state = get_current_session()
    browser = state.browser
    if browser is None:
        return make_result("skyvern_tab_list", ok=False, error=no_browser_error())

    raw_pages = browser._browser_context.pages
    active_page = page.page  # The raw Playwright Page currently active

    tabs = []
    for i, p in enumerate(raw_pages):
        tabs.append(await _tab_info_with_title(p, index=i, is_active=(p is active_page)))

    return make_result(
        "skyvern_tab_list",
        browser_context=ctx,
        data={
            "tabs": [t.model_dump() for t in tabs],
            "count": len(tabs),
            "active_tab_id": str(id(active_page)),
        },
    )


async def skyvern_tab_new(
    session_id: Annotated[str | None, Field(description="Browser session ID (pbs_...)")] = None,
    cdp_url: Annotated[str | None, Field(description="CDP WebSocket URL")] = None,
    url: Annotated[
        str | None, Field(description="URL to navigate to in the new tab. Opens about:blank if omitted.")
    ] = None,
) -> dict[str, Any]:
    """Open a new browser tab. Optionally navigate to a URL. The new tab becomes the active tab.

    Use skyvern_tab_switch to go back to a previous tab.
    """
    try:
        _, ctx = await get_page(session_id=session_id, cdp_url=cdp_url)
    except BrowserNotAvailableError:
        return make_result("skyvern_tab_new", ok=False, error=no_browser_error())

    state = get_current_session()
    browser = state.browser
    if browser is None:
        return make_result("skyvern_tab_new", ok=False, error=no_browser_error())

    prev_active = state._active_page
    new_page = None
    with Timer() as timer:
        try:
            new_page = await browser._browser_context.new_page()
            state._active_page = new_page
            # New tab has no iframes yet — clear stale frame reference
            state._working_frame = None
            # Drain the event that _on_new_page() buffered for this explicitly
            # created page, so tab_wait_for_new doesn't return it as a popup.
            state._page_events = deque(
                (e for e in state._page_events if e["page"] is not new_page),
                maxlen=state._page_events.maxlen,
            )
            timer.mark("new_page")

            if url:
                await new_page.goto(url, wait_until="domcontentloaded", timeout=30000)
                timer.mark("navigate")
        except Exception as e:
            # Clean up the orphan tab and restore the previous active page
            try:
                state._active_page = prev_active
                if new_page is not None:
                    await new_page.close()
            except Exception:
                pass
            return make_result(
                "skyvern_tab_new",
                ok=False,
                browser_context=ctx,
                timing_ms=timer.timing_ms,
                error=make_error(ErrorCode.ACTION_FAILED, str(e), "Check URL or browser state"),
            )

    pages = browser._browser_context.pages
    index = pages.index(new_page) if new_page in pages else len(pages) - 1
    tab = await _tab_info_with_title(new_page, index=index, is_active=True)

    return make_result(
        "skyvern_tab_new",
        browser_context=ctx,
        data=tab.model_dump(),
        timing_ms=timer.timing_ms,
    )


async def skyvern_tab_switch(
    session_id: Annotated[str | None, Field(description="Browser session ID (pbs_...)")] = None,
    cdp_url: Annotated[str | None, Field(description="CDP WebSocket URL")] = None,
    tab_id: Annotated[str | None, Field(description="Tab ID from skyvern_tab_list to switch to")] = None,
    index: Annotated[int | None, Field(description="Tab index (0-based) to switch to")] = None,
) -> dict[str, Any]:
    """Switch the active browser tab. All subsequent browser tools will operate on this tab.

    Provide either tab_id (from skyvern_tab_list) or index (0-based position).
    Use skyvern_tab_list first to see available tabs and their IDs.
    """
    from skyvern.cli.core.session_manager import is_stateless_http_mode

    if is_stateless_http_mode():
        return make_result(
            "skyvern_tab_switch",
            ok=False,
            error=make_error(ErrorCode.ACTION_FAILED, _STATELESS_TAB_MSG, _STATELESS_TAB_HINT),
        )

    if tab_id is None and index is None:
        return make_result(
            "skyvern_tab_switch",
            ok=False,
            error=make_error(
                ErrorCode.INVALID_INPUT,
                "Must provide tab_id or index",
                "Use skyvern_tab_list to see available tabs, then pass tab_id or index",
            ),
        )

    try:
        _, ctx = await get_page(session_id=session_id, cdp_url=cdp_url)
    except BrowserNotAvailableError:
        return make_result("skyvern_tab_switch", ok=False, error=no_browser_error())

    state = get_current_session()
    browser = state.browser
    if browser is None:
        return make_result("skyvern_tab_switch", ok=False, error=no_browser_error())

    raw_pages = browser._browser_context.pages
    target = _resolve_tab(raw_pages, tab_id=tab_id, index=index)

    if target is None:
        return make_result(
            "skyvern_tab_switch",
            ok=False,
            browser_context=ctx,
            error=make_error(
                ErrorCode.INVALID_INPUT,
                f"Tab not found: tab_id={tab_id}, index={index}",
                "Use skyvern_tab_list to see available tabs",
            ),
        )

    state._active_page = target
    # Switching tabs invalidates any iframe frame reference from the old tab
    state._working_frame = None

    # bring_to_front is a no-op in headless but helps in headed mode
    try:
        await target.bring_to_front()
    except Exception:
        pass

    tab_index = raw_pages.index(target) if target in raw_pages else 0
    tab = await _tab_info_with_title(target, index=tab_index, is_active=True)

    return make_result(
        "skyvern_tab_switch",
        browser_context=ctx,
        data=tab.model_dump(),
    )


async def skyvern_tab_close(
    session_id: Annotated[str | None, Field(description="Browser session ID (pbs_...)")] = None,
    cdp_url: Annotated[str | None, Field(description="CDP WebSocket URL")] = None,
    tab_id: Annotated[str | None, Field(description="Tab ID to close. Closes active tab if omitted.")] = None,
    index: Annotated[int | None, Field(description="Tab index (0-based) to close.")] = None,
) -> dict[str, Any]:
    """Close a browser tab. Closes the active tab if no tab_id or index is given.

    If the last tab is closed, a new blank tab is created automatically.
    If the active tab is closed, the most recent remaining tab becomes active.
    """
    from skyvern.cli.core.session_manager import is_stateless_http_mode

    if is_stateless_http_mode():
        return make_result(
            "skyvern_tab_close",
            ok=False,
            error=make_error(ErrorCode.ACTION_FAILED, _STATELESS_TAB_MSG, _STATELESS_TAB_HINT),
        )

    try:
        page, ctx = await get_page(session_id=session_id, cdp_url=cdp_url)
    except BrowserNotAvailableError:
        return make_result("skyvern_tab_close", ok=False, error=no_browser_error())

    state = get_current_session()
    browser = state.browser
    if browser is None:
        return make_result("skyvern_tab_close", ok=False, error=no_browser_error())

    raw_pages = browser._browser_context.pages

    if tab_id is not None or index is not None:
        target = _resolve_tab(raw_pages, tab_id=tab_id, index=index)
        if target is None:
            return make_result(
                "skyvern_tab_close",
                ok=False,
                browser_context=ctx,
                error=make_error(
                    ErrorCode.INVALID_INPUT,
                    f"Tab not found: tab_id={tab_id}, index={index}",
                    "Use skyvern_tab_list to see available tabs",
                ),
            )
    else:
        target = page.page  # Close the active tab

    target_id = id(target)
    closed_tab_id = str(target_id)
    closing_active = target is page.page

    try:
        await target.close()
    except Exception as e:
        return make_result(
            "skyvern_tab_close",
            ok=False,
            browser_context=ctx,
            error=make_error(ErrorCode.ACTION_FAILED, str(e), "Tab may already be closed"),
        )

    # Clear active page — get_working_page() will lazily pick the last remaining page
    if closing_active or (state._active_page is not None and state._active_page is target):
        state._active_page = None
        # Closed tab's frame reference is no longer valid
        state._working_frame = None

    # Clean up inspection hooks for the closed page
    state._hooked_page_ids.discard(target_id)
    state._hooked_handlers_map.pop(target_id, None)

    remaining = len(browser._browser_context.pages)

    return make_result(
        "skyvern_tab_close",
        browser_context=ctx,
        data={
            "closed_tab_id": closed_tab_id,
            "remaining_tabs": remaining,
        },
    )


async def skyvern_tab_wait_for_new(
    session_id: Annotated[str | None, Field(description="Browser session ID (pbs_...)")] = None,
    cdp_url: Annotated[str | None, Field(description="CDP WebSocket URL")] = None,
    timeout_ms: Annotated[
        int,
        Field(description="Max time to wait for a new tab in ms. Default 30000 (30s)", ge=1000, le=120000),
    ] = 30000,
) -> dict[str, Any]:
    """Wait for a new browser tab to open (popup, target=_blank link, window.open).

    Checks the event buffer first — if a new tab already opened, returns it immediately.
    Returns one tab per call. If multiple popups may open, call repeatedly to drain them.
    Does NOT auto-switch to the new tab. Use skyvern_tab_switch after if desired.
    """
    from skyvern.cli.core.session_manager import is_stateless_http_mode

    if is_stateless_http_mode():
        return make_result(
            "skyvern_tab_wait_for_new",
            ok=False,
            error=make_error(ErrorCode.ACTION_FAILED, _STATELESS_TAB_MSG, _STATELESS_TAB_HINT),
        )

    try:
        _, ctx = await get_page(session_id=session_id, cdp_url=cdp_url)
    except BrowserNotAvailableError:
        return make_result("skyvern_tab_wait_for_new", ok=False, error=no_browser_error())

    state = get_current_session()
    browser = state.browser
    if browser is None:
        return make_result("skyvern_tab_wait_for_new", ok=False, error=no_browser_error())

    with Timer() as timer:
        # Check event buffer first — popup may have already opened.
        # Drain closed pages so we don't miss valid events behind them.
        while state._page_events:
            event = state._page_events.popleft()
            raw_page = event["page"]
            if not raw_page.is_closed():
                pages = browser._browser_context.pages
                idx = pages.index(raw_page) if raw_page in pages else -1
                tab = await _tab_info_with_title(raw_page, index=idx, is_active=False)
                timer.mark("from_buffer")
                return make_result(
                    "skyvern_tab_wait_for_new",
                    browser_context=ctx,
                    data=tab.model_dump(),
                    timing_ms=timer.timing_ms,
                )

        # Wait for a new page event
        try:
            new_page = await asyncio.wait_for(
                _wait_for_page_event(state),
                timeout=timeout_ms / 1000.0,
            )
            timer.mark("waited")
        except asyncio.TimeoutError:
            return make_result(
                "skyvern_tab_wait_for_new",
                ok=False,
                browser_context=ctx,
                timing_ms=timer.timing_ms,
                error=make_error(
                    ErrorCode.TIMEOUT,
                    f"No new tab opened within {timeout_ms}ms",
                    "Ensure the page action that opens a new tab has been triggered first",
                ),
            )

    pages = browser._browser_context.pages
    idx = pages.index(new_page) if new_page in pages else -1
    tab = await _tab_info_with_title(new_page, index=idx, is_active=False)

    return make_result(
        "skyvern_tab_wait_for_new",
        browser_context=ctx,
        data=tab.model_dump(),
        timing_ms=timer.timing_ms,
    )


async def _wait_for_page_event(state: Any) -> Any:
    """Wait for a new page event using asyncio.Event for near-instant response."""
    while True:
        # Clear BEFORE draining the queue to prevent lost wakeups: if _on_new_page
        # fires between the drain and the clear, the set() lands after the clear
        # and the next iteration catches the event.
        state._page_event_signal.clear()
        while state._page_events:
            event = state._page_events.popleft()
            raw_page = event["page"]
            if not raw_page.is_closed():
                return raw_page
        await state._page_event_signal.wait()
