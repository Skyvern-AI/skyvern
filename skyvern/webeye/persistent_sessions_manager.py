"""Protocol definition for PersistentSessionsManager implementations."""

from __future__ import annotations

from typing import Protocol

from skyvern.forge.sdk.schemas.persistent_browser_sessions import (
    Extensions,
    PersistentBrowserSession,
    PersistentBrowserType,
)
from skyvern.schemas.runs import ProxyLocation, ProxyLocationInput
from skyvern.webeye.browser_state import BrowserState


class PersistentSessionsManager(Protocol):
    """Protocol defining the interface for persistent browser session management."""

    def watch_session_pool(self) -> None:
        """Initialize monitoring of the session pool."""
        ...

    def start_reaper(self) -> None:
        """Start the periodic reaper that closes idle/expired sessions."""
        ...

    def can_probe_registered_browser_state(self) -> bool: ...

    def supports_evict_and_reconnect(self) -> bool:
        """Whether ``evict_cached_browser_state`` followed by ``get_browser_state`` can
        yield a fresh, reconnected ``BrowserState`` for the same ``session_id``.

        True for managers (e.g. the cloud V2 manager) whose ``get_browser_state`` performs
        a network-level reconnect when the cache is empty. False for managers (e.g. the
        OSS default impl) whose ``get_browser_state`` is a pure in-memory lookup — for
        those, evicting removes the only ``BrowserState`` and the next call returns None,
        so callers must avoid the evict-and-reconnect pattern entirely or risk leaving
        the cache empty for the rest of the session's lifetime.
        """
        ...

    async def begin_session(
        self,
        *,
        browser_session_id: str,
        runnable_type: str,
        runnable_id: str,
        organization_id: str,
    ) -> None:
        """Begin a browser session for a specific runnable."""
        ...

    async def get_browser_address(self, session_id: str, organization_id: str) -> str:
        """Get the browser address for a session."""
        ...

    async def get_browser_address_if_ready(
        self,
        session_id: str,
        organization_id: str,
        *,
        timeout: float = 0.0,
        poll_interval: float = 0.25,
    ) -> str | None:
        """Get the browser address for a session if it is already available."""
        ...

    async def get_session_by_runnable_id(
        self, runnable_id: str, organization_id: str
    ) -> PersistentBrowserSession | None:
        """Get a browser session by runnable ID."""
        ...

    async def get_active_sessions(self, organization_id: str) -> list[PersistentBrowserSession]:
        """Get all active sessions for an organization."""
        ...

    async def get_browser_state(self, session_id: str, organization_id: str | None = None) -> BrowserState | None:
        """Get the browser state for a session."""
        ...

    async def set_browser_state(self, session_id: str, browser_state: BrowserState) -> None:
        """Set the browser state for a session."""
        ...

    async def get_session(self, session_id: str, organization_id: str) -> PersistentBrowserSession | None:
        """Get a browser session by session ID."""
        ...

    async def create_session(
        self,
        organization_id: str,
        proxy_location: ProxyLocationInput | None = ProxyLocation.RESIDENTIAL,
        url: str | None = None,
        runnable_id: str | None = None,
        runnable_type: str | None = None,
        timeout_minutes: int | None = None,
        extensions: list[Extensions] | None = None,
        browser_type: PersistentBrowserType | None = None,
        proxy_session_id: str | None = None,
        is_high_priority: bool = False,
        browser_profile_id: str | None = None,
        generate_browser_profile: bool = False,
        inherit_profile_proxy: bool = False,
        wait_for_startup: bool = True,
    ) -> PersistentBrowserSession:
        """Create a new browser session."""
        ...

    async def occupy_browser_session(
        self,
        session_id: str,
        runnable_type: str,
        runnable_id: str,
        organization_id: str,
    ) -> None:
        """Occupy a browser session for use."""
        ...

    async def renew_or_close_session(self, session_id: str, organization_id: str) -> PersistentBrowserSession:
        """Renew a session or close it if renewal fails."""
        ...

    async def update_status(
        self, session_id: str, organization_id: str, status: str
    ) -> PersistentBrowserSession | None:
        """Update the status of a browser session."""
        ...

    async def release_browser_session(self, session_id: str, organization_id: str) -> None:
        """Release a browser session."""
        ...

    async def evict_cached_browser_state(
        self,
        session_id: str,
        organization_id: str | None = None,
        expected: BrowserState | None = None,
    ) -> None:
        """Drop any in-process cache entry for this session and close its BrowserState,
        so the next get_browser_state call re-establishes a fresh CDP connection.

        When ``expected`` is provided the eviction is race-safe: callers can pass the
        stale BrowserState they just navigated against, and the manager skips eviction
        if the cached wrapper now holds a different (fresh) BrowserState that another
        coroutine just stored. This guards against closing a fresh wrapper that a
        parallel caller is already holding.
        """
        ...

    async def close_session(self, organization_id: str, browser_session_id: str) -> None:
        """Close a specific browser session."""
        ...

    async def close_all_sessions(self, organization_id: str) -> None:
        """Close all browser sessions for an organization."""
        ...

    async def cleanup_stale_sessions(self) -> None:
        """Clean up sessions left active by a previous process."""
        ...

    @classmethod
    async def close(cls) -> None:
        """Close all browser sessions across all organizations."""
        ...
