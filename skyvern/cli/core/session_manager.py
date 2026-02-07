from __future__ import annotations

from contextlib import asynccontextmanager
from contextvars import ContextVar
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, AsyncIterator

from .client import get_skyvern
from .result import BrowserContext, ErrorCode, make_error

if TYPE_CHECKING:
    from skyvern.library.skyvern_browser import SkyvernBrowser
    from skyvern.library.skyvern_browser_page import SkyvernBrowserPage


@dataclass
class SessionState:
    browser: SkyvernBrowser | None = None
    context: BrowserContext | None = None
    console_messages: list[dict[str, Any]] = field(default_factory=list)
    tracing_active: bool = False
    har_enabled: bool = False


_current_session: ContextVar[SessionState | None] = ContextVar("mcp_session", default=None)


def get_current_session() -> SessionState:
    state = _current_session.get()
    if state is None:
        state = SessionState()
        _current_session.set(state)
    return state


def set_current_session(state: SessionState) -> None:
    _current_session.set(state)


async def resolve_browser(
    session_id: str | None = None,
    cdp_url: str | None = None,
    local: bool = False,
    create_session: bool = False,
    timeout: int | None = None,
    headless: bool = False,
) -> tuple[SkyvernBrowser, BrowserContext]:
    """Resolve browser from parameters or current session.

    Note: For MCP tools, sessions are stored in ContextVar and persist across tool calls.
    Cleanup is done via explicit skyvern_session_close() call. For scripts that need
    guaranteed cleanup, use the browser_session() context manager instead.
    """
    skyvern = get_skyvern()
    current = get_current_session()

    browser: SkyvernBrowser | None = None
    try:
        if session_id:
            browser = await skyvern.connect_to_cloud_browser_session(session_id)
            ctx = BrowserContext(mode="cloud_session", session_id=session_id)
            set_current_session(SessionState(browser=browser, context=ctx))
            return browser, ctx

        if cdp_url:
            browser = await skyvern.connect_to_browser_over_cdp(cdp_url)
            ctx = BrowserContext(mode="cdp", cdp_url=cdp_url)
            set_current_session(SessionState(browser=browser, context=ctx))
            return browser, ctx

        if local:
            browser = await skyvern.launch_local_browser(headless=headless)
            ctx = BrowserContext(mode="local")
            set_current_session(SessionState(browser=browser, context=ctx))
            return browser, ctx

        if create_session:
            browser = await skyvern.launch_cloud_browser(timeout=timeout)
            ctx = BrowserContext(mode="cloud_session", session_id=browser.browser_session_id)
            set_current_session(SessionState(browser=browser, context=ctx))
            return browser, ctx
    except Exception:
        if browser is not None:
            try:
                await browser.close()
            except Exception:
                pass
        set_current_session(SessionState())
        raise

    if current.browser is not None and current.context is not None:
        return current.browser, current.context

    raise BrowserNotAvailableError()


async def get_page(
    session_id: str | None = None,
    cdp_url: str | None = None,
) -> tuple[SkyvernBrowserPage, BrowserContext]:
    """Get the working page from the current or specified browser session."""
    browser, ctx = await resolve_browser(session_id=session_id, cdp_url=cdp_url)
    page = await browser.get_working_page()
    return page, ctx


@asynccontextmanager
async def browser_session(
    session_id: str | None = None,
    cdp_url: str | None = None,
    local: bool = False,
    timeout: int | None = None,
    headless: bool = False,
) -> AsyncIterator[tuple[SkyvernBrowser, BrowserContext]]:
    """Context manager for browser sessions with guaranteed cleanup.

    Use this in scripts that need guaranteed resource cleanup on error.
    MCP tools use resolve_browser() directly since sessions persist across calls.

    Example:
        async with browser_session(local=True) as (browser, ctx):
            page = await browser.get_working_page()
            await page.goto("https://example.com")
        # Browser is automatically closed on exit or exception
    """
    browser, ctx = await resolve_browser(
        session_id=session_id,
        cdp_url=cdp_url,
        local=local,
        create_session=not (session_id or cdp_url or local),
        timeout=timeout,
        headless=headless,
    )
    try:
        yield browser, ctx
    finally:
        try:
            await browser.close()
        except Exception:
            pass  # Best effort cleanup
        set_current_session(SessionState())


class BrowserNotAvailableError(Exception):
    """Raised when no browser session is available."""


def no_browser_error() -> dict[str, Any]:
    return make_error(
        ErrorCode.NO_ACTIVE_BROWSER,
        "No browser session available",
        "Create a session with skyvern_session_create, provide session_id, or cdp_url",
    )
