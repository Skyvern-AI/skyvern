"""Cover the OSS ``AgentFunction.get_google_*_credentials`` paths.

These methods used to be no-ops; SKY-9463 wired them through the OSS
``google_oauth_service``. The tests below pin down the success and the
failure-modes (missing encryption, missing credential, refresh failure)
so a regression doesn't silently break Sheets blocks in OSS.
"""

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from skyvern.forge.agent_functions import AgentFunction
from skyvern.forge.sdk.services import google_oauth_service


@pytest.mark.asyncio
async def test_get_google_sheets_credentials_returns_token_on_success(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    secrets = google_oauth_service.GoogleCredentialSecrets(refresh_token="rt", scopes=[])
    monkeypatch.setattr(
        google_oauth_service,
        "load_credential_secrets",
        AsyncMock(return_value=secrets),
    )
    monkeypatch.setattr(
        google_oauth_service,
        "access_token_from_secrets",
        AsyncMock(return_value="ya29.access-token"),
    )

    result = await AgentFunction().get_google_sheets_credentials(
        organization_id="org_1",
        credential_id="goac_1",
    )
    assert result == "ya29.access-token"


@pytest.mark.asyncio
async def test_get_google_sheets_credentials_returns_none_when_encryption_disabled(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Missing ENABLE_ENCRYPTION must surface as None so callers route to reconnect."""

    async def raise_encryption_not_configured(*_args, **_kwargs):
        raise google_oauth_service.EncryptionNotConfiguredError("disabled")

    monkeypatch.setattr(
        google_oauth_service,
        "load_credential_secrets",
        raise_encryption_not_configured,
    )

    result = await AgentFunction().get_google_sheets_credentials(
        organization_id="org_1",
        credential_id="goac_1",
    )
    assert result is None


@pytest.mark.asyncio
async def test_get_google_sheets_credentials_returns_none_when_credential_missing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def raise_missing(*_args, **_kwargs):
        raise ValueError("No active Google OAuth credential found: goac_missing")

    monkeypatch.setattr(google_oauth_service, "load_credential_secrets", raise_missing)

    result = await AgentFunction().get_google_sheets_credentials(
        organization_id="org_1",
        credential_id="goac_missing",
    )
    assert result is None


@pytest.mark.asyncio
async def test_get_google_sheets_credentials_returns_none_on_refresh_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    secrets = google_oauth_service.GoogleCredentialSecrets(refresh_token="rt", scopes=[])
    monkeypatch.setattr(
        google_oauth_service,
        "load_credential_secrets",
        AsyncMock(return_value=secrets),
    )

    async def raise_refresh_failure(*_args, **_kwargs):
        raise google_oauth_service.MissingAccessTokenError("Google token refresh failed")

    monkeypatch.setattr(google_oauth_service, "access_token_from_secrets", raise_refresh_failure)

    result = await AgentFunction().get_google_sheets_credentials(
        organization_id="org_1",
        credential_id="goac_1",
    )
    assert result is None


@pytest.mark.asyncio
async def test_get_google_workspace_credentials_returns_credentials_on_success(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    secrets = google_oauth_service.GoogleCredentialSecrets(refresh_token="rt", scopes=[])
    fake_credentials = SimpleNamespace(token="ya29.access-token")
    monkeypatch.setattr(
        google_oauth_service,
        "load_credential_secrets",
        AsyncMock(return_value=secrets),
    )
    monkeypatch.setattr(
        google_oauth_service,
        "credentials_from_secrets",
        AsyncMock(return_value=fake_credentials),
    )

    result = await AgentFunction().get_google_workspace_credentials(
        organization_id="org_1",
        credential_id="goac_1",
    )
    assert result is fake_credentials


@pytest.mark.asyncio
async def test_get_google_workspace_credentials_returns_none_when_encryption_disabled(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def raise_encryption_not_configured(*_args, **_kwargs):
        raise google_oauth_service.EncryptionNotConfiguredError("disabled")

    monkeypatch.setattr(
        google_oauth_service,
        "load_credential_secrets",
        raise_encryption_not_configured,
    )

    result = await AgentFunction().get_google_workspace_credentials(
        organization_id="org_1",
        credential_id="goac_1",
    )
    assert result is None


@pytest.mark.asyncio
async def test_get_google_workspace_credentials_returns_none_on_unexpected_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def raise_unexpected(*_args, **_kwargs):
        raise RuntimeError("network blew up")

    monkeypatch.setattr(google_oauth_service, "load_credential_secrets", raise_unexpected)

    result = await AgentFunction().get_google_workspace_credentials(
        organization_id="org_1",
        credential_id="goac_1",
    )
    assert result is None
