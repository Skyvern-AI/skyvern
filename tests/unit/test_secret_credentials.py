import pytest

from skyvern.forge.sdk.schemas.credentials import (
    CreateCredentialRequest,
    CredentialType,
    SecretCredential,
)


class TestSecretCredentialModels:
    def test_secret_credential_creation(self) -> None:
        cred = SecretCredential(secret_value="sk-abc123", secret_label="API Key")
        assert cred.secret_value == "sk-abc123"
        assert cred.secret_label == "API Key"

    def test_secret_credential_optional_type(self) -> None:
        cred = SecretCredential(secret_value="token123")
        assert cred.secret_label is None

    def test_non_empty_validation(self) -> None:
        with pytest.raises(ValueError):
            SecretCredential(secret_value="")

    def test_create_request_with_secret(self) -> None:
        req = CreateCredentialRequest(
            name="My API Key",
            credential_type=CredentialType.SECRET,
            credential=SecretCredential(secret_value="sk-12345"),
        )
        assert req.credential_type == CredentialType.SECRET
        assert req.credential.secret_value == "sk-12345"
