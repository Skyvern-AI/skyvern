from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from skyvern.forge.sdk.routes import debug_sessions as debug_sessions_mod


@pytest.mark.asyncio
async def test_new_debug_session_skips_redundant_local_close_before_create() -> None:
    created_debug_session = SimpleNamespace(debug_session_id="ds_new")
    new_browser_session = SimpleNamespace(
        persistent_browser_session_id="pbs_new",
        ip_address=None,
        browser_address=None,
    )

    app_mock = MagicMock()
    app_mock.DATABASE.debug.get_debug_session = AsyncMock(return_value=None)
    app_mock.DATABASE.debug.complete_debug_sessions = AsyncMock(
        return_value=[SimpleNamespace(browser_session_id="pbs_old")]
    )
    app_mock.DATABASE.debug.create_debug_session = AsyncMock(return_value=created_debug_session)
    app_mock.DATABASE.browser_sessions.get_persistent_browser_session = AsyncMock()
    app_mock.WORKFLOW_SERVICE.get_workflow_by_permanent_id = AsyncMock(
        return_value=SimpleNamespace(proxy_location=None)
    )
    app_mock.PERSISTENT_SESSIONS_MANAGER.close_session = AsyncMock()
    app_mock.PERSISTENT_SESSIONS_MANAGER.create_session = AsyncMock(return_value=new_browser_session)

    with (
        patch.object(debug_sessions_mod, "app", app_mock),
        patch.object(debug_sessions_mod.settings, "ENV", "local"),
    ):
        result = await debug_sessions_mod.new_debug_session(
            "wpid_test",
            current_org=SimpleNamespace(organization_id="org_123"),
            current_user_id="user_123",
        )

    assert result is created_debug_session
    app_mock.DATABASE.browser_sessions.get_persistent_browser_session.assert_not_awaited()
    app_mock.PERSISTENT_SESSIONS_MANAGER.close_session.assert_not_awaited()
    app_mock.PERSISTENT_SESSIONS_MANAGER.create_session.assert_awaited_once_with(
        organization_id="org_123",
        timeout_minutes=debug_sessions_mod.settings.DEBUG_SESSION_TIMEOUT_MINUTES,
        proxy_location=debug_sessions_mod.ProxyLocation.RESIDENTIAL,
        wait_for_startup=False,
    )
    app_mock.DATABASE.debug.create_debug_session.assert_awaited_once_with(
        browser_session_id="pbs_new",
        organization_id="org_123",
        user_id="user_123",
        workflow_permanent_id="wpid_test",
        vnc_streaming_supported=True,
    )
