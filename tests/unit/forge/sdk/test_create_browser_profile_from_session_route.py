"""Tests for POST /browser_profiles with a browser_session_id (create-from-session + source reap)."""

from __future__ import annotations

from datetime import datetime
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
from skyvern.forge.sdk.schemas.persistent_browser_sessions import PersistentBrowserSession
from skyvern.forge.sdk.services import org_auth_service


def _session(**kwargs: object) -> PersistentBrowserSession:
    base: dict[str, object] = {
        "persistent_browser_session_id": "pbs_1",
        "organization_id": "org_oss",
        "created_at": datetime(2026, 1, 1),
        "modified_at": datetime(2026, 1, 1),
    }
    base.update(kwargs)
    return PersistentBrowserSession(**base)


def _profile() -> BrowserProfile:
    return BrowserProfile(
        browser_profile_id="bp_new",
        organization_id="org_oss",
        name="my profile",
        created_at=datetime(2026, 1, 1),
        modified_at=datetime(2026, 1, 1),
    )


def _build_client(monkeypatch: pytest.MonkeyPatch) -> tuple[TestClient, SimpleNamespace]:
    async def _fake_org() -> SimpleNamespace:
        return SimpleNamespace(organization_id="org_oss")

    mocks = SimpleNamespace(
        get_session=AsyncMock(),
        create_profile=AsyncMock(return_value=_profile()),
        delete_db_profile=AsyncMock(),
        retrieve_profile=AsyncMock(),
        store_profile=AsyncMock(),
        delete_profile_blob=AsyncMock(),
    )
    monkeypatch.setattr(forge_app.DATABASE.browser_sessions, "get_persistent_browser_session", mocks.get_session)
    monkeypatch.setattr(forge_app.DATABASE.browser_sessions, "create_browser_profile", mocks.create_profile)
    monkeypatch.setattr(forge_app.DATABASE.browser_sessions, "delete_browser_profile", mocks.delete_db_profile)
    monkeypatch.setattr(forge_app.STORAGE, "retrieve_browser_profile", mocks.retrieve_profile)
    monkeypatch.setattr(forge_app.STORAGE, "store_browser_profile", mocks.store_profile)
    monkeypatch.setattr(forge_app.STORAGE, "delete_browser_profile", mocks.delete_profile_blob)

    fastapi_app = FastAPI()
    fastapi_app.dependency_overrides[org_auth_service.get_current_org] = _fake_org
    fastapi_app.include_router(routers_module.base_router, prefix="/v1")

    @fastapi_app.exception_handler(SkyvernHTTPException)
    async def _handle(request: Request, exc: SkyvernHTTPException) -> JSONResponse:
        return JSONResponse(status_code=exc.status_code, content={"detail": exc.message})

    # raise_server_exceptions=False so an un-mapped error surfaces as a 500 response (as in prod)
    # rather than propagating out of the TestClient — the failed-promote path re-raises after rollback.
    return TestClient(fastapi_app, raise_server_exceptions=False), mocks


def test_promote_deletes_source_session_blob(monkeypatch: pytest.MonkeyPatch) -> None:
    client, mocks = _build_client(monkeypatch)
    mocks.get_session.return_value = _session(generate_browser_profile=True)
    mocks.retrieve_profile.return_value = "/tmp/session_dir"

    response = client.post("/v1/browser_profiles/", json={"name": "my profile", "browser_session_id": "pbs_1"})

    assert response.status_code == 200
    mocks.store_profile.assert_awaited_once()
    mocks.delete_profile_blob.assert_awaited_once_with(organization_id="org_oss", profile_id="pbs_1")


def test_failed_promote_does_not_delete_source_blob(monkeypatch: pytest.MonkeyPatch) -> None:
    client, mocks = _build_client(monkeypatch)
    mocks.get_session.return_value = _session(generate_browser_profile=True)
    mocks.retrieve_profile.return_value = "/tmp/session_dir"
    mocks.store_profile.side_effect = RuntimeError("s3 upload failed")

    response = client.post("/v1/browser_profiles/", json={"name": "my profile", "browser_session_id": "pbs_1"})

    assert response.status_code == 500
    mocks.delete_db_profile.assert_awaited_once()  # rolled back the half-created profile
    mocks.delete_profile_blob.assert_not_awaited()  # never reap the source on a failed promote


def test_promote_succeeds_even_if_source_reap_fails(monkeypatch: pytest.MonkeyPatch) -> None:
    client, mocks = _build_client(monkeypatch)
    mocks.get_session.return_value = _session(generate_browser_profile=True)
    mocks.retrieve_profile.return_value = "/tmp/session_dir"
    mocks.delete_profile_blob.side_effect = RuntimeError("s3 AccessDenied")

    response = client.post("/v1/browser_profiles/", json={"name": "my profile", "browser_session_id": "pbs_1"})

    assert response.status_code == 200
    mocks.delete_profile_blob.assert_awaited_once()


def test_non_opted_in_session_returns_400(monkeypatch: pytest.MonkeyPatch) -> None:
    client, mocks = _build_client(monkeypatch)
    mocks.get_session.return_value = _session(generate_browser_profile=False)
    mocks.retrieve_profile.return_value = None

    response = client.post("/v1/browser_profiles/", json={"name": "my profile", "browser_session_id": "pbs_1"})

    assert response.status_code == 400
    assert "not configured to generate a browser profile" in response.json()["detail"]
    mocks.store_profile.assert_not_awaited()
    mocks.delete_profile_blob.assert_not_awaited()
