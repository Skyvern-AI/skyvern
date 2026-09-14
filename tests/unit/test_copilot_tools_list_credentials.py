import json
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
            "accessor": "await <key>.otp()",
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
            "accessor": "await <key>.otp()",
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
        "code",
        "username",
        "totp_type",
        "totp_identifier",
        "one_time_code",
    }
    assert entry["code"]["accessors"] == [
        "<key>.username",
        "<key>.password",
        "await <key>.otp()",
        "await <key>.magic_link(page)",
    ]
    assert entry["one_time_code"]["code"]["accessor"] in entry["code"]["accessors"]
    assert not any(SECRET_MARKER in str(value) for value in entry.values())


@pytest.mark.parametrize("totp_type", [TotpType.NONE, TotpType.PASSKEY])
def test_a_password_credential_without_a_one_time_code_source_advertises_no_otp_accessor(
    totp_type: TotpType,
) -> None:
    entry = _serialize_credential(_password_credential(totp_type=totp_type))

    assert entry["code"]["accessors"] == ["<key>.username", "<key>.password"]
    assert "one_time_code" not in entry


def test_a_secret_credential_advertises_only_secret_value_and_never_its_value() -> None:
    entry = _serialize_credential(
        _password_credential(
            credential_type=CredentialType.SECRET,
            username=None,
            secret_label="fixture api key",
            item_id=SECRET_MARKER,
            user_context=SECRET_MARKER,
        )
    )

    assert set(entry) == {"credential_id", "name", "credential_type", "tested_url", "code", "secret_label"}
    assert entry["credential_type"] == "secret"
    assert entry["code"] == {"workflow_parameter_type": "credential_id", "accessors": ["<key>.secret_value"]}
    serialized = json.dumps(entry)
    assert SECRET_MARKER not in serialized
    for guessed in ("username", "password", "api_key", "token"):
        assert guessed not in serialized


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


@pytest.mark.asyncio
async def test_citing_a_shorter_saved_name_inside_the_one_the_user_typed_is_not_naming_it() -> None:
    # The user typed "portal login", so the grounded check drops "portal" as a substring of it. Citing
    # the shorter credential must not claim the user settled it this turn, which would unpin its origin.
    approved = _password_credential(credential_id="cred_portal", name="portal")
    longer = _password_credential(credential_id="cred_portal_login", name="portal login")
    policy = RequestPolicy(canonical_user_message="use portal login to finish the build")
    policy.prior_approved_credential_ids = {approved.credential_id}
    policy.resolved_credentials = [approved]
    policy.seeded_proposal_credential_ids = {approved.credential_id}
    policy.live_page_admitted_urls = {approved.credential_id: "https://a.example/login"}

    data = await _resolve("portal", policy, [approved, longer])

    assert data["status"] == "resolved"
    assert policy.current_turn_named_credential_ids == set()
    assert policy.live_page_admitted_urls == {approved.credential_id: "https://a.example/login"}


@pytest.mark.asyncio
@pytest.mark.parametrize("reference_is_approved", [True, False])
async def test_an_approval_from_an_earlier_turn_resolves_without_the_user_naming_it_again(
    reference_is_approved: bool,
) -> None:
    approved = _password_credential(credential_id="cred_approved", name="portal-login")
    other = _password_credential(credential_id="cred_other", name="billing-login")
    # What a turn looks like after _seed_prior_approved_credentials rehydrates an approval the user
    # gave earlier in this chat, on a turn whose message names no credential.
    policy = RequestPolicy(canonical_user_message="now finish the workflow and test it")
    policy.prior_approved_credential_ids = {approved.credential_id}
    policy.resolved_credentials = [approved]

    reference = approved.credential_id if reference_is_approved else other.credential_id
    data = await _resolve(reference, policy, [approved, other])

    assert data["status"] == ("resolved" if reference_is_approved else "denied")
    # The approval answers which credential; it never claims the user named one this turn.
    assert policy.current_turn_named_credential_ids == set()


@pytest.mark.asyncio
@pytest.mark.parametrize("arm", ["user_named_this_turn", "server_auto_bound", "user_approved_earlier_turn"])
async def test_no_resume_arm_transfers_authority_to_whichever_credential_now_carries_that_name(arm: str) -> None:
    # Every arm's record is written against an id. If that credential is renamed and a different one
    # takes the old name, the name on the stored record must not vouch for the new holder.
    recorded = _password_credential(credential_id="cred_recorded", name="Portal login")
    recorded_now = _password_credential(credential_id="cred_recorded", name="Old portal")
    renamed_onto_the_old_name = _password_credential(credential_id="cred_other", name="Portal login")
    policy = RequestPolicy(canonical_user_message="carry on and finish the build")
    if arm == "user_named_this_turn":
        policy.resolved_credentials = [recorded]
        policy.current_turn_named_credential_ids = {recorded.credential_id}
    elif arm == "server_auto_bound":
        policy.auto_bound_credentials = [recorded]
    else:
        policy.prior_approved_credential_ids = {recorded.credential_id}
        policy.resolved_credentials = [recorded]

    data = await _resolve("Portal login", policy, [recorded_now, renamed_onto_the_old_name])

    assert data["status"] == "denied"
    assert renamed_onto_the_old_name.credential_id not in {
        credential.credential_id for credential in policy.resolved_credentials
    }


@pytest.mark.asyncio
@pytest.mark.parametrize("saved", [True, False])
async def test_saved_workflow_selection_resolves_but_a_canvas_proposal_does_not(saved: bool) -> None:
    credential = _password_credential()
    policy = RequestPolicy(
        canonical_user_message="Run the whole workflow again",
        persisted_workflow_credential_ids=[credential.credential_id] if saved else [],
        existing_workflow_credential_ids=[credential.credential_id],
    )
    data = await _resolve(credential.name, policy, [credential])
    assert data["status"] == ("resolved" if saved else "denied")
    assert policy.current_turn_named_credential_ids == set()
