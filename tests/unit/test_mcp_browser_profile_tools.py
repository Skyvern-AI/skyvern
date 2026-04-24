from __future__ import annotations

from datetime import datetime, timezone
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock

import pytest

from skyvern.cli.core.result import ErrorCode
from skyvern.cli.mcp_tools import browser_profile as mcp_bp
from skyvern.client.errors import BadRequestError, ConflictError, NotFoundError


def _make_profile(**overrides: Any) -> SimpleNamespace:
    created = datetime(2026, 4, 20, 12, 0, 0, tzinfo=timezone.utc)
    defaults = {
        "browser_profile_id": "bp_abc123",
        "organization_id": "o_123",
        "name": "example-app-signed-in",
        "description": "logged in",
        "created_at": created,
        "modified_at": created,
        "deleted_at": None,
    }
    defaults.update(overrides)
    return SimpleNamespace(**defaults)


@pytest.mark.asyncio
async def test_list_returns_normalized_profiles(monkeypatch: pytest.MonkeyPatch) -> None:
    skyvern = SimpleNamespace(
        list_browser_profiles=AsyncMock(return_value=[_make_profile(), _make_profile(browser_profile_id="bp_def456")])
    )
    monkeypatch.setattr(mcp_bp, "get_skyvern", lambda: skyvern)

    result = await mcp_bp.skyvern_browser_profile_list()

    assert result["ok"] is True
    assert result["action"] == "skyvern_browser_profile_list"
    assert result["data"]["count"] == 2
    ids = [p["browser_profile_id"] for p in result["data"]["browser_profiles"]]
    assert ids == ["bp_abc123", "bp_def456"]
    # Datetimes must be serialized to ISO strings.
    assert isinstance(result["data"]["browser_profiles"][0]["created_at"], str)
    skyvern.list_browser_profiles.assert_awaited_once_with(include_deleted=False)


@pytest.mark.asyncio
async def test_list_passes_include_deleted(monkeypatch: pytest.MonkeyPatch) -> None:
    skyvern = SimpleNamespace(list_browser_profiles=AsyncMock(return_value=[]))
    monkeypatch.setattr(mcp_bp, "get_skyvern", lambda: skyvern)

    result = await mcp_bp.skyvern_browser_profile_list(include_deleted=True)

    assert result["ok"] is True
    assert result["data"]["count"] == 0
    skyvern.list_browser_profiles.assert_awaited_once_with(include_deleted=True)


@pytest.mark.asyncio
async def test_list_sdk_exception_returns_error(monkeypatch: pytest.MonkeyPatch) -> None:
    skyvern = SimpleNamespace(list_browser_profiles=AsyncMock(side_effect=RuntimeError("boom")))
    monkeypatch.setattr(mcp_bp, "get_skyvern", lambda: skyvern)

    result = await mcp_bp.skyvern_browser_profile_list()

    assert result["ok"] is False
    assert result["error"]["code"] == ErrorCode.API_ERROR
    assert "boom" in result["error"]["message"]


@pytest.mark.parametrize(
    ("browser_profile_id", "expected_text"),
    [
        ("not-a-real-id", "bp_"),
        ("bp_../etc/passwd", "path separators"),
    ],
)
@pytest.mark.asyncio
async def test_get_rejects_invalid_id(browser_profile_id: str, expected_text: str) -> None:
    result = await mcp_bp.skyvern_browser_profile_get(browser_profile_id=browser_profile_id)

    assert result["ok"] is False
    assert result["error"]["code"] == ErrorCode.INVALID_INPUT
    assert expected_text in f"{result['error']['message']} {result['error']['hint']}"


@pytest.mark.asyncio
async def test_get_returns_profile(monkeypatch: pytest.MonkeyPatch) -> None:
    skyvern = SimpleNamespace(get_browser_profile=AsyncMock(return_value=_make_profile()))
    monkeypatch.setattr(mcp_bp, "get_skyvern", lambda: skyvern)

    result = await mcp_bp.skyvern_browser_profile_get(browser_profile_id="bp_abc123")

    assert result["ok"] is True
    assert result["data"]["browser_profile_id"] == "bp_abc123"
    assert result["data"]["name"] == "example-app-signed-in"
    skyvern.get_browser_profile.assert_awaited_once_with("bp_abc123")


@pytest.mark.asyncio
async def test_get_404_maps_to_invalid_input(monkeypatch: pytest.MonkeyPatch) -> None:
    skyvern = SimpleNamespace(get_browser_profile=AsyncMock(side_effect=NotFoundError(body={"detail": "not found"})))
    monkeypatch.setattr(mcp_bp, "get_skyvern", lambda: skyvern)

    result = await mcp_bp.skyvern_browser_profile_get(browser_profile_id="bp_missing")

    assert result["ok"] is False
    assert result["error"]["code"] == ErrorCode.INVALID_INPUT
    assert "not found" in result["error"]["message"]


@pytest.mark.asyncio
async def test_create_requires_name() -> None:
    result = await mcp_bp.skyvern_browser_profile_create(name="", browser_session_id="pbs_123")
    assert result["ok"] is False
    assert result["error"]["code"] == ErrorCode.INVALID_INPUT
    assert "name" in result["error"]["message"]


@pytest.mark.asyncio
async def test_create_requires_exactly_one_source() -> None:
    both = await mcp_bp.skyvern_browser_profile_create(name="n", browser_session_id="pbs_1", workflow_run_id="wr_1")
    assert both["ok"] is False
    assert both["error"]["code"] == ErrorCode.INVALID_INPUT
    assert "exactly ONE" in both["error"]["message"]

    neither = await mcp_bp.skyvern_browser_profile_create(name="n")
    assert neither["ok"] is False
    assert neither["error"]["code"] == ErrorCode.INVALID_INPUT


@pytest.mark.parametrize(
    ("source_kwargs", "expected_hint"),
    [
        ({"browser_session_id": "wr_wrong_prefix"}, "pbs_"),
        ({"workflow_run_id": "pbs_wrong_prefix"}, "wr_"),
        # tsk_v2_ IDs look valid to the generic run-id validator but the
        # browser-profile-create source lookup only accepts wr_. Reject
        # client-side so agents get a pointed INVALID_INPUT instead of a
        # less helpful server-side failure.
        ({"workflow_run_id": "tsk_v2_abc123"}, "wr_"),
    ],
)
@pytest.mark.asyncio
async def test_create_rejects_bad_source_id(source_kwargs: dict[str, str], expected_hint: str) -> None:
    result = await mcp_bp.skyvern_browser_profile_create(name="n", **source_kwargs)

    assert result["ok"] is False
    assert result["error"]["code"] == ErrorCode.INVALID_INPUT
    assert expected_hint in result["error"]["hint"]


@pytest.mark.asyncio
async def test_create_from_session_happy_path(monkeypatch: pytest.MonkeyPatch) -> None:
    skyvern = SimpleNamespace(create_browser_profile=AsyncMock(return_value=_make_profile()))
    monkeypatch.setattr(mcp_bp, "get_skyvern", lambda: skyvern)

    result = await mcp_bp.skyvern_browser_profile_create(
        name="example-app-signed-in", browser_session_id="pbs_abc", description="logged in"
    )

    assert result["ok"] is True
    assert result["data"]["browser_profile_id"] == "bp_abc123"
    skyvern.create_browser_profile.assert_awaited_once_with(
        name="example-app-signed-in",
        description="logged in",
        browser_session_id="pbs_abc",
        workflow_run_id=None,
    )


@pytest.mark.asyncio
async def test_create_from_workflow_run_happy_path(monkeypatch: pytest.MonkeyPatch) -> None:
    skyvern = SimpleNamespace(create_browser_profile=AsyncMock(return_value=_make_profile()))
    monkeypatch.setattr(mcp_bp, "get_skyvern", lambda: skyvern)

    result = await mcp_bp.skyvern_browser_profile_create(name="wf-run-profile", workflow_run_id="wr_xyz")

    assert result["ok"] is True
    skyvern.create_browser_profile.assert_awaited_once_with(
        name="wf-run-profile",
        description=None,
        browser_session_id=None,
        workflow_run_id="wr_xyz",
    )


@pytest.mark.asyncio
async def test_create_conflict_surfaces_clear_hint(monkeypatch: pytest.MonkeyPatch) -> None:
    skyvern = SimpleNamespace(
        create_browser_profile=AsyncMock(side_effect=ConflictError(body={"detail": "already exists"}))
    )
    monkeypatch.setattr(mcp_bp, "get_skyvern", lambda: skyvern)

    result = await mcp_bp.skyvern_browser_profile_create(name="dup", browser_session_id="pbs_1")

    assert result["ok"] is False
    assert result["error"]["code"] == ErrorCode.INVALID_INPUT
    assert "already exists" in result["error"]["message"]


@pytest.mark.asyncio
async def test_create_archive_not_ready_is_retryable(monkeypatch: pytest.MonkeyPatch) -> None:
    skyvern = SimpleNamespace(
        create_browser_profile=AsyncMock(
            side_effect=BadRequestError(body={"detail": "Browser session does not have a persisted profile archive."})
        )
    )
    monkeypatch.setattr(mcp_bp, "get_skyvern", lambda: skyvern)

    result = await mcp_bp.skyvern_browser_profile_create(name="n", browser_session_id="pbs_pending")

    assert result["ok"] is False
    assert result["error"]["code"] == ErrorCode.ACTION_FAILED
    hint = result["error"]["hint"].lower()
    assert "retry" in hint
    assert "wait" in hint


@pytest.mark.asyncio
async def test_create_source_not_found_maps_to_invalid_input(monkeypatch: pytest.MonkeyPatch) -> None:
    skyvern = SimpleNamespace(create_browser_profile=AsyncMock(side_effect=NotFoundError(body={"detail": "not found"})))
    monkeypatch.setattr(mcp_bp, "get_skyvern", lambda: skyvern)

    result = await mcp_bp.skyvern_browser_profile_create(name="n", browser_session_id="pbs_ghost")

    assert result["ok"] is False
    assert result["error"]["code"] == ErrorCode.INVALID_INPUT
    assert "not found" in result["error"]["message"].lower()


@pytest.mark.asyncio
async def test_create_source_not_found_via_generic_api_error_maps_to_invalid_input(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Reproduce Fern's actual behavior: create_browser_profile only explicitly
    # maps 400/409/422, so a 404 arrives as a generic ApiError, not
    # NotFoundError. The handler must key off status_code to still route to
    # INVALID_INPUT.
    from skyvern.client.core.api_error import ApiError

    skyvern = SimpleNamespace(
        create_browser_profile=AsyncMock(
            side_effect=ApiError(status_code=404, body={"detail": "Browser session pbs_ghost not found"})
        )
    )
    monkeypatch.setattr(mcp_bp, "get_skyvern", lambda: skyvern)

    result = await mcp_bp.skyvern_browser_profile_create(name="n", browser_session_id="pbs_ghost")

    assert result["ok"] is False
    assert result["error"]["code"] == ErrorCode.INVALID_INPUT
    assert "not found" in result["error"]["message"].lower()


@pytest.mark.asyncio
async def test_delete_rejects_invalid_id() -> None:
    result = await mcp_bp.skyvern_browser_profile_delete(browser_profile_id="not-valid")
    assert result["ok"] is False
    assert result["error"]["code"] == ErrorCode.INVALID_INPUT


@pytest.mark.asyncio
async def test_delete_happy_path(monkeypatch: pytest.MonkeyPatch) -> None:
    skyvern = SimpleNamespace(delete_browser_profile=AsyncMock(return_value=None))
    monkeypatch.setattr(mcp_bp, "get_skyvern", lambda: skyvern)

    result = await mcp_bp.skyvern_browser_profile_delete(browser_profile_id="bp_abc123")

    assert result["ok"] is True
    assert result["data"] == {"browser_profile_id": "bp_abc123", "deleted": True}
    skyvern.delete_browser_profile.assert_awaited_once_with("bp_abc123")


@pytest.mark.asyncio
async def test_delete_404_maps_to_invalid_input(monkeypatch: pytest.MonkeyPatch) -> None:
    skyvern = SimpleNamespace(delete_browser_profile=AsyncMock(side_effect=NotFoundError(body={"detail": "gone"})))
    monkeypatch.setattr(mcp_bp, "get_skyvern", lambda: skyvern)

    result = await mcp_bp.skyvern_browser_profile_delete(browser_profile_id="bp_ghost")

    assert result["ok"] is False
    assert result["error"]["code"] == ErrorCode.INVALID_INPUT
    assert "not found" in result["error"]["message"]


def test_tools_registered_on_fastmcp() -> None:
    from skyvern.cli import mcp_tools as pkg

    for name in (
        "skyvern_browser_profile_list",
        "skyvern_browser_profile_get",
        "skyvern_browser_profile_create",
        "skyvern_browser_profile_delete",
    ):
        assert hasattr(pkg, name), f"{name} not re-exported from mcp_tools package"
        assert name in pkg.__all__, f"{name} missing from mcp_tools.__all__"
