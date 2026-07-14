from __future__ import annotations

import asyncio
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from math import floor
from pathlib import Path

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
from skyvern.webeye.async_utils import await_to_terminal_state
from skyvern.webeye.browser_state import BrowserState
from skyvern.webeye.cdp_ports import _release_cdp_port
from skyvern.webeye.persistent_sessions_manager import PersistentSessionsManager
from skyvern.webeye.real_browser_manager import RealBrowserManager
from skyvern.webeye.session_cookies import persist_session_cookies
from skyvern.webeye.vnc_manager import VncManager, VncStartupError, VncTeardownError

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
    _session_locks: dict[str, asyncio.Lock] = {}
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

    def requires_local_vnc_display(self) -> bool:
        """Whether this manager owns a local X display for each browser session."""

        return settings.BROWSER_STREAMING_MODE == "vnc"

    def owns_local_vnc_stack(
        self,
        *,
        session_id: str,
        organization_id: str,
        display_number: int,
        vnc_port: int,
    ) -> bool:
        """Return whether this OSS process owns the session's exact ready VNC stack."""

        return self.requires_local_vnc_display() and VncManager.owns_ready_stack(
            session_id,
            organization_id=organization_id,
            display_number=display_number,
            vnc_port=vnc_port,
        )

    def _get_session_lock(self, session_id: str) -> asyncio.Lock:
        # Locks intentionally live for the manager lifetime so waiters can never
        # retain a different lock for the same session.
        return self._session_locks.setdefault(session_id, asyncio.Lock())

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
        if (
            browser_session is not None
            and self.requires_local_vnc_display()
            and organization_id is not None
            and browser_session.organization_id != organization_id
        ):
            LOG.warning(
                "Skipping cached browser state lookup for organization mismatch",
                session_id=session_id,
                organization_id=organization_id,
                cached_organization_id=browser_session.organization_id,
            )
            return None
        return browser_session.browser_state if browser_session else None

    async def set_browser_state(
        self, session_id: str, browser_state: BrowserState, organization_id: str | None = None
    ) -> None:
        if self.requires_local_vnc_display():
            if organization_id is None:
                await self._close_rejected_browser_state(session_id, organization_id, browser_state)
                raise VncStartupError(
                    f"Persistent browser session {session_id} cannot register a VNC browser without an organization"
                )
            await self.compare_and_install_browser_state(session_id, browser_state, organization_id)
            return

        browser_session = BrowserSession(browser_state=browser_state, organization_id=organization_id)
        self._browser_sessions[session_id] = browser_session

    async def compare_and_install_browser_state(
        self,
        session_id: str,
        browser_state: BrowserState,
        organization_id: str,
    ) -> BrowserState:
        """Atomically install a local-VNC browser or return the existing winner."""

        if not self.requires_local_vnc_display():
            raise VncStartupError("Compare-and-install is only available for local VNC browser sessions")

        async with self._get_session_lock(session_id):
            winner = await self._compare_and_install_browser_state_locked(
                session_id,
                browser_state,
                organization_id,
            )
            if winner is None:
                await self._close_rejected_browser_state(session_id, organization_id, browser_state)
                raise BrowserSessionClosed(session_id)
            if winner is not browser_state:
                await self._close_rejected_browser_state(session_id, organization_id, browser_state)
            return winner

    async def _compare_and_install_browser_state_locked(
        self,
        session_id: str,
        browser_state: BrowserState,
        organization_id: str,
    ) -> BrowserState | None:
        """Compare and install while the caller owns ``session_id``'s VNC lock."""

        session = await self.get_session(session_id, organization_id)
        if session is None or is_final_status(session.status) or session.completed_at is not None:
            return None

        cached = self._browser_sessions.get(session_id)
        if cached is not None:
            if cached.organization_id != organization_id:
                LOG.warning(
                    "Rejecting VNC browser registration for organization mismatch",
                    browser_session_id=session_id,
                    organization_id=organization_id,
                    cached_organization_id=cached.organization_id,
                )
                return None
            return cached.browser_state

        self._browser_sessions[session_id] = BrowserSession(
            browser_state=browser_state,
            organization_id=organization_id,
        )
        return browser_state

    async def _close_rejected_browser_state(
        self,
        session_id: str,
        organization_id: str | None,
        browser_state: BrowserState,
    ) -> None:
        try:
            await await_to_terminal_state(browser_state.close())
        except TargetClosedError:
            pass
        except Exception:
            LOG.warning(
                "Failed to close rejected browser without affecting the winning session",
                browser_session_id=session_id,
                organization_id=organization_id,
                exc_info=True,
            )

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

    async def _stop_vnc_safely(self, session_id: str, organization_id: str) -> bool:
        try:
            await VncManager.stop_vnc_for_session(session_id, organization_id=organization_id)
            return True
        except Exception:
            LOG.warning(
                "Failed to stop VNC process stack",
                browser_session_id=session_id,
                organization_id=organization_id,
                exc_info=True,
            )
            return False

    async def _finalize_failed_vnc_session(self, session_id: str, organization_id: str) -> None:
        if not await self._stop_vnc_safely(session_id, organization_id):
            return
        try:
            await self.update_status(session_id, organization_id, PersistentBrowserSessionStatus.failed)
        except Exception:
            LOG.warning(
                "Failed to finalize browser session after VNC startup error",
                browser_session_id=session_id,
                organization_id=organization_id,
                exc_info=True,
            )

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

        session_id = session.persistent_browser_session_id
        if settings.BROWSER_STREAMING_MODE == "vnc":
            try:
                display_number, vnc_port = await VncManager.start_vnc_for_session(
                    session_id,
                    organization_id=organization_id,
                )
                session = await self.database.browser_sessions.update_persistent_browser_session(
                    session_id,
                    organization_id=organization_id,
                    display_number=display_number,
                    vnc_port=vnc_port,
                )
            except BaseException as startup_error:
                try:
                    await await_to_terminal_state(self._finalize_failed_vnc_session(session_id, organization_id))
                except BaseException as cleanup_error:
                    LOG.warning(
                        "VNC creation failure cleanup did not complete cleanly",
                        browser_session_id=session_id,
                        organization_id=organization_id,
                        exc_info=True,
                    )
                    startup_error.add_note(f"VNC creation cleanup error: {cleanup_error!r}")
                raise

        # Launch the browser immediately for standalone sessions so streaming/input
        # endpoints can connect. CDP and VNC stream local in-process browsers;
        # cdp-connect still needs a registered BrowserState for its remote browser.
        should_launch = settings.BROWSER_STREAMING_MODE in {"cdp", "vnc"} or settings.BROWSER_TYPE == "cdp-connect"
        if should_launch and runnable_id is None:
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
        if self.requires_local_vnc_display():
            async with self._get_session_lock(session_id):
                await self._launch_browser_for_session_locked(session_id, organization_id, proxy_location, url)
            return
        await self._launch_browser_for_session_locked(session_id, organization_id, proxy_location, url)

    async def _launch_browser_for_session_locked(
        self,
        session_id: str,
        organization_id: str,
        proxy_location: ProxyLocationInput | None = None,
        url: str | None = None,
    ) -> None:
        if session_id in self._browser_sessions:
            LOG.info("Session already has browser state, skipping duplicate launch", browser_session_id=session_id)
            return

        browser_state: BrowserState | None = None
        duplicate_loser = False
        requires_local_display = self.requires_local_vnc_display()
        try:
            session = await self.get_session(session_id, organization_id)
            if session is None or is_final_status(session.status) or session.completed_at is not None:
                LOG.info(
                    "Session closed before browser launch, skipping browser launch",
                    browser_session_id=session_id,
                )
                if requires_local_display:
                    await self._stop_vnc_safely(session_id, organization_id)
                return

            launch_proxy_location = session.proxy_location if session.proxy_location is not None else proxy_location
            extra_http_headers = app.AGENT_FUNCTION.build_proxy_session_extra_http_headers(session.proxy_session_id)
            if requires_local_display:
                if session.display_number is None:
                    raise VncStartupError(f"Persistent browser session {session_id} has no assigned VNC display")
                browser_state = await RealBrowserManager._create_browser_state(
                    proxy_location=launch_proxy_location,
                    url=url,
                    organization_id=organization_id,
                    extra_http_headers=extra_http_headers,
                    browser_profile_id=session.browser_profile_id,
                    display_number=session.display_number,
                )
            else:
                browser_state = await RealBrowserManager._create_browser_state(
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

            if requires_local_display:
                winner = await self._compare_and_install_browser_state_locked(
                    session_id,
                    browser_state,
                    organization_id,
                )
                if winner is None:
                    LOG.info(
                        "Session closed during browser launch, discarding browser",
                        browser_session_id=session_id,
                    )
                    await await_to_terminal_state(
                        self._close_browser_then_vnc(browser_state, session_id, organization_id)
                    )
                    return
                if winner is not browser_state:
                    LOG.info(
                        "Session already has browser state, discarding duplicate",
                        browser_session_id=session_id,
                    )
                    duplicate_loser = True
                    await self._close_rejected_browser_state(session_id, organization_id, browser_state)
                    return
            else:
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
                if requires_local_display:
                    raise VncStartupError(f"Persistent browser session {session_id} closed before launch completed")
                cached = self._browser_sessions.get(session_id)
                if cached is not None and cached.browser_state is browser_state:
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
        except BaseException as launch_error:
            if duplicate_loser:
                raise
            if isinstance(launch_error, Exception):
                LOG.exception(
                    "Failed to launch browser for standalone session",
                    browser_session_id=session_id,
                    organization_id=organization_id,
                )
            else:
                LOG.warning(
                    "Standalone browser launch interrupted by control flow",
                    browser_session_id=session_id,
                    organization_id=organization_id,
                    error_type=type(launch_error).__name__,
                )

            cleanup_control_flow: BaseException | None = None
            if requires_local_display:
                try:
                    await await_to_terminal_state(
                        self._cleanup_failed_vnc_launch(session_id, organization_id, browser_state)
                    )
                except asyncio.CancelledError as cleanup_cancellation:
                    cleanup_control_flow = cleanup_cancellation
                except Exception:
                    LOG.warning(
                        "Failed to clean up standalone VNC launch",
                        browser_session_id=session_id,
                        organization_id=organization_id,
                        exc_info=True,
                    )
                except BaseException as cleanup_error:
                    cleanup_control_flow = cleanup_error

            if not isinstance(launch_error, Exception):
                raise
            if cleanup_control_flow is not None:
                raise cleanup_control_flow from launch_error

    async def _close_browser_then_vnc(
        self,
        browser_state: BrowserState,
        session_id: str,
        organization_id: str,
    ) -> None:
        close_error: BaseException | None = None
        try:
            await browser_state.close()
        except TargetClosedError:
            pass
        except BaseException as error:
            close_error = error
            LOG.warning(
                "Failed to close discarded browser",
                browser_session_id=session_id,
                organization_id=organization_id,
                exc_info=True,
            )
        if self.requires_local_vnc_display():
            await VncManager.stop_vnc_for_session(session_id, organization_id=organization_id)
        if close_error is not None:
            raise close_error

    async def _cleanup_failed_vnc_launch(
        self,
        session_id: str,
        organization_id: str,
        browser_state: BrowserState | None,
    ) -> None:
        browser_control_flow: BaseException | None = None
        if browser_state is not None:
            try:
                await browser_state.close()
            except TargetClosedError:
                pass
            except Exception:
                LOG.warning(
                    "Failed to close browser after standalone VNC launch error",
                    browser_session_id=session_id,
                    organization_id=organization_id,
                    exc_info=True,
                )
            except BaseException as error:
                browser_control_flow = error

        cached = self._browser_sessions.get(session_id)
        another_browser_won = cached is not None and cached.browser_state is not browser_state
        if not another_browser_won:
            stopped = await self._stop_vnc_safely(session_id, organization_id)
            if stopped:
                if cached is not None and cached.browser_state is browser_state:
                    self._browser_sessions.pop(session_id, None)
                try:
                    await self.update_status(session_id, organization_id, PersistentBrowserSessionStatus.failed)
                except Exception:
                    LOG.warning(
                        "Failed to finalize browser session after VNC launch error",
                        browser_session_id=session_id,
                        organization_id=organization_id,
                        exc_info=True,
                    )
        if browser_control_flow is not None:
            raise browser_control_flow

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

    async def close_session(self, organization_id: str, browser_session_id: str) -> None:
        if self.requires_local_vnc_display():
            async with self._get_session_lock(browser_session_id):
                await await_to_terminal_state(self._close_session_impl(organization_id, browser_session_id))
            return
        await self._close_session_impl(organization_id, browser_session_id)

    async def _close_session_impl(self, organization_id: str, browser_session_id: str) -> None:
        """Close a specific browser session."""
        requires_local_display = self.requires_local_vnc_display()
        browser_session = self._browser_sessions.get(browser_session_id)
        organization_mismatch = False
        if browser_session and browser_session.organization_id != organization_id:
            LOG.warning(
                "Skipping in-memory browser session close for organization mismatch",
                organization_id=organization_id,
                cached_organization_id=browser_session.organization_id,
                session_id=browser_session_id,
            )
            browser_session = None
            organization_mismatch = True
        if browser_session:
            browser_artifacts = browser_session.browser_state.browser_artifacts
            browser_close_attempted = False
            vnc_stopped = False
            vnc_stop_failed = False
            try:
                LOG.info(
                    "Closing browser session",
                    organization_id=organization_id,
                    session_id=browser_session_id,
                )
                # Export session profile before closing (so it can be used to create browser profiles)
                if browser_artifacts and browser_artifacts.browser_session_dir:
                    # Snapshot session cookies before store_browser_profile copies the dir; the later
                    # close() persist runs after this export, too late to land in the archive. The
                    # snapshot stays unconditional even when the export is skipped so the sidecar is
                    # ready if the session opted in.
                    await persist_session_cookies(
                        browser_session.browser_state.browser_context,
                        browser_artifacts.browser_session_dir,
                    )
                    # Re-read the persisted row so an update-while-alive opt-in toggle is honored.
                    # Isolated and fail-open: a lookup error must neither block the close cleanup below
                    # nor drop an opted-in / profile-reuse session's profile, so skip only on a confirmed
                    # opted-out row.
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

                browser_close_attempted = True
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

                if requires_local_display:
                    vnc_stopped = await self._stop_vnc_safely(browser_session_id, organization_id)
                    if not vnc_stopped:
                        vnc_stop_failed = True
                        raise VncTeardownError(
                            browser_session_id,
                            errors=("VNC stack remains tracked after close",),
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

                if not requires_local_display:
                    cached = self._browser_sessions.get(browser_session_id)
                    if cached is browser_session:
                        self._browser_sessions.pop(browser_session_id, None)
                        if browser_session.cdp_port is not None:
                            _release_cdp_port(browser_session.cdp_port)
            finally:
                # Profile/cookie export errors must not leave Chromium running over a live X server.
                if not browser_close_attempted and requires_local_display:
                    browser_close_attempted = True
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
                if requires_local_display and not vnc_stopped:
                    vnc_stopped = await self._stop_vnc_safely(browser_session_id, organization_id)
                if browser_close_attempted and requires_local_display and vnc_stopped and not vnc_stop_failed:
                    cached = self._browser_sessions.get(browser_session_id)
                    if cached is browser_session:
                        self._browser_sessions.pop(browser_session_id, None)
                if requires_local_display and not vnc_stopped:
                    raise VncTeardownError(
                        browser_session_id,
                        errors=("VNC stack remains tracked after close",),
                    )
        else:
            LOG.info(
                "Browser session not found in memory, marking as deleted in database",
                organization_id=organization_id,
                session_id=browser_session_id,
            )
            if requires_local_display and not organization_mismatch:
                if not await self._stop_vnc_safely(browser_session_id, organization_id):
                    raise VncTeardownError(
                        browser_session_id,
                        errors=("VNC stack remains tracked after close",),
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
            settings.BROWSER_STREAMING_MODE in {"cdp", "vnc"} or settings.BROWSER_TYPE == "cdp-connect"
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
            owns_vnc_stack = settings.BROWSER_STREAMING_MODE == "vnc" and VncManager.has_session(
                db_session.persistent_browser_session_id
            )
            if db_session.persistent_browser_session_id not in self._browser_sessions and not owns_vnc_stack:
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

    @classmethod
    async def close(cls) -> None:
        """Close all browser sessions across all organizations."""
        LOG.info("Closing PersistentSessionsManager")
        try:
            if cls.instance:
                # Deployment boundary: multi-worker VNC is unsupported. This DB-wide
                # shutdown can finalize a peer worker's row, while VncManager can stop
                # only stacks tracked by this process; cross-worker enforcement is deferred.
                active_sessions = (
                    await cls.instance.database.browser_sessions.get_all_active_persistent_browser_sessions()
                )
                for db_session in active_sessions:
                    await cls.instance.close_session(
                        db_session.organization_id, db_session.persistent_browser_session_id
                    )
        finally:
            if settings.BROWSER_STREAMING_MODE == "vnc":
                await VncManager.stop_all()
        LOG.info("PersistentSessionsManager is closed")
