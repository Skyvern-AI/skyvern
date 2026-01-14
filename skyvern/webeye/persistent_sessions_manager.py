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

    async def close_session(self, organization_id: str, browser_session_id: str) -> None:
        """Close a specific browser session."""
        ...

    async def close_all_sessions(self, organization_id: str) -> None:
        """Close all browser sessions for an organization."""
        ...

    @classmethod
    async def close(cls) -> None:
        """Close all browser sessions across all organizations."""
        ...
