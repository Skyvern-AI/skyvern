from __future__ import annotations

from typing import Dict, List, Optional, Tuple
import datetime

import structlog
from playwright.async_api import async_playwright
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from skyvern.forge.sdk.db.client import get_async_session
from skyvern.forge.sdk.db.id import generate_persistent_browser_session_id
from skyvern.forge.sdk.db.models import PersistentBrowserSessionModel
from skyvern.forge.sdk.schemas.tasks import ProxyLocation
from skyvern.webeye.browser_factory import BrowserContextFactory, BrowserState
from skyvern.webeye.models import BrowserSessionResponse

LOG = structlog.get_logger()


class PersistentSessionsManager:
    instance = None
    # Store BrowserState objects in memory since they can't be serialized to DB
    _browser_states: Dict[str, BrowserState] = dict()

    def __new__(cls) -> PersistentSessionsManager:
        if cls.instance is None:
            cls.instance = super().__new__(cls)
        return cls.instance

    async def get_active_session_ids(self, organization_id: str) -> List[str]:
        """Get all active session IDs for an organization."""
        async with get_async_session() as session:
            result = await session.execute(
                select(PersistentBrowserSessionModel.persistent_browser_session_id)
                .where(
                    PersistentBrowserSessionModel.organization_id == organization_id,
                    PersistentBrowserSessionModel.deleted_at.is_(None)
                )
            )
            return [row[0] for row in result.all()]

    def get_session(self, organization_id: str, session_id: str) -> Optional[BrowserState]:
        """Get a specific browser session by organization ID and session ID."""
        return self._browser_states.get(session_id)

    async def create_session(
        self,
        organization_id: str,
        proxy_location: ProxyLocation | None = None,
        url: str | None = None,
    ) -> Tuple[str, BrowserState]:
        """Create a new browser session for an organization and return its ID with the browser state."""
        session_id = generate_persistent_browser_session_id()
        
        LOG.info(
            "Creating new browser session",
            organization_id=organization_id,
            session_id=session_id,
        )
        
        # Create database record
        async with get_async_session() as session:
            db_session = PersistentBrowserSessionModel(
                persistent_browser_session_id=session_id,
                organization_id=organization_id,
                runnable_type="browser_session",
                runnable_id=session_id,
            )
            session.add(db_session)
            await session.commit()

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
            self._browser_states.pop(session_id, None)

            # Mark as deleted in database
            async with get_async_session() as session:
                result = await session.execute(
                    select(PersistentBrowserSessionModel)
                    .where(
                        PersistentBrowserSessionModel.persistent_browser_session_id == session_id,
                        PersistentBrowserSessionModel.organization_id == organization_id
                    )
                )
                db_session = result.scalar_one_or_none()
                if db_session:
                    db_session.deleted_at = datetime.datetime.utcnow()
                    await session.commit()

    async def close_all_sessions(self, organization_id: str) -> None:
        """Close all browser sessions for an organization."""
        session_ids = await self.get_active_session_ids(organization_id)
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
        async with get_async_session() as session:
            result = await session.execute(
                select(PersistentBrowserSessionModel)
                .where(PersistentBrowserSessionModel.deleted_at.is_(None))
            )
            active_sessions = result.scalars().all()
            
            for db_session in active_sessions:
                await instance.close_session(
                    db_session.organization_id,
                    db_session.persistent_browser_session_id
                )
        LOG.info("PersistentSessionsManager is closed")
