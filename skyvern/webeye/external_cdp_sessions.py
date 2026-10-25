from __future__ import annotations

import asyncio
from urllib.parse import urlsplit

import structlog
from playwright.async_api import Playwright, async_playwright

from skyvern.constants import BROWSER_CLOSE_TIMEOUT
from skyvern.exceptions import (
    BlockedHost,
    BrowserSessionNotRenewable,
    ExternalBrowserEndpointNotEncrypted,
    ExternalBrowserUnavailable,
)
from skyvern.forge.sdk.browser_network_egress_monitor import BrowserNetworkEgressMonitor
from skyvern.forge.sdk.core.http_request_authorization import deny_unenrolled_redirect_hop
from skyvern.forge.sdk.db.repositories.browser_sessions import BrowserSessionsRepository
from skyvern.forge.sdk.encrypt import encryptor
from skyvern.forge.sdk.encrypt.base import EncryptMethod
from skyvern.forge.sdk.schemas.persistent_browser_sessions import (
    EXTERNAL_CDP_BROWSER_VENDOR,
    PersistentBrowserSession,
    is_external_cdp_session_expired,
    is_external_cdp_session_open,
    seconds_until_external_deadline,
)
from skyvern.schemas.browser_session_close import BrowserSessionCloseReason
from skyvern.utils.url_validators import (
    is_allowed_local_browser_host,
    is_tls_or_local_browser_address,
    resolve_fetch_host_ips,
    validate_browser_host,
)
from skyvern.webeye.browser_artifacts import BrowserArtifacts
from skyvern.webeye.cdp_discovery import resolve_websocket_url
from skyvern.webeye.cdp_download_interceptor import CDPDownloadInterceptor, bind_download_interceptor_to_context
from skyvern.webeye.main_world_eval import default_evaluate_to_main_world
from skyvern.webeye.persistent_sessions_manager import PersistentSessionsManager
from skyvern.webeye.real_browser_state import RealBrowserState

LOG = structlog.get_logger()

EXTERNAL_CDP_CONNECT_TIMEOUT_MS = 10_000
EXTERNAL_CDP_CLOSE_POLL_SECONDS = 15.0


async def _close_at_external_deadline(
    manager: PersistentSessionsManager,
    browser_sessions: BrowserSessionsRepository,
    browser_session: PersistentBrowserSession,
    deadlines: dict[str, asyncio.Task[None]],
    runnable_id: str,
    runnable_generation_id: str,
) -> None:
    """Detach the holder at the fixed deadline, within one poll of a close another process wrote to the row, or
    once the row names a different owner than the lease this watcher was armed for."""
    session_id = browser_session.persistent_browser_session_id
    organization_id = browser_session.organization_id
    reason = BrowserSessionCloseReason.expired
    while (remaining := seconds_until_external_deadline(browser_session)) > 0:
        await asyncio.sleep(min(EXTERNAL_CDP_CLOSE_POLL_SECONDS, remaining))
        # The raw row, not manager.get_session, which closes an expired registration inline and would cancel this task.
        try:
            row = await browser_sessions.get_persistent_browser_session(session_id, organization_id)
        except Exception:
            LOG.warning("Failed to re-read a leased external CDP session", session_id=session_id, exc_info=True)
            continue
        if row is None:
            continue
        if row.close_requested_at is not None or not is_external_cdp_session_open(row):
            reason = BrowserSessionCloseReason.user_requested
            break
        if row.runnable_id != runnable_id or row.runnable_generation_id != runnable_generation_id:
            await _detach_lost_lease(manager, deadlines, session_id, organization_id)
            return
    while True:
        # Unregistered while closing, so the close path's own disarm cannot cancel the retry.
        if deadlines.get(session_id) is asyncio.current_task():
            deadlines.pop(session_id)
        try:
            await manager.close_session(organization_id, session_id, reason=reason)
        except Exception:
            LOG.warning("Failed to close an external CDP session at its deadline", session_id=session_id, exc_info=True)
        try:
            row = await browser_sessions.get_persistent_browser_session(session_id, organization_id)
            if row is None or not is_external_cdp_session_open(row):
                return
        except Exception:
            LOG.warning("Failed to confirm an external CDP session closed", session_id=session_id, exc_info=True)
        if session_id in deadlines:
            return
        current = asyncio.current_task()
        assert current is not None
        deadlines[session_id] = current
        await asyncio.sleep(EXTERNAL_CDP_CLOSE_POLL_SECONDS)


async def _detach_lost_lease(
    manager: PersistentSessionsManager,
    deadlines: dict[str, asyncio.Task[None]],
    session_id: str,
    organization_id: str,
) -> None:
    # A same-process reacquire re-arms this slot, so a slot still holding this task means another process owns the
    # lease and this process's driver must not keep acting on the browser.
    if deadlines.get(session_id) is not asyncio.current_task():
        return
    deadlines.pop(session_id)
    try:
        await manager.evict_cached_browser_state(session_id, organization_id, detach_remote_driver=True)
    except Exception:
        LOG.warning(
            "Failed to detach an external CDP session after losing its lease", session_id=session_id, exc_info=True
        )


def arm_external_deadline(
    manager: PersistentSessionsManager,
    browser_sessions: BrowserSessionsRepository,
    browser_session: PersistentBrowserSession,
    deadlines: dict[str, asyncio.Task[None]],
    *,
    runnable_id: str,
    runnable_generation_id: str,
) -> None:
    """A leased registration closes at its fixed deadline, or after another process closes it, even while the run
    holding it never touches the row. Arm with the owner the lease CAS just wrote, not the row read before it."""
    session_id = browser_session.persistent_browser_session_id
    disarm_external_deadline(deadlines, session_id)
    deadlines[session_id] = asyncio.create_task(
        _close_at_external_deadline(
            manager, browser_sessions, browser_session, deadlines, runnable_id, runnable_generation_id
        ),
        name=f"external_cdp_deadline_{session_id}",
    )


def disarm_external_deadline(deadlines: dict[str, asyncio.Task[None]], session_id: str) -> None:
    task = deadlines.pop(session_id, None)
    if task is not None and task is not asyncio.current_task():
        task.cancel()


def disarm_released_deadline(
    deadlines: dict[str, asyncio.Task[None]], session_id: str, released: asyncio.Task[None] | None
) -> None:
    """Cancel only the watcher captured when the release CAS ran; a newer owner may already have armed the slot."""
    if released is None:
        return
    if deadlines.get(session_id) is released:
        deadlines.pop(session_id)
    if released is not asyncio.current_task():
        released.cancel()


async def renew_external_cdp_session(
    manager: PersistentSessionsManager, browser_session: PersistentBrowserSession, *, close_on_failure: bool
) -> PersistentBrowserSession:
    """Renewal never moves a registration's deadline; it only answers whether time is left."""
    session_id = browser_session.persistent_browser_session_id
    if not is_external_cdp_session_open(browser_session):
        raise BrowserSessionNotRenewable("Browser session has already completed", session_id)
    if is_external_cdp_session_expired(browser_session):
        if close_on_failure:
            await manager.close_session(
                browser_session.organization_id, session_id, reason=BrowserSessionCloseReason.expired
            )
        raise BrowserSessionNotRenewable("Session has expired", session_id)
    return browser_session


async def close_expired_external_cdp_sessions(
    browser_sessions: BrowserSessionsRepository, organization_id: str | None = None
) -> int:
    """Close every expired registration from its row alone, for deployments with no in-process reaper."""
    closed = 0
    for row in await browser_sessions.get_uncompleted_persistent_browser_sessions(
        EXTERNAL_CDP_BROWSER_VENDOR, organization_id=organization_id
    ):
        session = PersistentBrowserSession.model_validate(row)
        if not is_external_cdp_session_expired(session):
            continue
        session_id, row_org_id = session.persistent_browser_session_id, session.organization_id
        try:
            await browser_sessions.record_persistent_browser_session_close_reason(
                session_id, row_org_id, BrowserSessionCloseReason.expired.value
            )
            await browser_sessions.close_persistent_browser_session(session_id, row_org_id)
        except Exception:
            LOG.warning("Failed to close an expired external CDP session", session_id=session_id, exc_info=True)
            continue
        closed += 1
    return closed


async def seal_cdp_url(cdp_url: str) -> str:
    """The stored form of a registration's address; the database, and every copy made of it, sees only this."""
    return await encryptor.encrypt(cdp_url, EncryptMethod.AES)


async def attach_registered_cdp_browser(sealed_cdp_url: str) -> RealBrowserState:
    try:
        cdp_url = await encryptor.decrypt(sealed_cdp_url, EncryptMethod.AES)
    except Exception as error:
        LOG.warning("Could not read a registered external CDP address", error_type=type(error).__name__)
        raise ExternalBrowserUnavailable("the stored address cannot be read") from None
    return await connect_external_cdp_browser(cdp_url)


async def check_external_cdp_address(address: str) -> None:
    """Outside local development TLS is required: the certificate check is what stops DNS from repointing an allowed
    name at an internal address between this check and the dial. A name DNS cannot resolve is refused."""
    if not is_tls_or_local_browser_address(address):
        raise ExternalBrowserEndpointNotEncrypted()
    host = urlsplit(address).hostname or ""
    await asyncio.to_thread(validate_browser_host, host, resolve_dns=False)
    if not is_allowed_local_browser_host(host):
        await asyncio.to_thread(resolve_fetch_host_ips, host)


async def _stop_driver(pw: Playwright) -> None:
    try:
        async with asyncio.timeout(BROWSER_CLOSE_TIMEOUT):
            await pw.stop()
    except Exception as error:
        LOG.warning("Failed to stop Playwright after an external CDP connect failure", error_type=type(error).__name__)


async def connect_external_cdp_browser(cdp_url: str) -> RealBrowserState:
    """Attach to a caller-owned browser. Discovery runs before any driver starts, and connect errors embed the
    address, so only the error class reaches logs and the raised error."""
    cdp_host = urlsplit(cdp_url).hostname
    pw: Playwright | None = None
    try:
        async with asyncio.timeout(EXTERNAL_CDP_CONNECT_TIMEOUT_MS / 1000):
            await check_external_cdp_address(cdp_url)
            ws_url = await resolve_websocket_url(cdp_url, timeout_ms=EXTERNAL_CDP_CONNECT_TIMEOUT_MS)
            if ws_url != cdp_url:
                await check_external_cdp_address(ws_url)
            pw = await async_playwright().start()
            default_evaluate_to_main_world(pw)
            browser = await pw.chromium.connect_over_cdp(ws_url, timeout=EXTERNAL_CDP_CONNECT_TIMEOUT_MS)
    except (BlockedHost, ExternalBrowserEndpointNotEncrypted):
        if pw is not None:
            await _stop_driver(pw)
        raise
    except Exception as error:
        if pw is not None:
            await _stop_driver(pw)
        LOG.warning("External CDP browser is unreachable", cdp_host=cdp_host, error_type=type(error).__name__)
        # One message for every cause, so the reply cannot tell a closed port from an HTTP error on an open one.
        raise ExternalBrowserUnavailable("connection failed") from None
    except BaseException:
        if pw is not None:
            await _stop_driver(pw)
        raise
    if not browser.contexts:
        await _stop_driver(pw)
        raise ExternalBrowserUnavailable("the browser exposes no default context")
    browser_context = browser.contexts[0]
    browser_context._skyvern_external_browser = True  # type: ignore[attr-defined]
    # Ownership only, as for an adopted session: page-level Fetch would put a fail-closed request gate on the
    # caller's own tabs. A run rebinds it to its download scope when it acquires the browser.
    await bind_download_interceptor_to_context(
        CDPDownloadInterceptor(
            network_egress_monitor=BrowserNetworkEgressMonitor.unenrolled(),
            redirect_hop_authorizer=deny_unenrolled_redirect_hop,
        ),
        browser_context,
        enable_page_interception=False,
    )
    return RealBrowserState(
        pw=pw,
        browser_context=browser_context,
        browser_artifacts=BrowserArtifacts(),
        release_driver_on_close=True,
        external_browser=True,
    )
