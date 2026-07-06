from datetime import datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from skyvern.forge.sdk.routes import browser_sessions as browser_sessions_mod
from skyvern.forge.sdk.schemas.persistent_browser_sessions import PersistentBrowserSession
from skyvern.schemas.browser_sessions import CreateBrowserSessionRequest


def _session(**kwargs: object) -> PersistentBrowserSession:
    base: dict[str, object] = {
        "persistent_browser_session_id": "pbs_1",
        "organization_id": "org_123",
        "created_at": datetime(2026, 1, 1),
        "modified_at": datetime(2026, 1, 1),
    }
    base.update(kwargs)
    return PersistentBrowserSession(**base)


@pytest.mark.asyncio
async def test_create_browser_session_passes_startup_url_to_session_manager() -> None:
    app_mock = MagicMock()
    app_mock.PERSISTENT_SESSIONS_MANAGER.create_session = AsyncMock(return_value=_session())

    with patch.object(browser_sessions_mod, "app", app_mock):
        await browser_sessions_mod.create_browser_session(
            CreateBrowserSessionRequest(
                url="https://web.telegram.org",
                timeout=120,
                generate_browser_profile=True,
            ),
            current_org=SimpleNamespace(organization_id="org_123"),
        )

    app_mock.PERSISTENT_SESSIONS_MANAGER.create_session.assert_awaited_once_with(
        organization_id="org_123",
        timeout_minutes=120,
        proxy_location=None,
        proxy_session_id=None,
        extensions=None,
        browser_type=None,
        browser_profile_id=None,
        generate_browser_profile=True,
        url="https://web.telegram.org",
    )
