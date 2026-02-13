import pytest

from skyvern.forge.sdk.api.custom_credential_client import CustomCredentialAPIClient
from skyvern.forge.sdk.schemas.credentials import CredentialType, SecretCredential


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
