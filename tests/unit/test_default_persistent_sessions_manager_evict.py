"""``DefaultPersistentSessionsManager.evict_cached_browser_state`` must close the
cached ``BrowserState`` before dropping the entry; otherwise the Playwright resources
are orphaned and the subsequent ``close_session()`` finds nothing to clean up, so
artifact/profile/video sync never runs on the dropped session.
"""

import asyncio
from unittest.mock import AsyncMock, MagicMock

import pytest

from skyvern.webeye.browser_retirement import BrowserRetirementReason
from skyvern.webeye.default_persistent_sessions_manager import BrowserSession, DefaultPersistentSessionsManager
from skyvern.webeye.persistent_sessions_manager import BrowserOperationRejected


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
async def test_detach_only_evict_does_not_close_remote_browser_context(
    manager: DefaultPersistentSessionsManager,
) -> None:
    browser_state = MagicMock()
    browser_state.close = AsyncMock()
    browser_state.detach_remote_driver = AsyncMock()
    manager._browser_sessions["pbs_local"] = BrowserSession(browser_state=browser_state)

    await manager.evict_cached_browser_state(
        "pbs_local",
        "org_local",
        expected=browser_state,
        detach_remote_driver=True,
    )

    browser_state.detach_remote_driver.assert_awaited_once_with()
    browser_state.close.assert_not_awaited()
    assert "pbs_local" not in manager._browser_sessions


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
async def test_browser_operation_rejects_a_state_that_is_no_longer_current(
    manager: DefaultPersistentSessionsManager,
) -> None:
    current_state = MagicMock()
    stale_state = MagicMock()
    manager._browser_sessions["pbs_local"] = BrowserSession(browser_state=current_state)

    async with manager.browser_operation("pbs_local", stale_state) as operation:
        assert operation == BrowserOperationRejected(BrowserRetirementReason.replacement)


@pytest.mark.asyncio
@pytest.mark.parametrize("transition", ["evict", "replace"])
async def test_cache_transition_retires_the_active_generation(
    manager: DefaultPersistentSessionsManager,
    transition: str,
) -> None:
    stale_state = MagicMock()
    stale_state.close = AsyncMock()
    fresh_state = MagicMock()
    manager._browser_sessions["pbs_local"] = BrowserSession(browser_state=stale_state)
    entered = asyncio.Event()
    retirement_started: asyncio.Event | None = None
    retirement_reason: BrowserRetirementReason | None = None

    async def _active_operation() -> None:
        nonlocal retirement_reason, retirement_started
        async with manager.browser_operation("pbs_local", stale_state) as operation:
            assert not isinstance(operation, BrowserOperationRejected)
            retirement_started = operation.retirement_started
            entered.set()
            try:
                await asyncio.Future()
            finally:
                retirement_reason = operation.retirement_reason

    operation_task = asyncio.create_task(_active_operation())
    await entered.wait()

    if transition == "evict":
        await manager.evict_cached_browser_state("pbs_local", "org_local")
    else:
        await manager.set_browser_state("pbs_local", fresh_state, "org_local")

    with pytest.raises(asyncio.CancelledError):
        await operation_task
    assert retirement_started is not None and retirement_started.is_set()
    if transition == "evict":
        assert retirement_reason is BrowserRetirementReason.session_ending
    else:
        assert retirement_reason is BrowserRetirementReason.replacement
    if transition == "evict":
        assert "pbs_local" not in manager._browser_sessions
    else:
        assert manager._browser_sessions["pbs_local"].browser_state is fresh_state
