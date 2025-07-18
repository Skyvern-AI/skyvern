from __future__ import annotations

from dataclasses import dataclass

import structlog
from playwright._impl._errors import TargetClosedError

from skyvern.exceptions import MissingBrowserAddressError
from skyvern.forge.sdk.db.client import AgentDB
from skyvern.forge.sdk.db.polls import wait_on_persistent_browser_address
from skyvern.forge.sdk.schemas.persistent_browser_sessions import PersistentBrowserSession
from skyvern.webeye.browser_factory import BrowserState

LOG = structlog.get_logger()


@dataclass
class BrowserSession:
    browser_state: BrowserState


class PersistentSessionsManager:
    instance: PersistentSessionsManager | None = None
    _browser_sessions: dict[str, BrowserSession] = dict()
    database: AgentDB

    def __new__(cls, database: AgentDB) -> PersistentSessionsManager:
        if cls.instance is None:
            new_instance = super().__new__(cls)
            cls.instance = new_instance
            cls.instance.database = database
            return new_instance

        cls.instance.database = database
        return cls.instance

    def watch_session_pool(self) -> None:
        return None

    async def begin_session(
        self,
        *,
        browser_session_id: str,
        runnable_type: str,
        runnable_id: str,
        organization_id: str,
    ) -> None:
        """
        Attempt to begin a session.

        TODO: cloud-side, temporal and ECS fargate are used to effect the session. These tools are not presently
        available OSS-side.
        """

        LOG.info("Begin browser session", browser_session_id=browser_session_id)

        persistent_browser_session = await self.database.get_persistent_browser_session(
            browser_session_id, organization_id
        )

        if persistent_browser_session is None:
            raise Exception(f"Persistent browser session not found for {browser_session_id}")

        await self.occupy_browser_session(
            session_id=browser_session_id,
            runnable_type=runnable_type,
            runnable_id=runnable_id,
            organization_id=organization_id,
        )

        LOG.info("Browser session begin", browser_session_id=browser_session_id)

    async def get_browser_address(self, session_id: str, organization_id: str) -> str:
        address = await wait_on_persistent_browser_address(self.database, session_id, organization_id)

        if address is None:
            raise MissingBrowserAddressError(session_id)

        return address

    async def get_session_by_runnable_id(
        self, runnable_id: str, organization_id: str
    ) -> PersistentBrowserSession | None:
        """Get a specific browser session by runnable ID."""
        return await self.database.get_persistent_browser_session_by_runnable_id(runnable_id, organization_id)

    async def get_active_sessions(self, organization_id: str) -> list[PersistentBrowserSession]:
        """Get all active sessions for an organization."""
        return await self.database.get_active_persistent_browser_sessions(organization_id)

    async def get_browser_state(self, session_id: str, organization_id: str | None = None) -> BrowserState | None:
        """Get a specific browser session's state by session ID."""
        browser_session = self._browser_sessions.get(session_id)
        return browser_session.browser_state if browser_session else None

    async def get_session(self, session_id: str, organization_id: str) -> PersistentBrowserSession | None:
        """Get a specific browser session by session ID."""
        return await self.database.get_persistent_browser_session(session_id, organization_id)

    async def create_session(
        self,
        organization_id: str,
        runnable_id: str | None = None,
        runnable_type: str | None = None,
        timeout_minutes: int | None = None,
    ) -> PersistentBrowserSession:
        """Create a new browser session for an organization and return its ID with the browser state."""

        LOG.info(
            "Creating new browser session",
            organization_id=organization_id,
        )

        browser_session_db = await self.database.create_persistent_browser_session(
            organization_id=organization_id,
            runnable_type=runnable_type,
            runnable_id=runnable_id,
            timeout_minutes=timeout_minutes,
        )

        return browser_session_db

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

    async def release_browser_session(self, session_id: str, organization_id: str) -> None:
        """Release a specific browser session."""
        await self.database.release_persistent_browser_session(session_id, organization_id)

    async def close_session(self, organization_id: str, browser_session_id: str) -> None:
        """Close a specific browser session."""
        browser_session = self._browser_sessions.get(browser_session_id)
        if browser_session:
            LOG.info(
                "Closing browser session",
                organization_id=organization_id,
                session_id=browser_session_id,
            )
            self._browser_sessions.pop(browser_session_id, None)

            try:
                await browser_session.browser_state.close()
            except TargetClosedError:
                LOG.info(
                    "Browser context already closed",
                    organization_id=organization_id,
                    session_id=browser_session_id,
                )
            except Exception:
                LOG.warning(
                    "Error while closing browser session",
                    organization_id=organization_id,
                    session_id=browser_session_id,
                    exc_info=True,
                )
        else:
            LOG.info(
                "Browser session not found in memory, marking as deleted in database",
                organization_id=organization_id,
                session_id=browser_session_id,
            )

        await self.database.mark_persistent_browser_session_deleted(browser_session_id, organization_id)

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
