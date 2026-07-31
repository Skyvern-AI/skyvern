"""What "this session has a browser you can connect to" is keyed on.

A client-facing address can be minted when a session is created, before anything is provisioned,
so its presence no longer means a browser exists. Everything that gates on readiness reads
``is_browser_ready`` (the upstream endpoint) instead, or an adopter connects to an address whose
browser is still starting and is refused by the router.
"""

from __future__ import annotations

from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock

import pytest

from skyvern.forge.sdk.db.polls import await_browser_session, wait_on_persistent_browser_address
from skyvern.forge.sdk.schemas.persistent_browser_sessions import PersistentBrowserSession
from skyvern.webeye.default_persistent_sessions_manager import DefaultPersistentSessionsManager

SESSION_ID = "pbs_ready"
ORG_ID = "o_ready"
ROUTER_ADDRESS = f"wss://session-router.skyvern.com/{SESSION_ID}?token={SESSION_ID}.minted-secret"
LEGACY_ADDRESS = f"wss://sessions.skyvern.com/{SESSION_ID}/payload.signature/devtools/browser/b-1"
UPSTREAM_URL = "ws://10.0.0.7:9223/devtools/browser/b-1"


def _session(*, browser_address: str | None, upstream_cdp_url: str | None) -> PersistentBrowserSession:
    now = datetime.now(timezone.utc)
    return PersistentBrowserSession(
        persistent_browser_session_id=SESSION_ID,
        organization_id=ORG_ID,
        status="running",
        browser_address=browser_address,
        upstream_cdp_url=upstream_cdp_url,
        created_at=now,
        modified_at=now,
    )


def test_an_address_minted_before_the_browser_exists_is_not_readiness() -> None:
    """The regression: a session created with a router address has an address and no browser."""
    session = _session(browser_address=ROUTER_ADDRESS, upstream_cdp_url=None)

    assert session.is_browser_ready is False


def test_a_session_with_an_upstream_is_ready() -> None:
    assert _session(browser_address=ROUTER_ADDRESS, upstream_cdp_url=UPSTREAM_URL).is_browser_ready is True


def test_a_legacy_session_is_ready_exactly_as_it_is_today() -> None:
    """The session worker writes both columns in one call, so gating on the upstream is the same
    answer it always was for every session that predates minted addresses."""
    assert _session(browser_address=LEGACY_ADDRESS, upstream_cdp_url=UPSTREAM_URL).is_browser_ready is True
    assert _session(browser_address=None, upstream_cdp_url=None).is_browser_ready is False


def _manager(session: PersistentBrowserSession | None) -> DefaultPersistentSessionsManager:
    database = MagicMock()
    database.browser_sessions.get_persistent_browser_session = AsyncMock(return_value=session)
    return DefaultPersistentSessionsManager(database=database)


@pytest.mark.asyncio
async def test_a_session_whose_browser_is_still_starting_is_not_reported_ready() -> None:
    manager = _manager(_session(browser_address=ROUTER_ADDRESS, upstream_cdp_url=None))

    assert await manager.get_browser_address_if_ready(SESSION_ID, ORG_ID) is None


@pytest.mark.asyncio
async def test_a_ready_session_still_returns_its_client_facing_address() -> None:
    """Only the gate moved to the upstream: what comes back is still the address the client uses,
    never the upstream endpoint, which must not reach a client."""
    manager = _manager(_session(browser_address=ROUTER_ADDRESS, upstream_cdp_url=UPSTREAM_URL))

    assert await manager.get_browser_address_if_ready(SESSION_ID, ORG_ID) == ROUTER_ADDRESS


@pytest.mark.asyncio
async def test_a_ready_legacy_session_returns_its_address_as_it_does_today() -> None:
    manager = _manager(_session(browser_address=LEGACY_ADDRESS, upstream_cdp_url=UPSTREAM_URL))

    assert await manager.get_browser_address_if_ready(SESSION_ID, ORG_ID) == LEGACY_ADDRESS


@pytest.mark.asyncio
async def test_the_address_poll_waits_for_the_browser_not_for_the_address() -> None:
    database = MagicMock()
    database.browser_sessions.get_persistent_browser_session = AsyncMock(
        side_effect=[
            _session(browser_address=ROUTER_ADDRESS, upstream_cdp_url=None),
            _session(browser_address=ROUTER_ADDRESS, upstream_cdp_url=UPSTREAM_URL),
        ]
    )

    session = await await_browser_session(database, SESSION_ID, ORG_ID, timeout=5, poll_interval=0)

    assert session is not None
    assert session.is_browser_ready is True
    assert database.browser_sessions.get_persistent_browser_session.await_count == 2


@pytest.mark.asyncio
async def test_the_address_poll_still_returns_the_client_facing_address() -> None:
    database = MagicMock()
    database.browser_sessions.get_persistent_browser_session = AsyncMock(
        return_value=_session(browser_address=ROUTER_ADDRESS, upstream_cdp_url=UPSTREAM_URL)
    )

    address = await wait_on_persistent_browser_address(database, SESSION_ID, ORG_ID, timeout=5, poll_interval=0)

    assert address == ROUTER_ADDRESS
