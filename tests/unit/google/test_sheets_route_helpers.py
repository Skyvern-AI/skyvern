"""Tests for skyvern.forge.sdk.routes.google_sheets helper functions.

Focuses on _mint_access_token's exception triage so DB outages don't get
mislabeled as user reconnect prompts.
"""

from unittest.mock import AsyncMock, MagicMock

import pytest
from fastapi import HTTPException
from sqlalchemy.exc import OperationalError

from skyvern.forge.sdk.routes import google_sheets as sheets_routes


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
