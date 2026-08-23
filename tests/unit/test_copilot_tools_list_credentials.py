from datetime import datetime

from skyvern.forge.sdk.copilot.tools.credentials import _serialize_credential
from skyvern.forge.sdk.schemas.credentials import Credential, CredentialType, TotpType

SECRET_MARKER = "SECRET_VALUE"


def _password_credential(**overrides: object) -> Credential:
    defaults = {
        "credential_id": "cred_1",
        "organization_id": "o_test_org",
        "name": "Portal login",
        "vault_type": "bitwarden",
        "item_id": "6e3e136a-f457-44ea-8462-b49700735145",
        "credential_type": CredentialType.PASSWORD,
        "username": "user@example.com",
        "card_last4": None,
        "card_brand": None,
        "created_at": datetime(2026, 8, 13),
        "modified_at": datetime(2026, 8, 13),
    }
    return Credential(**{**defaults, **overrides})


def test_surfaces_the_two_fa_identifier_so_the_agent_can_see_which_credential_carries_which() -> None:
    entry = _serialize_credential(_password_credential(totp_type=TotpType.EMAIL, totp_identifier="inbox@example.com"))

    assert entry["totp_identifier"] == "inbox@example.com"
    assert entry["totp_type"] == str(TotpType.EMAIL)


def test_omits_the_identifier_when_the_credential_carries_none() -> None:
    entry = _serialize_credential(_password_credential(totp_type=TotpType.AUTHENTICATOR))

    assert "totp_identifier" not in entry
    assert entry["totp_type"] == str(TotpType.AUTHENTICATOR)
    assert entry["one_time_code"] == {
        "available": True,
        "source": "authenticator",
        "scouting": {
            "tool": "fill_credential_field",
            "credential_id": "cred_1",
            "field": "totp",
        },
        "code": {
            "workflow_parameter_type": "credential_id",
            "accessor": "await <credential_parameter_key>.otp()",
        },
    }


def test_email_otp_is_code_only_during_scouting() -> None:
    entry = _serialize_credential(_password_credential(totp_type=TotpType.EMAIL))

    assert entry["one_time_code"] == {
        "available": True,
        "source": "email",
        "scouting": {"available": False, "reason": "workflow_run_context_required"},
        "code": {
            "workflow_parameter_type": "credential_id",
            "accessor": "await <credential_parameter_key>.otp()",
        },
    }


def test_serializes_metadata_only_so_no_secret_or_vault_material_reaches_the_agent() -> None:
    entry = _serialize_credential(
        _password_credential(
            totp_type=TotpType.EMAIL,
            totp_identifier="inbox@example.com",
            tested_url="https://portal.example.com/login",
            item_id=SECRET_MARKER,
            user_context=SECRET_MARKER,
        )
    )

    assert set(entry) == {
        "credential_id",
        "name",
        "credential_type",
        "tested_url",
        "username",
        "totp_type",
        "totp_identifier",
        "one_time_code",
    }
    assert not any(SECRET_MARKER in str(value) for value in entry.values())
