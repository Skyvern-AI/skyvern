"""Tests for PATCH /browser_profiles/{profile_id}."""

from __future__ import annotations

from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from fastapi.testclient import TestClient

from skyvern.exceptions import BrowserProfileNotFound, SkyvernHTTPException
from skyvern.forge import app as forge_app
from skyvern.forge.sdk.routes import routers as routers_module
from skyvern.forge.sdk.schemas.browser_profiles import BrowserProfile
from skyvern.forge.sdk.services import org_auth_service

ORG_ID = "org_oss"
PROFILE_ID = "bp_test_profile"


def _profile(
    name: str = "My Profile",
    description: str | None = "A profile",
    organization_id: str = ORG_ID,
    profile_id: str = PROFILE_ID,
) -> BrowserProfile:
    now = datetime.now(timezone.utc)
    return BrowserProfile(
        browser_profile_id=profile_id,
        organization_id=organization_id,
        name=name,
        description=description,
        source_browser_type=None,
        created_at=now,
        modified_at=now,
        deleted_at=None,
    )


def _build_client(monkeypatch: pytest.MonkeyPatch) -> tuple[TestClient, AsyncMock]:
    async def _fake_org() -> SimpleNamespace:
        return SimpleNamespace(organization_id=ORG_ID)

    update_browser_profile = AsyncMock()
    monkeypatch.setattr(
        forge_app.DATABASE.browser_sessions,
        "update_browser_profile",
        update_browser_profile,
    )

    fastapi_app = FastAPI()
    fastapi_app.dependency_overrides[org_auth_service.get_current_org] = _fake_org
    fastapi_app.include_router(routers_module.base_router, prefix="/v1")

    @fastapi_app.exception_handler(SkyvernHTTPException)
    async def _handle_skyvern_http_exception(request: Request, exc: SkyvernHTTPException) -> JSONResponse:
        return JSONResponse(status_code=exc.status_code, content={"detail": exc.message})

    return TestClient(fastapi_app), update_browser_profile


def test_update_browser_profile_renames(monkeypatch: pytest.MonkeyPatch) -> None:
    client, update_browser_profile = _build_client(monkeypatch)
    update_browser_profile.return_value = _profile(name="Renamed")

    response = client.patch(
        f"/v1/browser_profiles/{PROFILE_ID}",
        json={"name": "Renamed"},
    )

    assert response.status_code == 200
    body = response.json()
    assert body["browser_profile_id"] == PROFILE_ID
    assert body["name"] == "Renamed"
    update_browser_profile.assert_awaited_once_with(
        profile_id=PROFILE_ID,
        organization_id=ORG_ID,
        name="Renamed",
        description=None,
    )


def test_update_browser_profile_description_only(monkeypatch: pytest.MonkeyPatch) -> None:
    client, update_browser_profile = _build_client(monkeypatch)
    update_browser_profile.return_value = _profile(description="Brand new description")

    response = client.patch(
        f"/v1/browser_profiles/{PROFILE_ID}",
        json={"description": "Brand new description"},
    )

    assert response.status_code == 200
    body = response.json()
    assert body["description"] == "Brand new description"
    update_browser_profile.assert_awaited_once_with(
        profile_id=PROFILE_ID,
        organization_id=ORG_ID,
        name=None,
        description="Brand new description",
    )


def test_update_browser_profile_name_and_description(monkeypatch: pytest.MonkeyPatch) -> None:
    client, update_browser_profile = _build_client(monkeypatch)
    update_browser_profile.return_value = _profile(name="Renamed", description="New desc")

    response = client.patch(
        f"/v1/browser_profiles/{PROFILE_ID}",
        json={"name": "Renamed", "description": "New desc"},
    )

    assert response.status_code == 200
    body = response.json()
    assert body["name"] == "Renamed"
    assert body["description"] == "New desc"
    update_browser_profile.assert_awaited_once_with(
        profile_id=PROFILE_ID,
        organization_id=ORG_ID,
        name="Renamed",
        description="New desc",
    )


def test_update_browser_profile_not_found(monkeypatch: pytest.MonkeyPatch) -> None:
    client, update_browser_profile = _build_client(monkeypatch)
    update_browser_profile.side_effect = BrowserProfileNotFound(profile_id=PROFILE_ID, organization_id=ORG_ID)

    response = client.patch(
        f"/v1/browser_profiles/{PROFILE_ID}",
        json={"name": "Renamed"},
    )

    assert response.status_code == 404


def test_update_browser_profile_cross_org_returns_404(monkeypatch: pytest.MonkeyPatch) -> None:
    client, update_browser_profile = _build_client(monkeypatch)

    owning_org_id = "org_other"

    async def _org_scoped_update(*, organization_id: str, **kwargs: object) -> BrowserProfile:
        if organization_id != owning_org_id:
            raise BrowserProfileNotFound(profile_id=PROFILE_ID, organization_id=organization_id)
        return _profile(organization_id=owning_org_id)

    update_browser_profile.side_effect = _org_scoped_update

    response = client.patch(
        f"/v1/browser_profiles/{PROFILE_ID}",
        json={"name": "Renamed"},
    )

    assert response.status_code == 404
    update_browser_profile.assert_awaited_once_with(
        profile_id=PROFILE_ID,
        organization_id=ORG_ID,
        name="Renamed",
        description=None,
    )


def test_update_browser_profile_empty_body_is_rejected(monkeypatch: pytest.MonkeyPatch) -> None:
    client, update_browser_profile = _build_client(monkeypatch)

    response = client.patch(f"/v1/browser_profiles/{PROFILE_ID}", json={})

    assert response.status_code == 422
    update_browser_profile.assert_not_awaited()


def test_update_browser_profile_alias_trailing_slash(monkeypatch: pytest.MonkeyPatch) -> None:
    client, update_browser_profile = _build_client(monkeypatch)
    update_browser_profile.return_value = _profile(name="Renamed")

    response = client.patch(
        f"/v1/browser_profiles/{PROFILE_ID}/",
        json={"name": "Renamed"},
    )

    assert response.status_code == 200
    update_browser_profile.assert_awaited_once_with(
        profile_id=PROFILE_ID,
        organization_id=ORG_ID,
        name="Renamed",
        description=None,
    )
