from __future__ import annotations

import asyncio
import socket
from dataclasses import dataclass
from typing import Dict, List, Optional, Tuple

import structlog
from playwright._impl._errors import TargetClosedError
from playwright.async_api import async_playwright

from skyvern.forge.sdk.db.client import AgentDB
from skyvern.forge.sdk.schemas.persistent_browser_sessions import PersistentBrowserSession
from skyvern.forge.sdk.schemas.tasks import ProxyLocation
from skyvern.webeye.browser_factory import BrowserContextFactory, BrowserState

LOG = structlog.get_logger()


@dataclass
class BrowserSession:
    browser_state: BrowserState
    cdp_port: int
    cdp_host: str = "localhost"


class PersistentSessionsManager:
    instance: PersistentSessionsManager | None = None
    _browser_sessions: Dict[str, BrowserSession] = dict()
    database: AgentDB

    def __new__(cls, database: AgentDB) -> PersistentSessionsManager:
        if cls.instance is None:
            cls.instance = super().__new__(cls)
        cls.instance.database = database
        return cls.instance

    async def get_active_sessions(self, organization_id: str) -> List[PersistentBrowserSession]:
        """Get all active sessions for an organization."""
        return await self.database.get_active_persistent_browser_sessions(organization_id)

    def get_browser_state(self, session_id: str) -> BrowserState | None:
        """Get a specific browser session's state by session ID."""
        browser_session = self._browser_sessions.get(session_id)
        return browser_session.browser_state if browser_session else None

    async def get_session(self, session_id: str, organization_id: str) -> Optional[PersistentBrowserSession]:
        """Get a specific browser session by session ID."""
        return await self.database.get_persistent_browser_session(session_id, organization_id)

    async def create_session(
        self,
        organization_id: str,
        proxy_location: ProxyLocation | None = None,
        url: str | None = None,
        runnable_id: str | None = None,
        runnable_type: str | None = None,
    ) -> Tuple[PersistentBrowserSession, BrowserState]:
        """Create a new browser session for an organization and return its ID with the browser state."""

        LOG.info(
            "Creating new browser session",
            organization_id=organization_id,
        )

        browser_session_db = await self.database.create_persistent_browser_session(
            organization_id=organization_id,
            runnable_type=runnable_type,
            runnable_id=runnable_id,
        )

        cdp_port = None
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            s.bind(("", 0))
            cdp_port = s.getsockname()[1]

        session_id = browser_session_db.persistent_browser_session_id

        pw = await async_playwright().start()
        browser_context, browser_artifacts, browser_cleanup = await BrowserContextFactory.create_browser_context(
            pw,
            proxy_location=proxy_location,
            url=url,
            organization_id=organization_id,
            cdp_port=cdp_port,
        )

        async def on_context_close() -> None:
            await self._clean_up_on_session_close(session_id, organization_id)

        browser_context.on("close", lambda: asyncio.create_task(on_context_close()))

        browser_state = BrowserState(
            pw=pw,
            browser_context=browser_context,
            page=None,
            browser_artifacts=browser_artifacts,
            browser_cleanup=browser_cleanup,
        )

        browser_session = BrowserSession(
            browser_state=browser_state,
            cdp_port=cdp_port,
        )
        LOG.info(
            "Created new browser session",
            session_id=session_id,
            cdp_port=cdp_port,
            cdp_host="localhost",
        )
        self._browser_sessions[session_id] = browser_session

        if url:
            await browser_state.get_or_create_page(
                url=url,
                proxy_location=proxy_location,
                organization_id=organization_id,
            )

        return browser_session_db, browser_state

    async def occupy_browser_session(
        self,
        session_id: str,
        runnable_type: str,
        runnable_id: str,
        organization_id: str,
    ) -> None:
        """Occupy a specific browser session."""
        await self.database.occupy_persistent_browser_session(
            session_id=session_id,
            runnable_type=runnable_type,
            runnable_id=runnable_id,
            organization_id=organization_id,
        )

    async def get_network_info(self, session_id: str) -> Tuple[Optional[int], Optional[str]]:
        """Returns cdp port and ip address of the browser session"""
        browser_session = self._browser_sessions.get(session_id)
        if browser_session:
            return (
                browser_session.cdp_port,
                browser_session.cdp_host,
            )
        return None, None

    async def release_browser_session(self, session_id: str, organization_id: str) -> None:
        """Release a specific browser session."""
        await self.database.release_persistent_browser_session(session_id, organization_id)

    async def _clean_up_on_session_close(self, session_id: str, organization_id: str) -> None:
        """Clean up session data when browser session is closed"""
        browser_session = self._browser_sessions.get(session_id)
        if browser_session:
            await self.database.mark_persistent_browser_session_deleted(session_id, organization_id)
            self._browser_sessions.pop(session_id, None)

    async def close_session(self, organization_id: str, session_id: str) -> None:
        """Close a specific browser session."""
        browser_session = self._browser_sessions.get(session_id)
        if browser_session:
            LOG.info(
                "Closing browser session",
                organization_id=organization_id,
                session_id=session_id,
            )
            self._browser_sessions.pop(session_id, None)

            try:
                await browser_session.browser_state.close()
            except TargetClosedError:
                LOG.info(
                    "Browser context already closed",
                    organization_id=organization_id,
                    session_id=session_id,
                )
            except Exception:
                LOG.warning(
                    "Error while closing browser session",
                    organization_id=organization_id,
                    session_id=session_id,
                    exc_info=True,
                )
        else:
            LOG.info(
                "Browser session not found in memory, marking as deleted in database",
                organization_id=organization_id,
                session_id=session_id,
            )

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
