import json
from unittest.mock import AsyncMock

import pytest
from pydantic import BaseModel, ValidationError
from structlog.testing import capture_logs

from skyvern.exceptions import HttpException
from skyvern.forge.sdk.api import custom_credential_client as client_module
from skyvern.forge.sdk.api.custom_credential_client import CustomCredentialAPIClient
from skyvern.forge.sdk.schemas.credentials import (
    CredentialType,
    PasswordCredential,
    SecretCredential,
)
from skyvern.forge.sdk.services import credentials as credential_helpers
from skyvern.forge.sdk.services.credential import custom_credential_vault_service as vault_module


class _IntegerPayload(BaseModel):
    count: int


@pytest.fixture
def client() -> CustomCredentialAPIClient:
    return CustomCredentialAPIClient(api_base_url="https://custom.example.com", api_token="token-123")


def test_credential_to_api_payload_with_label(client: CustomCredentialAPIClient) -> None:
    credential = SecretCredential(secret_value="sk-secret", secret_label="api-key")

    payload = client._credential_to_api_payload(credential)

    assert payload == {
        "type": "secret",
        "secret_value": "sk-secret",
        "secret_label": "api-key",
    }


def test_credential_to_api_payload_without_label(client: CustomCredentialAPIClient) -> None:
    credential = SecretCredential(secret_value="sk-secret-no-label")

    payload = client._credential_to_api_payload(credential)

    assert payload == {
        "type": "secret",
        "secret_value": "sk-secret-no-label",
    }


def test_api_response_to_credential_secret_with_label(client: CustomCredentialAPIClient) -> None:
    response = {
        "type": "secret",
        "secret_value": "shhh",
        "secret_label": "prod-api",
    }

    credential_item = client._api_response_to_credential(response, name="Prod API", item_id="cred_123")

    assert credential_item.item_id == "cred_123"
    assert credential_item.name == "Prod API"
    assert credential_item.credential_type == CredentialType.SECRET
    assert isinstance(credential_item.credential, SecretCredential)
    assert credential_item.credential.secret_value == "shhh"
    assert credential_item.credential.secret_label == "prod-api"


def test_api_response_to_credential_secret_without_label(client: CustomCredentialAPIClient) -> None:
    response = {
        "type": "secret",
        "secret_value": "token-only",
    }

    credential_item = client._api_response_to_credential(response, name="Token", item_id="cred_456")

    assert credential_item.item_id == "cred_456"
    assert credential_item.name == "Token"
    assert credential_item.credential_type == CredentialType.SECRET
    assert isinstance(credential_item.credential, SecretCredential)
    assert credential_item.credential.secret_value == "token-only"
    assert credential_item.credential.secret_label is None


def test_api_response_to_credential_secret_missing_required_field(client: CustomCredentialAPIClient) -> None:
    response = {
        "type": "secret",
        "secret_label": "no-secret-value",
    }

    with pytest.raises(ValueError, match="Missing required secret fields from API"):
        client._api_response_to_credential(response, name="Broken Secret", item_id="cred_789")


@pytest.mark.parametrize("metadata", [{"region": "us-east"}, None])
def test_password_credential_metadata_payload_round_trip(
    client: CustomCredentialAPIClient,
    metadata: dict[str, str] | None,
) -> None:
    credential = PasswordCredential(username="user@example.com", password="pw", metadata=metadata)

    payload = client._credential_to_api_payload(credential)
    assert payload.get("metadata") == metadata
    assert ("metadata" in payload) is (metadata is not None)
    restored = client._api_response_to_credential(payload, name="Login", item_id="cred_password")
    assert isinstance(restored.credential, PasswordCredential)
    assert restored.credential.metadata == metadata


def test_format_error_response_redacts_password(client: CustomCredentialAPIClient) -> None:
    password = "password-material"

    detail = client._format_error_response({"detail": {"password": password, "message": "invalid"}})

    assert password not in detail


@pytest.mark.asyncio
async def test_missing_id_response_log_never_contains_echoed_private_value(
    client: CustomCredentialAPIClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    private_value = "echoed-private-value"
    response_body = {
        "echo": json.dumps(
            {
                "private_key": private_value,
                "username": "user@example.com",
            }
        )
    }
    monkeypatch.setattr(
        client_module,
        "aiohttp_request",
        AsyncMock(return_value=(200, {}, response_body)),
    )

    with capture_logs() as logs, pytest.raises(HttpException) as exc_info:
        await client.create_credential(
            name="Login",
            credential=PasswordCredential(username="user@example.com", password="pw"),
        )

    assert private_value not in json.dumps(logs)
    assert private_value not in str(exc_info.value)


def test_shared_credential_error_formatter_omits_pydantic_input() -> None:
    submitted_value = "submitted-sensitive-value"

    with pytest.raises(ValidationError) as exc_info:
        _IntegerPayload(count=submitted_value)  # type: ignore[arg-type]

    assert hasattr(credential_helpers, "safe_error_message")
    message = credential_helpers.safe_error_message(exc_info.value)
    assert "count" in message
    assert submitted_value not in message


def test_custom_credential_components_share_error_formatter() -> None:
    assert client_module.safe_error_message is credential_helpers.safe_error_message
    assert vault_module.safe_error_message is credential_helpers.safe_error_message
