"""Tests for skyvern.forge.sdk.routes.google_sheets helper functions.

Focuses on _mint_access_token's exception triage so DB outages don't get
mislabeled as user reconnect prompts.
"""

from collections.abc import Callable
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock
from urllib.parse import parse_qs, urlparse

import httpx
import pytest
from fastapi import FastAPI, HTTPException
from sqlalchemy.exc import OperationalError

from skyvern.forge import app
from skyvern.forge.sdk.experimentation.drive_shared_drive_search import DRIVE_SHARED_DRIVE_SEARCH_FLAG
from skyvern.forge.sdk.routes import google_sheets as sheets_routes
from skyvern.forge.sdk.services import org_auth_service


def _patch_session(monkeypatch: pytest.MonkeyPatch) -> None:
    """MagicMock's __aexit__ returns truthy by default, which would suppress
    in-block exceptions; pin it to None so raises propagate to the caller."""
    from skyvern.forge import app

    monkeypatch.setattr(
        app,
        "DATABASE",
        MagicMock(
            Session=lambda: MagicMock(
                __aenter__=AsyncMock(return_value=MagicMock()),
                __aexit__=AsyncMock(return_value=None),
            )
        ),
    )


@pytest.mark.asyncio
async def test_mint_access_token_forwards_organization_id_to_refresh(monkeypatch: pytest.MonkeyPatch) -> None:
    secrets = MagicMock()
    load_mock = AsyncMock(return_value=secrets)
    access_mock = AsyncMock(return_value="access-token")
    monkeypatch.setattr(sheets_routes.google_oauth_service, "load_credential_secrets", load_mock)
    monkeypatch.setattr(sheets_routes.google_oauth_service, "access_token_from_secrets", access_mock)

    token = await sheets_routes._mint_access_token("org_1", "cred_1")

    assert token == "access-token"
    load_mock.assert_awaited_once_with(organization_id="org_1", credential_id="cred_1")
    access_mock.assert_awaited_once_with(secrets, organization_id="org_1")


@pytest.mark.asyncio
async def test_mint_access_token_returns_503_on_database_error(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        "skyvern.forge.sdk.services.google_oauth_service.load_credential_secrets",
        AsyncMock(side_effect=OperationalError("SELECT 1", {}, BaseException("connection reset"))),
    )
    _patch_session(monkeypatch)

    with pytest.raises(HTTPException) as exc_info:
        await sheets_routes._mint_access_token("o_1", "cred_1")

    assert exc_info.value.status_code == 503
    assert exc_info.value.detail == "Database unavailable"


@pytest.mark.asyncio
async def test_mint_access_token_returns_409_reconnect_on_decrypt_failure(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        "skyvern.forge.sdk.services.google_oauth_service.load_credential_secrets",
        AsyncMock(side_effect=Exception("Failed to decrypt token")),
    )
    _patch_session(monkeypatch)

    with pytest.raises(HTTPException) as exc_info:
        await sheets_routes._mint_access_token("o_1", "cred_1")

    assert exc_info.value.status_code == 409
    assert exc_info.value.detail["code"] == "reconnect_required"


@pytest.mark.asyncio
@pytest.mark.parametrize("organization_id", ["o_flag_on", "o_flag_off"])
async def test_list_spreadsheets_route_searches_all_drives_only_for_flagged_org(
    monkeypatch: pytest.MonkeyPatch,
    mock_sheets_transport: Callable[[Callable[[httpx.Request], httpx.Response]], None],
    organization_id: str,
) -> None:
    captured: dict[str, str | None] = {}

    def google(request: httpx.Request) -> httpx.Response:
        captured["url"] = str(request.url)
        captured["auth"] = request.headers.get("Authorization")
        return httpx.Response(
            200,
            json={
                "nextPageToken": "tok-2",
                "incompleteSearch": True,
                "files": [
                    {
                        "id": "1xyz",
                        "name": "Leads",
                        "modifiedTime": "2026-04-10T00:00:00Z",
                        "webViewLink": "https://docs.google.com/spreadsheets/d/1xyz",
                    },
                    {"id": "", "name": "skip-empty-id"},
                ],
            },
        )

    async def flag_for_one_org(feature_name: str, distinct_id: str, properties: dict | None = None) -> bool:
        return feature_name == DRIVE_SHARED_DRIVE_SEARCH_FLAG and distinct_id == "o_flag_on"

    async def mint(org_id: str, credential_id: str) -> str:
        return "at-1"

    real_client = httpx.AsyncClient
    mock_sheets_transport(google)
    monkeypatch.setattr(app.EXPERIMENTATION_PROVIDER, "is_feature_enabled_cached", flag_for_one_org)
    monkeypatch.setattr(sheets_routes, "_mint_access_token", mint)
    api = FastAPI()
    api.include_router(sheets_routes.google_sheets_router, prefix="/v1/google/sheets")
    api.dependency_overrides[org_auth_service.get_current_org] = lambda: SimpleNamespace(
        organization_id=organization_id
    )

    async with real_client(transport=httpx.ASGITransport(app=api), base_url="http://skyvern.test") as client:
        response = await client.get(
            "/v1/google/sheets/spreadsheets",
            params={"credential_id": "cred_1", "q": "leads", "page_token": "tok-1", "page_size": 50},
        )

    assert response.status_code == 200
    body = response.json()
    assert body["next_page_token"] == "tok-2"
    assert [s["id"] for s in body["spreadsheets"]] == ["1xyz"]
    assert captured["auth"] == "Bearer at-1"
    params = parse_qs(urlparse(captured["url"]).query)
    assert params["pageSize"] == ["50"]
    assert params["pageToken"] == ["tok-1"]
    assert params["supportsAllDrives"] == ["true"]
    assert params["includeItemsFromAllDrives"] == ["true"]
    if organization_id == "o_flag_on":
        assert params["corpora"] == ["allDrives"]
        assert "incompleteSearch" in params["fields"][0]
        assert body["incomplete_search"] is True
    else:
        assert "corpora" not in params
        assert params["fields"] == ["nextPageToken,files(id,name,modifiedTime,webViewLink)"]
        assert "incomplete_search" not in body
