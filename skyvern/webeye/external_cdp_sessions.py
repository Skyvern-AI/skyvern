from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone
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
from skyvern.forge.sdk.db.repositories.browser_sessions import BrowserSessionsRepository
from skyvern.forge.sdk.encrypt import encryptor
from skyvern.forge.sdk.encrypt.base import EncryptMethod
from skyvern.forge.sdk.schemas.persistent_browser_sessions import (
    EXTERNAL_CDP_BROWSER_VENDOR,
    PersistentBrowserSession,
    is_final_status,
)
from skyvern.schemas.browser_session_close import BrowserSessionCloseReason
from skyvern.schemas.browser_session_timeouts import DEFAULT_TIMEOUT
from skyvern.utils.url_validators import (
    is_allowed_local_browser_host,
    is_tls_or_local_browser_address,
    resolve_fetch_host_ips,
    validate_browser_host,
)
from skyvern.webeye.browser_artifacts import BrowserArtifacts
from skyvern.webeye.cdp_discovery import resolve_websocket_url
from skyvern.webeye.persistent_sessions_manager import PersistentSessionsManager
from skyvern.webeye.real_browser_state import RealBrowserState

LOG = structlog.get_logger()

EXTERNAL_CDP_CONNECT_TIMEOUT_MS = 10_000


def seconds_until_external_deadline(browser_session: PersistentBrowserSession) -> float:
    """The deadline is fixed at registration: activity never extends it."""
    started_at = browser_session.started_at or browser_session.created_at
    if started_at.tzinfo is None:
        started_at = started_at.replace(tzinfo=timezone.utc)
    deadline = started_at + timedelta(minutes=browser_session.timeout_minutes or DEFAULT_TIMEOUT)
    return (deadline - datetime.now(timezone.utc)).total_seconds()


def is_external_cdp_session_expired(browser_session: PersistentBrowserSession) -> bool:
    return seconds_until_external_deadline(browser_session) <= 0


def is_external_cdp_session_open(browser_session: PersistentBrowserSession) -> bool:
    return (
        browser_session.completed_at is None
        and not is_final_status(browser_session.status)
        and bool(browser_session.upstream_cdp_url)
    )


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
    return RealBrowserState(
        pw=pw,
        browser_context=browser.contexts[0],
        browser_artifacts=BrowserArtifacts(),
        release_driver_on_close=True,
        external_browser=True,
    )
