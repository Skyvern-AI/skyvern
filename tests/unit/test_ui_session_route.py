from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from fastapi import FastAPI, HTTPException, status
from fastapi.testclient import TestClient

from skyvern.forge.sdk.db.enums import OrganizationAuthTokenType
from skyvern.forge.sdk.routes import ui_session
from skyvern.forge.sdk.routes.routers import base_router, legacy_base_router


@pytest.fixture
def client() -> TestClient:
    fastapi_app = FastAPI()
    fastapi_app.include_router(base_router, prefix="/v1")
    fastapi_app.include_router(legacy_base_router, prefix="/api/v1")
    return TestClient(fastapi_app)


@pytest.mark.parametrize("prefix", ["/v1", "/api/v1"])
def test_ui_session_route_mints_from_api_key_only(
    client: TestClient,
    monkeypatch: pytest.MonkeyPatch,
    prefix: str,
) -> None:
    caller_key = "caller-api-key-canary"
    organization_id = "org-ui-session-route"
    minted_token = "minted-ui-session-canary"
    expires_at = 1_893_456_789

    async def resolve_org(api_key: str, _db: object, token_types: tuple[OrganizationAuthTokenType, ...]) -> object:
        assert api_key == caller_key
        assert token_types == (OrganizationAuthTokenType.api,)
        return SimpleNamespace(organization=SimpleNamespace(organization_id=organization_id))

    mint_token = AsyncMock(return_value=(SimpleNamespace(token=minted_token), expires_at))
    monkeypatch.setattr(ui_session, "resolve_org_from_api_key", resolve_org)
    monkeypatch.setattr(ui_session, "create_org_ui_session_token", mint_token)

    response = client.post(f"{prefix}/ui-session", headers={"x-api-key": caller_key})

    assert response.status_code == status.HTTP_200_OK
    assert response.headers["cache-control"] == "no-store"
    assert set(response.json()) == {"token", "expires_at"}
    assert response.json()["token"] == minted_token
    assert response.json()["expires_at"] == expires_at
    mint_token.assert_awaited_once_with(organization_id)


def test_ui_session_route_returns_not_found_when_org_disappears(
    client: TestClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    caller_key = "caller-api-key-canary"
    organization_id = "missing-org-id"
    organizations = SimpleNamespace(get_organization=AsyncMock(return_value=None))
    database = SimpleNamespace(organizations=organizations)

    async def resolve_org(api_key: str, _db: object, token_types: tuple[OrganizationAuthTokenType, ...]) -> object:
        assert api_key == caller_key
        assert token_types == (OrganizationAuthTokenType.api,)
        return SimpleNamespace(organization=SimpleNamespace(organization_id=organization_id))

    monkeypatch.setattr(ui_session.app, "DATABASE", database)
    monkeypatch.setattr(ui_session, "resolve_org_from_api_key", resolve_org)

    response = client.post("/v1/ui-session", headers={"x-api-key": caller_key})

    assert response.status_code == status.HTTP_404_NOT_FOUND
    assert response.json() == {"detail": "Organization not found"}
    organizations.get_organization.assert_awaited_once_with(organization_id)


def test_ui_session_route_rejects_ui_session_credential(
    client: TestClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    session_credential = "ui-session-credential-canary"

    async def reject_session(
        api_key: str,
        _db: object,
        token_types: tuple[OrganizationAuthTokenType, ...],
    ) -> object:
        assert api_key == session_credential
        assert token_types == (OrganizationAuthTokenType.api,)
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Invalid credentials")

    mint_token = AsyncMock()
    monkeypatch.setattr(ui_session, "resolve_org_from_api_key", reject_session)
    monkeypatch.setattr(ui_session, "create_org_ui_session_token", mint_token)

    response = client.post("/v1/ui-session", headers={"x-api-key": session_credential})

    assert response.status_code == status.HTTP_403_FORBIDDEN
    mint_token.assert_not_awaited()
