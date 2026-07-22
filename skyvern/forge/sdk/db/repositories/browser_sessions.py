from __future__ import annotations

import uuid
from datetime import datetime, timedelta
from typing import cast

import structlog
from sqlalchemy import and_, case, desc, func, or_, select
from sqlalchemy.exc import IntegrityError, StatementError

from skyvern.config import settings
from skyvern.exceptions import BrowserProfileNotFound
from skyvern.forge.sdk.db._error_handling import db_operation
from skyvern.forge.sdk.db.base_alchemy_db import read_retry
from skyvern.forge.sdk.db.base_repository import BaseRepository
from skyvern.forge.sdk.db.datetime_utils import naive_utc_now, to_naive_utc
from skyvern.forge.sdk.db.exceptions import NotFoundError
from skyvern.forge.sdk.db.id import generate_browser_profile_id
from skyvern.forge.sdk.db.models import (
    BrowserProfileModel,
    CredentialModel,
    PersistentBrowserSessionModel,
    WorkflowModel,
    WorkflowRunModel,
)
from skyvern.forge.sdk.db.repositories.proxy_pin_update import apply_proxy_pin_to_model, normalize_proxy_pin_for_create
from skyvern.forge.sdk.db.utils import serialize_proxy_location
from skyvern.forge.sdk.schemas.browser_profiles import (
    BrowserProfile,
    BrowserProfileUsage,
    BrowserProfileUsageCredential,
    BrowserProfileUsageWorkflow,
)
from skyvern.forge.sdk.schemas.persistent_browser_sessions import (
    Extensions,
    PersistentBrowserSession,
    PersistentBrowserType,
)
from skyvern.schemas.proxy_pinning import generate_proxy_session_id, parse_proxy_location_input
from skyvern.schemas.runs import ProxyLocation, ProxyLocationInput

LOG = structlog.get_logger()
_UNSET = object()


class BrowserSessionsRepository(BaseRepository):
    """Database operations for browser profiles and persistent browser sessions."""

    @db_operation("create_browser_profile")
    async def create_browser_profile(
        self,
        organization_id: str,
        name: str,
        description: str | None = None,
        source_browser_type: str | None = None,
        proxy_location: ProxyLocationInput = None,
        proxy_session_id: str | None = None,
    ) -> BrowserProfile:
        async with self.Session() as session:
            browser_profile_id = generate_browser_profile_id()
            proxy_location, proxy_session_id = normalize_proxy_pin_for_create(
                proxy_location=proxy_location,
                proxy_session_id=proxy_session_id,
                entity_id=browser_profile_id,
            )
            browser_profile = BrowserProfileModel(
                browser_profile_id=browser_profile_id,
                organization_id=organization_id,
                name=name,
                description=description,
                source_browser_type=source_browser_type,
                proxy_location=serialize_proxy_location(proxy_location),
                proxy_session_id=proxy_session_id,
            )
            session.add(browser_profile)
            await session.commit()
            await session.refresh(browser_profile)
            return BrowserProfile.model_validate(browser_profile)

    @db_operation("get_or_create_managed_browser_profile")
    async def get_or_create_managed_browser_profile(
        self,
        *,
        organization_id: str,
        workflow_permanent_id: str,
        browser_profile_key_digest: str,
        name: str,
    ) -> tuple[BrowserProfile, bool]:
        digest = browser_profile_key_digest or ""
        async with self.Session() as session:
            query = (
                select(BrowserProfileModel)
                .filter_by(
                    organization_id=organization_id,
                    workflow_permanent_id=workflow_permanent_id,
                    browser_profile_key_digest=digest,
                    is_managed=True,
                )
                .filter(BrowserProfileModel.deleted_at.is_(None))
            )
            browser_profile = (await session.scalars(query)).first()
            if browser_profile:
                return BrowserProfile.model_validate(browser_profile), False

            browser_profile = BrowserProfileModel(
                browser_profile_id=generate_browser_profile_id(),
                organization_id=organization_id,
                name=name,
                is_managed=True,
                workflow_permanent_id=workflow_permanent_id,
                browser_profile_key_digest=digest,
            )
            session.add(browser_profile)
            try:
                await session.commit()
            except IntegrityError:
                await session.rollback()
                browser_profile = (await session.scalars(query)).first()
                if browser_profile:
                    return BrowserProfile.model_validate(browser_profile), False
                raise
            await session.refresh(browser_profile)
            return BrowserProfile.model_validate(browser_profile), True

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

    @db_operation("list_managed_browser_profiles_for_workflow")
    async def list_managed_browser_profiles_for_workflow(
        self,
        *,
        organization_id: str,
        workflow_permanent_id: str,
        include_deleted: bool = False,
    ) -> list[BrowserProfile]:
        async with self.Session() as session:
            query = select(BrowserProfileModel).filter_by(
                organization_id=organization_id,
                workflow_permanent_id=workflow_permanent_id,
                is_managed=True,
            )
            if not include_deleted:
                query = query.filter(BrowserProfileModel.deleted_at.is_(None))
            browser_profiles = await session.scalars(query)
            return [BrowserProfile.model_validate(profile) for profile in browser_profiles.all()]

    @db_operation("list_browser_profiles")
    async def list_browser_profiles(
        self,
        organization_id: str,
        include_deleted: bool = False,
        page: int = 1,
        page_size: int = 10,
        search_key: str | None = None,
        managed: bool | None = None,
    ) -> list[BrowserProfile]:
        if page < 1:
            raise ValueError(f"Page must be greater than 0, got {page}")
        db_page = page - 1
        async with self.Session() as session:
            query = select(BrowserProfileModel).filter_by(organization_id=organization_id)
            if not include_deleted:
                query = query.filter(BrowserProfileModel.deleted_at.is_(None))
            if managed is not None:
                query = query.filter(BrowserProfileModel.is_managed.is_(managed))
            if search_key:
                search_like = f"%{search_key}%"
                query = query.filter(
                    or_(
                        BrowserProfileModel.name.ilike(search_like),
                        BrowserProfileModel.description.ilike(search_like),
                    )
                )
            # The id tie-break only needs to be deterministic so pagination stays
            # stable when created_at collides; it isn't meant to encode recency.
            query = (
                query.order_by(desc(BrowserProfileModel.created_at), desc(BrowserProfileModel.browser_profile_id))
                .limit(page_size)
                .offset(db_page * page_size)
            )
            browser_profiles = await session.scalars(query)
            profiles = [BrowserProfile.model_validate(profile) for profile in browser_profiles.all()]

            # One batched reverse-lookup for the whole page so the UI can render the credential-login role
            # without a per-row usage fetch (which fanned out one 3-table join per row on every list load).
            if profiles:
                credential_rows = await session.execute(
                    select(CredentialModel.browser_profile_id, CredentialModel.name).where(
                        CredentialModel.browser_profile_id.in_([p.browser_profile_id for p in profiles]),
                        CredentialModel.organization_id == organization_id,
                        CredentialModel.deleted_at.is_(None),
                    )
                )
                name_by_profile: dict[str, str] = {}
                for browser_profile_id, name in credential_rows.all():
                    name_by_profile.setdefault(browser_profile_id, name)
                for profile in profiles:
                    profile.linked_credential_name = name_by_profile.get(profile.browser_profile_id)

            return profiles

    @read_retry()
    @db_operation("get_browser_profile_usage")
    async def get_browser_profile_usage(
        self,
        profile_id: str,
        organization_id: str,
        recent_window_days: int = 30,
    ) -> BrowserProfileUsage:
        """Who depends on this profile: workflows that pin it, credentials that link it, and how many
        runs it has seeded lately. Powers the Refresh/Delete used-by confirmation and the list-row badges."""
        async with self.Session() as session:
            latest_versions = (
                select(
                    WorkflowModel.workflow_permanent_id.label("wpid"),
                    func.max(WorkflowModel.version).label("max_version"),
                )
                .where(
                    WorkflowModel.organization_id == organization_id,
                    WorkflowModel.deleted_at.is_(None),
                )
                .group_by(WorkflowModel.workflow_permanent_id)
                .subquery()
            )
            workflows_query = (
                select(WorkflowModel.workflow_permanent_id, WorkflowModel.title)
                .join(
                    latest_versions,
                    and_(
                        WorkflowModel.workflow_permanent_id == latest_versions.c.wpid,
                        WorkflowModel.version == latest_versions.c.max_version,
                    ),
                )
                .where(
                    WorkflowModel.organization_id == organization_id,
                    WorkflowModel.browser_profile_id == profile_id,
                )
            )
            workflow_rows = (await session.execute(workflows_query)).all()
            workflows = [
                BrowserProfileUsageWorkflow(workflow_permanent_id=wpid, title=title, via="browser_profile_id")
                for wpid, title in workflow_rows
            ]
            # SKY-12643 adds workflows.seed_browser_profile_id (the both-checked quadrant). Until it merges
            # that column doesn't exist, so only browser_profile_id usage is reported; drop this guard and
            # add the via="seed_browser_profile_id" branch once 12643 lands (STOP-signal-3 integrate step).
            if hasattr(WorkflowModel, "seed_browser_profile_id"):
                seed_query = (
                    select(WorkflowModel.workflow_permanent_id, WorkflowModel.title)
                    .join(
                        latest_versions,
                        and_(
                            WorkflowModel.workflow_permanent_id == latest_versions.c.wpid,
                            WorkflowModel.version == latest_versions.c.max_version,
                        ),
                    )
                    .where(
                        WorkflowModel.organization_id == organization_id,
                        WorkflowModel.seed_browser_profile_id == profile_id,
                    )
                )
                # A workflow can hold the same profile in both columns (lossless both-set encoding), so emit
                # a per-role entry for each rather than deduping the seed relationship away.
                for wpid, title in (await session.execute(seed_query)).all():
                    workflows.append(
                        BrowserProfileUsageWorkflow(
                            workflow_permanent_id=wpid, title=title, via="seed_browser_profile_id"
                        )
                    )

            credential_rows = (
                await session.execute(
                    select(CredentialModel.credential_id, CredentialModel.name).where(
                        CredentialModel.browser_profile_id == profile_id,
                        CredentialModel.organization_id == organization_id,
                        CredentialModel.deleted_at.is_(None),
                    )
                )
            ).all()
            credentials = [
                BrowserProfileUsageCredential(credential_id=credential_id, name=name)
                for credential_id, name in credential_rows
            ]

            recent_cutoff = naive_utc_now() - timedelta(days=recent_window_days)
            recent_seeded_run_count = (
                await session.execute(
                    select(func.count())
                    .select_from(WorkflowRunModel)
                    .where(
                        WorkflowRunModel.browser_profile_id == profile_id,
                        WorkflowRunModel.organization_id == organization_id,
                        WorkflowRunModel.created_at >= recent_cutoff,
                    )
                )
            ).scalar_one()

            return BrowserProfileUsage(
                workflows=workflows,
                credentials=credentials,
                recent_seeded_run_count=recent_seeded_run_count,
            )

    @db_operation("delete_browser_profile")
    async def delete_browser_profile(
        self,
        profile_id: str,
        organization_id: str,
    ) -> list[str]:
        """Soft-delete a profile and detach any credentials linking it in ONE transaction, so a mid-failure
        can't leave the profile deleted with a credential still holding the dangling bp_ id (which a retry
        would never re-clear, since the second delete 404s on the already-deleted profile). Returns the
        credential ids that were detached."""
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
            browser_profile.deleted_at = naive_utc_now()

            linked_credentials = (
                await session.scalars(
                    select(CredentialModel)
                    .filter_by(browser_profile_id=profile_id, organization_id=organization_id)
                    .filter(CredentialModel.deleted_at.is_(None))
                )
            ).all()
            cleared_credential_ids = [credential.credential_id for credential in linked_credentials]
            for credential in linked_credentials:
                credential.browser_profile_id = None

            await session.commit()
            return cleared_credential_ids

    @db_operation("hard_delete_browser_profile")
    async def hard_delete_browser_profile(
        self,
        profile_id: str,
        organization_id: str,
    ) -> None:
        async with self.Session() as session:
            query = (
                select(BrowserProfileModel)
                .filter_by(browser_profile_id=profile_id)
                .filter_by(organization_id=organization_id)
            )
            browser_profile = (await session.scalars(query)).first()
            if not browser_profile:
                raise BrowserProfileNotFound(profile_id=profile_id, organization_id=organization_id)
            await session.delete(browser_profile)
            await session.commit()

    @db_operation("update_browser_profile")
    async def update_browser_profile(
        self,
        profile_id: str,
        organization_id: str,
        name: str | None = None,
        description: str | None = None,
        proxy_location: ProxyLocationInput | object = _UNSET,
        proxy_session_id: str | None | object = _UNSET,
        rotate_proxy_session_id: bool = False,
    ) -> BrowserProfile:
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

            if name is not None:
                browser_profile.name = name
            if description is not None:
                browser_profile.description = description
            apply_proxy_pin_to_model(
                browser_profile,
                entity_id=profile_id,
                proxy_location=proxy_location,
                proxy_session_id=proxy_session_id,
                unset=_UNSET,
                rotate_proxy_session_id=rotate_proxy_session_id,
            )

            await session.commit()
            await session.refresh(browser_profile)
            return BrowserProfile.model_validate(browser_profile)

    @db_operation("touch_browser_profile")
    async def touch_browser_profile(self, profile_id: str, organization_id: str) -> None:
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
            browser_profile.modified_at = naive_utc_now()
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
                .filter(PersistentBrowserSessionModel.created_at > naive_utc_now() - timedelta(hours=active_hours))
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
                .filter(PersistentBrowserSessionModel.created_at > (naive_utc_now() - timedelta(hours=lookback_hours)))
                .order_by(
                    open_first.asc(),  # open sessions first
                    PersistentBrowserSessionModel.created_at.desc(),  # then newest within each group
                )
                .offset((page - 1) * page_size)
                .limit(page_size)
            )
            sessions = result.scalars().all()
            return [PersistentBrowserSession.model_validate(session) for session in sessions]

    @db_operation("get_persistent_browser_sessions_history_count")
    async def get_persistent_browser_sessions_history_count(
        self,
        organization_id: str,
        lookback_hours: int = 24 * 7,
    ) -> int:
        """Count persistent browser sessions in an organization's history window.

        Mirrors the filters of :meth:`get_persistent_browser_sessions_history` so the
        total matches what the paginated read returns.
        """
        async with self.Session() as session:
            count_query = (
                select(func.count())
                .select_from(PersistentBrowserSessionModel)
                .filter_by(organization_id=organization_id)
                .filter_by(deleted_at=None)
                .filter(PersistentBrowserSessionModel.created_at > (naive_utc_now() - timedelta(hours=lookback_hours)))
            )
            return (await session.execute(count_query)).scalar_one()

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
            query = (
                select(PersistentBrowserSessionModel)
                .filter_by(persistent_browser_session_id=session_id)
                .filter_by(deleted_at=None)
            )
            if organization_id is not None or settings.ENV != "local":
                if organization_id is None:
                    raise ValueError("organization_id is required outside local development")
                query = query.filter_by(organization_id=organization_id)
            persistent_browser_session = (await session.scalars(query)).first()
            if persistent_browser_session:
                return PersistentBrowserSession.model_validate(persistent_browser_session)
            return None

    @db_operation("create_imported_persistent_browser_session")
    async def create_imported_persistent_browser_session(
        self,
        *,
        session_id: str,
        organization_id: str,
        started_at: datetime | None = None,
        completed_at: datetime | None = None,
    ) -> PersistentBrowserSession:
        """Create an inert, already-completed session row for externally recorded runs."""
        async with self.Session() as session:
            browser_session = PersistentBrowserSessionModel(
                persistent_browser_session_id=session_id,
                organization_id=organization_id,
                status="completed",
                started_at=to_naive_utc(started_at) if started_at else None,
                completed_at=to_naive_utc(completed_at) if completed_at else naive_utc_now(),
            )
            session.add(browser_session)
            await session.commit()
            await session.refresh(browser_session)
            return PersistentBrowserSession.model_validate(browser_session)

    @db_operation("get_persistent_browser_session_unscoped")
    async def get_persistent_browser_session_unscoped(self, session_id: str) -> PersistentBrowserSession | None:
        """Primary-key read without organization scoping, for trusted internal session
        resolution (e.g. the CDP proxy) that learns the owning organization from the row."""
        async with self.Session() as session:
            query = (
                select(PersistentBrowserSessionModel)
                .filter_by(persistent_browser_session_id=session_id)
                .filter_by(deleted_at=None)
            )
            persistent_browser_session = (await session.scalars(query)).first()
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
        proxy_session_id: str | None = None,
        extensions: list[Extensions] | None = None,
        browser_type: PersistentBrowserType | None = None,
        browser_profile_id: str | None = None,
        generate_browser_profile: bool = False,
        inherit_profile_proxy: bool = False,
    ) -> PersistentBrowserSession:
        """Create a new persistent browser session."""
        extensions_str: list[str] | None = (
            [extension.value for extension in extensions] if extensions is not None else None
        )
        async with self.Session() as session:
            if inherit_profile_proxy and browser_profile_id and proxy_session_id is None:
                query = (
                    select(BrowserProfileModel)
                    .filter_by(browser_profile_id=browser_profile_id)
                    .filter_by(organization_id=organization_id)
                    .filter(BrowserProfileModel.deleted_at.is_(None))
                )
                browser_profile = (await session.scalars(query)).first()
                if browser_profile and browser_profile.proxy_session_id:
                    proxy_session_id = browser_profile.proxy_session_id
                    # The ORM column stores the serialized string; deserialize before it flows
                    # into serialize_proxy_location, which rejects a bare str.
                    proxy_location = cast(
                        ProxyLocationInput, parse_proxy_location_input(browser_profile.proxy_location)
                    )

            proxy_location, proxy_session_id = normalize_proxy_pin_for_create(
                proxy_location=proxy_location,
                proxy_session_id=proxy_session_id,
            )
            serialized_proxy_location = serialize_proxy_location(proxy_location)
            browser_session = PersistentBrowserSessionModel(
                organization_id=organization_id,
                runnable_type=runnable_type,
                runnable_id=runnable_id,
                timeout_minutes=timeout_minutes,
                proxy_location=serialized_proxy_location,
                proxy_session_id=proxy_session_id,
                extensions=extensions_str,
                browser_type=browser_type.value if browser_type else None,
                browser_profile_id=browser_profile_id,
                generate_browser_profile=generate_browser_profile,
            )
            session.add(browser_session)
            await session.flush()
            if (
                serialized_proxy_location == ProxyLocation.RESIDENTIAL_ISP.value
                and browser_session.proxy_session_id is None
            ):
                browser_session.proxy_session_id = generate_proxy_session_id(
                    browser_session.persistent_browser_session_id
                )
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
        generate_browser_profile: bool | None = None,
        browser_profile_loaded: bool | None = None,
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
                persistent_browser_session.completed_at = to_naive_utc(completed_at)
            if started_at:
                persistent_browser_session.started_at = to_naive_utc(started_at)
            if generate_browser_profile is not None:
                persistent_browser_session.generate_browser_profile = generate_browser_profile
            if browser_profile_loaded is not None:
                persistent_browser_session.browser_profile_loaded = browser_profile_loaded

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
        upstream_cdp_url: str | None = None,
        browser_vendor: str | None = None,
    ) -> None:
        """Set the browser address for a persistent browser session.

        browser_address is the client-facing (proxied) URL; upstream_cdp_url is the endpoint the
        CDP proxy dials and must never be handed to a client or carry a credential — connect-time
        credentials are injected from env at dial time.
        """
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
                    persistent_browser_session.started_at = naive_utc_now()
                if ip_address:
                    persistent_browser_session.ip_address = ip_address
                if ecs_task_arn:
                    persistent_browser_session.ecs_task_arn = ecs_task_arn
                if upstream_cdp_url:
                    persistent_browser_session.upstream_cdp_url = upstream_cdp_url
                if browser_vendor:
                    persistent_browser_session.browser_vendor = browser_vendor
                try:
                    await session.commit()
                except StatementError as exc:
                    # A failed statement renders its bound parameters — including upstream_cdp_url —
                    # into the text that callers log. The type and statement still identify the fault.
                    exc.hide_parameters = True
                    raise
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
                persistent_browser_session.deleted_at = naive_utc_now()
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
                persistent_browser_session.completed_at = naive_utc_now()
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
