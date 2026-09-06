from datetime import datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest

from skyvern.forge.sdk.copilot.request_policy import RequestPolicy
from skyvern.forge.sdk.copilot.tools import credentials as credentials_module
from skyvern.forge.sdk.copilot.tools.credential_fill import _request_settled_credential
from skyvern.forge.sdk.copilot.tools.credentials import _resolve_exact_credential, _serialize_credential
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


async def _resolve(reference: str, policy: RequestPolicy, inventory: list[Credential]) -> dict[str, object]:
    ctx = SimpleNamespace(organization_id="o_test_org", request_policy=policy)
    with patch.object(credentials_module, "load_credentials", AsyncMock(return_value=inventory)):
        result = await _resolve_exact_credential(reference, ctx)
    return result["data"]


def _proposal_hydrated_policy(proposed: Credential, message: str) -> RequestPolicy:
    policy = RequestPolicy(canonical_user_message=message)
    policy.resolved_credentials = [proposed]
    policy.auto_bound_credentials = [proposed]
    return policy


@pytest.mark.asyncio
async def test_a_credential_the_server_proposed_resolves_on_a_reply_that_never_names_it() -> None:
    proposed = _password_credential(credential_id="cred_proposed", name="portal-login")
    policy = _proposal_hydrated_policy(proposed, "yep, that one works for me")

    data = await _resolve("cred_proposed", policy, [proposed])

    assert data["status"] == "resolved"
    assert policy.current_turn_named_credential_ids == set()


@pytest.mark.asyncio
async def test_a_credential_other_than_the_recorded_proposal_is_denied_with_the_pass_routes() -> None:
    proposed = _password_credential(credential_id="cred_proposed", name="portal-login")
    other = _password_credential(credential_id="cred_other", name="billing-login")
    policy = _proposal_hydrated_policy(proposed, "yep, that one works for me")

    data = await _resolve("cred_other", policy, [proposed, other])

    assert data["status"] == "denied"
    assert data["reason"] == "reference_not_literal_in_current_user_turn"
    assert data["pass_routes"] == ["typed_resume", "request_credential_tool", "literal_credential_id"]


@pytest.mark.asyncio
async def test_the_user_naming_another_credential_settles_the_fill_on_that_one() -> None:
    proposed = _password_credential(credential_id="cred_proposed", name="portal-login")
    other = _password_credential(credential_id="cred_other", name="billing-login")
    policy = _proposal_hydrated_policy(proposed, "actually use cred_other")

    data = await _resolve("cred_other", policy, [proposed, other])

    assert data["status"] == "resolved"
    assert policy.current_turn_named_credential_ids == {"cred_other"}
    assert _request_settled_credential(policy, "cred_other")
