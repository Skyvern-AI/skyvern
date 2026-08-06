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


@pytest.mark.asyncio
@pytest.mark.parametrize("needs_live_view", [True, False])
async def test_create_browser_session_forwards_whether_the_session_will_be_watched(
    needs_live_view: bool,
) -> None:
    """Dropped here, the session is still created and still connects — it only fails later, when
    someone opens the live view. Defaulting it False is what keeps unattended automation routable."""
    app_mock = MagicMock()
    app_mock.PERSISTENT_SESSIONS_MANAGER.create_session = AsyncMock(return_value=MagicMock())

    with (
        patch.object(browser_sessions_mod, "app", app_mock),
        patch.object(browser_sessions_mod.BrowserSessionResponse, "from_browser_session", AsyncMock()),
    ):
        await browser_sessions_mod.create_browser_session(
            browser_sessions_mod.CreateBrowserSessionRequest(needs_live_view=needs_live_view),
            current_org=SimpleNamespace(organization_id="org_1"),
        )

    assert app_mock.PERSISTENT_SESSIONS_MANAGER.create_session.await_args.kwargs["needs_live_view"] is needs_live_view


def test_a_session_request_is_unwatched_unless_it_says_otherwise() -> None:
    """The public default. True would route every API-created session to first-party and make the
    provider rollout unreachable."""
    assert browser_sessions_mod.CreateBrowserSessionRequest().needs_live_view is False


@pytest.mark.asyncio
async def test_get_browser_session_enables_strict_download_lookup() -> None:
    browser_session = MagicMock()
    response = MagicMock()
    app_mock = MagicMock()
    app_mock.PERSISTENT_SESSIONS_MANAGER.get_session = AsyncMock(return_value=browser_session)
    from_browser_session = AsyncMock(return_value=response)

    with (
        patch.object(browser_sessions_mod, "app", app_mock),
        patch.object(browser_sessions_mod.BrowserSessionResponse, "from_browser_session", from_browser_session),
    ):
        result = await browser_sessions_mod.get_browser_session(
            "pbs_1",
            current_org=SimpleNamespace(organization_id="org_1"),
        )

    assert result is response
    from_browser_session.assert_awaited_once_with(
        browser_session,
        app_mock.STORAGE,
        fail_download_lookup=True,
    )
