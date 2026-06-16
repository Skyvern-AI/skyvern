"""Tests for DELETE /browser_profiles/{profile_id} (soft-delete + S3 blob reap)."""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from fastapi.testclient import TestClient

from skyvern.exceptions import BrowserProfileNotFound, SkyvernHTTPException
from skyvern.forge import app as forge_app
from skyvern.forge.sdk.routes import routers as routers_module
from skyvern.forge.sdk.services import org_auth_service


def _build_client(monkeypatch: pytest.MonkeyPatch) -> tuple[TestClient, SimpleNamespace]:
    async def _fake_org() -> SimpleNamespace:
        return SimpleNamespace(organization_id="org_oss")

    mocks = SimpleNamespace(
        delete_db_profile=AsyncMock(),
        delete_profile_blob=AsyncMock(),
    )
    monkeypatch.setattr(forge_app.DATABASE.browser_sessions, "delete_browser_profile", mocks.delete_db_profile)
    monkeypatch.setattr(forge_app.STORAGE, "delete_browser_profile", mocks.delete_profile_blob)

    fastapi_app = FastAPI()
    fastapi_app.dependency_overrides[org_auth_service.get_current_org] = _fake_org
    fastapi_app.include_router(routers_module.base_router, prefix="/v1")

    @fastapi_app.exception_handler(SkyvernHTTPException)
    async def _handle(request: Request, exc: SkyvernHTTPException) -> JSONResponse:
        return JSONResponse(status_code=exc.status_code, content={"detail": exc.message})

    return TestClient(fastapi_app), mocks


def test_delete_reaps_profile_blob(monkeypatch: pytest.MonkeyPatch) -> None:
    client, mocks = _build_client(monkeypatch)

    response = client.delete("/v1/browser_profiles/bp_123/")

    assert response.status_code == 204
    mocks.delete_db_profile.assert_awaited_once_with(profile_id="bp_123", organization_id="org_oss")
    mocks.delete_profile_blob.assert_awaited_once_with(organization_id="org_oss", profile_id="bp_123")


def test_delete_succeeds_when_blob_already_gone(monkeypatch: pytest.MonkeyPatch) -> None:
    client, mocks = _build_client(monkeypatch)
    mocks.delete_profile_blob.side_effect = RuntimeError("object not found / s3 error")

    response = client.delete("/v1/browser_profiles/bp_123/")

    assert response.status_code == 204
    mocks.delete_profile_blob.assert_awaited_once()


def test_delete_missing_profile_does_not_reap_blob(monkeypatch: pytest.MonkeyPatch) -> None:
    client, mocks = _build_client(monkeypatch)
    mocks.delete_db_profile.side_effect = BrowserProfileNotFound(profile_id="bp_missing")

    response = client.delete("/v1/browser_profiles/bp_missing/")

    assert response.status_code == 404
    mocks.delete_profile_blob.assert_not_awaited()
