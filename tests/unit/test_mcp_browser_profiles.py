from __future__ import annotations

from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from skyvern.cli.core import session_manager
from skyvern.cli.core.result import BrowserContext
from skyvern.cli.mcp_tools import browser_profiles as mcp_browser_profiles
from skyvern.cli.mcp_tools import skyvern_browser_profile_create
from skyvern.cli.mcp_tools._session import SessionState, set_current_session


@pytest.fixture(autouse=True)
def _reset_session_state() -> None:
    session_manager._current_session.set(None)
    session_manager._global_session = None
    yield
    session_manager._current_session.set(None)
    session_manager._global_session = None


def _profile(profile_id: str = "bp_123") -> SimpleNamespace:
    now = datetime(2026, 1, 2, 3, 4, 5, tzinfo=UTC)
    return SimpleNamespace(
        browser_profile_id=profile_id,
        organization_id="org_123",
        name="Test profile",
        description="Reusable login state",
        source_browser_type="chrome",
        created_at=now,
        modified_at=now,
        deleted_at=None,
    )


@pytest.mark.asyncio
async def test_browser_profile_create_accepts_explicit_browser_session(monkeypatch: pytest.MonkeyPatch) -> None:
    fake_skyvern = MagicMock()
    fake_skyvern.create_browser_profile = AsyncMock(return_value=_profile())
    monkeypatch.setattr(mcp_browser_profiles, "get_skyvern", lambda: fake_skyvern)

    result = await mcp_browser_profiles.skyvern_browser_profile_create(
        name="Test profile",
        description="Reusable login state",
        browser_session_id="pbs_123",
    )

    assert result["ok"] is True
    assert result["browser_context"]["session_id"] == "pbs_123"
    assert result["data"]["browser_profile_id"] == "bp_123"
    fake_skyvern.create_browser_profile.assert_awaited_once_with(
        name="Test profile",
        description="Reusable login state",
        browser_session_id="pbs_123",
        workflow_run_id=None,
    )


@pytest.mark.asyncio
async def test_browser_profile_create_requires_explicit_source_even_with_active_session(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake_skyvern = MagicMock()
    fake_skyvern.create_browser_profile = AsyncMock(return_value=_profile())
    monkeypatch.setattr(mcp_browser_profiles, "get_skyvern", lambda: fake_skyvern)
    set_current_session(SessionState(context=BrowserContext(mode="cloud_session", session_id="pbs_123")))

    result = await mcp_browser_profiles.skyvern_browser_profile_create(name="Test profile")

    assert result["ok"] is False
    assert result["error"]["code"] == "INVALID_INPUT"
    fake_skyvern.create_browser_profile.assert_not_awaited()


@pytest.mark.asyncio
async def test_browser_profile_create_accepts_workflow_run_source(monkeypatch: pytest.MonkeyPatch) -> None:
    fake_skyvern = MagicMock()
    fake_skyvern.create_browser_profile = AsyncMock(return_value=_profile())
    monkeypatch.setattr(mcp_browser_profiles, "get_skyvern", lambda: fake_skyvern)

    result = await mcp_browser_profiles.skyvern_browser_profile_create(
        name="Workflow profile",
        workflow_run_id="wr_123",
    )

    assert result["ok"] is True
    fake_skyvern.create_browser_profile.assert_awaited_once_with(
        name="Workflow profile",
        description=None,
        browser_session_id=None,
        workflow_run_id="wr_123",
    )


@pytest.mark.asyncio
async def test_browser_profile_create_workflow_run_source_ignores_current_session(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake_skyvern = MagicMock()
    fake_skyvern.create_browser_profile = AsyncMock(return_value=_profile())
    monkeypatch.setattr(mcp_browser_profiles, "get_skyvern", lambda: fake_skyvern)
    set_current_session(SessionState(context=BrowserContext(mode="cloud_session", session_id="pbs_123")))

    result = await mcp_browser_profiles.skyvern_browser_profile_create(
        name="Workflow profile",
        workflow_run_id="wr_123",
    )

    assert result["ok"] is True
    fake_skyvern.create_browser_profile.assert_awaited_once_with(
        name="Workflow profile",
        description=None,
        browser_session_id=None,
        workflow_run_id="wr_123",
    )


@pytest.mark.asyncio
async def test_browser_profile_create_rejects_missing_source() -> None:
    result = await mcp_browser_profiles.skyvern_browser_profile_create(name="No source")

    assert result["ok"] is False
    assert result["error"]["code"] == "INVALID_INPUT"
    assert "timing_ms" in result


@pytest.mark.asyncio
async def test_browser_profile_create_rejects_multiple_sources() -> None:
    result = await mcp_browser_profiles.skyvern_browser_profile_create(
        name="Too many sources",
        browser_session_id="pbs_123",
        workflow_run_id="wr_123",
    )

    assert result["ok"] is False
    assert result["error"]["code"] == "INVALID_INPUT"


@pytest.mark.asyncio
async def test_browser_profile_list_forwards_filters(monkeypatch: pytest.MonkeyPatch) -> None:
    fake_skyvern = MagicMock()
    fake_skyvern.list_browser_profiles = AsyncMock(return_value=[_profile("bp_1"), _profile("bp_2")])
    monkeypatch.setattr(mcp_browser_profiles, "get_skyvern", lambda: fake_skyvern)

    result = await mcp_browser_profiles.skyvern_browser_profile_list(
        page=2,
        page_size=10,
        include_deleted=True,
        search_key="login",
    )

    assert result["ok"] is True
    assert result["data"]["count"] == 2
    assert [profile["browser_profile_id"] for profile in result["data"]["profiles"]] == ["bp_1", "bp_2"]
    fake_skyvern.list_browser_profiles.assert_awaited_once_with(
        page=2,
        page_size=10,
        include_deleted=True,
        search_key="login",
    )


@pytest.mark.asyncio
async def test_browser_profile_get_update_delete(monkeypatch: pytest.MonkeyPatch) -> None:
    fake_skyvern = MagicMock()
    fake_skyvern.get_browser_profile = AsyncMock(return_value=_profile())
    fake_skyvern.update_browser_profile = AsyncMock(return_value=_profile())
    fake_skyvern.delete_browser_profile = AsyncMock(return_value=None)
    monkeypatch.setattr(mcp_browser_profiles, "get_skyvern", lambda: fake_skyvern)

    get_result = await mcp_browser_profiles.skyvern_browser_profile_get("bp_123")
    update_result = await mcp_browser_profiles.skyvern_browser_profile_update(
        "bp_123",
        name="Renamed",
        description="Updated",
    )
    delete_result = await mcp_browser_profiles.skyvern_browser_profile_delete("bp_123")

    assert get_result["ok"] is True
    assert update_result["ok"] is True
    assert delete_result["data"] == {"browser_profile_id": "bp_123", "deleted": True}
    fake_skyvern.get_browser_profile.assert_awaited_once_with("bp_123")
    fake_skyvern.update_browser_profile.assert_awaited_once_with(
        "bp_123",
        name="Renamed",
        description="Updated",
    )
    fake_skyvern.delete_browser_profile.assert_awaited_once_with("bp_123")


@pytest.mark.asyncio
async def test_browser_profile_update_rejects_empty_update() -> None:
    result = await mcp_browser_profiles.skyvern_browser_profile_update("bp_123")

    assert result["ok"] is False
    assert result["error"]["code"] == "INVALID_INPUT"


def test_browser_profile_tools_are_exported() -> None:
    assert skyvern_browser_profile_create is mcp_browser_profiles.skyvern_browser_profile_create
