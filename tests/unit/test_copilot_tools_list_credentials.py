import json
from datetime import UTC, datetime
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, patch

import pytest
import yaml

from skyvern.exceptions import BitwardenListItemsError, BitwardenLoginError
from skyvern.forge import app
from skyvern.forge.sdk.copilot import credential_resolution as credential_resolution_module
from skyvern.forge.sdk.copilot.context import record_approved_credentials_in_global_llm_context
from skyvern.forge.sdk.copilot.request_policy import (
    RequestPolicy,
    _seed_prior_approved_credentials,
    resolve_credential_for_live_page,
)
from skyvern.forge.sdk.copilot.tools import credentials as credentials_module
from skyvern.forge.sdk.copilot.tools.credential_fill import _request_settled_credential
from skyvern.forge.sdk.copilot.tools.credentials import (
    _credential_ids_validation_error,
    _credential_run_approval_error,
    _list_credentials,
    _resolve_exact_credential,
    _serialize_credential,
)
from skyvern.forge.sdk.copilot.tools.guardrails import _workflow_yaml_output_policy_guardrail
from skyvern.forge.sdk.schemas.credentials import Credential, CredentialType, TotpType
from skyvern.forge.sdk.services.request_principal import AuthKind, RequestPrincipal
from skyvern.forge.sdk.workflow.context_manager import WorkflowRunContext
from skyvern.forge.sdk.workflow.models.parameter import BitwardenLoginCredentialParameter
from tests.unit.conftest import (
    ORG_VAULT_ACCOUNT_EMAIL,
    ORG_VAULT_ITEM_ID,
    ORG_VAULT_LOGIN_ID,
    ORG_VAULT_LOGIN_PASSWORD,
    ORG_VAULT_MASTER_PASSWORD,
    OrgVaultFake,
    org_vault_overview,
)

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
@pytest.mark.parametrize("bound_by", ["page_after_lookup", "page_before_lookup", "carry_from_last_turn"])
async def test_a_named_login_that_is_not_saved_leaves_no_unnamed_login_bound(bound_by: str) -> None:
    unnamed = _password_credential(credential_id="cred_page", name="site login", tested_url="https://a.example/login")
    message = 'log in to https://a.example/login with "my other login"'
    policy = RequestPolicy(canonical_user_message=message)

    async def observe_page() -> None:
        await resolve_credential_for_live_page(
            policy,
            organization_id="o_test_org",
            page_url="https://a.example/login",
            load_org_credentials=AsyncMock(return_value=[unnamed]),
        )

    if bound_by == "page_before_lookup":
        await observe_page()
    if bound_by == "carry_from_last_turn":
        policy = _proposal_hydrated_policy(unnamed, message)
        policy.seeded_proposal_credential_ids = {unnamed.credential_id}
        policy.live_page_admitted_urls = {unnamed.credential_id: "https://a.example/login"}
        # The model citing the carry is not the user naming it.
        policy.carry_cited_credential_ids = {unnamed.credential_id}
    bound_before_lookup = list(policy.resolved_credentials)
    data = await _resolve("my other login", policy, [unnamed])
    if bound_by == "page_after_lookup":
        await observe_page()

    assert bound_before_lookup == ([] if bound_by == "page_after_lookup" else [unnamed])
    assert data["status"] == "not_found"
    assert data["withdrawn_credential_ids"] == [credential.credential_id for credential in bound_before_lookup]
    assert policy.resolved_credentials == []
    assert policy.auto_bound_credentials == []
    assert policy.live_page_admitted_urls == {}
    assert policy.seeded_proposal_credential_ids == set()


@pytest.mark.asyncio
async def test_a_lookup_that_lands_while_the_page_is_loading_logins_still_stops_the_bind() -> None:
    page_login = _password_credential(
        credential_id="cred_page", name="site login", tested_url="https://a.example/login"
    )
    policy = RequestPolicy(canonical_user_message='log in to https://a.example/login with "my other login"')

    async def load_while_the_lookup_lands() -> list[Credential]:
        await _resolve("my other login", policy, [page_login])
        return [page_login]

    await resolve_credential_for_live_page(
        policy,
        organization_id="o_test_org",
        page_url="https://a.example/login",
        load_org_credentials=load_while_the_lookup_lands,
    )

    assert policy.resolved_credentials == []
    assert policy.live_page_admitted_urls == {}


@pytest.mark.asyncio
@pytest.mark.parametrize("settled_by", ["named_this_turn", "approved_earlier", "saved_workflow"])
async def test_a_login_that_is_the_users_survives_another_name_that_is_not_saved(settled_by: str) -> None:
    theirs = _password_credential(credential_id="cred_theirs", name="site login", tested_url="https://a.example/login")
    policy = RequestPolicy(canonical_user_message='use cred_theirs on https://a.example/login and "my other login"')
    await resolve_credential_for_live_page(
        policy,
        organization_id="o_test_org",
        page_url="https://a.example/login",
        load_org_credentials=AsyncMock(return_value=[theirs]),
    )
    if settled_by == "named_this_turn":
        await _resolve("cred_theirs", policy, [theirs])
    elif settled_by == "approved_earlier":
        policy.prior_approved_credential_ids = {"cred_theirs"}
    else:
        policy.persisted_workflow_credential_ids = ["cred_theirs"]

    data = await _resolve("my other login", policy, [theirs])

    assert data["withdrawn_credential_ids"] == []
    assert policy.resolved_credentials == [theirs]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("reference", "status"),
    [("dup login", "ambiguous"), ("unwritten login", "denied")],
)
async def test_a_lookup_that_is_ambiguous_or_denied_leaves_the_page_free_to_bind(reference: str, status: str) -> None:
    page_login = _password_credential(
        credential_id="cred_page", name="site login", tested_url="https://a.example/login"
    )
    inventory = [
        page_login,
        _password_credential(credential_id="cred_a", name="dup login"),
        _password_credential(credential_id="cred_b", name="dup login"),
        _password_credential(credential_id="cred_c", name="unwritten login"),
    ]
    policy = RequestPolicy(canonical_user_message='log in to https://a.example/login with "dup login"')

    data = await _resolve(reference, policy, inventory)
    await resolve_credential_for_live_page(
        policy,
        organization_id="o_test_org",
        page_url="https://a.example/login",
        load_org_credentials=AsyncMock(return_value=inventory),
    )

    assert data["status"] == status
    assert policy.resolved_credentials == [page_login]


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


async def _list(policy: RequestPolicy, saved: list[Credential], page: int = 1, page_size: int = 10) -> dict[str, Any]:
    ctx = SimpleNamespace(organization_id="o_test_org", request_policy=policy)
    with patch.object(credentials_module, "load_credentials", AsyncMock(return_value=saved)):
        return await _list_credentials({"page": page, "page_size": page_size}, ctx)


async def _resolve_vault(reference: str, policy: RequestPolicy) -> dict[str, Any]:
    ctx = SimpleNamespace(organization_id="o_test_org", request_policy=policy)
    with patch.object(credentials_module, "load_credentials", AsyncMock(return_value=[])):
        return await _resolve_exact_credential(reference, ctx)


@pytest.mark.asyncio
async def test_discovery_pages_connected_vault_logins_beside_saved_credentials_without_their_binding(
    org_vault: OrgVaultFake,
) -> None:
    card = org_vault_overview(
        item_id="5c1d2e3f-0000-4000-8000-000000000001", credential_type=CredentialType.CREDIT_CARD
    )
    second = org_vault_overview(item_id="5c1d2e3f-0000-4000-8000-000000000002", title="Second portal")
    org_vault.listing.return_value = [org_vault_overview(), card, second]

    first_page = await _list(RequestPolicy(), [_password_credential()], page_size=1)
    second_page = await _list(RequestPolicy(), [], page=2, page_size=1)

    data = first_page["data"]
    assert data["connected_vault"] == {"provider": "bitwarden", "status": "connected"}
    assert [entry["credential_id"] for entry in data["credentials"]] == ["cred_1"]
    assert data["vault_logins"] == [
        {"vault_login_id": ORG_VAULT_LOGIN_ID, "title": "Example portal", "site": "https://portal.example.com"}
    ]
    assert [entry["title"] for entry in second_page["data"]["vault_logins"]] == ["Second portal"]
    assert second_page["data"]["has_more"] is False
    serialized = json.dumps(first_page)
    assert ORG_VAULT_MASTER_PASSWORD not in serialized
    assert ORG_VAULT_ACCOUNT_EMAIL not in serialized


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("error", "reason"),
    [
        (BitwardenLoginError(f"Invalid master password. Confirm your email {ORG_VAULT_ACCOUNT_EMAIL}"), "login_failed"),
        (TimeoutError(), "timeout"),
        (BitwardenListItemsError(f"Failed to list items for {ORG_VAULT_ACCOUNT_EMAIL}"), "unreadable"),
    ],
)
async def test_an_unreadable_connected_vault_is_reported_with_what_to_check_and_retried_next_call(
    org_vault: OrgVaultFake, error: Exception, reason: str
) -> None:
    org_vault.listing.side_effect = error
    policy = RequestPolicy()

    result = await _list(policy, [])
    org_vault.listing.side_effect = None
    retried = await _list(policy, [])

    vault = result["data"]["connected_vault"]
    assert vault["status"] == "unreadable"
    assert vault["reason"] == reason
    assert vault["check"]
    assert result["data"]["vault_logins"] == []
    assert ORG_VAULT_ACCOUNT_EMAIL not in json.dumps(result)
    assert retried["data"]["connected_vault"]["status"] == "connected"


@pytest.mark.asyncio
async def test_an_organization_without_a_connected_vault_says_so(org_vault: OrgVaultFake) -> None:
    org_vault.org_token.return_value = None

    result = await _list(RequestPolicy(), [_password_credential()])

    assert result["data"]["connected_vault"] == {"provider": "bitwarden", "status": "not_configured"}
    assert result["data"]["vault_logins"] == []
    org_vault.listing.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("principal", [None, RequestPrincipal("o_test_org", AuthKind.ui_session, user_id="u_1")])
async def test_a_folder_restricted_requester_sees_no_vault_logins_and_cannot_bind_one(
    org_vault: OrgVaultFake, monkeypatch: pytest.MonkeyPatch, principal: RequestPrincipal | None
) -> None:
    org_vault.folders_restricted.return_value = True
    monkeypatch.setattr(credential_resolution_module, "get_request_principal", lambda: principal)
    monkeypatch.setattr(app.AGENT_FUNCTION, "accessible_credential_folder_ids", AsyncMock(return_value={"cfld_1"}))
    policy = RequestPolicy()
    ctx = SimpleNamespace(organization_id="o_test_org", request_policy=policy)

    result = await _list(policy, [])
    binding_error = await _credential_ids_validation_error(
        [ORG_VAULT_LOGIN_ID], ctx, workflow_definition={"parameters": [_vault_parameter()]}
    )

    assert result["data"]["connected_vault"]["status"] == "not_available_to_user"
    assert result["data"]["vault_logins"] == []
    org_vault.listing.assert_not_awaited()
    assert binding_error is not None and "not available to this user" in binding_error


@pytest.mark.asyncio
async def test_a_vault_login_the_user_names_by_title_returns_its_binding_and_is_approved_for_the_run(
    org_vault: OrgVaultFake,
) -> None:
    policy = RequestPolicy(canonical_user_message="sign in with my Bitwarden login Example portal")

    result = await _resolve_vault("Example portal", policy)
    by_id = await _resolve_vault(ORG_VAULT_LOGIN_ID, policy)

    assert result["data"]["status"] == "resolved"
    assert result["data"]["vault_login"]["workflow_parameter"] == _vault_parameter(include_key=False)
    assert ORG_VAULT_MASTER_PASSWORD not in json.dumps(result)
    assert (
        _credential_run_approval_error(
            [ORG_VAULT_LOGIN_ID], policy, workflow_definition={"parameters": [_vault_parameter()]}
        )
        is None
    )
    assert by_id["ok"] is False and "title" in by_id["error"]
    assert policy.named_login_not_found is False


@pytest.mark.asyncio
@pytest.mark.parametrize("title", ["bwitem_payroll", "cred_payroll"])
async def test_a_vault_login_titled_like_an_id_resolves_by_its_title(org_vault: OrgVaultFake, title: str) -> None:
    org_vault.listing.return_value = [org_vault_overview(title=title)]
    policy = RequestPolicy(canonical_user_message=f"sign in with my Bitwarden login {title}")

    result = await _resolve_vault(title, policy)

    assert result["data"]["status"] == "resolved"
    assert result["data"]["vault_login"]["workflow_parameter"] == _vault_parameter(include_key=False)


@pytest.mark.asyncio
async def test_a_vault_login_approved_on_one_turn_hydrates_on_the_next_without_listing_the_vault(
    org_vault: OrgVaultFake,
) -> None:
    first_turn = RequestPolicy(canonical_user_message="use Example portal")
    await _resolve_vault("Example portal", first_turn)
    carried_context = record_approved_credentials_in_global_llm_context(
        SimpleNamespace(request_policy=first_turn), None
    )
    org_vault.listing.reset_mock()

    next_turn = RequestPolicy(canonical_user_message="now run it")
    await _seed_prior_approved_credentials(
        next_turn, organization_id="o_test_org", global_llm_context=carried_context or ""
    )

    org_vault.listing.assert_not_awaited()
    assert [credential.credential_id for credential in next_turn.resolved_credentials] == [ORG_VAULT_LOGIN_ID]
    assert (
        _credential_run_approval_error(
            [ORG_VAULT_LOGIN_ID], next_turn, workflow_definition={"parameters": [_vault_parameter()]}
        )
        is None
    )
    assert (
        _credential_run_approval_error(
            ["bwitem_5c1d2e3f-0000-4000-8000-000000000001"],
            next_turn,
            workflow_definition={
                "parameters": [_vault_parameter(bitwarden_item_id="5c1d2e3f-0000-4000-8000-000000000001")]
            },
        )
        is not None
    )


@pytest.mark.asyncio
async def test_the_returned_binding_resolves_at_run_time_through_the_organizations_own_vault_account(
    org_vault: OrgVaultFake,
) -> None:
    policy = RequestPolicy(canonical_user_message="use Example portal")
    recipe = (await _resolve_vault("Example portal", policy))["data"]["vault_login"]["workflow_parameter"]
    now = datetime(2026, 1, 1, tzinfo=UTC)
    parameter = BitwardenLoginCredentialParameter(
        **{key: value for key, value in recipe.items() if key != "parameter_type"},
        key="portal_login",
        bitwarden_login_credential_parameter_id="blc_1",
        workflow_id="wf-1",
        created_at=now,
        modified_at=now,
    )
    context = WorkflowRunContext(
        workflow_title="title",
        workflow_id="wf-1",
        workflow_permanent_id="wfp-1",
        workflow_run_id="wr-1",
        aws_client=SimpleNamespace(),
    )

    await context.register_bitwarden_login_credential_parameter_value(parameter, org_vault.organization)

    fetch = org_vault.secret.await_args
    assert fetch.args[:3] == (None, None, ORG_VAULT_MASTER_PASSWORD)
    assert fetch.kwargs["email"] == ORG_VAULT_ACCOUNT_EMAIL
    assert fetch.kwargs["item_id"] == ORG_VAULT_ITEM_ID
    assert fetch.kwargs["collection_id"] == "col_1"
    assert context.secrets[context.values["portal_login"]["password"]] == ORG_VAULT_LOGIN_PASSWORD


def _vault_login_draft(block_url: str) -> str:
    return yaml.safe_dump(
        {
            "title": "Sign in",
            "workflow_definition": {
                "parameters": [_vault_parameter()],
                "blocks": [
                    {
                        "block_type": "login",
                        "label": "login_to_portal",
                        "url": block_url,
                        "parameter_keys": ["portal_login"],
                    }
                ],
            },
        }
    )


def _guardrail_reason_codes(policy: RequestPolicy, workflow_yaml: str) -> list[str]:
    tool_context = SimpleNamespace(
        tool_name="update_workflow",
        tool_call_id="call_1",
        tool_arguments=json.dumps({"workflow_yaml": workflow_yaml}),
        context=SimpleNamespace(request_policy=policy),
    )
    output = _workflow_yaml_output_policy_guardrail(SimpleNamespace(context=tool_context))  # type: ignore[arg-type]
    return output.output_info["reason_codes"]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("looked_up", "block_url", "reason_codes"),
    [
        pytest.param(True, "https://portal.example.com/login", [], id="own-site"),
        pytest.param(True, "https://other.example.org/login", ["connected_vault_login_off_site"], id="off-site"),
        pytest.param(
            False, "https://portal.example.com/login", ["connected_vault_login_sites_unread"], id="not-looked-up"
        ),
    ],
)
async def test_a_vault_login_draft_passes_the_input_guardrail_only_on_a_site_its_lookup_read(
    org_vault: OrgVaultFake, looked_up: bool, block_url: str, reason_codes: list[str]
) -> None:
    policy = RequestPolicy(canonical_user_message="sign in with Example portal")
    if looked_up:
        resolved = await _resolve_vault("Example portal", policy)
        assert resolved["data"]["vault_login"]["sites"] == ["https://portal.example.com"]
        assert resolved["data"]["vault_login"]["workflow_parameter"] == _vault_parameter(include_key=False)

    assert _guardrail_reason_codes(policy, _vault_login_draft(block_url)) == reason_codes


def _vault_parameter(*, include_key: bool = True, bitwarden_item_id: str = ORG_VAULT_ITEM_ID) -> dict[str, str]:
    parameter = {
        "parameter_type": "bitwarden_login_credential",
        "bitwarden_item_id": bitwarden_item_id,
        "bitwarden_collection_id": "col_1",
        "bitwarden_client_id_aws_secret_key": "SKYVERN_BITWARDEN_CLIENT_ID",
        "bitwarden_client_secret_aws_secret_key": "SKYVERN_BITWARDEN_CLIENT_SECRET",
        "bitwarden_master_password_aws_secret_key": "SKYVERN_BITWARDEN_MASTER_PASSWORD",
    }
    return {"key": "portal_login", **parameter} if include_key else parameter
