from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone

import structlog
from sqlalchemy import asc, case, select

from skyvern.exceptions import BrowserProfileNotFound
from skyvern.forge.sdk.db._error_handling import db_operation
from skyvern.forge.sdk.db.base_alchemy_db import read_retry
from skyvern.forge.sdk.db.base_repository import BaseRepository
from skyvern.forge.sdk.db.exceptions import NotFoundError
from skyvern.forge.sdk.db.models import (
    BrowserProfileModel,
    PersistentBrowserSessionModel,
)
from skyvern.forge.sdk.db.utils import serialize_proxy_location
from skyvern.forge.sdk.schemas.browser_profiles import BrowserProfile
from skyvern.forge.sdk.schemas.persistent_browser_sessions import (
    Extensions,
    PersistentBrowserSession,
    PersistentBrowserType,
)
from skyvern.schemas.runs import ProxyLocation, ProxyLocationInput

LOG = structlog.get_logger()


class BrowserSessionsRepository(BaseRepository):
    """Database operations for browser profiles and persistent browser sessions."""

    @db_operation("create_browser_profile")
    async def create_browser_profile(
        self,
        organization_id: str,
        name: str,
        description: str | None = None,
        source_browser_type: str | None = None,
    ) -> BrowserProfile:
        async with self.Session() as session:
            browser_profile = BrowserProfileModel(
                organization_id=organization_id,
                name=name,
                description=description,
                source_browser_type=source_browser_type,
            )
            session.add(browser_profile)
            await session.commit()
            await session.refresh(browser_profile)
            return BrowserProfile.model_validate(browser_profile)

    @db_operation("get_browser_profile")
    async def get_browser_profile(
        self,
        profile_id: str,
        organization_id: str,
        include_deleted: bool = False,
    ) -> BrowserProfile | None:
        async with self.Session() as session:
            query = (
                select(BrowserProfileModel)
                .filter_by(browser_profile_id=profile_id)
                .filter_by(organization_id=organization_id)
            )
            if not include_deleted:
                query = query.filter(BrowserProfileModel.deleted_at.is_(None))

            browser_profile = (await session.scalars(query)).first()
            if not browser_profile:
                return None
            return BrowserProfile.model_validate(browser_profile)

    @db_operation("list_browser_profiles")
    async def list_browser_profiles(
        self,
        organization_id: str,
        include_deleted: bool = False,
    ) -> list[BrowserProfile]:
        async with self.Session() as session:
            query = select(BrowserProfileModel).filter_by(organization_id=organization_id)
            if not include_deleted:
                query = query.filter(BrowserProfileModel.deleted_at.is_(None))
            browser_profiles = await session.scalars(query.order_by(asc(BrowserProfileModel.created_at)))
            return [BrowserProfile.model_validate(profile) for profile in browser_profiles.all()]

    @db_operation("delete_browser_profile")
    async def delete_browser_profile(
        self,
        profile_id: str,
        organization_id: str,
    ) -> None:
        async with self.Session() as session:
            query = (
                select(BrowserProfileModel)
                .filter_by(browser_profile_id=profile_id)
                .filter_by(organization_id=organization_id)
                .filter(BrowserProfileModel.deleted_at.is_(None))
            )
            browser_profile = (await session.scalars(query)).first()
            if not browser_profile:
                raise BrowserProfileNotFound(profile_id=profile_id, organization_id=organization_id)
            browser_profile.deleted_at = datetime.now(timezone.utc)
            await session.commit()

    @db_operation("get_active_persistent_browser_sessions")
    async def get_active_persistent_browser_sessions(
        self,
        organization_id: str,
        active_hours: int = 24,
    ) -> list[PersistentBrowserSession]:
        """Get all active persistent browser sessions for an organization."""
        async with self.Session() as session:
            result = await session.execute(
                select(PersistentBrowserSessionModel)
                .filter_by(organization_id=organization_id)
                .filter_by(deleted_at=None)
                .filter_by(completed_at=None)
                .filter(
                    PersistentBrowserSessionModel.created_at
                    > datetime.now(timezone.utc) - timedelta(hours=active_hours)
                )
            )
            sessions = result.scalars().all()
            return [PersistentBrowserSession.model_validate(session) for session in sessions]

    @db_operation("get_persistent_browser_sessions_history")
    async def get_persistent_browser_sessions_history(
        self,
        organization_id: str,
        page: int = 1,
        page_size: int = 10,
        lookback_hours: int = 24 * 7,
    ) -> list[PersistentBrowserSession]:
        """Get persistent browser sessions history for an organization."""
        async with self.Session() as session:
            open_first = case(
                (
                    PersistentBrowserSessionModel.status == "running",
                    0,  # open
                ),
                else_=1,  # not open
            )

            result = await session.execute(
                select(PersistentBrowserSessionModel)
                .filter_by(organization_id=organization_id)
                .filter_by(deleted_at=None)
                .filter(
                    PersistentBrowserSessionModel.created_at
                    > (datetime.now(timezone.utc) - timedelta(hours=lookback_hours))
                )
                .order_by(
                    open_first.asc(),  # open sessions first
                    PersistentBrowserSessionModel.created_at.desc(),  # then newest within each group
                )
                .offset((page - 1) * page_size)
                .limit(page_size)
            )
            sessions = result.scalars().all()
            return [PersistentBrowserSession.model_validate(session) for session in sessions]

    @read_retry()
    @db_operation("get_persistent_browser_session_by_runnable_id", log_errors=False)
    async def get_persistent_browser_session_by_runnable_id(
        self, runnable_id: str, organization_id: str | None = None
    ) -> PersistentBrowserSession | None:
        """Get a specific persistent browser session."""
        async with self.Session() as session:
            query = (
                select(PersistentBrowserSessionModel)
                .filter_by(runnable_id=runnable_id)
                .filter_by(deleted_at=None)
                .filter_by(completed_at=None)
            )
            if organization_id:
                query = query.filter_by(organization_id=organization_id)
            persistent_browser_session = (await session.scalars(query)).first()
            if persistent_browser_session:
                return PersistentBrowserSession.model_validate(persistent_browser_session)
            return None

    @db_operation("get_persistent_browser_session")
    async def get_persistent_browser_session(
        self,
        session_id: str,
        organization_id: str | None = None,
    ) -> PersistentBrowserSession | None:
        """Get a specific persistent browser session."""
        async with self.Session() as session:
            persistent_browser_session = (
                await session.scalars(
                    select(PersistentBrowserSessionModel)
                    .filter_by(persistent_browser_session_id=session_id)
                    .filter_by(organization_id=organization_id)
                    .filter_by(deleted_at=None)
                )
            ).first()
            if persistent_browser_session:
                return PersistentBrowserSession.model_validate(persistent_browser_session)
            return None

    @db_operation("create_persistent_browser_session")
    async def create_persistent_browser_session(
        self,
        organization_id: str,
        runnable_type: str | None = None,
        runnable_id: str | None = None,
        timeout_minutes: int | None = None,
        proxy_location: ProxyLocationInput = ProxyLocation.RESIDENTIAL,
        extensions: list[Extensions] | None = None,
        browser_type: PersistentBrowserType | None = None,
        browser_profile_id: str | None = None,
    ) -> PersistentBrowserSession:
        """Create a new persistent browser session."""
        extensions_str: list[str] | None = (
            [extension.value for extension in extensions] if extensions is not None else None
        )
        async with self.Session() as session:
            browser_session = PersistentBrowserSessionModel(
                organization_id=organization_id,
                runnable_type=runnable_type,
                runnable_id=runnable_id,
                timeout_minutes=timeout_minutes,
                proxy_location=serialize_proxy_location(proxy_location),
                extensions=extensions_str,
                browser_type=browser_type.value if browser_type else None,
                browser_profile_id=browser_profile_id,
            )
            session.add(browser_session)
            await session.commit()
            await session.refresh(browser_session)
            return PersistentBrowserSession.model_validate(browser_session)

    @db_operation("update_persistent_browser_session")
    async def update_persistent_browser_session(
        self,
        browser_session_id: str,
        *,
        status: str | None = None,
        timeout_minutes: int | None = None,
        organization_id: str | None = None,
        completed_at: datetime | None = None,
        started_at: datetime | None = None,
    ) -> PersistentBrowserSession:
        async with self.Session() as session:
            persistent_browser_session = (
                await session.scalars(
                    select(PersistentBrowserSessionModel)
                    .filter_by(persistent_browser_session_id=browser_session_id)
                    .filter_by(organization_id=organization_id)
                    .filter_by(deleted_at=None)
                )
            ).first()
            if not persistent_browser_session:
                raise NotFoundError(f"PersistentBrowserSession {browser_session_id} not found")

            if status:
                persistent_browser_session.status = status
            if timeout_minutes:
                persistent_browser_session.timeout_minutes = timeout_minutes
            if completed_at:
                persistent_browser_session.completed_at = completed_at
            if started_at:
                persistent_browser_session.started_at = started_at

            await session.commit()
            await session.refresh(persistent_browser_session)
            return PersistentBrowserSession.model_validate(persistent_browser_session)

    @db_operation("set_persistent_browser_session_browser_address")
    async def set_persistent_browser_session_browser_address(
        self,
        browser_session_id: str,
        browser_address: str | None,
        ip_address: str | None,
        ecs_task_arn: str | None,
        organization_id: str | None = None,
    ) -> None:
        """Set the browser address for a persistent browser session."""
        async with self.Session() as session:
            persistent_browser_session = (
                await session.scalars(
                    select(PersistentBrowserSessionModel)
                    .filter_by(persistent_browser_session_id=browser_session_id)
                    .filter_by(organization_id=organization_id)
                    .filter_by(deleted_at=None)
                )
            ).first()
            if persistent_browser_session:
                if browser_address:
                    persistent_browser_session.browser_address = browser_address
                    # once the address is set, the session is started
                    persistent_browser_session.started_at = datetime.now(timezone.utc)
                if ip_address:
                    persistent_browser_session.ip_address = ip_address
                if ecs_task_arn:
                    persistent_browser_session.ecs_task_arn = ecs_task_arn
                await session.commit()
                await session.refresh(persistent_browser_session)
            else:
                raise NotFoundError(f"PersistentBrowserSession {browser_session_id} not found")

    @db_operation("update_persistent_browser_session_compute_cost")
    async def update_persistent_browser_session_compute_cost(
        self,
        session_id: str,
        organization_id: str,
        instance_type: str,
        vcpu_millicores: int,
        memory_mb: int,
        duration_ms: int,
        compute_cost: float,
    ) -> None:
        """Update the compute cost fields for a persistent browser session"""
        async with self.Session() as session:
            persistent_browser_session = (
                await session.scalars(
                    select(PersistentBrowserSessionModel)
                    .filter_by(persistent_browser_session_id=session_id)
                    .filter_by(organization_id=organization_id)
                    .filter_by(deleted_at=None)
                )
            ).first()
            if persistent_browser_session:
                persistent_browser_session.instance_type = instance_type
                persistent_browser_session.vcpu_millicores = vcpu_millicores
                persistent_browser_session.memory_mb = memory_mb
                persistent_browser_session.duration_ms = duration_ms
                persistent_browser_session.compute_cost = compute_cost
                await session.commit()
                await session.refresh(persistent_browser_session)
            else:
                raise NotFoundError(f"PersistentBrowserSession {session_id} not found")

    @db_operation("mark_persistent_browser_session_deleted")
    async def mark_persistent_browser_session_deleted(self, session_id: str, organization_id: str) -> None:
        """Mark a persistent browser session as deleted."""
        async with self.Session() as session:
            persistent_browser_session = (
                await session.scalars(
                    select(PersistentBrowserSessionModel)
                    .filter_by(persistent_browser_session_id=session_id)
                    .filter_by(organization_id=organization_id)
                )
            ).first()
            if persistent_browser_session:
                persistent_browser_session.deleted_at = datetime.now(timezone.utc)
                await session.commit()
                await session.refresh(persistent_browser_session)
            else:
                raise NotFoundError(f"PersistentBrowserSession {session_id} not found")

    @db_operation("occupy_persistent_browser_session")
    async def occupy_persistent_browser_session(
        self, session_id: str, runnable_type: str, runnable_id: str, organization_id: str
    ) -> None:
        """Occupy a specific persistent browser session."""
        async with self.Session() as session:
            persistent_browser_session = (
                await session.scalars(
                    select(PersistentBrowserSessionModel)
                    .filter_by(persistent_browser_session_id=session_id)
                    .filter_by(organization_id=organization_id)
                    .filter_by(deleted_at=None)
                )
            ).first()
            if persistent_browser_session:
                persistent_browser_session.runnable_type = runnable_type
                persistent_browser_session.runnable_id = runnable_id
                await session.commit()
                await session.refresh(persistent_browser_session)
            else:
                raise NotFoundError(f"PersistentBrowserSession {session_id} not found")

    @db_operation("release_persistent_browser_session")
    async def release_persistent_browser_session(
        self,
        session_id: str,
        organization_id: str,
    ) -> PersistentBrowserSession:
        """Release a specific persistent browser session."""
        async with self.Session() as session:
            persistent_browser_session = (
                await session.scalars(
                    select(PersistentBrowserSessionModel)
                    .filter_by(persistent_browser_session_id=session_id)
                    .filter_by(organization_id=organization_id)
                    .filter_by(deleted_at=None)
                )
            ).first()
            if persistent_browser_session:
                persistent_browser_session.runnable_type = None
                persistent_browser_session.runnable_id = None
                await session.commit()
                await session.refresh(persistent_browser_session)
                return PersistentBrowserSession.model_validate(persistent_browser_session)
            else:
                raise NotFoundError(f"PersistentBrowserSession {session_id} not found")

    @db_operation("close_persistent_browser_session")
    async def close_persistent_browser_session(self, session_id: str, organization_id: str) -> PersistentBrowserSession:
        """Close a specific persistent browser session."""
        async with self.Session() as session:
            persistent_browser_session = (
                await session.scalars(
                    select(PersistentBrowserSessionModel)
                    .filter_by(persistent_browser_session_id=session_id)
                    .filter_by(organization_id=organization_id)
                    .filter_by(deleted_at=None)
                )
            ).first()
            if persistent_browser_session:
                if persistent_browser_session.completed_at:
                    return PersistentBrowserSession.model_validate(persistent_browser_session)
                persistent_browser_session.completed_at = datetime.now(timezone.utc)
                persistent_browser_session.status = "completed"
                await session.commit()
                await session.refresh(persistent_browser_session)
                return PersistentBrowserSession.model_validate(persistent_browser_session)
            raise NotFoundError(f"PersistentBrowserSession {session_id} not found")

    @db_operation("get_all_active_persistent_browser_sessions")
    async def get_all_active_persistent_browser_sessions(self) -> list[PersistentBrowserSessionModel]:
        """Get all active persistent browser sessions across all organizations."""
        async with self.Session() as session:
            result = await session.execute(select(PersistentBrowserSessionModel).filter_by(deleted_at=None))
            return result.scalars().all()

    @db_operation("archive_browser_session_address")
    async def archive_browser_session_address(self, session_id: str, organization_id: str) -> None:
        """Suffix browser_address with a unique tag so the unique constraint
        no longer blocks new sessions that reuse the same local address."""
        async with self.Session() as session:
            row = (
                await session.scalars(
                    select(PersistentBrowserSessionModel)
                    .filter_by(persistent_browser_session_id=session_id)
                    .filter_by(organization_id=organization_id)
                    .filter_by(deleted_at=None)
                )
            ).first()

            if not row or not row.browser_address:
                return
            if "::closed::" in row.browser_address:
                return

            row.browser_address = f"{row.browser_address}::closed::{uuid.uuid4().hex}"
            await session.commit()

    @db_operation("get_uncompleted_persistent_browser_sessions")
    async def get_uncompleted_persistent_browser_sessions(self) -> list[PersistentBrowserSessionModel]:
        """Get all browser sessions that have not been completed or deleted."""
        async with self.Session() as session:
            result = await session.execute(
                select(PersistentBrowserSessionModel).filter_by(deleted_at=None).filter_by(completed_at=None)
            )
            return result.scalars().all()
