from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from math import floor

import structlog
from playwright._impl._errors import TargetClosedError

from skyvern.config import settings
from skyvern.exceptions import BrowserSessionNotRenewable, MissingBrowserAddressError
from skyvern.forge import app
from skyvern.forge.sdk.db.agent_db import AgentDB
from skyvern.forge.sdk.db.polls import wait_on_persistent_browser_address
from skyvern.forge.sdk.schemas.persistent_browser_sessions import (
    PersistentBrowserSession,
    PersistentBrowserSessionStatus,
    is_final_status,
)
from skyvern.schemas.runs import ProxyLocation, ProxyLocationInput
from skyvern.webeye.browser_state import BrowserState

LOG = structlog.get_logger()


@dataclass
class BrowserSession:
    browser_state: BrowserState


async def validate_session_for_renewal(
    database: AgentDB,
    session_id: str,
    organization_id: str,
) -> tuple[PersistentBrowserSession, datetime, int]:
    """
    Validate a specific browser session for renewal. Otherwise raise.
    """

    browser_session = await database.get_persistent_browser_session(
        session_id=session_id,
        organization_id=organization_id,
    )

    if not browser_session:
        LOG.warning(
            "Attempted to renew non-existent browser session",
            browser_session_id=session_id,
            organization_id=organization_id,
        )
        raise BrowserSessionNotRenewable("Browser session does not exist", session_id)

    if browser_session.completed_at is not None:
        LOG.warning(
            "Attempted to renew completed browser session",
            browser_session_id=session_id,
            organization_id=organization_id,
        )
        raise BrowserSessionNotRenewable("Browser session has already completed", session_id)

    if browser_session.started_at is None or browser_session.timeout_minutes is None:
        LOG.warning(
            "Attempted to renew browser session that has not started yet",
            browser_session_id=session_id,
            organization_id=organization_id,
        )
        raise BrowserSessionNotRenewable("Browser session has not started yet", session_id)

    if browser_session.status not in [
        PersistentBrowserSessionStatus.created,
        PersistentBrowserSessionStatus.retry,
        PersistentBrowserSessionStatus.running,
    ]:
        LOG.warning(
            "Attempted to renew browser session that is not in the 'created', 'retry' or 'running' state",
            browser_session_id=session_id,
            organization_id=organization_id,
        )
        raise BrowserSessionNotRenewable(
            "Browser session is not in the 'created', 'retry' or 'running' state", session_id
        )

    started_at_utc = (
        browser_session.started_at.replace(tzinfo=timezone.utc)
        if browser_session.started_at.tzinfo is None
        else browser_session.started_at
    )

    return browser_session, started_at_utc, browser_session.timeout_minutes


async def renew_session(database: AgentDB, session_id: str, organization_id: str) -> PersistentBrowserSession:
    """
    Renew a specific browser session, if it is deemed renewable.
    """

    browser_session, started_at_utc, current_timeout_minutes = await validate_session_for_renewal(
        database,
        organization_id=organization_id,
        session_id=session_id,
    )

    right_now = datetime.now(timezone.utc)
    current_timeout_datetime = started_at_utc + timedelta(minutes=float(current_timeout_minutes))
    minutes_left = (current_timeout_datetime - right_now).total_seconds() / 60

    if minutes_left >= settings.DEBUG_SESSION_TIMEOUT_THRESHOLD_MINUTES:
        new_timeout_datetime = right_now + timedelta(minutes=settings.DEBUG_SESSION_TIMEOUT_MINUTES)
        minutes_diff = floor((new_timeout_datetime - current_timeout_datetime).total_seconds() / 60)
        new_timeout_minutes = current_timeout_minutes + minutes_diff

        browser_session = await database.update_persistent_browser_session(
            session_id,
            organization_id=organization_id,
            timeout_minutes=new_timeout_minutes,
        )

        LOG.info(
            f"Extended browser session by {minutes_diff} minute(s)",
            minutes_diff=minutes_diff,
            session_id=session_id,
            organization_id=organization_id,
        )

        return browser_session

    raise BrowserSessionNotRenewable("Session has expired", session_id)


async def update_status(
    db: AgentDB, session_id: str, organization_id: str, status: str
) -> PersistentBrowserSession | None:
    persistent_browser_session = await db.get_persistent_browser_session(session_id, organization_id)

    if not persistent_browser_session:
        LOG.warning(
            "Cannot update browser session status, browser session not found in database",
            organization_id=organization_id,
            session_id=session_id,
            desired_status=status,
        )
        return None

    if is_final_status(status):
        if is_final_status(persistent_browser_session.status):
            LOG.warning(
                "Attempted to update browser session status to a final status when it is already final",
                browser_session_id=session_id,
                organization_id=organization_id,
                desired_status=status,
                current_status=persistent_browser_session.status,
            )
            return None

    LOG.info(
        "Updating browser session status",
        browser_session_id=session_id,
        organization_id=organization_id,
        browser_status=status,
    )
    persistent_browser_session = await db.update_persistent_browser_session(
        session_id,
        status=status,
        organization_id=organization_id,
    )

    return persistent_browser_session


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

    async def set_browser_state(self, session_id: str, browser_state: BrowserState) -> None:
        browser_session = BrowserSession(browser_state=browser_state)
        self._browser_sessions[session_id] = browser_session

    async def get_session(self, session_id: str, organization_id: str) -> PersistentBrowserSession | None:
        """Get a specific browser session by session ID."""
        return await self.database.get_persistent_browser_session(session_id, organization_id)

    async def create_session(
        self,
        organization_id: str,
        runnable_id: str | None = None,
        runnable_type: str | None = None,
        timeout_minutes: int | None = None,
        proxy_location: ProxyLocationInput = ProxyLocation.RESIDENTIAL,
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
            proxy_location=proxy_location,
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

    async def renew_or_close_session(self, session_id: str, organization_id: str) -> PersistentBrowserSession:
        try:
            return await renew_session(self.database, session_id, organization_id)
        except BrowserSessionNotRenewable:
            await self.close_session(organization_id, session_id)
            raise

    async def update_status(
        self, session_id: str, organization_id: str, status: str
    ) -> PersistentBrowserSession | None:
        return await update_status(self.database, session_id, organization_id, status)

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

            # Export session profile before closing (so it can be used to create browser profiles)
            browser_artifacts = browser_session.browser_state.browser_artifacts
            if browser_artifacts and browser_artifacts.browser_session_dir:
                try:
                    await app.STORAGE.store_browser_profile(
                        organization_id=organization_id,
                        profile_id=browser_session_id,
                        directory=browser_artifacts.browser_session_dir,
                    )
                    LOG.info(
                        "Exported browser session profile",
                        browser_session_id=browser_session_id,
                        organization_id=organization_id,
                    )
                except Exception:
                    LOG.exception(
                        "Failed to export browser session profile",
                        browser_session_id=browser_session_id,
                        organization_id=organization_id,
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

        await self.database.close_persistent_browser_session(browser_session_id, organization_id)

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
