from __future__ import annotations

import datetime
from typing import Dict, List, Optional, Tuple
import asyncio

import structlog
from playwright.async_api import async_playwright

from skyvern.forge.sdk.db.client import AgentDB
from skyvern.forge.sdk.schemas.tasks import ProxyLocation
from skyvern.webeye.browser_factory import BrowserContextFactory, BrowserState

LOG = structlog.get_logger()


class PersistentSessionsManager:
    instance = None
    _browser_states: Dict[str, BrowserState] = dict()

    def __init__(self, database: AgentDB):
        self.database = database

    def __new__(cls, database: AgentDB) -> PersistentSessionsManager:
        if cls.instance is None:
            cls.instance = super().__new__(cls)
            cls.instance.database = database
        return cls.instance

    async def get_active_sessions(self, organization_id: str) -> List[str]:
        """Get all active session IDs for an organization."""
        return await self.database.get_active_persistent_browser_sessions(organization_id)

    def get_browser_state(self, session_id: str) -> Optional[BrowserState]:
        """Get a specific browser session by session ID."""
        return self._browser_states.get(session_id)

    async def get_session(self, session_id: str, organization_id: str) -> Optional[BrowserState]:
        """Get a specific browser session by session ID."""
        return await self.database.get_persistent_browser_session(session_id, organization_id)

    async def create_session(
        self,
        organization_id: str,
        proxy_location: ProxyLocation | None = None,
        url: str | None = None,
        runnable_id: str | None = None,
        runnable_type: str | None = None,
    ) -> Tuple[str, BrowserState]:
        """Create a new browser session for an organization and return its ID with the browser state."""
        
        LOG.info(
            "Creating new browser session",
            organization_id=organization_id,
        )
        
        browser_session = await self.database.create_persistent_browser_session(
            organization_id=organization_id,
            runnable_type=runnable_type,
            runnable_id=runnable_id,
        )
        print("---", browser_session)
        session_id = browser_session.persistent_browser_session_id

        pw = await async_playwright().start()
        browser_context, browser_artifacts, browser_cleanup = await BrowserContextFactory.create_browser_context(
            pw,
            proxy_location=proxy_location,
            url=url,
            organization_id=organization_id,
        )

        async def on_context_close():
            await self.close_session(organization_id, session_id)

        browser_context.on("close", lambda: asyncio.create_task(on_context_close()))
        
        browser_state = BrowserState(
            pw=pw,
            browser_context=browser_context,
            page=None,
            browser_artifacts=browser_artifacts,
            browser_cleanup=browser_cleanup,
        )

        self._browser_states[session_id] = browser_state

        if url:
            await browser_state.get_or_create_page(
                url=url,
                proxy_location=proxy_location,
                organization_id=organization_id,
            )

        return browser_session, browser_state

    async def occupy_browser_session(
        self,
        session_id: str,
        runnable_type: str,
        runnable_id: str,
    ) -> None:
        """Occupy a specific browser session."""
        await self.database.occupy_persistent_browser_session(session_id, runnable_type, runnable_id)


    async def release_browser_session(self, session_id: str) -> None:
        """Release a specific browser session."""
        await self.database.release_persistent_browser_session(session_id)


    async def close_session(self, organization_id: str, session_id: str) -> None:
        """Close a specific browser session."""
        browser_state = self.get_browser_state(session_id)
        if browser_state:
            LOG.info(
                "Closing browser session",
                organization_id=organization_id,
                session_id=session_id,
            )
            await browser_state.close()

            # Mark as deleted in database
        await self.database.mark_persistent_browser_session_deleted(session_id, organization_id)

    async def close_all_sessions(self, organization_id: str) -> None:
        """Close all browser sessions for an organization."""
        browser_sessions = await self.database.get_active_persistent_browser_sessions(organization_id)
        for browser_session in browser_sessions:
            await self.close_session(organization_id, browser_session.persistent_browser_session_id)

    @classmethod
    async def close(cls) -> None:
        """Close all browser sessions across all organizations."""
        LOG.info("Closing PersistentSessionsManager")
        if cls.instance:
            active_sessions = await cls.instance.database.get_all_active_persistent_browser_sessions()
            for db_session in active_sessions:
                await cls.instance.close_session(db_session.organization_id, db_session.persistent_browser_session_id)
        LOG.info("PersistentSessionsManager is closed")