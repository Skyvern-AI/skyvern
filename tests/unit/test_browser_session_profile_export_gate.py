from datetime import datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from skyvern.exceptions import SkyvernHTTPException
from skyvern.forge.sdk.routes import browser_sessions as browser_sessions_mod
from skyvern.forge.sdk.schemas.persistent_browser_sessions import (
    PersistentBrowserSession,
    export_profile_storage_id,
)
from skyvern.schemas.browser_sessions import CreateBrowserSessionRequest, UpdateBrowserSessionRequest


def _session(**kwargs: object) -> PersistentBrowserSession:
    base: dict[str, object] = {
        "persistent_browser_session_id": "pbs_1",
        "organization_id": "o_1",
        "created_at": datetime(2026, 1, 1),
        "modified_at": datetime(2026, 1, 1),
    }
    base.update(kwargs)
    return PersistentBrowserSession(**base)


def test_should_export_profile_opted_out_by_default() -> None:
    assert _session().should_export_profile() is False
    assert _session(generate_browser_profile=False).should_export_profile() is False


def test_should_export_profile_when_opted_in() -> None:
    assert _session(generate_browser_profile=True).should_export_profile() is True


def test_should_export_profile_always_true_when_reusing_a_profile() -> None:
    # A session reusing a saved profile must always re-export so the updated session-cookie
    # sidecar survives — gating it off would silently log the profile out on the next reuse.
    assert _session(browser_profile_id="bp_1").should_export_profile() is True
    assert _session(browser_profile_id="bp_1", generate_browser_profile=False).should_export_profile() is True


def test_create_request_defaults_to_opt_out() -> None:
    assert CreateBrowserSessionRequest().generate_browser_profile is False


def test_create_request_accepts_start_url() -> None:
    request = CreateBrowserSessionRequest(url="https://example.com/path", generate_browser_profile=True)

    assert request.url == "https://example.com/path"


def test_create_request_rejects_invalid_start_url() -> None:
    with pytest.raises(SkyvernHTTPException):
        CreateBrowserSessionRequest(url="ftp://example.com")


@pytest.mark.asyncio
async def test_create_browser_session_passes_start_url_to_session_manager() -> None:
    created_session = SimpleNamespace(persistent_browser_session_id="pbs_1")
    response = SimpleNamespace(browser_session_id="pbs_1")
    app_mock = MagicMock()
    app_mock.PERSISTENT_SESSIONS_MANAGER.create_session = AsyncMock(return_value=created_session)
    from_browser_session = AsyncMock(return_value=response)

    with (
        patch.object(browser_sessions_mod, "app", app_mock),
        patch.object(browser_sessions_mod.BrowserSessionResponse, "from_browser_session", from_browser_session),
    ):
        result = await browser_sessions_mod.create_browser_session(
            CreateBrowserSessionRequest(
                url="https://example.com/login",
                timeout=120,
                generate_browser_profile=True,
            ),
            current_org=SimpleNamespace(organization_id="org_1"),
        )

    assert result is response
    app_mock.PERSISTENT_SESSIONS_MANAGER.create_session.assert_awaited_once_with(
        organization_id="org_1",
        url="https://example.com/login",
        timeout_minutes=120,
        proxy_location=None,
        proxy_session_id=None,
        extensions=None,
        browser_type=None,
        browser_profile_id=None,
        generate_browser_profile=True,
    )
    from_browser_session.assert_awaited_once_with(created_session)


def test_update_request_carries_flag() -> None:
    assert UpdateBrowserSessionRequest(generate_browser_profile=True).generate_browser_profile is True
    assert UpdateBrowserSessionRequest(generate_browser_profile=False).generate_browser_profile is False


def test_export_profile_storage_id_pure_reuse_targets_profile() -> None:
    assert (
        export_profile_storage_id(session_id="pbs_1", browser_profile_id="bp_1", generate_browser_profile=False)
        == "bp_1"
    )


def test_export_profile_storage_id_generate_targets_session_even_over_reused_profile() -> None:
    assert (
        export_profile_storage_id(session_id="pbs_1", browser_profile_id="bp_1", generate_browser_profile=True)
        == "pbs_1"
    )


def test_export_profile_storage_id_falls_back_to_session_when_no_profile() -> None:
    assert (
        export_profile_storage_id(session_id="pbs_1", browser_profile_id=None, generate_browser_profile=False)
        == "pbs_1"
    )


def test_export_profile_storage_id_generate_without_profile_targets_session() -> None:
    assert (
        export_profile_storage_id(session_id="pbs_1", browser_profile_id=None, generate_browser_profile=True) == "pbs_1"
    )
