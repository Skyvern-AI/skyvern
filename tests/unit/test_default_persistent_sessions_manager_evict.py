"""``DefaultPersistentSessionsManager.evict_cached_browser_state`` must close the
cached ``BrowserState`` before dropping the entry; otherwise the Playwright resources
are orphaned and the subsequent ``close_session()`` finds nothing to clean up, so
artifact/profile/video sync never runs on the dropped session.
"""

import asyncio
from unittest.mock import AsyncMock, MagicMock

import pytest

from skyvern.webeye.default_persistent_sessions_manager import BrowserSession, DefaultPersistentSessionsManager


@pytest.fixture
def manager() -> DefaultPersistentSessionsManager:
    DefaultPersistentSessionsManager.instance = None
    mgr = DefaultPersistentSessionsManager(database=MagicMock())
    mgr._browser_sessions.clear()
    return mgr


@pytest.mark.asyncio
async def test_evict_closes_browser_state_before_dropping_entry(
    manager: DefaultPersistentSessionsManager,
) -> None:
    browser_state = MagicMock()
    browser_state.close = AsyncMock()
    manager._browser_sessions["pbs_local"] = BrowserSession(browser_state=browser_state)

    await manager.evict_cached_browser_state("pbs_local", "org_local")

    browser_state.close.assert_awaited_once()
    assert "pbs_local" not in manager._browser_sessions


@pytest.mark.asyncio
async def test_evict_with_expected_skips_when_cache_holds_different_state(
    manager: DefaultPersistentSessionsManager,
) -> None:
    stale_state = MagicMock()
    stale_state.close = AsyncMock()
    fresh_state = MagicMock()
    fresh_state.close = AsyncMock()
    manager._browser_sessions["pbs_local"] = BrowserSession(browser_state=fresh_state)

    await manager.evict_cached_browser_state("pbs_local", "org_local", expected=stale_state)

    fresh_state.close.assert_not_awaited()
    assert "pbs_local" in manager._browser_sessions


@pytest.mark.asyncio
async def test_evict_swallows_target_closed_during_close(
    manager: DefaultPersistentSessionsManager,
) -> None:
    """When the cached-CDP recovery path triggers eviction, the underlying CDP
    transport is dead, so ``close()`` will raise ``TargetClosedError`` (or another
    transport error). Eviction must still drop the cache entry so the next
    ``get_browser_state`` can reconnect."""
    from playwright._impl._errors import TargetClosedError as PWTargetClosedError

    browser_state = MagicMock()
    browser_state.close = AsyncMock(side_effect=PWTargetClosedError("driver gone"))
    manager._browser_sessions["pbs_dead"] = BrowserSession(browser_state=browser_state)

    await manager.evict_cached_browser_state("pbs_dead", "org_local")

    assert "pbs_dead" not in manager._browser_sessions
    browser_state.close.assert_awaited_once()


@pytest.mark.asyncio
async def test_evict_does_not_swallow_cancellation(
    manager: DefaultPersistentSessionsManager,
) -> None:
    browser_state = MagicMock()
    browser_state.close = AsyncMock(side_effect=asyncio.CancelledError())
    manager._browser_sessions["pbs_cancelled"] = BrowserSession(browser_state=browser_state)

    with pytest.raises(asyncio.CancelledError):
        await manager.evict_cached_browser_state("pbs_cancelled", "org_local")

    assert "pbs_cancelled" not in manager._browser_sessions
    browser_state.close.assert_awaited_once()
