"""Tests for per-credential vault_type override in CreateCredentialRequest and vault service routing."""

from unittest.mock import MagicMock, patch

import pytest
from fastapi import HTTPException

from skyvern.forge.sdk.routes.credentials import _get_credential_vault_service
from skyvern.forge.sdk.schemas.credentials import (
    CreateCredentialRequest,
    CredentialResponse,
    CredentialType,
    CredentialVaultType,
    NonEmptyPasswordCredential,
    PasswordCredentialResponse,
    SecretCredential,
    SecretCredentialResponse,
)
from skyvern.forge.sdk.services.credential.credential_vault_service import CredentialVaultService


class TestCreateCredentialRequestVaultType:
    """Verify the optional vault_type field on CreateCredentialRequest."""

    def test_vault_type_defaults_to_none(self) -> None:
        req = CreateCredentialRequest(
            name="Test",
            credential_type=CredentialType.PASSWORD,
            credential=NonEmptyPasswordCredential(username="user", password="pass"),
        )
        assert req.vault_type is None

    def test_vault_type_can_be_set_to_custom(self) -> None:
        req = CreateCredentialRequest(
            name="Test",
            credential_type=CredentialType.PASSWORD,
            credential=NonEmptyPasswordCredential(username="user", password="pass"),
            vault_type=CredentialVaultType.CUSTOM,
        )
        assert req.vault_type == CredentialVaultType.CUSTOM

    def test_vault_type_can_be_set_to_azure_vault(self) -> None:
        req = CreateCredentialRequest(
            name="Test",
            credential_type=CredentialType.SECRET,
            credential=SecretCredential(secret_value="s3cr3t"),
            vault_type=CredentialVaultType.AZURE_VAULT,
        )
        assert req.vault_type == CredentialVaultType.AZURE_VAULT

    def test_vault_type_can_be_set_to_bitwarden(self) -> None:
        req = CreateCredentialRequest(
            name="Test",
            credential_type=CredentialType.PASSWORD,
            credential=NonEmptyPasswordCredential(username="u", password="p"),
            vault_type=CredentialVaultType.BITWARDEN,
        )
        assert req.vault_type == CredentialVaultType.BITWARDEN

    def test_vault_type_serializes_in_json(self) -> None:
        req = CreateCredentialRequest(
            name="Test",
            credential_type=CredentialType.PASSWORD,
            credential=NonEmptyPasswordCredential(username="user", password="pass"),
            vault_type=CredentialVaultType.CUSTOM,
        )
        data = req.model_dump()
        assert data["vault_type"] == "custom"

    def test_vault_type_none_excluded_from_json_when_none(self) -> None:
        req = CreateCredentialRequest(
            name="Test",
            credential_type=CredentialType.PASSWORD,
            credential=NonEmptyPasswordCredential(username="user", password="pass"),
        )
        data = req.model_dump()
        assert data["vault_type"] is None

    def test_vault_type_deserialized_from_dict(self) -> None:
        data = {
            "name": "Test",
            "credential_type": "password",
            "credential": {"username": "user", "password": "pass"},
            "vault_type": "custom",
        }
        req = CreateCredentialRequest.model_validate(data)
        assert req.vault_type == CredentialVaultType.CUSTOM

    def test_vault_type_omitted_in_dict_gives_none(self) -> None:
        data = {
            "name": "Test",
            "credential_type": "password",
            "credential": {"username": "user", "password": "pass"},
        }
        req = CreateCredentialRequest.model_validate(data)
        assert req.vault_type is None


class TestCredentialResponseVaultType:
    """Verify vault_type is present in CredentialResponse."""

    def test_response_includes_vault_type(self) -> None:
        resp = CredentialResponse(
            credential_id="cred_123",
            credential=PasswordCredentialResponse(username="user"),
            credential_type=CredentialType.PASSWORD,
            name="Test",
            vault_type=CredentialVaultType.CUSTOM,
        )
        assert resp.vault_type == CredentialVaultType.CUSTOM

    def test_response_vault_type_defaults_to_none(self) -> None:
        resp = CredentialResponse(
            credential_id="cred_123",
            credential=PasswordCredentialResponse(username="user"),
            credential_type=CredentialType.PASSWORD,
            name="Test",
        )
        assert resp.vault_type is None

    def test_response_vault_type_in_serialized_output(self) -> None:
        resp = CredentialResponse(
            credential_id="cred_123",
            credential=SecretCredentialResponse(secret_label="api-key"),
            credential_type=CredentialType.SECRET,
            name="API Key",
            vault_type=CredentialVaultType.AZURE_VAULT,
        )
        data = resp.model_dump()
        assert data["vault_type"] == "azure_vault"


class TestGetCredentialVaultServiceRouting:
    """Verify _get_credential_vault_service routes correctly with and without overrides."""

    @pytest.mark.asyncio
    async def test_no_override_uses_global_bitwarden(self) -> None:
        mock_bw = MagicMock(spec=CredentialVaultService)
        with (
            patch("skyvern.forge.sdk.routes.credentials.settings") as mock_settings,
            patch("skyvern.forge.sdk.routes.credentials.app") as mock_app,
        ):
            mock_settings.CREDENTIAL_VAULT_TYPE = CredentialVaultType.BITWARDEN
            mock_app.BITWARDEN_CREDENTIAL_VAULT_SERVICE = mock_bw
            result = await _get_credential_vault_service()
            assert result is mock_bw

    @pytest.mark.asyncio
    async def test_override_custom_ignores_global(self) -> None:
        mock_bw = MagicMock(spec=CredentialVaultService)
        mock_custom = MagicMock(spec=CredentialVaultService)
        with (
            patch("skyvern.forge.sdk.routes.credentials.settings") as mock_settings,
            patch("skyvern.forge.sdk.routes.credentials.app") as mock_app,
        ):
            mock_settings.CREDENTIAL_VAULT_TYPE = CredentialVaultType.BITWARDEN
            mock_app.BITWARDEN_CREDENTIAL_VAULT_SERVICE = mock_bw
            mock_app.CUSTOM_CREDENTIAL_VAULT_SERVICE = mock_custom
            result = await _get_credential_vault_service(
                vault_type_override=CredentialVaultType.CUSTOM,
            )
            assert result is mock_custom

    @pytest.mark.asyncio
    async def test_override_azure_ignores_global(self) -> None:
        mock_bw = MagicMock(spec=CredentialVaultService)
        mock_azure = MagicMock(spec=CredentialVaultService)
        with (
            patch("skyvern.forge.sdk.routes.credentials.settings") as mock_settings,
            patch("skyvern.forge.sdk.routes.credentials.app") as mock_app,
        ):
            mock_settings.CREDENTIAL_VAULT_TYPE = CredentialVaultType.BITWARDEN
            mock_app.BITWARDEN_CREDENTIAL_VAULT_SERVICE = mock_bw
            mock_app.AZURE_CREDENTIAL_VAULT_SERVICE = mock_azure
            result = await _get_credential_vault_service(
                vault_type_override=CredentialVaultType.AZURE_VAULT,
            )
            assert result is mock_azure

    @pytest.mark.asyncio
    async def test_override_custom_raises_when_not_configured(self) -> None:
        with (
            patch("skyvern.forge.sdk.routes.credentials.settings") as mock_settings,
            patch("skyvern.forge.sdk.routes.credentials.app") as mock_app,
        ):
            mock_settings.CREDENTIAL_VAULT_TYPE = CredentialVaultType.BITWARDEN
            mock_app.CUSTOM_CREDENTIAL_VAULT_SERVICE = None
            with pytest.raises(HTTPException) as exc_info:
                await _get_credential_vault_service(
                    vault_type_override=CredentialVaultType.CUSTOM,
                )
            assert exc_info.value.status_code == 400
            assert "Custom credential vault" in str(exc_info.value.detail)

    @pytest.mark.asyncio
    async def test_override_none_falls_back_to_global(self) -> None:
        mock_custom = MagicMock(spec=CredentialVaultService)
        with (
            patch("skyvern.forge.sdk.routes.credentials.settings") as mock_settings,
            patch("skyvern.forge.sdk.routes.credentials.app") as mock_app,
        ):
            mock_settings.CREDENTIAL_VAULT_TYPE = CredentialVaultType.CUSTOM
            mock_app.CUSTOM_CREDENTIAL_VAULT_SERVICE = mock_custom
            result = await _get_credential_vault_service(vault_type_override=None)
            assert result is mock_custom
