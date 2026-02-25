from __future__ import annotations

from contextlib import asynccontextmanager
from contextvars import ContextVar
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, AsyncIterator

import structlog

from .api_key_hash import hash_api_key_for_cache
from .client import get_active_api_key, get_skyvern
from .result import BrowserContext, ErrorCode, make_error

LOG = structlog.get_logger(__name__)

if TYPE_CHECKING:
    from skyvern.library.skyvern_browser import SkyvernBrowser
    from skyvern.library.skyvern_browser_page import SkyvernBrowserPage


@dataclass
class SessionState:
    browser: SkyvernBrowser | None = None
    context: BrowserContext | None = None
    api_key_hash: str | None = None
    console_messages: list[dict[str, Any]] = field(default_factory=list)
    tracing_active: bool = False
    har_enabled: bool = False


_current_session: ContextVar[SessionState | None] = ContextVar("mcp_session", default=None)
_global_session: SessionState | None = None
_stateless_http_mode = False


def get_current_session() -> SessionState:
    global _global_session

    state = _current_session.get()
    if state is not None:
        return state

    # In stateless HTTP mode, avoid process-wide fallback state so requests
    # cannot inherit session context from other requests.
    if _stateless_http_mode:
        state = SessionState()
        _current_session.set(state)
        return state

    if _global_session is None:
        _global_session = SessionState()
    state = _global_session
    _current_session.set(state)
    return state


def set_current_session(state: SessionState) -> None:
    global _global_session
    if not _stateless_http_mode:
        _global_session = state
    _current_session.set(state)


def set_stateless_http_mode(enabled: bool) -> None:
    global _stateless_http_mode
    _stateless_http_mode = enabled


def is_stateless_http_mode() -> bool:
    return _stateless_http_mode


def _api_key_hash(api_key: str | None) -> str | None:
    if not api_key:
        return None
    return hash_api_key_for_cache(api_key)


def _matches_current(
    current: SessionState,
    *,
    session_id: str | None = None,
    cdp_url: str | None = None,
    local: bool = False,
) -> bool:
    if current.browser is None or current.context is None:
        return False
    if current.api_key_hash != _api_key_hash(get_active_api_key()):
        return False

    if session_id:
        return current.context.mode == "cloud_session" and current.context.session_id == session_id
    if cdp_url:
        return current.context.mode == "cdp" and current.context.cdp_url == cdp_url
    if local:
        return current.context.mode == "local"
    return False


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
    Cleanup is done via explicit skyvern_browser_session_close() call. For scripts that need
    guaranteed cleanup, use the browser_session() context manager instead.
    """
    skyvern = get_skyvern()
    current = get_current_session()

    if _stateless_http_mode and not (session_id or cdp_url or local or create_session):
        raise BrowserNotAvailableError()

    if _matches_current(current, session_id=session_id, cdp_url=cdp_url, local=local):
        if current.browser is None or current.context is None:
            raise RuntimeError("Expected active browser and context for matching session")
        return current.browser, current.context

    active_api_key_hash = _api_key_hash(get_active_api_key())
    browser: SkyvernBrowser | None = None
    try:
        if session_id:
            browser = await skyvern.connect_to_cloud_browser_session(session_id)
            ctx = BrowserContext(mode="cloud_session", session_id=session_id)
            set_current_session(SessionState(browser=browser, context=ctx, api_key_hash=active_api_key_hash))
            return browser, ctx

        if cdp_url:
            browser = await skyvern.connect_to_browser_over_cdp(cdp_url)
            ctx = BrowserContext(mode="cdp", cdp_url=cdp_url)
            set_current_session(SessionState(browser=browser, context=ctx, api_key_hash=active_api_key_hash))
            return browser, ctx

        if local:
            browser = await skyvern.launch_local_browser(headless=headless)
            ctx = BrowserContext(mode="local")
            set_current_session(SessionState(browser=browser, context=ctx, api_key_hash=active_api_key_hash))
            return browser, ctx

        if create_session:
            browser = await skyvern.launch_cloud_browser(timeout=timeout)
            ctx = BrowserContext(mode="cloud_session", session_id=browser.browser_session_id)
            set_current_session(SessionState(browser=browser, context=ctx, api_key_hash=active_api_key_hash))
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


async def close_current_session() -> None:
    """Close the active browser session (if any) and clear local session state."""
    from .session_ops import do_session_close

    current = get_current_session()
    try:
        if current.context and current.context.mode == "cloud_session" and current.context.session_id:
            try:
                skyvern = get_skyvern()
                await do_session_close(skyvern, current.context.session_id)
                # Prevent SkyvernBrowser.close() from making a redundant API call
                if current.browser is not None:
                    current.browser._browser_session_id = None
            except Exception:
                LOG.warning(
                    "Best-effort cloud session close failed",
                    session_id=current.context.session_id,
                    exc_info=True,
                )
        if current.browser is not None:
            await current.browser.close()
    finally:
        set_current_session(SessionState())


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
        "Create a session with skyvern_browser_session_create, provide session_id, or cdp_url",
    )
