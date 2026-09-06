from __future__ import annotations

import asyncio
import itertools
import os
import secrets
import time
import weakref
from collections import deque
from contextlib import asynccontextmanager, contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, AsyncIterator, Callable, Iterator

import httpx
import structlog

from skyvern.config import settings
from skyvern.exceptions import get_user_facing_exception_message
from skyvern.forge.sdk.copilot.turn_origin import is_self_heal_session_id
from skyvern.forge.sdk.forge_log import current_codeblock_log_redactor
from skyvern.webeye.browser_errors import is_context_lost_message, is_target_closed_message

from .api_key_hash import hash_api_key_for_cache
from .client import get_active_api_key, get_skyvern, has_api_key_override
from .result import BrowserContext, ErrorCode, count_browser_attach, make_error
from .trajectory_store import delete_session_trajectories

LOG = structlog.get_logger(__name__)

_BODY_SEMAPHORE_LIMIT = 5  # concurrent CDP body downloads (worst case: 5 * 10s timeout = 50s backlog)

if TYPE_CHECKING:
    from playwright.async_api import Frame, Page

    from skyvern.browser_extension.runtime import BrowserExtensionRuntime
    from skyvern.library.skyvern_browser import SkyvernBrowser
    from skyvern.library.skyvern_browser_page import SkyvernBrowserPage
    from skyvern.webeye.browser_engine import EngineFrame


@dataclass
class ObserveV2State:
    page_key: tuple[int, int | None, str, str | None] | None = None
    document_id: str | None = None
    params: dict[str, Any] = field(default_factory=dict)
    refs: dict[str, dict[str, Any]] = field(default_factory=dict)
    next_ref: int = 0
    host_budgets: dict[str, int] = field(default_factory=dict)

    def reset_refs(self) -> None:
        """Clear per-document refs while preserving monotonic ref IDs and host budgets."""
        self.page_key = None
        self.document_id = None
        self.params = {}
        self.refs = {}


@dataclass
class SessionState:
    browser: SkyvernBrowser | None = None
    context: BrowserContext | None = None
    api_key_hash: str | None = None
    organization_id: str | None = None
    console_messages: deque[dict[str, Any]] = field(default_factory=lambda: deque(maxlen=1000))
    network_requests: deque[dict[str, Any]] = field(default_factory=lambda: deque(maxlen=1000))
    dialog_events: deque[dict[str, Any]] = field(default_factory=lambda: deque(maxlen=1000))
    page_errors: deque[dict[str, Any]] = field(default_factory=lambda: deque(maxlen=1000))
    tracing_active: bool = False
    har_enabled: bool = False
    _har_entries: deque[dict[str, Any]] = field(default_factory=lambda: deque(maxlen=5000))
    # -- Active page tracking (tab management) --
    # True only when a caller retains this state beyond one tool call (stdio/global session,
    # organization registry, copilot registry); tab switch/close/wait_for_new refuse otherwise.
    tab_state_persists: bool = False
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
    _working_frame: EngineFrame | None = None
    _observed_refs: dict[str, dict[str, Any]] = field(default_factory=dict)
    _observed_refs_generation: int = 0
    # Local fallback when no cloud/CDP/extension browser-session identity is available.
    _observe_v2_state: ObserveV2State = field(default_factory=ObserveV2State)
    _codeblock_redactor: Callable[[Any], Any] | None = field(default=None, repr=False)

    def get_response_body(self, request_id: int) -> str | None:
        """Public accessor for cached response bodies (keyed by request_id)."""
        return self._body_store.get(request_id)


_current_session: ContextVar[SessionState | None] = ContextVar("mcp_session", default=None)
_current_organization_id: ContextVar[str | None] = ContextVar("mcp_session_organization_id", default=None)
_global_session: SessionState | None = None
_organization_sessions: dict[str, SessionState] = {}
_stateless_http_mode = False
_stdio_local_file_access_enabled = False

# Process-wide registry for copilot browser sessions. Keys always carry the
# owning organization so authenticated HTTP requests cannot address another
# tenant's entry even when the browser session ID is known.
# This bypasses ContextVar propagation issues when FastMCP runs tool handlers
# in a separate task whose context snapshot predates scoped_session().
_copilot_sessions: dict[tuple[str, str], list[SessionState]] = {}
# Ref snapshots and their invalidation generations, FIFO-capped like _body_store.
# Capacity eviction can drop a live session's refs early; that fails safe (unknown
# ref → the caller re-observes), which is why the tool contract doesn't mention it.
# Generation tokens come from one process-wide counter and are allocated on first
# touch, so an evicted entry re-materializes with a NEVER-seen token — a stale
# observe holding an old token can't collide with it (no ABA reset to a default).
_SESSION_REF_STORE_MAX = 64
_session_ref_maps: dict[tuple[str | None, str, str, str | None], dict[str, Any]] = {}
_session_ref_generations: dict[tuple[str | None, str, str, str | None], int] = {}
_observe_v2_states: dict[tuple[str | None, str, str, str | None], ObserveV2State] = {}
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


def _generation_for(key: tuple[str | None, str, str, str | None]) -> int:
    generation = _session_ref_generations.get(key)
    if generation is None:
        generation = next(_session_ref_generation_counter)
        _session_ref_generations[key] = generation
        while len(_session_ref_generations) > _SESSION_REF_STORE_MAX:
            _session_ref_generations.pop(next(iter(_session_ref_generations)))
    return generation


def _advance_generation_for(key: tuple[str | None, str, str, str | None]) -> int:
    generation = next(_session_ref_generation_counter)
    # Pop-then-reinsert: an advance is activity, so it must LRU-touch the key -
    # FIFO eviction would drop the longest-lived (often busiest) session and a
    # freshly minted generation would break its in-flight reservation.
    _session_ref_generations.pop(key, None)
    _session_ref_generations[key] = generation
    while len(_session_ref_generations) > _SESSION_REF_STORE_MAX:
        _session_ref_generations.pop(next(iter(_session_ref_generations)))
    return generation


def _session_ref_key(
    state: SessionState,
    *,
    session_id: str | None = None,
    cdp_url: str | None = None,
) -> tuple[str | None, str, str, str | None] | None:
    context = state.context
    resolved_session_id = session_id or (context.session_id if context else None)
    resolved_cdp_url = cdp_url or (context.cdp_url if context else None)
    # Prefer the hash stored at resolve_browser time — recomputing runs a
    # deliberately slow PBKDF2 per call, and this sits on the per-ref hot path.
    api_key_hash = state.api_key_hash or _api_key_hash(get_active_api_key())
    organization_id = _current_organization_id.get() or state.organization_id

    if resolved_session_id:
        return (organization_id, "cloud_session", resolved_session_id, api_key_hash)
    if context is not None and context.mode == "extension":
        return (organization_id, "extension", "own-browser", api_key_hash)
    if resolved_cdp_url:
        return (organization_id, "cdp", resolved_cdp_url, api_key_hash)
    return None


def get_observe_v2_state(
    *,
    session_id: str | None = None,
    cdp_url: str | None = None,
) -> ObserveV2State:
    """Return observe-v2 state keyed exactly like the legacy per-browser ref map."""
    state = get_current_session()
    if key := _session_ref_key(state, session_id=session_id, cdp_url=cdp_url):
        observe_state = _observe_v2_states.pop(key, None)
        if observe_state is None:
            observe_state = ObserveV2State()
        _observe_v2_states[key] = observe_state
        while len(_observe_v2_states) > _SESSION_REF_STORE_MAX:
            _observe_v2_states.pop(next(iter(_observe_v2_states)))
        return observe_state
    return state._observe_v2_state


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


def begin_session_ref_publication(
    *,
    session_id: str | None = None,
    cdp_url: str | None = None,
) -> int:
    """Reserve a unique generation for one in-flight ref publication."""
    state = get_current_session()
    if key := _session_ref_key(state, session_id=session_id, cdp_url=cdp_url):
        return _advance_generation_for(key)
    state._observed_refs_generation = next(_session_ref_generation_counter)
    return state._observed_refs_generation


def invalidate_session_ref_map(
    *,
    session_id: str | None = None,
    cdp_url: str | None = None,
) -> int:
    """Invalidate published refs while retaining v2 budgets and monotonic ref IDs."""
    state = get_current_session()
    if key := _session_ref_key(state, session_id=session_id, cdp_url=cdp_url):
        _session_ref_maps.pop(key, None)
        observe_state = _observe_v2_states.get(key)
        if observe_state is not None:
            observe_state.reset_refs()
        return _advance_generation_for(key)
    state._observed_refs = {}
    state._observe_v2_state.reset_refs()
    state._observed_refs_generation = next(_session_ref_generation_counter)
    return state._observed_refs_generation


def replace_session_ref_map(
    ref_map: dict[str, dict[str, Any]],
    *,
    session_id: str | None = None,
    cdp_url: str | None = None,
    generation: int | None = None,
    page_key: tuple[int, int | None, str, str | None] | None = None,
    advance_on_commit: bool = True,
) -> bool:
    """Replace the session's ref snapshot (never merge). Returns False if discarded.

    A supplied generation is a publication reservation. The snapshot is discarded
    if a newer operation has reserved or invalidated the registry; an accepted
    commit advances the generation so work captured before the commit cannot
    mutate the published snapshot afterward.

    Legacy (pre-v2) publications pass ``advance_on_commit=False``: their generation
    is a plain read, so the CAS refuses snapshots that raced an invalidation
    (navigation/clear) while overlapping publications keep last-completer-wins -
    exactly the pre-reservation contract.
    """
    state = get_current_session()
    snapshot: dict[str, Any] = {"page_key": page_key, "refs": dict(ref_map)}
    if key := _session_ref_key(state, session_id=session_id, cdp_url=cdp_url):
        if generation is not None and generation != _generation_for(key):
            return False
        _session_ref_maps[key] = snapshot
        while len(_session_ref_maps) > _SESSION_REF_STORE_MAX:
            _session_ref_maps.pop(next(iter(_session_ref_maps)))
        if advance_on_commit:
            _advance_generation_for(key)
    else:
        if generation is not None and generation != state._observed_refs_generation:
            return False
        state._observed_refs = snapshot
        if advance_on_commit:
            state._observed_refs_generation = next(_session_ref_generation_counter)
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
    generation: int | None = None,
) -> bool:
    """Clear refs, unless a newer publication reservation superseded this caller."""
    state = get_current_session()
    if key := _session_ref_key(state, session_id=session_id, cdp_url=cdp_url):
        if generation is not None and generation != _generation_for(key):
            return False
        _session_ref_maps.pop(key, None)
        observe_state = _observe_v2_states.pop(key, None)
        if observe_state is None:
            observe_state = ObserveV2State()
        else:
            observe_state.reset_refs()
        _observe_v2_states[key] = observe_state
        while len(_observe_v2_states) > _SESSION_REF_STORE_MAX:
            _observe_v2_states.pop(next(iter(_observe_v2_states)))
        _advance_generation_for(key)
    else:
        if generation is not None and generation != session_ref_generation(
            session_id=session_id,
            cdp_url=cdp_url,
        ):
            return False
        state._observed_refs = {}
        state._observe_v2_state.reset_refs()
        state._observed_refs_generation = next(_session_ref_generation_counter)
    return True


def register_copilot_session(session_id: str, state: SessionState, *, organization_id: str) -> None:
    """Register a pre-configured browser session for cross-task lookup.

    The registry is process-local and in-memory: entries do not survive a
    process restart and are not shared across uvicorn workers. Callers that
    need cross-process continuity must reconnect via the cloud session API.
    """
    if not organization_id:
        raise ValueError("organization_id is required for a copilot browser session")
    if state.organization_id not in {None, organization_id}:
        raise ValueError("session state belongs to a different organization")
    if is_self_heal_session_id(session_id):
        os.environ.pop("DEBUGP", None)
        redactor = current_codeblock_log_redactor()
        if redactor is not None:
            state._codeblock_redactor = redactor
    state.organization_id = organization_id
    state.tab_state_persists = True
    registrations = _copilot_sessions.setdefault((organization_id, session_id), [])
    if not any(registered is state for registered in registrations):
        registrations.append(state)


def unregister_copilot_session(
    session_id: str,
    *,
    organization_id: str,
    expected_state: SessionState | None = None,
) -> None:
    """Remove one exact registration, or every registration for explicit cleanup callers."""
    key = (organization_id, session_id)
    if expected_state is None:
        _copilot_sessions.pop(key, None)
        return
    registrations = _copilot_sessions.get(key)
    if registrations is None:
        return
    registrations[:] = [registered for registered in registrations if registered is not expected_state]
    if not registrations:
        _copilot_sessions.pop(key, None)


def active_copilot_session_ids() -> set[str]:
    """Browser-session IDs currently bound to an active copilot turn."""
    return {session_id for _, session_id in _copilot_sessions}


def _registered_copilot_session(session_id: str, *, organization_id: str) -> SessionState | None:
    registrations = _copilot_sessions.get((organization_id, session_id))
    return registrations[-1] if registrations else None


def _explicit_cloud_session_can_access_localhost() -> bool:
    return settings.ENV == "local"


def get_current_session() -> SessionState:
    global _global_session

    organization_id = _current_organization_id.get()
    state = _current_session.get()

    if organization_id is not None and not _stateless_http_mode:
        state = _organization_sessions.get(organization_id)
        if state is None:
            state = SessionState(organization_id=organization_id)
            _organization_sessions[organization_id] = state
        state.tab_state_persists = True
        _current_session.set(state)
        return state

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
    state.tab_state_persists = True
    _current_session.set(state)
    return state


def set_current_session(state: SessionState) -> None:
    global _global_session

    organization_id = _current_organization_id.get()
    if organization_id is not None and not _stateless_http_mode:
        state.organization_id = organization_id
        _organization_sessions[organization_id] = state
        state.tab_state_persists = True
    elif not _stateless_http_mode:
        _global_session = state
        state.tab_state_persists = True
    _current_session.set(state)


@contextmanager
def request_session_scope(organization_id: str) -> Iterator[None]:
    if not organization_id:
        raise ValueError("organization_id is required for an authenticated MCP request")

    organization_token = _current_organization_id.set(organization_id)
    session_token = _current_session.set(None)
    try:
        yield
    finally:
        _current_session.reset(session_token)
        _current_organization_id.reset(organization_token)


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


def set_stdio_local_file_access_enabled(enabled: bool) -> None:
    global _stdio_local_file_access_enabled
    _stdio_local_file_access_enabled = enabled


def is_stdio_local_file_access_enabled() -> bool:
    return _stdio_local_file_access_enabled


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
    extension_runtime: BrowserExtensionRuntime | None = None,
    local: bool = False,
) -> bool:
    if current.browser is None or current.context is None:
        return False
    if not _hashes_equal(current.api_key_hash, _api_key_hash(get_active_api_key())):
        return False

    if session_id:
        return current.context.mode == "cloud_session" and current.context.session_id == session_id
    if extension_runtime is not None:
        return current.context.mode == "extension"
    if cdp_url:
        return current.context.mode == "cdp" and current.context.cdp_url == cdp_url
    if local:
        return current.context.mode == "local"
    return False


def _extension_browser_is_connected(browser: SkyvernBrowser) -> bool:
    try:
        playwright_browser = browser.browser
        return playwright_browser is not None and playwright_browser.is_connected()
    except Exception:
        return False


@count_browser_attach
async def resolve_browser(
    session_id: str | None = None,
    cdp_url: str | None = None,
    local: bool = False,
    create_session: bool = False,
    timeout: int | None = None,
    headless: bool = False,
    *,
    extension_runtime: BrowserExtensionRuntime | None = None,
) -> tuple[SkyvernBrowser, BrowserContext]:
    """Resolve browser from parameters or current session.

    Note: For MCP tools, sessions are stored in ContextVar and persist across tool calls.
    Cleanup is done via explicit skyvern_browser_session_close() call. For scripts that need
    guaranteed cleanup, use the browser_session() context manager instead.
    """
    # _session_ref_key must prefer session_id so raw cdp_url callers share refs with this normalized context.
    if cdp_url and cdp_url.startswith("pbs_") and not session_id:
        session_id, cdp_url = cdp_url, None

    skyvern = get_skyvern()
    current = get_current_session()

    if _stateless_http_mode and not (session_id or cdp_url or extension_runtime or local or create_session):
        raise BrowserNotAvailableError()

    if _matches_current(
        current,
        session_id=session_id,
        cdp_url=cdp_url,
        extension_runtime=extension_runtime,
        local=local,
    ):
        if current.browser is None or current.context is None:
            raise RuntimeError("Expected active browser and context for matching session")
        if extension_runtime is None or _extension_browser_is_connected(current.browser):
            return current.browser, current.context
        try:
            await _close_session_state(current, close_via_active_client=False)
        except Exception:
            pass
        finally:
            set_current_session(SessionState())
        current = get_current_session()

    # Cloud sessions created by the MCP session tool intentionally do not open a
    # second CDP connection. Connect lazily when a browser tool is used; this
    # leaves the initial page available to the backend persistent-session manager
    # for code-only workflow runs.
    if (
        current.browser is None
        and current.context is not None
        and current.context.mode == "cloud_session"
        and current.context.session_id
        and session_id is None
        and cdp_url is None
        and not local
        and _hashes_equal(current.api_key_hash, _api_key_hash(get_active_api_key()))
    ):
        connected_browser = await skyvern.connect_to_cloud_browser_session(current.context.session_id)
        current.browser = connected_browser
        return connected_browser, current.context

    active_api_key_hash = _api_key_hash(get_active_api_key())

    # Check copilot session registry (cross-task fallback when ContextVar
    # does not propagate through FastMCP in-process transport).
    organization_id = _current_organization_id.get()
    registered = (
        _registered_copilot_session(session_id, organization_id=organization_id)
        if session_id and organization_id
        else None
    )
    if registered is not None and registered.browser is not None and registered.context is not None:
        if _hashes_equal(registered.api_key_hash, active_api_key_hash) or not has_api_key_override():
            # FastMCP in-process tool tasks may miss the parent ContextVar.
            # Explicit request overrides still win; otherwise use the temporary Copilot registry.
            set_current_session(registered)
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

        if extension_runtime is not None:
            browser = await skyvern.connect_to_browser_extension(extension_runtime)
            ctx = BrowserContext(mode="extension", can_access_localhost=True)
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
    except Exception as exc:
        if browser is not None:
            try:
                await browser.close()
            except Exception:
                pass
        set_current_session(SessionState())
        wrapped = _wrap_browser_connection_failure(exc)
        if wrapped is None:
            # Not a recognized connect/CDP failure -- e.g. an explicit PermissionError from a
            # cross-organization ownership check. Callers scope-check on the real exception
            # class, so reclassifying it here would turn a 403-shaped denial into a generic
            # "retry" story.
            raise
        raise wrapped from exc

    if current.browser is not None and current.context is not None:
        if current.context.mode != "extension" or _extension_browser_is_connected(current.browser):
            return current.browser, current.context
        # The extension-mode CDP link died (extension reload, broker restart, session
        # churn). Heal through the live runtime instead of handing back a dead browser
        # or dead-ending targetless tools on NO_ACTIVE_BROWSER.
        healed = await _reconnect_extension_session(current, active_api_key_hash)
        if healed is not None:
            return healed

    raise BrowserNotAvailableError()


async def _reconnect_extension_session(
    current: SessionState,
    active_api_key_hash: str | None,
) -> tuple[SkyvernBrowser, BrowserContext] | None:
    from skyvern.browser_extension.runtime import BrowserExtensionRuntime

    runtime = BrowserExtensionRuntime.instance()
    if runtime is None:
        return None
    try:
        await _close_session_state(current, close_via_active_client=False)
    except Exception:
        pass
    finally:
        set_current_session(SessionState())
    skyvern = get_skyvern()
    try:
        browser = await skyvern.connect_to_browser_extension(runtime)
    except Exception:
        LOG.warning("browser_extension_session_reconnect_failed", exc_info=True)
        return None
    ctx = BrowserContext(mode="extension", can_access_localhost=True)
    set_current_session(SessionState(browser=browser, context=ctx, api_key_hash=active_api_key_hash))
    return browser, ctx


async def _close_session_state(current: SessionState, *, close_via_active_client: bool) -> None:
    from .session_ops import do_session_close

    try:
        if (
            close_via_active_client
            and current.context
            and current.context.mode == "cloud_session"
            and current.context.session_id
        ):
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
            organization_id = _current_organization_id.get() or current.organization_id
            if organization_id is not None:
                unregister_copilot_session(
                    current.context.session_id,
                    organization_id=organization_id,
                    expected_state=current,
                )


async def close_current_session() -> None:
    """Close the active browser session (if any) and clear local session state."""
    current = get_current_session()
    try:
        await _close_session_state(current, close_via_active_client=True)
    finally:
        set_current_session(SessionState())


async def close_all_sessions() -> None:
    errors: list[tuple[str | None, BaseException]] = []
    for organization_id in list(_organization_sessions):
        try:
            with request_session_scope(organization_id):
                current = get_current_session()
                try:
                    # Preserve the session ID so browser.close() uses the browser's owning client for remote cleanup.
                    await _close_session_state(current, close_via_active_client=False)
                finally:
                    set_current_session(SessionState())
        except BaseException as exc:
            errors.append((organization_id, exc))

    _organization_sessions.clear()
    try:
        await close_current_session()
    except BaseException as exc:
        errors.append((None, exc))
    finally:
        _current_session.set(None)
        _current_organization_id.set(None)

    if errors:
        for failed_organization_id, cleanup_error in errors[1:]:
            LOG.warning(
                "Additional session cleanup failed",
                organization_id=failed_organization_id,
                exc_info=(type(cleanup_error), cleanup_error, cleanup_error.__traceback__),
            )
        raise errors[0][1]


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

    try:
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
    except Exception as exc:
        # The connect above can succeed and the target still die before this command
        # reaches it -- same failure family as resolve_browser's connect attempt.
        wrapped = _wrap_browser_connection_failure(exc)
        if wrapped is None:
            raise
        raise wrapped from exc

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
        from skyvern.cli.mcp_tools.inspection import _redact_inspection_value

        try:
            event_data = _redact_inspection_value(
                state,
                {
                    "tab_id": str(id(page)),
                    "url": page.url,
                    "timestamp": time.time(),
                },
            )
        except BaseException:
            return
        if type(event_data) is not dict:
            return
        event = {**event_data, "page": page}
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
            except BaseException:
                pass

        try:
            page.on("close", _on_close)
        except BaseException:
            pass

        # Register inspection hooks eagerly so early popup events are captured
        try:
            from skyvern.cli.mcp_tools.inspection import _register_hooks_on_page

            _register_hooks_on_page(state, page)
        except BaseException:
            pass

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


class BrowserSessionConnectionError(BrowserNotAvailableError):
    """A browser/session was resolvable but connecting to it (or its page) failed.

    Subclasses BrowserNotAvailableError so every existing MCP tool's
    ``except BrowserNotAvailableError`` clause already handles it without modification.
    ``raw_detail`` keeps the original driver/CDP text -- which can carry an internal
    hostname or a raw proxy response body -- available to server-side classifiers
    without exposing it in ``str(self)``.

    ``session_gone`` is true only once we are sure the session itself is gone (not
    just this particular connect attempt), via two independent signals OR'd together:
    an engine-neutral "target already closed" message taxonomy, and the session
    router's own expiry close code paired with its reason text (the code alone is
    reused by an unrelated subsystem for a different meaning, and the taxonomy alone
    is not guaranteed to match every message shape that close can take).
    """

    def __init__(self, raw_detail: str) -> None:
        self.raw_detail = raw_detail
        lowered = raw_detail.lower()
        self.session_gone = is_target_closed_message(raw_detail) or (
            "code=4408" in lowered and "reason=session expired" in lowered
        )
        super().__init__("Browser session connection failed")


_BROWSER_CONNECT_MESSAGE_MARKERS = (
    "connect_over_cdp",
    "websocket error",
    "websocket was closed",
    "ws connecting",
    "ws unexpected response",
    "ws error",
)


def _looks_like_browser_connection_failure(exc: Exception) -> bool:
    """Recognize a failure that belongs to the connect/resolve-a-page boundary.

    Deliberately narrow: resolve_browser()'s try block and get_page()'s page-resolution
    step can also surface an exception that exists precisely so a CALLER can key off its
    real type -- e.g. a cross-organization PermissionError. Only convert what is
    unambiguously a browser/CDP/session-router failure; anything else re-raises unchanged.
    """
    if isinstance(exc, httpx.TransportError):
        # A network-level failure talking to our own API while fetching session
        # metadata; str(exc) is often empty for this class (e.g. httpx.ReadError).
        return True
    message = str(exc).lower()
    if not message.strip():
        # A typed authorization denial always carries a message; a message-less
        # exception at this exact boundary has nothing else to go on and nothing to
        # leak either way, so treat it as a connection failure rather than let an
        # uninformative, uncaught ToolError reach the client.
        return True
    return (
        is_target_closed_message(message)
        or is_context_lost_message(message)
        or any(marker in message for marker in _BROWSER_CONNECT_MESSAGE_MARKERS)
    )


def _wrap_browser_connection_failure(exc: Exception) -> BrowserSessionConnectionError | None:
    if not _looks_like_browser_connection_failure(exc):
        return None
    LOG.warning("Browser session connection failed", exc_info=True)
    # str(exc) can be empty (e.g. httpx.ReadError, asyncio TimeoutError) -- fall back to the
    # original exception's type name so raw_detail is never blank.
    return BrowserSessionConnectionError(str(exc) or type(exc).__name__)


def no_browser_error(exc: BrowserNotAvailableError | None = None) -> dict[str, Any]:
    if isinstance(exc, BrowserSessionConnectionError):
        if exc.session_gone:
            return make_error(
                ErrorCode.SESSION_EXPIRED,
                "Browser session expired or closed.",
                "Create a new browser session and retry this operation.",
            )
        return make_error(
            ErrorCode.SDK_ERROR,
            # get_user_facing_exception_message already strips CDP/session-router hostnames,
            # raw proxy response bodies, and driver call logs for anything connect_over_cdp-
            # shaped; anything else degrades to a generic "Unexpected error: <text>", also safe.
            get_user_facing_exception_message(Exception(exc.raw_detail)),
            "This is often transient. Retry with the same session_id/cdp_url; "
            "if it keeps failing, create a new browser session.",
        )
    return make_error(
        ErrorCode.NO_ACTIVE_BROWSER,
        "No browser session available",
        "Create a session with skyvern_browser_session_create, provide session_id, or cdp_url",
    )
