import json
from datetime import datetime
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi import HTTPException

from skyvern.exceptions import BrowserSessionExtensionUnconfirmed, BrowserSessionNotExtendable
from skyvern.forge.sdk.routes import browser_sessions as browser_sessions_mod
from skyvern.schemas.browser_session_timeouts import max_lifetime_exceeded_warning
from skyvern.webeye.persistent_sessions_manager import BrowserSessionExtension
from skyvern.webeye.schemas import BrowserSessionResponse


async def _extend(app_mock: MagicMock, additional_minutes: int) -> Any:
    built_response = BrowserSessionResponse(
        browser_session_id="pbs_1",
        organization_id="org_1",
        created_at=datetime(2026, 1, 1),
        modified_at=datetime(2026, 1, 1),
    )
    with (
        patch.object(browser_sessions_mod, "app", app_mock),
        patch.object(
            browser_sessions_mod.BrowserSessionResponse, "from_browser_session", AsyncMock(return_value=built_response)
        ),
    ):
        return await browser_sessions_mod.extend_browser_session(
            browser_sessions_mod.ExtendBrowserSessionRequest(additional_minutes=additional_minutes),
            "pbs_1",
            current_org=SimpleNamespace(organization_id="org_1"),
        )


@pytest.mark.asyncio
async def test_extend_browser_session_returns_404_without_org_owned_session() -> None:
    app_mock = MagicMock()
    app_mock.PERSISTENT_SESSIONS_MANAGER.get_session = AsyncMock(return_value=None)
    app_mock.PERSISTENT_SESSIONS_MANAGER.extend_session = AsyncMock()

    with pytest.raises(HTTPException) as exc_info:
        await _extend(app_mock, 30)

    assert exc_info.value.status_code == 404
    app_mock.PERSISTENT_SESSIONS_MANAGER.get_session.assert_awaited_once_with("pbs_1", "org_1")
    app_mock.PERSISTENT_SESSIONS_MANAGER.extend_session.assert_not_awaited()


@pytest.mark.asyncio
async def test_extend_browser_session_reports_a_clamped_grant_as_a_warning() -> None:
    app_mock = MagicMock()
    app_mock.PERSISTENT_SESSIONS_MANAGER.get_session = AsyncMock(return_value=SimpleNamespace(status="running"))
    app_mock.PERSISTENT_SESSIONS_MANAGER.extend_session = AsyncMock(
        return_value=BrowserSessionExtension(session=MagicMock(), granted_minutes=20)
    )

    clamped = await _extend(app_mock, 90)

    app_mock.PERSISTENT_SESSIONS_MANAGER.extend_session.assert_awaited_once_with("pbs_1", "org_1", 90)
    assert clamped.warning == max_lifetime_exceeded_warning(90, 20)

    app_mock.PERSISTENT_SESSIONS_MANAGER.extend_session = AsyncMock(
        return_value=BrowserSessionExtension(session=MagicMock(), granted_minutes=90)
    )
    assert (await _extend(app_mock, 90)).warning is None


@pytest.mark.asyncio
async def test_extend_browser_session_maps_refusals_to_conflict() -> None:
    app_mock = MagicMock()
    app_mock.PERSISTENT_SESSIONS_MANAGER.get_session = AsyncMock(return_value=SimpleNamespace(status="completed"))
    app_mock.PERSISTENT_SESSIONS_MANAGER.extend_session = AsyncMock()

    with pytest.raises(HTTPException) as ended:
        await _extend(app_mock, 30)
    assert ended.value.status_code == 409
    app_mock.PERSISTENT_SESSIONS_MANAGER.extend_session.assert_not_awaited()

    app_mock.PERSISTENT_SESSIONS_MANAGER.get_session = AsyncMock(return_value=SimpleNamespace(status="running"))
    app_mock.PERSISTENT_SESSIONS_MANAGER.extend_session = AsyncMock(
        side_effect=BrowserSessionNotExtendable("browser session has expired", "pbs_1")
    )
    with pytest.raises(HTTPException) as refused:
        await _extend(app_mock, 30)
    assert refused.value.status_code == 409
    assert "expired" in refused.value.detail


@pytest.mark.asyncio
async def test_an_unconfirmed_extension_is_accepted_not_failed() -> None:
    """The signal landed, so a retry adds again. The SDK retries 409 and every 5xx on its own, so the
    only honest answer is a 2xx that says the effect is not confirmed and points at GET."""
    app_mock = MagicMock()
    app_mock.PERSISTENT_SESSIONS_MANAGER.get_session = AsyncMock(return_value=SimpleNamespace(status="running"))
    app_mock.PERSISTENT_SESSIONS_MANAGER.extend_session = AsyncMock(
        side_effect=BrowserSessionExtensionUnconfirmed("pbs_1")
    )

    accepted = await _extend(app_mock, 30)

    assert accepted.status_code == 202
    body = json.loads(accepted.body)
    assert "Do not retry" in body["warning"]


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
        include_stream_transport=True,
    )
