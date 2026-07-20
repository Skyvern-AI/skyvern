from __future__ import annotations

import asyncio
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from math import floor
from pathlib import Path
from typing import cast

import structlog
from playwright._impl._errors import TargetClosedError

from skyvern.cli.core.session_manager import active_copilot_session_ids
from skyvern.config import settings
from skyvern.exceptions import BrowserSessionClosed, BrowserSessionNotRenewable, MissingBrowserAddressError
from skyvern.forge import app
from skyvern.forge.sdk.db.agent_db import AgentDB
from skyvern.forge.sdk.db.polls import wait_on_persistent_browser_address
from skyvern.forge.sdk.schemas.persistent_browser_sessions import (
    Extensions,
    PersistentBrowserSession,
    PersistentBrowserSessionStatus,
    PersistentBrowserType,
    is_final_status,
)
from skyvern.schemas.run_enums import RunType
from skyvern.schemas.runs import ProxyLocation, ProxyLocationInput
from skyvern.webeye.browser_state import BrowserState
from skyvern.webeye.cdp_ports import _release_cdp_port
from skyvern.webeye.persistent_sessions_manager import PersistentSessionsManager
from skyvern.webeye.real_browser_manager import RealBrowserManager
from skyvern.webeye.session_cookies import persist_session_cookies

LOG = structlog.get_logger()

# Grace margin past a session's timeout before reaping, so a reap can't race an in-flight renewal.
REAP_GRACE_SECONDS = 120


@dataclass
class BrowserSession:
    browser_state: BrowserState
    organization_id: str | None = None
    cdp_port: int | None = None


async def validate_session_for_renewal(
    database: AgentDB,
    session_id: str,
    organization_id: str,
) -> tuple[PersistentBrowserSession, datetime, int]:
    """
    Validate a specific browser session for renewal. Otherwise raise.
    """

    browser_session = await database.browser_sessions.get_persistent_browser_session(
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

        browser_session = await database.browser_sessions.update_persistent_browser_session(
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
    persistent_browser_session = await db.browser_sessions.get_persistent_browser_session(session_id, organization_id)

    if not persistent_browser_session:
        LOG.warning(
            "Cannot update browser session status, browser session not found in database",
            organization_id=organization_id,
            session_id=session_id,
            desired_status=status,
        )
        return None

    if is_final_status(persistent_browser_session.status):
        LOG.warning(
            "Attempted to update browser session status when it is already final",
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

    completed_at = datetime.now(timezone.utc) if is_final_status(status) else None
    persistent_browser_session = await db.browser_sessions.update_persistent_browser_session(
        session_id,
        status=status,
        organization_id=organization_id,
        completed_at=completed_at,
    )

    return persistent_browser_session


class DefaultPersistentSessionsManager(PersistentSessionsManager):
    """Default (OSS) implementation of PersistentSessionsManager protocol."""

    instance: DefaultPersistentSessionsManager | None = None
    _browser_sessions: dict[str, BrowserSession] = dict()
    _background_tasks: set[asyncio.Task[None]] = set()
    _reaper_task: asyncio.Task[None] | None = None
    database: AgentDB

    def __new__(cls, database: AgentDB) -> DefaultPersistentSessionsManager:
        if cls.instance is None:
            new_instance = super().__new__(cls)
            cls.instance = new_instance
            cls.instance.database = database
            return new_instance

        cls.instance.database = database
        return cls.instance

    def watch_session_pool(self) -> None:
        """No-op in OSS: browsers run in-process, no external pool to monitor."""

    def can_probe_registered_browser_state(self) -> bool:
        return True

    def supports_evict_and_reconnect(self) -> bool:
        # ``get_browser_state`` is a pure dict lookup against ``_browser_sessions``; it
        # does not reconnect after eviction. Callers must not run the evict-and-reconnect
        # recovery against this manager — the evict closes the only cached BrowserState
        # and leaves the session uncacheable for the rest of its lifetime, which also
        # breaks profile/video cleanup at ``close_session``.
        return False

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

        persistent_browser_session = await self.database.browser_sessions.get_persistent_browser_session(
            browser_session_id, organization_id
        )

        if persistent_browser_session is None:
            raise Exception(f"Persistent browser session not found for {browser_session_id}")

        if is_final_status(persistent_browser_session.status):
            raise BrowserSessionClosed(browser_session_id)

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

    async def get_browser_address_if_ready(
        self,
        session_id: str,
        organization_id: str,
        *,
        timeout: float = 0.0,
        poll_interval: float = 0.25,
    ) -> str | None:
        browser_session = await self.database.browser_sessions.get_persistent_browser_session(
            session_id, organization_id
        )
        if browser_session is None or is_final_status(browser_session.status):
            return None
        if browser_session.browser_address:
            return browser_session.browser_address
        if timeout <= 0:
            return None
        return await wait_on_persistent_browser_address(
            self.database,
            session_id,
            organization_id,
            timeout=max(1, int(timeout)),
            poll_interval=poll_interval,
        )

    async def get_session_by_runnable_id(
        self, runnable_id: str, organization_id: str
    ) -> PersistentBrowserSession | None:
        """Get a specific browser session by runnable ID."""
        return await self.database.browser_sessions.get_persistent_browser_session_by_runnable_id(
            runnable_id, organization_id
        )

    async def get_active_sessions(self, organization_id: str) -> list[PersistentBrowserSession]:
        """Get all active sessions for an organization."""
        return await self.database.browser_sessions.get_active_persistent_browser_sessions(organization_id)

    async def get_browser_state(self, session_id: str, organization_id: str | None = None) -> BrowserState | None:
        """Get a specific browser session's state by session ID."""
        browser_session = self._browser_sessions.get(session_id)
        return browser_session.browser_state if browser_session else None

    async def set_browser_state(
        self, session_id: str, browser_state: BrowserState, organization_id: str | None = None
    ) -> None:
        browser_session = BrowserSession(browser_state=browser_state, organization_id=organization_id)
        self._browser_sessions[session_id] = browser_session

    async def evict_cached_browser_state(
        self,
        session_id: str,
        organization_id: str | None = None,
        expected: BrowserState | None = None,
    ) -> None:
        cached = self._browser_sessions.get(session_id)
        if cached is None:
            return
        if expected is not None and cached.browser_state is not expected:
            return
        self._browser_sessions.pop(session_id, None)
        try:
            await cached.browser_state.close()
        except TargetClosedError:
            LOG.info(
                "Browser context already closed during evict",
                session_id=session_id,
            )
        except Exception:
            LOG.warning(
                "Error while closing evicted browser session",
                session_id=session_id,
                exc_info=True,
            )

    async def get_session(self, session_id: str, organization_id: str) -> PersistentBrowserSession | None:
        """Get a specific browser session by session ID."""
        return await self.database.browser_sessions.get_persistent_browser_session(session_id, organization_id)

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
        proxy_session_id: str | None = None,
        is_high_priority: bool = False,
        browser_profile_id: str | None = None,
        generate_browser_profile: bool = False,
        inherit_profile_proxy: bool = False,
        wait_for_startup: bool = True,
    ) -> PersistentBrowserSession:
        """Create a new browser session for an organization and return its ID with the browser state."""
        LOG.info(
            "Creating new browser session",
            organization_id=organization_id,
        )
        session = await self.database.browser_sessions.create_persistent_browser_session(
            organization_id=organization_id,
            runnable_type=runnable_type,
            runnable_id=runnable_id,
            timeout_minutes=timeout_minutes,
            proxy_location=proxy_location,
            proxy_session_id=proxy_session_id,
            extensions=extensions,
            browser_type=browser_type,
            browser_profile_id=browser_profile_id,
            generate_browser_profile=generate_browser_profile,
            inherit_profile_proxy=inherit_profile_proxy,
        )

        # Launch the browser immediately for standalone sessions so the
        # screencast/CDP input endpoints can connect. Triggered both by the
        # in-process CDP streaming mode and by cdp-connect, which forwards to a
        # remote browser and still needs a registered BrowserState locally.
        should_launch = settings.BROWSER_STREAMING_MODE == "cdp" or settings.BROWSER_TYPE == "cdp-connect"
        if should_launch and runnable_id is None:
            session_id = session.persistent_browser_session_id
            task = asyncio.create_task(
                self._launch_browser_for_session(session_id, organization_id, proxy_location, url)
            )
            self._background_tasks.add(task)
            task.add_done_callback(self._background_tasks.discard)

        return session

    async def _launch_browser_for_session(
        self,
        session_id: str,
        organization_id: str,
        proxy_location: ProxyLocationInput | None = None,
        url: str | None = None,
    ) -> None:
        try:
            session = await self.get_session(session_id, organization_id)
            if session is None or is_final_status(session.status) or session.completed_at is not None:
                LOG.info(
                    "Session closed before browser launch, skipping browser launch",
                    browser_session_id=session_id,
                )
                return

            launch_proxy_location = session.proxy_location if session.proxy_location is not None else proxy_location
            extra_http_headers = app.AGENT_FUNCTION.build_proxy_session_extra_http_headers(session.proxy_session_id)

            browser_state = await cast(RealBrowserManager, app.BROWSER_MANAGER)._create_browser_state(
                proxy_location=launch_proxy_location,
                url=url,
                organization_id=organization_id,
                extra_http_headers=extra_http_headers,
                browser_profile_id=session.browser_profile_id,
            )
            await browser_state.get_or_create_page(
                url=url or "about:blank",
                proxy_location=launch_proxy_location,
                organization_id=organization_id,
                extra_http_headers=extra_http_headers,
            )

            session = await self.get_session(session_id, organization_id)
            if session is None or is_final_status(session.status) or session.completed_at is not None:
                LOG.info(
                    "Session closed during browser launch, discarding browser",
                    browser_session_id=session_id,
                )
                await browser_state.close()
                return

            if session_id in self._browser_sessions:
                LOG.info(
                    "Session already has browser state, discarding duplicate",
                    browser_session_id=session_id,
                )
                await browser_state.close()
                return

            self._browser_sessions[session_id] = BrowserSession(
                browser_state=browser_state,
                organization_id=organization_id,
            )

            result = await self.update_status(session_id, organization_id, PersistentBrowserSessionStatus.running)
            if result is None:
                self._browser_sessions.pop(session_id, None)
                await browser_state.close()
                return
            # Set started_at so renewal knows the browser is live
            await self.database.browser_sessions.update_persistent_browser_session(
                session_id,
                organization_id=organization_id,
                started_at=datetime.now(timezone.utc),
            )
            LOG.info(
                "Browser launched for standalone session",
                browser_session_id=session_id,
                organization_id=organization_id,
            )
        except Exception:
            LOG.exception(
                "Failed to launch browser for standalone session",
                browser_session_id=session_id,
                organization_id=organization_id,
            )

    async def occupy_browser_session(
        self,
        session_id: str,
        runnable_type: str,
        runnable_id: str,
        organization_id: str,
    ) -> None:
        """Occupy a specific browser session."""
        await self.database.browser_sessions.occupy_persistent_browser_session(
            session_id=session_id,
            runnable_type=runnable_type,
            runnable_id=runnable_id,
            organization_id=organization_id,
        )

    async def renew_or_close_session(self, session_id: str, organization_id: str) -> PersistentBrowserSession:
        try:
            return await renew_session(self.database, session_id, organization_id)
        except BrowserSessionNotRenewable:
            session = await self.get_session(session_id, organization_id)
            # Don't close sessions that haven't started yet (browser still launching)
            # unless they're stuck (older than 120s)
            if session is not None and session.started_at is None and session.completed_at is None:
                created_at_utc = (
                    session.created_at.replace(tzinfo=timezone.utc)
                    if session.created_at.tzinfo is None
                    else session.created_at
                )
                age_seconds = (datetime.now(timezone.utc) - created_at_utc).total_seconds()
                if age_seconds < 120:
                    raise
            # Session doesn't exist, has started, is completed, or is stuck — close it
            if session is None or session.completed_at is None:
                await self.close_session(organization_id, session_id)
            raise

    async def update_status(
        self, session_id: str, organization_id: str, status: str
    ) -> PersistentBrowserSession | None:
        return await update_status(self.database, session_id, organization_id, status)

    async def release_browser_session(self, session_id: str, organization_id: str) -> None:
        """Release a specific browser session."""
        await self.database.browser_sessions.release_persistent_browser_session(session_id, organization_id)

    async def _release_local_browser_session(
        self, organization_id: str, browser_session_id: str, *, export_profile: bool | None = None
    ) -> bool:
        """Tear down and drop this process's in-memory BrowserState for a session WITHOUT completing
        the shared DB row. Returns True iff a locally-held state was released.

        Shared by close_session (which completes the DB row afterward) and reconcile_local_sessions,
        whose row is already terminal in the DB — so this path must never close the row itself
        (that would be a redundant write on a completed row, or a NotFoundError on a deleted one).

        export_profile controls whether the profile is persisted at teardown:
        - None (close_session): re-read the row to honor a live opt-in toggle; fail OPEN on a
          missing/errored re-read so a transient blip never drops an opted-in / reuse profile.
        - bool (reconcile): the caller already resolved the opt-in from its own fresh, authoritative
          read of a terminal row, so honor it directly with no re-read — a soft-delete or error
          between the two reads can't flip the verdict into a fail-open export. True exports; False
          skips the snapshot/upload entirely."""
        browser_session = self._browser_sessions.get(browser_session_id)
        if browser_session and browser_session.organization_id != organization_id:
            LOG.warning(
                "Skipping in-memory browser session close for organization mismatch",
                organization_id=organization_id,
                cached_organization_id=browser_session.organization_id,
                session_id=browser_session_id,
            )
            return False
        if not browser_session:
            return False

        LOG.info(
            "Closing browser session",
            organization_id=organization_id,
            session_id=browser_session_id,
        )
        # Export session profile before closing (so it can be used to create browser profiles)
        browser_artifacts = browser_session.browser_state.browser_artifacts
        if export_profile is not False and browser_artifacts and browser_artifacts.browser_session_dir:
            # Export-eligible paths snapshot session cookies before store_browser_profile copies
            # the dir; the later close() persist runs after this export, too late to land in the
            # archive. export_profile=False intentionally skips both the cookie snapshot and the
            # profile upload.
            await persist_session_cookies(
                browser_session.browser_state.browser_context, browser_artifacts.browser_session_dir
            )
            if export_profile is None:
                # close_session: re-read the row to honor an update-while-alive opt-in toggle. Fail
                # open on a missing/errored re-read so a transient blip never drops an opted-in /
                # profile-reuse session's profile.
                should_export = True
                try:
                    persisted_session = await self.database.browser_sessions.get_persistent_browser_session(
                        browser_session_id, organization_id
                    )
                    should_export = persisted_session is None or persisted_session.should_export_profile()
                except Exception:
                    LOG.warning(
                        "Failed to read browser session opt-in flag; exporting profile to be safe",
                        browser_session_id=browser_session_id,
                        organization_id=organization_id,
                        exc_info=True,
                    )
            else:
                # reconcile already resolved the opt-in from its own fresh, authoritative read of a
                # terminal row; honor it directly with no re-read, so a soft-delete or transient error
                # between the two reads can never flip the verdict into a fail-open export.
                should_export = export_profile
            if should_export:
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
            else:
                LOG.info(
                    "Skipping browser profile export; session did not opt into profile generation",
                    browser_session_id=browser_session_id,
                    organization_id=organization_id,
                )

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

        if browser_artifacts and browser_artifacts.video_artifacts:
            for video_artifact in browser_artifacts.video_artifacts:
                if video_artifact.video_path:
                    try:
                        video_path = Path(video_artifact.video_path)
                        if video_path.exists():
                            date = video_path.parent.name
                            await app.STORAGE.sync_browser_session_file(
                                organization_id=organization_id,
                                browser_session_id=browser_session_id,
                                artifact_type="videos",
                                local_file_path=str(video_path),
                                remote_path=video_path.name,
                                date=date,
                            )
                    except Exception:
                        LOG.exception(
                            "Failed to sync video recording",
                            browser_session_id=browser_session_id,
                            organization_id=organization_id,
                            video_path=video_artifact.video_path,
                        )

        self._browser_sessions.pop(browser_session_id, None)
        if browser_session.cdp_port is not None:
            _release_cdp_port(browser_session.cdp_port)
        return True

    async def close_session(self, organization_id: str, browser_session_id: str) -> None:
        """Close a specific browser session."""
        released = await self._release_local_browser_session(organization_id, browser_session_id)
        if not released:
            LOG.info(
                "Browser session not found in memory, marking as deleted in database",
                organization_id=organization_id,
                session_id=browser_session_id,
            )

        await self.database.browser_sessions.close_persistent_browser_session(browser_session_id, organization_id)
        if settings.BROWSER_STREAMING_MODE == "cdp":
            await self.database.browser_sessions.archive_browser_session_address(browser_session_id, organization_id)

    async def close_all_sessions(self, organization_id: str) -> None:
        """Close all browser sessions for an organization."""
        browser_sessions = await self.database.browser_sessions.get_active_persistent_browser_sessions(organization_id)
        for browser_session in browser_sessions:
            await self.close_session(organization_id, browser_session.persistent_browser_session_id)

    async def cleanup_stale_sessions(self) -> None:
        """Close sessions left active by a previous process."""
        if settings.BROWSER_STREAMING_MODE != "cdp":
            return
        stale_sessions = await self.database.browser_sessions.get_uncompleted_persistent_browser_sessions()
        for db_session in stale_sessions:
            LOG.info(
                "Closing stale browser session from previous run",
                session_id=db_session.persistent_browser_session_id,
                organization_id=db_session.organization_id,
            )
            await self.database.browser_sessions.close_persistent_browser_session(
                db_session.persistent_browser_session_id, db_session.organization_id
            )
            await self.database.browser_sessions.archive_browser_session_address(
                db_session.persistent_browser_session_id, db_session.organization_id
            )

    def start_reaper(self) -> None:
        """Start the background loop that closes persistent sessions past their timeout.

        Idempotent; gated to the configs that launch in-process browsers (the same predicate
        create_session uses). Without it, an abandoned session expires in the DB but its in-process
        Chromium + ffmpeg recorder leak, since nothing else closes a session once it stops renewing.
        """
        interval_seconds = settings.PERSISTENT_SESSIONS_REAPER_INTERVAL_SECONDS
        launches_in_process_browsers = (
            settings.BROWSER_STREAMING_MODE == "cdp" or settings.BROWSER_TYPE == "cdp-connect"
        )
        if not launches_in_process_browsers or interval_seconds <= 0:
            return
        if self._reaper_task is not None and not self._reaper_task.done():
            return
        task = asyncio.create_task(self._reap_expired_sessions_loop(interval_seconds))
        self._reaper_task = task
        self._background_tasks.add(task)
        task.add_done_callback(self._background_tasks.discard)
        LOG.info("Persistent browser session reaper started", interval_seconds=interval_seconds)

    async def _reap_expired_sessions_loop(self, interval_seconds: int) -> None:
        while True:
            await asyncio.sleep(interval_seconds)
            try:
                await self.reap_expired_sessions()
            except Exception:
                LOG.exception("Browser session reaper pass failed")
            try:
                await self.reconcile_local_sessions()
            except Exception:
                LOG.exception("Browser session reconcile pass failed")

    async def _owning_run_is_active(self, runnable_id: str, runnable_type: str | None, organization_id: str) -> bool:
        """Whether the run occupying a session is still live, so the reaper must leave teardown to it.

        A run that dies before release_browser_session leaves runnable_id set forever; treating a
        terminal or missing owner as inactive lets the normal timeout+grace gate reclaim the session
        instead of skipping it forever. Owners we can't authoritatively resolve — an unrecognized
        runnable type or a lookup failure — are treated as active, so a reap never races a run we
        can't prove is gone."""
        if runnable_type != RunType.workflow_run:
            return True
        try:
            workflow_run = await self.database.workflow_runs.get_workflow_run(
                workflow_run_id=runnable_id,
                organization_id=organization_id,
            )
        except Exception:
            LOG.warning(
                "Could not resolve owning run for persistent browser session; leaving it protected",
                runnable_id=runnable_id,
                organization_id=organization_id,
                exc_info=True,
            )
            return True
        if workflow_run is None:
            return False
        return not workflow_run.status.is_final()

    async def reap_expired_sessions(self) -> None:
        """Close the sessions this process holds whose timeout (plus grace) has elapsed via
        close_session, which tears down the context (stopping its ffmpeg recorder), syncs the
        recording, and releases the CDP port. One pass; already-completed rows are excluded."""
        now = datetime.now(timezone.utc)
        copilot_session_ids = active_copilot_session_ids()
        sessions = await self.database.browser_sessions.get_uncompleted_persistent_browser_sessions()
        for db_session in sessions:
            # Only reap browsers this process holds; completing another process's row would hide its leak.
            if db_session.persistent_browser_session_id not in self._browser_sessions:
                continue
            # Leave sessions occupied by a still-live run to that run's own teardown. A run that died
            # before releasing occupancy leaves runnable_id set forever, so resolve the owner and let
            # an expired session with a terminal/missing owner fall through to the expiry gate below.
            if db_session.runnable_id is not None and await self._owning_run_is_active(
                db_session.runnable_id, db_session.runnable_type, db_session.organization_id
            ):
                continue
            # Leave sessions an active copilot turn is driving (no runnable_id and not renewed).
            if db_session.persistent_browser_session_id in copilot_session_ids:
                continue
            # Not-yet-started sessions are still launching.
            if db_session.started_at is None or db_session.timeout_minutes is None:
                continue
            started_at_utc = (
                db_session.started_at.replace(tzinfo=timezone.utc)
                if db_session.started_at.tzinfo is None
                else db_session.started_at
            )
            expires_at = started_at_utc + timedelta(minutes=float(db_session.timeout_minutes))
            if now < expires_at + timedelta(seconds=REAP_GRACE_SECONDS):
                continue
            LOG.info(
                "Reaping expired persistent browser session",
                session_id=db_session.persistent_browser_session_id,
                organization_id=db_session.organization_id,
            )
            try:
                await self.close_session(db_session.organization_id, db_session.persistent_browser_session_id)
            except Exception:
                LOG.exception(
                    "Failed to reap expired persistent browser session",
                    session_id=db_session.persistent_browser_session_id,
                    organization_id=db_session.organization_id,
                )

    async def reconcile_local_sessions(self) -> None:
        """Release BrowserStates this process holds whose authoritative shared row is already
        completed/closed (or gone).

        reap_expired_sessions only scans uncompleted rows, so once another replica (or a
        renewal-failure/close-all/shutdown close) completes the shared persistent_browser_sessions
        row, this process's cached BrowserState + Playwright driver are never revisited and leak.
        This pass is bounded to the sessions THIS process holds — no global scan — and fails safe:
        a lookup error, a still-active/renewable row, a row a run still occupies, or an active
        copilot turn all leave the local state for a later pass rather than risk closing a live
        session. The DB row is authoritative for the closed/gone verdict, but the release is
        local-only: the row is already terminal, so completing it again is either redundant or an
        error."""
        copilot_session_ids = active_copilot_session_ids()
        for session_id, cached in list(self._browser_sessions.items()):
            if session_id in copilot_session_ids:
                continue
            organization_id = cached.organization_id
            if organization_id is None:
                # Without a known org we can't do the authoritative org-scoped lookup; fail safe.
                continue
            try:
                db_session = await self.database.browser_sessions.get_persistent_browser_session(
                    session_id, organization_id
                )
            except Exception:
                LOG.warning(
                    "Failed to reconcile worker-local browser session against its shared row; "
                    "leaving it for a later pass",
                    session_id=session_id,
                    organization_id=organization_id,
                    exc_info=True,
                )
                continue
            # A run still occupies this row (runnable_id set): leave teardown to that run's own
            # cleanup — never yank a browser out from under a live task/workflow. But close only sets
            # completed_at/status without clearing runnable_id, and a completed row is invisible to
            # reap_expired_sessions, so resolve the owner the same way the reaper does and skip only a
            # still-live/unknown owner; a terminal/missing owner falls through to reclaim below.
            if (
                db_session is not None
                and db_session.runnable_id is not None
                and await self._owning_run_is_active(
                    db_session.runnable_id, db_session.runnable_type, db_session.organization_id
                )
            ):
                continue
            # Reclaim only on an authoritative terminal verdict: a completed/final row, or a missing
            # row (soft-deleted / gone — get_persistent_browser_session returns None, never on error).
            # A present, non-final, uncompleted row is still active/renewable; leave it to ordinary
            # expiration in reap_expired_sessions.
            if db_session is not None and db_session.completed_at is None and not is_final_status(db_session.status):
                continue
            LOG.info(
                "Reclaiming worker-local browser session whose shared row is completed or gone",
                session_id=session_id,
                organization_id=organization_id,
                row_present=db_session is not None,
            )
            try:
                # Resolve the profile opt-in from THIS authoritative read and pass the verdict, so the
                # release never re-reads: a missing/deleted row (db_session is None) can't confirm the
                # opt-in -> no export; an opted-out present row -> no export; an opted-in present row
                # -> export. This never persists an unknown/opted-out session's data and closes the
                # soft-delete race between reconcile's read and a second in-release read.
                should_export_profile = db_session is not None and db_session.should_export_profile()
                await self._release_local_browser_session(
                    organization_id, session_id, export_profile=should_export_profile
                )
            except Exception:
                LOG.exception(
                    "Failed to reclaim worker-local browser session",
                    session_id=session_id,
                    organization_id=organization_id,
                )

    @classmethod
    async def close(cls) -> None:
        """Close all browser sessions across all organizations."""
        LOG.info("Closing PersistentSessionsManager")
        if cls.instance:
            active_sessions = await cls.instance.database.browser_sessions.get_all_active_persistent_browser_sessions()
            for db_session in active_sessions:
                await cls.instance.close_session(db_session.organization_id, db_session.persistent_browser_session_id)
        LOG.info("PersistentSessionsManager is closed")
