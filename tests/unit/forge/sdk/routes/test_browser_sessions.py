from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi import HTTPException

from skyvern.forge.sdk.routes import browser_sessions as browser_sessions_mod


@pytest.mark.asyncio
async def test_close_browser_session_returns_404_without_org_owned_session() -> None:
    app_mock = MagicMock()
    app_mock.PERSISTENT_SESSIONS_MANAGER.get_session = AsyncMock(return_value=None)
    app_mock.PERSISTENT_SESSIONS_MANAGER.close_session = AsyncMock()

    with (
        patch.object(browser_sessions_mod, "app", app_mock),
        pytest.raises(HTTPException) as exc_info,
    ):
        await browser_sessions_mod.close_browser_session(
            "pbs_foreign",
            current_org=SimpleNamespace(organization_id="org_requester"),
        )

    assert exc_info.value.status_code == 404
    app_mock.PERSISTENT_SESSIONS_MANAGER.get_session.assert_awaited_once_with("pbs_foreign", "org_requester")
    app_mock.PERSISTENT_SESSIONS_MANAGER.close_session.assert_not_awaited()
