from __future__ import annotations

import uuid
from typing import Dict, List, Optional, Tuple

import structlog
from playwright.async_api import async_playwright

from skyvern.forge.sdk.schemas.tasks import ProxyLocation
from skyvern.webeye.browser_factory import BrowserContextFactory, BrowserState
from skyvern.webeye.models import BrowserSessionResponse
LOG = structlog.get_logger()


class PersistentSessionsManager:
    instance = None
    # Dict structure: {organization_id: {session_id: BrowserState}}
    sessions: Dict[str, Dict[str, BrowserState]] = dict()

    def __new__(cls) -> PersistentSessionsManager:
        if cls.instance is None:
            cls.instance = super().__new__(cls)
        return cls.instance

    def get_active_session_ids(self, organization_id: str) -> List[str]:
        """Get all active session IDs for an organization."""
        return list(self.sessions.get(organization_id, {}).keys())

    def get_session(self, organization_id: str, session_id: str) -> Optional[BrowserState]:
        """Get a specific browser session by organization ID and session ID."""
        return self.sessions.get(organization_id, {}).get(session_id)

    async def create_session(
        self,
        organization_id: str,
        proxy_location: ProxyLocation | None = None,
        url: str | None = None,
    ) -> Tuple[str, BrowserState]:
        """Create a new browser session for an organization and return its ID with the browser state."""
        session_id = str(uuid.uuid4())
        
        LOG.info(
            "Creating new browser session",
            organization_id=organization_id,
            session_id=session_id,
        )
        
        pw = await async_playwright().start()
        browser_context, browser_artifacts, browser_cleanup = await BrowserContextFactory.create_browser_context(
            pw,
            proxy_location=proxy_location,
            url=url,
            organization_id=organization_id,
        )

        browser_context.on("close", lambda: self.sessions[organization_id].pop(session_id))
        
        browser_state = BrowserState(
            pw=pw,
            browser_context=browser_context,
            page=None,
            browser_artifacts=browser_artifacts,
            browser_cleanup=browser_cleanup,
        )

        # Initialize organization dict if it doesn't exist
        if organization_id not in self.sessions:
            self.sessions[organization_id] = {}
            
        self.sessions[organization_id][session_id] = browser_state

        # Create initial page if URL is provided
        if url:
            await browser_state.get_or_create_page(
                url=url,
                proxy_location=proxy_location,
                organization_id=organization_id,
            )

        return session_id, browser_state

    async def close_session(self, organization_id: str, session_id: str) -> None:
        """Close a specific browser session."""
        browser_state = self.get_session(organization_id, session_id)
        if browser_state:
            LOG.info(
                "Closing browser session",
                organization_id=organization_id,
                session_id=session_id,
            )
            await browser_state.close()
            if organization_id in self.sessions:
                self.sessions[organization_id].pop(session_id, None)
                if not self.sessions[organization_id]:
                    self.sessions.pop(organization_id)

    async def close_all_sessions(self, organization_id: str) -> None:
        """Close all browser sessions for an organization."""
        if organization_id in self.sessions:
            session_ids = list(self.sessions[organization_id].keys())
            for session_id in session_ids:
                await self.close_session(organization_id, session_id)

    async def build_browser_session_response(self, organization_id: str, session_id: str) -> BrowserSessionResponse:
        return BrowserSessionResponse(
            session_id=session_id,
            organization_id=organization_id,
        )

    @classmethod
    async def close(cls) -> None:
        """Close all browser sessions across all organizations."""
        LOG.info("Closing PersistentSessionsManager")
        instance = cls()
        org_ids = list(instance.sessions.keys())
        for org_id in org_ids:
            await instance.close_all_sessions(org_id)
        LOG.info("PersistentSessionsManager is closed")
