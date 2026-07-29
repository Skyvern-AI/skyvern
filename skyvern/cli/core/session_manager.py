from __future__ import annotations

import asyncio
import itertools
import secrets
import time
import weakref
from collections import deque
from contextlib import asynccontextmanager
from contextvars import ContextVar
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, AsyncIterator

import structlog

from skyvern.config import settings

from .api_key_hash import hash_api_key_for_cache
from .client import get_active_api_key, get_skyvern, has_api_key_override
from .result import BrowserContext, ErrorCode, make_error
from .trajectory_store import delete_session_trajectories

LOG = structlog.get_logger(__name__)

_BODY_SEMAPHORE_LIMIT = 5  # concurrent CDP body downloads (worst case: 5 * 10s timeout = 50s backlog)

if TYPE_CHECKING:
    from playwright.async_api import Frame, Page

    from skyvern.library.skyvern_browser import SkyvernBrowser
    from skyvern.library.skyvern_browser_page import SkyvernBrowserPage


@dataclass
class SessionState:
    browser: SkyvernBrowser | None = None
    context: BrowserContext | None = None
    api_key_hash: str | None = None
    console_messages: deque[dict[str, Any]] = field(default_factory=lambda: deque(maxlen=1000))
    network_requests: deque[dict[str, Any]] = field(default_factory=lambda: deque(maxlen=1000))
    dialog_events: deque[dict[str, Any]] = field(default_factory=lambda: deque(maxlen=1000))
    page_errors: deque[dict[str, Any]] = field(default_factory=lambda: deque(maxlen=1000))
    tracing_active: bool = False
    har_enabled: bool = False
    _har_entries: deque[dict[str, Any]] = field(default_factory=lambda: deque(maxlen=5000))
    # -- Active page tracking (tab management) --
    _active_page: Page | None = None
    # -- Page event buffer for tab_wait_for_new --
    _page_events: deque[dict[str, Any]] = field(default_factory=lambda: deque(maxlen=100))
    _page_event_signal: asyncio.Event = field(default_factory=lambda: asyncio.Event())
    _page_event_listener_installed: bool = False
    # -- Multi-page inspection hooks --
    _hooked_page_ids: set[int] = field(default_factory=set)
    _hooked_handlers_map: dict[int, dict[str, Any]] = field(default_factory=dict)
    # Per-request network state: ID counter, body cache, concurrency limiter, route interceptions
    _request_id_counter: itertools.count[int] = field(default_factory=itertools.count)
    # Body store keyed by request_id. Evicts by completion order (FIFO on dict insertion),
    # capped at _BODY_STORE_MAX. Entries may outlive their network_requests deque counterparts
    # (deque maxlen=1000 vs store max=100) — bounded at ~25MB worst case, acceptable.
    _body_store: dict[int, str] = field(default_factory=dict)
    _body_semaphore: asyncio.Semaphore = field(default_factory=lambda: asyncio.Semaphore(_BODY_SEMAPHORE_LIMIT))
    _pending_tasks: set[asyncio.Task[None]] = field(default_factory=set)
    # Routes keyed by page id — Playwright registers routes per-page, so tracking must match.
    active_routes: dict[int, set[str]] = field(default_factory=dict)
    # -- Iframe frame context --
    _working_frame: Frame | None = None
    _observed_refs: dict[str, dict[str, Any]] = field(default_factory=dict)
    _observed_refs_generation: int = 0

    def get_response_body(self, request_id: int) -> str | None:
        """Public accessor for cached response bodies (keyed by request_id)."""
        return self._body_store.get(request_id)


_current_session: ContextVar[SessionState | None] = ContextVar("mcp_session", default=None)
_global_session: SessionState | None = None
_stateless_http_mode = False

# Process-wide registry for copilot browser sessions. Keyed by browser_session_id.
# This bypasses ContextVar propagation issues when FastMCP runs tool handlers
# in a separate task whose context snapshot predates scoped_session().
_copilot_sessions: dict[str, SessionState] = {}
# Ref snapshots and their invalidation generations, FIFO-capped like _body_store.
# Capacity eviction can drop a live session's refs early; that fails safe (unknown
# ref → the caller re-observes), which is why the tool contract doesn't mention it.
# Generation tokens come from one process-wide counter and are allocated on first
# touch, so an evicted entry re-materializes with a NEVER-seen token — a stale
# observe holding an old token can't collide with it (no ABA reset to a default).
_SESSION_REF_STORE_MAX = 64
_session_ref_maps: dict[tuple[str, str, str | None], dict[str, Any]] = {}
_session_ref_generations: dict[tuple[str, str, str | None], int] = {}
_session_ref_generation_counter = itertools.count(1)
# Identity tokens instead of id(): a GC'd page's id() can be reused by a new
# page, making a stale snapshot look valid. Tokens are never reissued.
_page_identity_tokens: weakref.WeakKeyDictionary[Page | Frame, int] = weakref.WeakKeyDictionary()


def _identity_token(obj: Page | Frame) -> int:
    try:
        token = _page_identity_tokens.get(obj)
    except TypeError:
        # Not weak-referenceable (e.g. SimpleNamespace); real page/frame objects
        # always are, so only they get the no-reuse guarantee.
        return id(obj)
    if token is None:
        token = next(_session_ref_generation_counter)
        _page_identity_tokens[obj] = token
    return token


def page_ref_key(page: SkyvernBrowserPage) -> tuple[int, int | None, str, str | None]:
    """Identity of the document context a ref snapshot was taken from."""
    return (
        _identity_token(page.page),
        _identity_token(page._working_frame) if page._working_frame is not None else None,
        page.url,
        page._working_frame.url if page._working_frame is not None else None,
    )


def _generation_for(key: tuple[str, str, str | None]) -> int:
    generation = _session_ref_generations.get(key)
    if generation is None:
        generation = next(_session_ref_generation_counter)
        _session_ref_generations[key] = generation
        while len(_session_ref_generations) > _SESSION_REF_STORE_MAX:
            _session_ref_generations.pop(next(iter(_session_ref_generations)))
    return generation


def _session_ref_key(
    state: SessionState,
    *,
    session_id: str | None = None,
    cdp_url: str | None = None,
) -> tuple[str, str, str | None] | None:
    context = state.context
    resolved_session_id = session_id or (context.session_id if context else None)
    resolved_cdp_url = cdp_url or (context.cdp_url if context else None)
    # Prefer the hash stored at resolve_browser time — recomputing runs a
    # deliberately slow PBKDF2 per call, and this sits on the per-ref hot path.
    api_key_hash = state.api_key_hash or _api_key_hash(get_active_api_key())

    if resolved_session_id:
        return ("cloud_session", resolved_session_id, api_key_hash)
    if resolved_cdp_url:
        return ("cdp", resolved_cdp_url, api_key_hash)
    return None


def session_ref_generation(
    *,
    session_id: str | None = None,
    cdp_url: str | None = None,
) -> int:
    state = get_current_session()
    if key := _session_ref_key(state, session_id=session_id, cdp_url=cdp_url):
        return _generation_for(key)
    if state._observed_refs_generation == 0:
        state._observed_refs_generation = next(_session_ref_generation_counter)
    return state._observed_refs_generation


def replace_session_ref_map(
    ref_map: dict[str, dict[str, Any]],
    *,
    session_id: str | None = None,
    cdp_url: str | None = None,
    generation: int | None = None,
    page_key: tuple[int, int | None, str, str | None] | None = None,
) -> bool:
    """Replace the session's ref snapshot (never merge). Returns False if discarded.

    When ``generation`` is given, the snapshot is discarded if the registry was
    cleared after that generation was captured — the observe raced a navigation
    or context switch and describes a replaced document.
    """
    state = get_current_session()
    snapshot: dict[str, Any] = {"page_key": page_key, "refs": dict(ref_map)}
    if key := _session_ref_key(state, session_id=session_id, cdp_url=cdp_url):
        if generation is not None and generation != _generation_for(key):
            return False
        _session_ref_maps[key] = snapshot
        while len(_session_ref_maps) > _SESSION_REF_STORE_MAX:
            _session_ref_maps.pop(next(iter(_session_ref_maps)))
    else:
        if generation is not None and generation != state._observed_refs_generation:
            return False
        state._observed_refs = snapshot
    return True


def get_session_ref(
    ref: str,
    *,
    session_id: str | None = None,
    cdp_url: str | None = None,
    page_key: tuple[int, int | None, str, str | None] | None = None,
) -> dict[str, Any] | None:
    """Resolve a ref, failing closed when the snapshot was bound to a different
    page/frame than the one the caller is on (popup steal, external tab close,
    detached-frame fallback — transitions with no explicit clear hook)."""
    state = get_current_session()
    if key := _session_ref_key(state, session_id=session_id, cdp_url=cdp_url):
        snapshot = _session_ref_maps.get(key)
        if snapshot is not None:
            # LRU touch: active sessions shouldn't be evicted by unrelated churn
            _session_ref_maps[key] = _session_ref_maps.pop(key)
    else:
        snapshot = state._observed_refs or None
    if not snapshot:
        return None
    if snapshot.get("page_key") != page_key:
        return None
    element: dict[str, Any] | None = snapshot["refs"].get(ref)
    return element


def clear_session_ref_map(
    *,
    session_id: str | None = None,
    cdp_url: str | None = None,
) -> None:
    state = get_current_session()
    if key := _session_ref_key(state, session_id=session_id, cdp_url=cdp_url):
        _session_ref_maps.pop(key, None)
        _session_ref_generations[key] = next(_session_ref_generation_counter)
        while len(_session_ref_generations) > _SESSION_REF_STORE_MAX:
            _session_ref_generations.pop(next(iter(_session_ref_generations)))
    else:
        state._observed_refs = {}
        state._observed_refs_generation = next(_session_ref_generation_counter)


def register_copilot_session(session_id: str, state: SessionState) -> None:
    """Register a pre-configured browser session for cross-task lookup.

    The registry is process-local and in-memory: entries do not survive a
    process restart and are not shared across uvicorn workers. Callers that
    need cross-process continuity must reconnect via the cloud session API.
    """
    _copilot_sessions[session_id] = state


def unregister_copilot_session(session_id: str) -> None:
    """Remove a copilot browser session from the process-local registry."""
    _copilot_sessions.pop(session_id, None)


def active_copilot_session_ids() -> set[str]:
    """Browser-session IDs currently bound to an active copilot turn."""
    return set(_copilot_sessions)


def _explicit_cloud_session_can_access_localhost() -> bool:
    return settings.ENV == "local"


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


@asynccontextmanager
async def scoped_session(state: SessionState) -> AsyncIterator[None]:
    """Temporarily push a SessionState into ContextVar scope.

    Restores the previous value on exit. Does NOT touch _global_session,
    so it is safe for concurrent API-server requests.
    """
    token = _current_session.set(state)
    try:
        yield
    finally:
        _current_session.reset(token)


def set_stateless_http_mode(enabled: bool) -> None:
    global _stateless_http_mode
    _stateless_http_mode = enabled


def is_stateless_http_mode() -> bool:
    return _stateless_http_mode


def _api_key_hash(api_key: str | None) -> str | None:
    if not api_key:
        return None
    return hash_api_key_for_cache(api_key)


def active_api_key_hash() -> str | None:
    return _api_key_hash(get_active_api_key())


def current_api_key_hash() -> str | None:
    # Browser recorders call this only after resolve_browser has rebound the session to the active request key.
    state = get_current_session()
    return state.api_key_hash or _api_key_hash(get_active_api_key())


def _hashes_equal(a: str | None, b: str | None) -> bool:
    """Constant-time comparison of two API-key hashes (either may be None).

    Using ``==`` on secret-derived values leaks content byte-by-byte through
    response-time side channels. ``secrets.compare_digest`` avoids that; we
    wrap it so ``None`` on either side is handled without branching on
    contents.
    """
    if a is None or b is None:
        return a is b
    return secrets.compare_digest(a, b)


def _matches_current(
    current: SessionState,
    *,
    session_id: str | None = None,
    cdp_url: str | None = None,
    local: bool = False,
) -> bool:
    if current.browser is None or current.context is None:
        return False
    if not _hashes_equal(current.api_key_hash, _api_key_hash(get_active_api_key())):
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

    # Check copilot session registry (cross-task fallback when ContextVar
    # does not propagate through FastMCP in-process transport).
    registered = _copilot_sessions.get(session_id) if session_id else None
    if registered is not None and registered.browser is not None and registered.context is not None:
        if _hashes_equal(registered.api_key_hash, active_api_key_hash) or not has_api_key_override():
            # FastMCP in-process tool tasks may miss the parent ContextVar.
            # Explicit request overrides still win; otherwise use the temporary Copilot registry.
            _current_session.set(registered)
            return registered.browser, registered.context

    browser: SkyvernBrowser | None = None
    try:
        if session_id:
            browser = await skyvern.connect_to_cloud_browser_session(session_id)
            ctx = BrowserContext(
                mode="cloud_session",
                session_id=session_id,
                can_access_localhost=_explicit_cloud_session_can_access_localhost(),
            )
            set_current_session(SessionState(browser=browser, context=ctx, api_key_hash=active_api_key_hash))
            return browser, ctx

        if cdp_url:
            browser = await skyvern.connect_to_browser_over_cdp(cdp_url)
            ctx = BrowserContext(mode="cdp", cdp_url=cdp_url)
            set_current_session(SessionState(browser=browser, context=ctx, api_key_hash=active_api_key_hash))
            return browser, ctx

        if local:
            browser = await skyvern.launch_local_browser(headless=headless)
            ctx = BrowserContext(mode="local", can_access_localhost=True)
            set_current_session(SessionState(browser=browser, context=ctx, api_key_hash=active_api_key_hash))
            return browser, ctx

        if create_session:
            browser = await skyvern.launch_cloud_browser(timeout=timeout)
            ctx = BrowserContext(
                mode="cloud_session",
                session_id=browser.browser_session_id,
                can_access_localhost=False,
            )
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
        # Cancel pending body-capture tasks before closing the browser to avoid
        # "target closed" noise from CDP calls against a defunct context.
        for task in current._pending_tasks:
            task.cancel()
        current._pending_tasks.clear()
        current.active_routes.clear()
        if current.browser is not None:
            await current.browser.close()
    finally:
        clear_session_ref_map()
        if current.context and current.context.session_id:
            delete_session_trajectories(current.context.session_id)
            unregister_copilot_session(current.context.session_id)
        set_current_session(SessionState())


async def get_page(
    session_id: str | None = None,
    cdp_url: str | None = None,
) -> tuple[SkyvernBrowserPage, BrowserContext]:
    """Get the working page from the current or specified browser session.

    If an active page was set via tab_switch, returns that page.
    Otherwise falls back to the most recent page (browser.get_working_page()).
    """
    browser, ctx = await resolve_browser(session_id=session_id, cdp_url=cdp_url)
    state = get_current_session()

    # Use explicitly set active page if still valid
    if state._active_page is not None and not state._active_page.is_closed():
        try:
            context_pages = browser._browser_context.pages
            if state._active_page in context_pages:
                page = await browser.get_page_for(state._active_page)
            else:
                state._active_page = None
                page = await browser.get_working_page()
        except Exception:
            state._active_page = None
            page = await browser.get_working_page()
    else:
        if state._active_page is not None:
            state._active_page = None
        page = await browser.get_working_page()

    # Register inspection hooks on all pages in the context.
    # Import here to avoid circular imports.
    from skyvern.cli.mcp_tools.inspection import ensure_hooks_on_all_pages

    ensure_hooks_on_all_pages(state, browser._browser_context.pages)

    # Install page event listener for tab_wait_for_new (once per session)
    _install_page_event_listener(state, browser)

    # Propagate iframe frame context from session state to the page
    if state._working_frame is not None:
        try:
            detached = state._working_frame.is_detached()
        except AttributeError:
            detached = False  # frame object doesn't support is_detached (e.g., test mocks)
        if detached:
            LOG.debug("Propagating detached _working_frame for explicit action failure")
        page._working_frame = state._working_frame

    return page, ctx


def _install_page_event_listener(state: SessionState, browser: SkyvernBrowser) -> None:
    """Register a browser_context.on('page') listener to buffer new page events."""
    if state._page_event_listener_installed:
        return

    def _on_new_page(page: Page) -> None:
        event = {
            "tab_id": str(id(page)),
            "url": page.url,
            "timestamp": time.time(),
            "page": page,
        }
        state._page_events.append(event)
        state._page_event_signal.set()

        # Eagerly clean up when the page closes
        def _on_close() -> None:
            try:
                state._page_events = deque(
                    (e for e in state._page_events if e is not event),
                    maxlen=state._page_events.maxlen,
                )
                # Remove hook tracking so a new page with a recycled id() gets hooked
                page_id = id(page)
                state._hooked_page_ids.discard(page_id)
                state._hooked_handlers_map.pop(page_id, None)
            except Exception:
                LOG.debug("Failed to clean up closed page state", exc_info=True)

        page.on("close", _on_close)

        # Register inspection hooks eagerly so early popup events are captured
        try:
            from skyvern.cli.mcp_tools.inspection import _register_hooks_on_page

            _register_hooks_on_page(state, page)
        except Exception:
            LOG.debug("Failed to register inspection hooks on new page", exc_info=True)

    try:
        browser._browser_context.on("page", _on_new_page)
        state._page_event_listener_installed = True
    except Exception:
        LOG.debug("Failed to install page event listener", exc_info=True)


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
        clear_session_ref_map()
        set_current_session(SessionState())


class BrowserNotAvailableError(Exception):
    """Raised when no browser session is available."""


def no_browser_error() -> dict[str, Any]:
    return make_error(
        ErrorCode.NO_ACTIVE_BROWSER,
        "No browser session available",
        "Create a session with skyvern_browser_session_create, provide session_id, or cdp_url",
    )
