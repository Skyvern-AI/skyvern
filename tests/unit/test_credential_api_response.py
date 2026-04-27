"""Tests for _convert_to_response in credentials routes."""

from datetime import datetime

from skyvern.forge.sdk.routes.credentials import _convert_to_response
from skyvern.forge.sdk.schemas.credentials import (
    Credential,
    CredentialType,
    CredentialVaultType,
    TotpType,
)


def _make_credential(**overrides: object) -> Credential:
    defaults = {
        "credential_id": "cred_test",
        "organization_id": "org_test",
        "name": "Test Credential",
        "vault_type": CredentialVaultType.BITWARDEN,
        "item_id": "item_test",
        "credential_type": CredentialType.PASSWORD,
        "username": "user@example.com",
        "totp_type": TotpType.AUTHENTICATOR,
        "totp_identifier": None,
        "card_last4": None,
        "card_brand": None,
        "secret_label": None,
        "browser_profile_id": None,
        "tested_url": None,
        "user_context": None,
        "save_browser_session_intent": False,
        "created_at": datetime(2026, 1, 1),
        "modified_at": datetime(2026, 1, 1),
        "deleted_at": None,
    }
    defaults.update(overrides)
    return Credential(**defaults)


def test_convert_to_response_includes_totp_identifier() -> None:
    credential = _make_credential(totp_identifier="login_otp")
    response = _convert_to_response(credential)
    assert response.credential.totp_identifier == "login_otp"


def test_convert_to_response_totp_identifier_none_when_not_set() -> None:
    credential = _make_credential(totp_identifier=None)
    response = _convert_to_response(credential)
    assert response.credential.totp_identifier is None


def test_convert_to_response_includes_totp_type() -> None:
    credential = _make_credential(totp_type=TotpType.EMAIL)
    response = _convert_to_response(credential)
    assert response.credential.totp_type == TotpType.EMAIL
