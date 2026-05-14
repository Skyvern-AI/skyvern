"""Tests for GET /browser_profiles pagination."""

from __future__ import annotations

from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from fastapi.testclient import TestClient

from skyvern.exceptions import SkyvernHTTPException
from skyvern.forge import app as forge_app
from skyvern.forge.sdk.routes import routers as routers_module
from skyvern.forge.sdk.schemas.browser_profiles import BrowserProfile
from skyvern.forge.sdk.services import org_auth_service

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

    list_browser_profiles = AsyncMock()
    monkeypatch.setattr(
        forge_app.DATABASE.browser_sessions,
        "list_browser_profiles",
        list_browser_profiles,
    )

    fastapi_app = FastAPI()
    fastapi_app.dependency_overrides[org_auth_service.get_current_org] = _fake_org
    fastapi_app.include_router(routers_module.base_router, prefix="/v1")

    @fastapi_app.exception_handler(SkyvernHTTPException)
    async def _handle_skyvern_http_exception(request: Request, exc: SkyvernHTTPException) -> JSONResponse:
        return JSONResponse(status_code=exc.status_code, content={"detail": exc.message})

    return TestClient(fastapi_app), list_browser_profiles


def test_list_browser_profiles_default_pagination(monkeypatch: pytest.MonkeyPatch) -> None:
    client, list_browser_profiles = _build_client(monkeypatch)
    list_browser_profiles.return_value = [_profile("bp_1"), _profile("bp_2")]

    response = client.get("/v1/browser_profiles")

    assert response.status_code == 200
    body = response.json()
    assert [p["browser_profile_id"] for p in body] == ["bp_1", "bp_2"]
    list_browser_profiles.assert_awaited_once_with(
        organization_id=ORG_ID,
        include_deleted=False,
        page=1,
        page_size=10,
        search_key=None,
    )


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
    )
