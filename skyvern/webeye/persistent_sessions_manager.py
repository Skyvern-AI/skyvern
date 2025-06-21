from __future__ import annotations

from dataclasses import dataclass

import structlog
from playwright._impl._errors import TargetClosedError

from skyvern.forge.sdk.db.client import AgentDB
from skyvern.forge.sdk.db.polls import wait_on_persistent_browser_address
from skyvern.forge.sdk.schemas.persistent_browser_sessions import PersistentBrowserSession
from skyvern.webeye.browser_factory import BrowserState

LOG = structlog.get_logger()


@dataclass
class BrowserSession:
    browser_state: BrowserState
    cdp_port: int
    cdp_host: str = "localhost"


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

        LOG.info("Begin browser session", browser_session_id=browser_session_id, runnable_type=runnable_type, runnable_id=runnable_id, organization_id=organization_id)

        persistent_browser_session = await self.database.get_persistent_browser_session(
            browser_session_id, organization_id
        )

        if persistent_browser_session is None:
            raise Exception(f"Persistent browser session not found for {browser_session_id}")

        LOG.info("Found persistent browser session, calling occupy_browser_session", browser_session_id=browser_session_id, runnable_type=runnable_type, runnable_id=runnable_id)

        await self.occupy_browser_session(
            session_id=browser_session_id,
            runnable_type=runnable_type,
            runnable_id=runnable_id,
            organization_id=organization_id,
        )

        LOG.info("Successfully occupied browser session", browser_session_id=browser_session_id, runnable_type=runnable_type, runnable_id=runnable_id)

        # For OSS version, ensure the browser state is available for the workflow run
        # In cloud version, this would be handled by temporal and ECS fargate
        browser_session = self._browser_sessions.get(browser_session_id)
        if browser_session and browser_session.browser_state:
            # The browser state already exists, we just need to make sure it's associated with the runnable
            LOG.info(
                "Browser session already has browser state, ready for workflow run",
                browser_session_id=browser_session_id,
                runnable_id=runnable_id,
                runnable_type=runnable_type,
            )
        else:
            # Browser state doesn't exist, create it for OSS version
            LOG.info(
                "Creating browser state for workflow run in OSS version",
                browser_session_id=browser_session_id,
                runnable_id=runnable_id,
                runnable_type=runnable_type,
            )
            
            try:
                from skyvern.webeye.browser_factory import BrowserState
                from playwright.async_api import async_playwright
                
                # Create a new browser state
                pw = await async_playwright().start()
                from skyvern.webeye.browser_factory import BrowserContextFactory
                
                (
                    browser_context,
                    browser_artifacts,
                    browser_cleanup,
                ) = await BrowserContextFactory.create_browser_context(
                    pw,
                    organization_id=organization_id,
                )
                
                browser_state = BrowserState(
                    pw=pw,
                    browser_context=browser_context,
                    page=None,
                    browser_artifacts=browser_artifacts,
                    browser_cleanup=browser_cleanup,
                )
                
                # Store the browser state in memory
                browser_session = BrowserSession(
                    browser_state=browser_state,
                    cdp_port=0,  # Not used in OSS version
                    cdp_host="localhost"
                )
                self._browser_sessions[browser_session_id] = browser_session
                
                LOG.info(
                    "Created browser state for workflow run",
                    browser_session_id=browser_session_id,
                    runnable_id=runnable_id,
                    runnable_type=runnable_type,
                )
                
            except Exception as e:
                LOG.warning(
                    "Failed to create browser state for workflow run",
                    browser_session_id=browser_session_id,
                    runnable_id=runnable_id,
                    runnable_type=runnable_type,
                    error=str(e),
                )

        LOG.info("Browser session begin", browser_session_id=browser_session_id)

    async def get_browser_address(self, session_id: str, organization_id: str) -> tuple[str, str, str]:
        address = await wait_on_persistent_browser_address(self.database, session_id, organization_id)

        if address is None:
            raise Exception(f"Browser address not found for persistent browser session {session_id}")

        protocol = "http"
        host, cdp_port = address.split(":")

        return protocol, host, cdp_port

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
        if browser_session:
            return browser_session.browser_state
        
        # For OSS version, if browser state doesn't exist in memory but session exists in database,
        # create a new browser state
        if organization_id:
            try:
                # Check if session exists in database
                db_session = await self.database.get_persistent_browser_session(session_id, organization_id)
                if db_session and db_session.deleted_at is None:
                    LOG.info(
                        "Browser state not found in memory but session exists in database, creating new browser state for OSS version",
                        browser_session_id=session_id,
                        organization_id=organization_id,
                    )
                    
                    from skyvern.webeye.browser_factory import BrowserState
                    from playwright.async_api import async_playwright
                    
                    # Create a new browser state
                    pw = await async_playwright().start()
                    from skyvern.webeye.browser_factory import BrowserContextFactory
                    
                    (
                        browser_context,
                        browser_artifacts,
                        browser_cleanup,
                    ) = await BrowserContextFactory.create_browser_context(
                        pw,
                        organization_id=organization_id,
                    )
                    
                    browser_state = BrowserState(
                        pw=pw,
                        browser_context=browser_context,
                        page=None,
                        browser_artifacts=browser_artifacts,
                        browser_cleanup=browser_cleanup,
                    )
                    
                    # Store the browser state in memory
                    browser_session = BrowserSession(
                        browser_state=browser_state,
                        cdp_port=0,  # Not used in OSS version
                        cdp_host="localhost"
                    )
                    self._browser_sessions[session_id] = browser_session
                    
                    LOG.info(
                        "Created new browser state for existing session",
                        browser_session_id=session_id,
                        organization_id=organization_id,
                    )
                    
                    return browser_state
                    
            except Exception as e:
                LOG.warning(
                    "Failed to create browser state for existing session",
                    browser_session_id=session_id,
                    organization_id=organization_id,
                    error=str(e),
                )
        
        return None

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

        # For OSS version, create an actual browser state since cloud infrastructure is not available
        # In cloud version, this would be handled by temporal and ECS fargate
        try:
            from skyvern.webeye.browser_factory import BrowserState
            from playwright.async_api import async_playwright
            
            # Create a new browser state
            pw = await async_playwright().start()
            from skyvern.webeye.browser_factory import BrowserContextFactory
            
            (
                browser_context,
                browser_artifacts,
                browser_cleanup,
            ) = await BrowserContextFactory.create_browser_context(
                pw,
                organization_id=organization_id,
            )
            
            browser_state = BrowserState(
                pw=pw,
                browser_context=browser_context,
                page=None,
                browser_artifacts=browser_artifacts,
                browser_cleanup=browser_cleanup,
            )
            
            # Store the browser state in memory
            browser_session = BrowserSession(
                browser_state=browser_state,
                cdp_port=0,  # Not used in OSS version
                cdp_host="localhost"
            )
            self._browser_sessions[browser_session_db.persistent_browser_session_id] = browser_session
            
            LOG.info(
                "Created browser state for OSS version",
                browser_session_id=browser_session_db.persistent_browser_session_id,
                organization_id=organization_id,
            )
            
        except Exception as e:
            LOG.warning(
                "Failed to create browser state for OSS version, session will be created in database only",
                browser_session_id=browser_session_db.persistent_browser_session_id,
                organization_id=organization_id,
                error=str(e),
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

    async def get_network_info(self, session_id: str) -> tuple[int | None, str | None]:
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
