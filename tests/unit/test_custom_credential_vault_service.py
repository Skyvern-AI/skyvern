import asyncio
import json
import traceback
from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
import structlog.testing

from skyvern.exceptions import SkyvernHTTPException
from skyvern.forge import app
from skyvern.forge.api_app import create_api_app
from skyvern.forge.sdk.api.custom_credential_client import CustomCredentialAPIClient
from skyvern.forge.sdk.db.enums import OrganizationAuthTokenType
from skyvern.forge.sdk.schemas.credentials import (
    CreateCredentialRequest,
    Credential,
    CredentialType,
    CredentialVaultType,
    NonEmptyPasswordCredential,
)
from skyvern.forge.sdk.schemas.organizations import OrganizationAuthToken
from skyvern.forge.sdk.services.credential.custom_credential_vault_service import (
    CustomCredentialConfigurationError,
    CustomCredentialNotConfiguredError,
    CustomCredentialVaultService,
)


def _credential() -> Credential:
    return Credential(
        credential_id="cred_test",
        organization_id="org_test",
        name="Login",
        vault_type=CredentialVaultType.CUSTOM,
        item_id="item_old",
        credential_type=CredentialType.PASSWORD,
        username="user_test",
        totp_type="none",
        totp_identifier=None,
        card_last4=None,
        card_brand=None,
        secret_label=None,
        browser_profile_id=None,
        tested_url=None,
        user_context=None,
        save_browser_session_intent=False,
        folder_id=None,
        created_at=datetime(2026, 1, 1),
        modified_at=datetime(2026, 1, 1),
        deleted_at=None,
    )


def _password_request() -> CreateCredentialRequest:
    return CreateCredentialRequest(
        name="Login Updated",
        credential_type=CredentialType.PASSWORD,
        credential=NonEmptyPasswordCredential(
            username="user_test",
            password="secret_test",
            metadata={"key": "value"},
        ),
    )


def _prepare_update(
    monkeypatch: pytest.MonkeyPatch,
    failure: BaseException,
    *,
    delete_failure: Exception | None = None,
) -> tuple[CustomCredentialVaultService, AsyncMock]:
    client = AsyncMock(spec=CustomCredentialAPIClient)
    client.create_credential.return_value = "item_new"
    client.delete_credential.side_effect = delete_failure
    service = CustomCredentialVaultService(client=client)
    monkeypatch.setattr(service, "_update_db_credential", AsyncMock(side_effect=failure))
    return service, client.delete_credential


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("failure_type", "message"),
    [
        pytest.param(RuntimeError, "database unavailable", id="exception"),
        pytest.param(asyncio.CancelledError, "repoint cancelled", id="cancelled"),
    ],
)
async def test_update_credential_reclaims_new_item_when_repoint_fails(
    monkeypatch: pytest.MonkeyPatch,
    failure_type: type[BaseException],
    message: str,
) -> None:
    service, delete_credential = _prepare_update(monkeypatch, failure_type(message))

    with pytest.raises(failure_type):
        await service.update_credential(_credential(), _password_request())

    delete_credential.assert_awaited_with("item_new")


@pytest.mark.asyncio
async def test_update_credential_enqueues_orphan_when_cancelled_inline_cleanup_fails(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service, delete_credential = _prepare_update(
        monkeypatch,
        asyncio.CancelledError(),
        delete_failure=RuntimeError("vault unavailable"),
    )
    orphaned = AsyncMock()
    monkeypatch.setattr(app.AGENT_FUNCTION, "on_credential_item_orphaned", orphaned)

    with pytest.raises(asyncio.CancelledError):
        await service.update_credential(_credential(), _password_request())

    delete_credential.assert_awaited_with("item_new")
    orphaned.assert_awaited_with(
        organization_id="org_test",
        item_id="item_new",
        vault_type=CredentialVaultType.CUSTOM,
    )


class _FakeOrganizationsRepository:
    def __init__(self, token: OrganizationAuthToken | None) -> None:
        self._token = token

    async def get_valid_org_auth_token(self, organization_id: str, token_type: str) -> OrganizationAuthToken | None:
        return self._token


def _service_for_stored_config(monkeypatch: pytest.MonkeyPatch, token: str | None) -> CustomCredentialVaultService:
    now = datetime.now(timezone.utc)
    stored = (
        OrganizationAuthToken(
            id="oat_test",
            organization_id="org_test",
            token_type=OrganizationAuthTokenType.custom_credential_service,
            token=token,
            valid=True,
            created_at=now,
            modified_at=now,
        )
        if token is not None
        else None
    )
    monkeypatch.setattr(app, "DATABASE", SimpleNamespace(organizations=_FakeOrganizationsRepository(stored)))
    return CustomCredentialVaultService()


async def _run_vault_operation(service: CustomCredentialVaultService, operation: str) -> None:
    if operation == "create":
        await service.create_credential(organization_id="org_test", data=_password_request())
    elif operation == "update":
        await service.update_credential(_credential(), _password_request())
    elif operation == "delete":
        await service.delete_credential(_credential())
    else:
        await service.get_credential_item(_credential())


def _records_at(logs: list[dict[str, object]], *levels: str) -> list[dict[str, object]]:
    return [record for record in logs if record.get("log_level") in levels]


@pytest.mark.asyncio
@pytest.mark.parametrize("operation", ["create", "update", "delete", "get"])
async def test_missing_configuration_is_reported_once_at_warning(
    monkeypatch: pytest.MonkeyPatch,
    operation: str,
) -> None:
    service = _service_for_stored_config(monkeypatch, None)

    with structlog.testing.capture_logs() as logs:
        with pytest.raises(CustomCredentialNotConfiguredError):
            await _run_vault_operation(service, operation)

    records = _records_at(logs, "warning", "error")
    assert len(records) == 1
    assert records[0]["log_level"] == "warning"
    assert str(records[0]["error_type"]).endswith("CustomCredentialNotConfiguredError")
    assert records[0].get("exc_info") is None


@pytest.mark.asyncio
async def test_missing_configuration_refusal_is_a_client_error(monkeypatch: pytest.MonkeyPatch) -> None:
    service = _service_for_stored_config(monkeypatch, None)

    with pytest.raises(CustomCredentialConfigurationError) as exc_info:
        await service.get_credential_item(_credential())

    assert isinstance(exc_info.value, SkyvernHTTPException)
    assert 400 <= exc_info.value.status_code < 500


def test_missing_configuration_refusal_bypasses_the_unexpected_error_handler() -> None:
    handlers = create_api_app().exception_handlers

    # Starlette picks the first registered handler along the exception's MRO. Landing on the
    # Exception handler would log the expected refusal as a server error and re-trigger alerting.
    resolved = next(cls for cls in CustomCredentialNotConfiguredError.__mro__ if cls in handlers)

    assert resolved is SkyvernHTTPException


@pytest.mark.asyncio
async def test_invalid_configuration_keeps_error_visibility(monkeypatch: pytest.MonkeyPatch) -> None:
    service = _service_for_stored_config(monkeypatch, "}{ not json")

    with structlog.testing.capture_logs() as logs:
        with pytest.raises(CustomCredentialConfigurationError) as exc_info:
            await _run_vault_operation(service, "create")

    assert not isinstance(exc_info.value, CustomCredentialNotConfiguredError)
    assert _records_at(logs, "error")


@pytest.mark.asyncio
async def test_validate_organization_configuration_accepts_a_configured_organization(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service = _service_for_stored_config(
        monkeypatch,
        json.dumps({"api_base_url": "https://vault.example.test", "api_token": "token_test"}),
    )

    await service.validate_organization_configuration("org_test")


@pytest.mark.asyncio
async def test_validate_organization_configuration_reports_a_missing_configuration_once_at_warning(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service = _service_for_stored_config(monkeypatch, None)

    with structlog.testing.capture_logs() as logs:
        with pytest.raises(CustomCredentialNotConfiguredError):
            await service.validate_organization_configuration("org_test")

    records = _records_at(logs, "warning", "error")
    assert len(records) == 1
    assert records[0]["log_level"] == "warning"


@pytest.mark.asyncio
async def test_invalid_configuration_never_puts_the_stored_token_in_a_traceback(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # _log_vault_failure renders this exception with exc_info=True, so anything reachable through
    # the exception chain lands in the logs.
    service = _service_for_stored_config(monkeypatch, json.dumps({"api_token": "super-secret-token"}))

    with pytest.raises(CustomCredentialConfigurationError) as exc_info:
        await service.validate_organization_configuration("org_test")

    error = exc_info.value
    rendered = "".join(traceback.format_exception(type(error), error, error.__traceback__))
    assert "super-secret-token" not in rendered


@pytest.mark.asyncio
async def test_validate_organization_configuration_skips_the_lookup_for_a_process_wide_client(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _service_for_stored_config(monkeypatch, None)
    service = CustomCredentialVaultService(client=AsyncMock(spec=CustomCredentialAPIClient))

    await service.validate_organization_configuration("org_test")


@pytest.mark.asyncio
async def test_vault_failure_keeps_error_visibility(monkeypatch: pytest.MonkeyPatch) -> None:
    service = _service_for_stored_config(
        monkeypatch,
        json.dumps({"api_base_url": "https://vault.example.test", "api_token": "token_test"}),
    )
    monkeypatch.setattr(
        CustomCredentialAPIClient,
        "create_credential",
        AsyncMock(side_effect=RuntimeError("vault unavailable")),
    )

    with structlog.testing.capture_logs() as logs:
        with pytest.raises(RuntimeError):
            await _run_vault_operation(service, "create")

    errors = _records_at(logs, "error")
    assert len(errors) == 1
    assert errors[0]["exc_info"] is True
