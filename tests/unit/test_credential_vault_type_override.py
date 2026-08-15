"""Tests for per-credential vault_type override in CreateCredentialRequest and vault service routing."""

import json
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi import BackgroundTasks, HTTPException

from skyvern.config import Settings
from skyvern.forge.sdk.routes import credentials as credentials_routes
from skyvern.forge.sdk.routes.credentials import (
    _delete_temporary_test_login_credential,
    _get_credential_vault_service,
)
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
from skyvern.forge.sdk.services.credential import custom_credential_vault_service as custom_vault_module
from skyvern.forge.sdk.services.credential.credential_vault_service import CredentialVaultService
from skyvern.forge.sdk.services.credential.custom_credential_vault_service import (
    CONFIGURATION_INVALID_MESSAGE,
    CustomCredentialVaultService,
)


class TestLocalCredentialVaultSettings:
    """Verify the local filesystem vault is enabled only in local envs by default."""

    def test_oss_credential_vault_type_defaults_to_skyvern(self) -> None:
        assert Settings.model_fields["CREDENTIAL_VAULT_TYPE"].default == CredentialVaultType.SKYVERN

    def test_local_credential_vault_defaults_enabled_for_local_env(self) -> None:
        settings = Settings(_env_file=None, ENV="local", ENABLE_LOCAL_CREDENTIAL_VAULT=None)
        assert settings.is_local_credential_vault_enabled()

    def test_local_credential_vault_defaults_disabled_for_non_local_env(self) -> None:
        settings = Settings(_env_file=None, ENV="prod", ENABLE_LOCAL_CREDENTIAL_VAULT=None)
        assert not settings.is_local_credential_vault_enabled()

    def test_local_credential_vault_can_be_explicitly_enabled_for_non_local_env(self) -> None:
        settings = Settings(_env_file=None, ENV="prod", ENABLE_LOCAL_CREDENTIAL_VAULT=True)
        assert settings.is_local_credential_vault_enabled()


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

    def test_vault_type_can_be_set_to_skyvern(self) -> None:
        req = CreateCredentialRequest(
            name="Test",
            credential_type=CredentialType.PASSWORD,
            credential=NonEmptyPasswordCredential(username="u", password="p"),
            vault_type=CredentialVaultType.SKYVERN,
        )
        assert req.vault_type == CredentialVaultType.SKYVERN

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
        mock_settings = MagicMock(CREDENTIAL_VAULT_TYPE=CredentialVaultType.BITWARDEN)
        with (
            patch("skyvern.forge.sdk.routes.credentials.app") as mock_app,
            patch("skyvern.forge.sdk.routes.credentials.SettingsManager.get_settings", return_value=mock_settings),
        ):
            mock_app.BITWARDEN_CREDENTIAL_VAULT_SERVICE = mock_bw
            result = await _get_credential_vault_service()
            assert result is mock_bw

    @pytest.mark.asyncio
    async def test_no_override_uses_global_skyvern(self) -> None:
        mock_skyvern = MagicMock(spec=CredentialVaultService)
        mock_settings = MagicMock(CREDENTIAL_VAULT_TYPE=CredentialVaultType.SKYVERN)
        mock_settings.is_local_credential_vault_enabled.return_value = True
        with (
            patch("skyvern.forge.sdk.routes.credentials.app") as mock_app,
            patch("skyvern.forge.sdk.routes.credentials.SettingsManager.get_settings", return_value=mock_settings),
        ):
            mock_app.SKYVERN_CREDENTIAL_VAULT_SERVICE = mock_skyvern
            result = await _get_credential_vault_service()
            assert result is mock_skyvern

    @pytest.mark.asyncio
    async def test_no_override_skyvern_raises_when_local_vault_disabled(self) -> None:
        mock_settings = MagicMock(CREDENTIAL_VAULT_TYPE=CredentialVaultType.SKYVERN)
        mock_settings.is_local_credential_vault_enabled.return_value = False
        with (
            patch("skyvern.forge.sdk.routes.credentials.app") as mock_app,
            patch("skyvern.forge.sdk.routes.credentials.SettingsManager.get_settings", return_value=mock_settings),
        ):
            mock_app.SKYVERN_CREDENTIAL_VAULT_SERVICE = MagicMock(spec=CredentialVaultService)
            with pytest.raises(HTTPException) as exc_info:
                await _get_credential_vault_service()
            assert exc_info.value.status_code == 400
            assert "local credential vault is not enabled" in str(exc_info.value.detail)

    @pytest.mark.asyncio
    async def test_override_skyvern_ignores_global(self) -> None:
        mock_bw = MagicMock(spec=CredentialVaultService)
        mock_skyvern = MagicMock(spec=CredentialVaultService)
        mock_settings = MagicMock(CREDENTIAL_VAULT_TYPE=CredentialVaultType.BITWARDEN)
        mock_settings.is_local_credential_vault_enabled.return_value = True
        with (
            patch("skyvern.forge.sdk.routes.credentials.app") as mock_app,
            patch("skyvern.forge.sdk.routes.credentials.SettingsManager.get_settings", return_value=mock_settings),
        ):
            mock_app.BITWARDEN_CREDENTIAL_VAULT_SERVICE = mock_bw
            mock_app.SKYVERN_CREDENTIAL_VAULT_SERVICE = mock_skyvern
            result = await _get_credential_vault_service(
                vault_type_override=CredentialVaultType.SKYVERN,
            )
            assert result is mock_skyvern

    @pytest.mark.asyncio
    async def test_override_skyvern_raises_when_local_vault_disabled(self) -> None:
        mock_bw = MagicMock(spec=CredentialVaultService)
        mock_skyvern = MagicMock(spec=CredentialVaultService)
        mock_settings = MagicMock(CREDENTIAL_VAULT_TYPE=CredentialVaultType.BITWARDEN)
        mock_settings.is_local_credential_vault_enabled.return_value = False
        with (
            patch("skyvern.forge.sdk.routes.credentials.app") as mock_app,
            patch("skyvern.forge.sdk.routes.credentials.SettingsManager.get_settings", return_value=mock_settings),
        ):
            mock_app.BITWARDEN_CREDENTIAL_VAULT_SERVICE = mock_bw
            mock_app.SKYVERN_CREDENTIAL_VAULT_SERVICE = mock_skyvern
            with pytest.raises(HTTPException) as exc_info:
                await _get_credential_vault_service(
                    vault_type_override=CredentialVaultType.SKYVERN,
                )
            assert exc_info.value.status_code == 400
            assert "local credential vault is not enabled" in str(exc_info.value.detail)

    @pytest.mark.asyncio
    async def test_override_custom_ignores_global(self) -> None:
        mock_bw = MagicMock(spec=CredentialVaultService)
        mock_custom = MagicMock(spec=CredentialVaultService)
        with patch("skyvern.forge.sdk.routes.credentials.app") as mock_app:
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
        with patch("skyvern.forge.sdk.routes.credentials.app") as mock_app:
            mock_app.BITWARDEN_CREDENTIAL_VAULT_SERVICE = mock_bw
            mock_app.AZURE_CREDENTIAL_VAULT_SERVICE = mock_azure
            result = await _get_credential_vault_service(
                vault_type_override=CredentialVaultType.AZURE_VAULT,
            )
            assert result is mock_azure

    @pytest.mark.asyncio
    async def test_override_gcp_ignores_global(self) -> None:
        mock_bw = MagicMock(spec=CredentialVaultService)
        mock_gcp = MagicMock(spec=CredentialVaultService)
        with patch("skyvern.forge.sdk.routes.credentials.app") as mock_app:
            mock_app.BITWARDEN_CREDENTIAL_VAULT_SERVICE = mock_bw
            mock_app.GCP_CREDENTIAL_VAULT_SERVICE = mock_gcp
            result = await _get_credential_vault_service(
                vault_type_override=CredentialVaultType.GCP,
            )
            assert result is mock_gcp

    @pytest.mark.asyncio
    async def test_override_gcp_raises_when_not_configured(self) -> None:
        with patch("skyvern.forge.sdk.routes.credentials.app") as mock_app:
            mock_app.GCP_CREDENTIAL_VAULT_SERVICE = None
            with pytest.raises(HTTPException) as exc_info:
                await _get_credential_vault_service(
                    vault_type_override=CredentialVaultType.GCP,
                )
            assert exc_info.value.status_code == 400
            assert "GCP credential vault" in str(exc_info.value.detail)

    @pytest.mark.asyncio
    async def test_override_custom_raises_when_not_configured(self) -> None:
        with patch("skyvern.forge.sdk.routes.credentials.app") as mock_app:
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
        mock_settings = MagicMock(CREDENTIAL_VAULT_TYPE=CredentialVaultType.CUSTOM)
        with (
            patch("skyvern.forge.sdk.routes.credentials.app") as mock_app,
            patch("skyvern.forge.sdk.routes.credentials.SettingsManager.get_settings", return_value=mock_settings),
        ):
            mock_app.CUSTOM_CREDENTIAL_VAULT_SERVICE = mock_custom
            result = await _get_credential_vault_service(vault_type_override=None)
            assert result is mock_custom


def _install_stored_config(monkeypatch: pytest.MonkeyPatch, stored_config: str | None) -> None:
    """Point the forge app's org-auth-token lookup at the given stored custom-vault configuration."""
    auth_token = None if stored_config is None else SimpleNamespace(token=stored_config)
    monkeypatch.setattr(
        custom_vault_module.app,
        "DATABASE",
        SimpleNamespace(
            organizations=SimpleNamespace(get_valid_org_auth_token=AsyncMock(return_value=auth_token)),
        ),
    )


class TestCustomVaultOrganizationPreflight:
    """The gate preflights the custom vault's per-organization configuration before any vault call."""

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        ("stored_config", "expected_detail"),
        [
            pytest.param(
                None,
                "Custom credential service not configured for organization org_test",
                id="no-configuration",
            ),
            pytest.param("not json at all", CONFIGURATION_INVALID_MESSAGE, id="unparsable-configuration"),
            pytest.param(
                json.dumps({"api_base_url": "https://credentials.example.test/api"}),
                CONFIGURATION_INVALID_MESSAGE,
                id="missing-api-token",
            ),
            pytest.param(
                json.dumps({"api_base_url": "   ", "api_token": "tok_valid"}),
                CONFIGURATION_INVALID_MESSAGE,
                id="blank-api-base-url",
            ),
        ],
    )
    async def test_unusable_configuration_is_rejected_before_any_vault_call(
        self,
        monkeypatch: pytest.MonkeyPatch,
        stored_config: str | None,
        expected_detail: str,
    ) -> None:
        api_client = MagicMock()
        _install_stored_config(monkeypatch, stored_config)
        monkeypatch.setattr(custom_vault_module, "CustomCredentialAPIClient", api_client)

        with patch("skyvern.forge.sdk.routes.credentials.app") as mock_app:
            mock_app.CUSTOM_CREDENTIAL_VAULT_SERVICE = CustomCredentialVaultService()
            with pytest.raises(HTTPException) as exc_info:
                await _get_credential_vault_service(
                    vault_type_override=CredentialVaultType.CUSTOM,
                    organization_id="org_test",
                )

        assert exc_info.value.status_code == 400
        assert exc_info.value.detail == expected_detail
        api_client.assert_not_called()

    @pytest.mark.asyncio
    async def test_rejection_detail_never_leaks_the_stored_api_token(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        _install_stored_config(monkeypatch, json.dumps({"api_token": "super-secret-token"}))

        with patch("skyvern.forge.sdk.routes.credentials.app") as mock_app:
            mock_app.CUSTOM_CREDENTIAL_VAULT_SERVICE = CustomCredentialVaultService()
            with pytest.raises(HTTPException) as exc_info:
                await _get_credential_vault_service(
                    vault_type_override=CredentialVaultType.CUSTOM,
                    organization_id="org_test",
                )

        assert "super-secret-token" not in str(exc_info.value.detail)

    @pytest.mark.asyncio
    async def test_configured_organization_resolves_the_custom_vault(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        _install_stored_config(
            monkeypatch,
            json.dumps({"api_base_url": "https://credentials.example.test/api", "api_token": "tok_valid"}),
        )
        service = CustomCredentialVaultService()

        with patch("skyvern.forge.sdk.routes.credentials.app") as mock_app:
            mock_app.CUSTOM_CREDENTIAL_VAULT_SERVICE = service
            result = await _get_credential_vault_service(
                vault_type_override=CredentialVaultType.CUSTOM,
                organization_id="org_test",
            )

        assert result is service

    @pytest.mark.asyncio
    async def test_process_wide_vault_needs_no_organization_configuration(self) -> None:
        mock_bw = MagicMock(spec=CredentialVaultService)
        mock_settings = MagicMock(CREDENTIAL_VAULT_TYPE=CredentialVaultType.BITWARDEN)
        with (
            patch("skyvern.forge.sdk.routes.credentials.app") as mock_app,
            patch("skyvern.forge.sdk.routes.credentials.SettingsManager.get_settings", return_value=mock_settings),
        ):
            mock_app.BITWARDEN_CREDENTIAL_VAULT_SERVICE = mock_bw
            result = await _get_credential_vault_service(organization_id="org_test")

        assert result is mock_bw
        mock_bw.validate_organization_configuration.assert_awaited_once_with("org_test")

    @pytest.mark.asyncio
    async def test_callers_without_an_organization_skip_the_preflight(self) -> None:
        # The TOTP read path relies on this: it needs CustomCredentialNotConfiguredError to reach
        # its own handler rather than being converted at the gate.
        mock_custom = MagicMock(spec=CredentialVaultService)
        with patch("skyvern.forge.sdk.routes.credentials.app") as mock_app:
            mock_app.CUSTOM_CREDENTIAL_VAULT_SERVICE = mock_custom
            result = await _get_credential_vault_service(vault_type_override=CredentialVaultType.CUSTOM)

        assert result is mock_custom
        mock_custom.validate_organization_configuration.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_create_route_rejects_unconfigured_organization_without_storing_a_secret(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        api_client = MagicMock()
        _install_stored_config(monkeypatch, None)
        monkeypatch.setattr(custom_vault_module, "CustomCredentialAPIClient", api_client)
        service = CustomCredentialVaultService()
        create_credential = AsyncMock()
        monkeypatch.setattr(service, "create_credential", create_credential)

        data = CreateCredentialRequest(
            name="Test",
            credential_type=CredentialType.PASSWORD,
            credential=NonEmptyPasswordCredential(username="user", password="pass"),
            vault_type=CredentialVaultType.CUSTOM,
        )

        with patch("skyvern.forge.sdk.routes.credentials.app") as mock_app:
            mock_app.CUSTOM_CREDENTIAL_VAULT_SERVICE = service
            mock_app.AGENT_FUNCTION.validate_credential_write = AsyncMock()
            with pytest.raises(HTTPException) as exc_info:
                await credentials_routes.create_credential(
                    background_tasks=BackgroundTasks(),
                    data=data,
                    current_org=SimpleNamespace(organization_id="org_test"),
                )

        assert exc_info.value.status_code == 400
        create_credential.assert_not_awaited()
        api_client.assert_not_called()


class TestTemporaryTestLoginCredentialCleanup:
    """Verify temporary login-test credentials are deleted through the owning vault service."""

    @pytest.mark.asyncio
    async def test_deletes_temporary_skyvern_credential_through_vault_service(self) -> None:
        credential = SimpleNamespace(
            credential_id="cred_temp",
            organization_id="org_test",
            name="_test_login_example",
            vault_type=CredentialVaultType.SKYVERN,
        )
        mock_repository = MagicMock()
        mock_repository.get_credential = AsyncMock(return_value=credential)
        mock_service = MagicMock(spec=CredentialVaultService)
        mock_service.delete_credential = AsyncMock()
        mock_settings = MagicMock()
        mock_settings.is_local_credential_vault_enabled.return_value = True

        with (
            patch("skyvern.forge.sdk.routes.credentials.app") as mock_app,
            patch("skyvern.forge.sdk.routes.credentials.SettingsManager.get_settings", return_value=mock_settings),
        ):
            mock_app.DATABASE.credentials = mock_repository
            mock_app.SKYVERN_CREDENTIAL_VAULT_SERVICE = mock_service

            await _delete_temporary_test_login_credential(
                credential_id="cred_temp",
                organization_id="org_test",
                reason="test",
            )

        mock_service.delete_credential.assert_awaited_once_with(credential)

    @pytest.mark.asyncio
    async def test_defaults_missing_vault_type_to_bitwarden_for_legacy_credentials(self) -> None:
        credential = SimpleNamespace(
            credential_id="cred_temp",
            organization_id="org_test",
            name="_test_login_example",
            vault_type=None,
        )
        mock_repository = MagicMock()
        mock_repository.get_credential = AsyncMock(return_value=credential)
        mock_service = MagicMock(spec=CredentialVaultService)
        mock_service.delete_credential = AsyncMock()

        with patch("skyvern.forge.sdk.routes.credentials.app") as mock_app:
            mock_app.DATABASE.credentials = mock_repository
            mock_app.BITWARDEN_CREDENTIAL_VAULT_SERVICE = mock_service

            await _delete_temporary_test_login_credential(
                credential_id="cred_temp",
                organization_id="org_test",
                reason="test",
            )

        mock_service.delete_credential.assert_awaited_once_with(credential)

    @pytest.mark.asyncio
    async def test_ignores_non_temporary_credentials(self) -> None:
        credential = SimpleNamespace(
            credential_id="cred_regular",
            organization_id="org_test",
            name="regular credential",
            vault_type=CredentialVaultType.SKYVERN,
        )
        mock_repository = MagicMock()
        mock_repository.get_credential = AsyncMock(return_value=credential)
        mock_service = MagicMock(spec=CredentialVaultService)
        mock_service.delete_credential = AsyncMock()

        with patch("skyvern.forge.sdk.routes.credentials.app") as mock_app:
            mock_app.DATABASE.credentials = mock_repository

            await _delete_temporary_test_login_credential(
                credential_id="cred_regular",
                organization_id="org_test",
                reason="test",
            )

        mock_service.delete_credential.assert_not_awaited()
