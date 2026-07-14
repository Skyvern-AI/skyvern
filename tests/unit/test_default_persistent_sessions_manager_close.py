from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from skyvern.webeye import default_persistent_sessions_manager as manager_mod
from skyvern.webeye.default_persistent_sessions_manager import BrowserSession, DefaultPersistentSessionsManager


@pytest.fixture
def manager() -> DefaultPersistentSessionsManager:
    DefaultPersistentSessionsManager.instance = None
    DefaultPersistentSessionsManager._browser_sessions = {}
    DefaultPersistentSessionsManager._background_tasks = set()
    DefaultPersistentSessionsManager._reaper_task = None
    db = MagicMock()
    db.browser_sessions.get_persistent_browser_session = AsyncMock()
    db.browser_sessions.close_persistent_browser_session = AsyncMock()
    db.browser_sessions.archive_browser_session_address = AsyncMock()
    return DefaultPersistentSessionsManager(database=db)


@pytest.mark.asyncio
async def test_close_session_skips_in_memory_export_for_mismatched_org(
    manager: DefaultPersistentSessionsManager,
) -> None:
    browser_state = MagicMock()
    browser_state.close = AsyncMock()
    browser_state.browser_artifacts = SimpleNamespace(
        browser_session_dir="/tmp/pbs_foreign",
        video_artifacts=[],
    )
    storage = MagicMock()
    storage.store_browser_profile = AsyncMock()

    manager._browser_sessions["pbs_foreign"] = BrowserSession(
        browser_state=browser_state,
        organization_id="org_owner",
    )

    with (
        patch.object(manager_mod, "app", SimpleNamespace(STORAGE=storage)),
        patch.object(manager_mod, "persist_session_cookies", new=AsyncMock()) as persist_session_cookies,
        patch.object(manager_mod.settings, "BROWSER_STREAMING_MODE", "vnc"),
    ):
        await manager.close_session("org_requester", "pbs_foreign")

    persist_session_cookies.assert_not_awaited()
    storage.store_browser_profile.assert_not_awaited()
    browser_state.close.assert_not_awaited()
    assert "pbs_foreign" in manager._browser_sessions
    manager.database.browser_sessions.get_persistent_browser_session.assert_not_awaited()
    manager.database.browser_sessions.close_persistent_browser_session.assert_awaited_once_with(
        "pbs_foreign",
        "org_requester",
    )


@pytest.mark.asyncio
async def test_close_session_exports_and_closes_for_matching_org(
    manager: DefaultPersistentSessionsManager,
) -> None:
    browser_state = MagicMock()
    browser_state.close = AsyncMock()
    browser_state.browser_context = MagicMock()
    browser_state.browser_artifacts = SimpleNamespace(
        browser_session_dir="/tmp/pbs_owned",
        video_artifacts=[],
    )
    storage = MagicMock()
    storage.store_browser_profile = AsyncMock()
    persisted_session = MagicMock()
    persisted_session.should_export_profile.return_value = True
    manager.database.browser_sessions.get_persistent_browser_session.return_value = persisted_session

    manager._browser_sessions["pbs_owned"] = BrowserSession(
        browser_state=browser_state,
        organization_id="org_owner",
    )

    with (
        patch.object(manager_mod, "app", SimpleNamespace(STORAGE=storage)),
        patch.object(manager_mod, "persist_session_cookies", new=AsyncMock()) as persist_session_cookies,
        patch.object(manager_mod.settings, "BROWSER_STREAMING_MODE", "vnc"),
    ):
        await manager.close_session("org_owner", "pbs_owned")

    persist_session_cookies.assert_awaited_once_with(browser_state.browser_context, "/tmp/pbs_owned")
    manager.database.browser_sessions.get_persistent_browser_session.assert_awaited_once_with(
        "pbs_owned",
        "org_owner",
    )
    storage.store_browser_profile.assert_awaited_once_with(
        organization_id="org_owner",
        profile_id="pbs_owned",
        directory="/tmp/pbs_owned",
    )
    browser_state.close.assert_awaited_once()
    assert "pbs_owned" not in manager._browser_sessions
    manager.database.browser_sessions.close_persistent_browser_session.assert_awaited_once_with(
        "pbs_owned",
        "org_owner",
    )
