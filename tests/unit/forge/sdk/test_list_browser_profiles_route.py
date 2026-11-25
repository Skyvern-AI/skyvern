"""Tests for GET /browser_profiles pagination."""

from __future__ import annotations

from datetime import datetime, timezone
from types import SimpleNamespace
from typing import cast
from unittest.mock import AsyncMock

import pytest
from fastapi import Request
from fastapi.responses import JSONResponse
from fastapi.testclient import TestClient

from skyvern.exceptions import SkyvernHTTPException
from skyvern.forge import app as forge_app
from skyvern.forge.sdk.schemas.browser_profiles import BrowserProfile
from skyvern.forge.sdk.services import org_auth_service
from tests.unit.shared_route_app import shared_route_app

ORG_ID = "org_oss"


def _profile(profile_id: str, name: str = "P") -> BrowserProfile:
    now = datetime.now(timezone.utc)
    return BrowserProfile(
        browser_profile_id=profile_id,
        organization_id=ORG_ID,
        name=name,
        description=None,
        source_browser_type=None,
        created_at=now,
        modified_at=now,
        deleted_at=None,
    )


def _build_client(monkeypatch: pytest.MonkeyPatch) -> tuple[TestClient, AsyncMock]:
    async def _fake_org() -> SimpleNamespace:
        return SimpleNamespace(organization_id=ORG_ID)

    monkeypatch.setattr(forge_app.AGENT_FUNCTION, "credential_folders_restricted", AsyncMock(return_value=False))
    monkeypatch.setattr(forge_app.AGENT_FUNCTION, "browser_profile_source_rules_apply", AsyncMock(return_value=False))
    list_browser_profiles = AsyncMock()
    monkeypatch.setattr(
        forge_app.DATABASE.browser_sessions,
        "list_browser_profiles",
        list_browser_profiles,
    )

    fastapi_app = shared_route_app(monkeypatch)
    fastapi_app.dependency_overrides[org_auth_service.get_current_org] = _fake_org

    @fastapi_app.exception_handler(SkyvernHTTPException)
    async def _handle_skyvern_http_exception(request: Request, exc: SkyvernHTTPException) -> JSONResponse:
        return JSONResponse(status_code=exc.status_code, content={"detail": exc.message})

    return TestClient(fastapi_app), list_browser_profiles


@pytest.mark.parametrize(
    ("source_rules_apply", "hidden_by_source", "page_size", "expected_ids"),
    [(False, set(), 10, ["bp_1", "bp_2"]), (True, {"bp_1"}, 2, ["bp_2", "bp_3"])],
    ids=["no_source_rules", "source_hidden_before_paging"],
)
def test_list_browser_profiles_default_pagination(
    monkeypatch: pytest.MonkeyPatch,
    source_rules_apply: bool,
    hidden_by_source: set[str],
    page_size: int,
    expected_ids: list[str],
) -> None:
    client, list_browser_profiles = _build_client(monkeypatch)
    stored = [_profile(f"bp_{index}") for index in range(1, 3 if not source_rules_apply else 31)]
    checked: set[str] = set()

    async def paged(**kwargs: object) -> list[BrowserProfile]:
        size = cast(int, kwargs["page_size"])
        offset = (cast(int, kwargs["page"]) - 1) * size
        return stored[offset : offset + size]

    async def hidden(_principal: object, _organization_id: str, profile_ids: set[str]) -> set[str]:
        checked.update(profile_ids)
        return profile_ids & hidden_by_source

    list_browser_profiles.side_effect = paged
    monkeypatch.setattr(
        forge_app.AGENT_FUNCTION, "browser_profile_source_rules_apply", AsyncMock(return_value=source_rules_apply)
    )
    monkeypatch.setattr(forge_app.AGENT_FUNCTION, "hidden_browser_profile_ids", hidden)

    response = client.get(f"/v1/browser_profiles?page_size={page_size}")

    assert response.status_code == 200
    body = response.json()
    assert [p["browser_profile_id"] for p in body] == expected_ids
    if not source_rules_apply:
        list_browser_profiles.assert_awaited_once_with(
            organization_id=ORG_ID,
            include_deleted=False,
            page=1,
            page_size=10,
            search_key=None,
            managed=None,
            exclude_search_profile_ids=set(),
        )
    else:
        # Only the rows needed to fill the page are checked, not every saved profile in the organization.
        assert len(checked) < len(stored)


def test_list_browser_profiles_explicit_pagination(monkeypatch: pytest.MonkeyPatch) -> None:
    client, list_browser_profiles = _build_client(monkeypatch)
    list_browser_profiles.return_value = [_profile("bp_11")]

    response = client.get("/v1/browser_profiles?page=2&page_size=10")

    assert response.status_code == 200
    list_browser_profiles.assert_awaited_once_with(
        organization_id=ORG_ID,
        include_deleted=False,
        page=2,
        page_size=10,
        search_key=None,
        managed=None,
        exclude_search_profile_ids=set(),
    )


def test_list_browser_profiles_rejects_page_zero(monkeypatch: pytest.MonkeyPatch) -> None:
    client, list_browser_profiles = _build_client(monkeypatch)

    response = client.get("/v1/browser_profiles?page=0")

    assert response.status_code == 422
    list_browser_profiles.assert_not_awaited()


def test_list_browser_profiles_rejects_page_size_zero(monkeypatch: pytest.MonkeyPatch) -> None:
    client, list_browser_profiles = _build_client(monkeypatch)

    response = client.get("/v1/browser_profiles?page_size=0")

    assert response.status_code == 422
    list_browser_profiles.assert_not_awaited()


def test_list_browser_profiles_alias_trailing_slash(monkeypatch: pytest.MonkeyPatch) -> None:
    client, list_browser_profiles = _build_client(monkeypatch)
    list_browser_profiles.return_value = []

    response = client.get("/v1/browser_profiles/?page=3&page_size=5")

    assert response.status_code == 200
    list_browser_profiles.assert_awaited_once_with(
        organization_id=ORG_ID,
        include_deleted=False,
        page=3,
        page_size=5,
        search_key=None,
        managed=None,
        exclude_search_profile_ids=set(),
    )


def test_list_browser_profiles_search_key_hit_on_name(monkeypatch: pytest.MonkeyPatch) -> None:
    client, list_browser_profiles = _build_client(monkeypatch)
    list_browser_profiles.return_value = [_profile("bp_match", name="production_profile")]

    response = client.get("/v1/browser_profiles?search_key=production")

    assert response.status_code == 200
    body = response.json()
    assert [p["browser_profile_id"] for p in body] == ["bp_match"]
    list_browser_profiles.assert_awaited_once_with(
        organization_id=ORG_ID,
        include_deleted=False,
        page=1,
        page_size=10,
        search_key="production",
        managed=None,
        exclude_search_profile_ids=set(),
    )


def test_list_browser_profiles_search_key_hit_on_description(monkeypatch: pytest.MonkeyPatch) -> None:
    client, list_browser_profiles = _build_client(monkeypatch)
    list_browser_profiles.return_value = [_profile("bp_desc")]

    response = client.get("/v1/browser_profiles?search_key=staging_login")

    assert response.status_code == 200
    body = response.json()
    assert [p["browser_profile_id"] for p in body] == ["bp_desc"]
    list_browser_profiles.assert_awaited_once_with(
        organization_id=ORG_ID,
        include_deleted=False,
        page=1,
        page_size=10,
        search_key="staging_login",
        managed=None,
        exclude_search_profile_ids=set(),
    )


def test_list_browser_profiles_search_key_miss(monkeypatch: pytest.MonkeyPatch) -> None:
    client, list_browser_profiles = _build_client(monkeypatch)
    list_browser_profiles.return_value = []

    response = client.get("/v1/browser_profiles?search_key=no_such_profile")

    assert response.status_code == 200
    assert response.json() == []
    list_browser_profiles.assert_awaited_once_with(
        organization_id=ORG_ID,
        include_deleted=False,
        page=1,
        page_size=10,
        search_key="no_such_profile",
        managed=None,
        exclude_search_profile_ids=set(),
    )


def test_list_browser_profiles_search_key_with_pagination(monkeypatch: pytest.MonkeyPatch) -> None:
    client, list_browser_profiles = _build_client(monkeypatch)
    list_browser_profiles.return_value = []

    response = client.get("/v1/browser_profiles?search_key=prod&page=2&page_size=5")

    assert response.status_code == 200
    list_browser_profiles.assert_awaited_once_with(
        organization_id=ORG_ID,
        include_deleted=False,
        page=2,
        page_size=5,
        search_key="prod",
        managed=None,
        exclude_search_profile_ids=set(),
    )


def test_list_browser_profiles_search_key_with_include_deleted(monkeypatch: pytest.MonkeyPatch) -> None:
    client, list_browser_profiles = _build_client(monkeypatch)
    list_browser_profiles.return_value = []

    response = client.get("/v1/browser_profiles?search_key=archived&include_deleted=true")

    assert response.status_code == 200
    list_browser_profiles.assert_awaited_once_with(
        organization_id=ORG_ID,
        include_deleted=True,
        page=1,
        page_size=10,
        search_key="archived",
        managed=None,
        exclude_search_profile_ids=set(),
    )
