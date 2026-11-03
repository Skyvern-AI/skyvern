import json
from datetime import datetime
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
import yaml

from skyvern.exceptions import BitwardenLoginError
from skyvern.forge import app
from skyvern.forge.sdk.copilot import vault_logins as vault_logins_module
from skyvern.forge.sdk.copilot.request_policy import (
    RequestPolicy,
    resolve_credential_for_live_page,
)
from skyvern.forge.sdk.copilot.tools import credentials as credentials_module
from skyvern.forge.sdk.copilot.tools import run_execution as run_execution_module
from skyvern.forge.sdk.copilot.tools.credential_fill import _request_settled_credential
from skyvern.forge.sdk.copilot.tools.credentials import (
    _credential_run_approval_error,
    _list_credentials,
    _resolve_exact_credential,
    _serialize_credential,
    import_bound_vault_logins,
)
from skyvern.forge.sdk.copilot.tools.run_execution import _run_blocks_and_collect_debug
from skyvern.forge.sdk.credential_site_policy import LoginSite
from skyvern.forge.sdk.schemas.credentials import Credential, CredentialType, CredentialVaultType, TotpType
from skyvern.forge.sdk.services.credential import credential_creation_service
from skyvern.forge.sdk.services.credential import credential_vault_service as credential_vault_service_module
from skyvern.forge.sdk.services.credential import onepassword_credential_vault_service as onepassword_vault
from skyvern.forge.sdk.services.credential.credential_vault_service import VaultLogin, VaultLoginListing
from skyvern.forge.sdk.services.request_principal import AuthKind, RequestPrincipal
from skyvern.services import workflow_service as workflow_service_module
from tests.unit.conftest import (
    ORG_VAULT_ACCOUNT_EMAIL,
    ORG_VAULT_ITEM_ID,
    ORG_VAULT_MASTER_PASSWORD,
    OrgVaultFake,
    org_vault_overview,
)
from tests.unit.copilot_test_helpers import install_run_blocks_harness, make_copilot_ctx

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


async def _listed_ref(policy: RequestPolicy) -> str:
    [login] = (await _list(policy, []))["data"]["vault_logins"]
    return login["credential_id"]


def _draft_binding(credential_id: str, block_url: str) -> str:
    return yaml.safe_dump(
        {
            "title": "Sign in",
            "workflow_definition": {
                "parameters": [{"key": "portal_login", "parameter_type": "credential", "credential_id": credential_id}],
                "blocks": [
                    {"block_type": "login", "label": "sign_in", "url": block_url, "parameter_keys": ["portal_login"]}
                ],
            },
        }
    )


@pytest.mark.asyncio
async def test_discovery_pages_vault_logins_not_saved_yet_beside_saved_credentials_without_secrets(
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
    assert [entry["credential_id"] for entry in data["credentials"]] == ["cred_1"]
    [login] = data["vault_logins"]
    assert login["credential_id"].startswith("vlogin_")
    assert {key: login[key] for key in ("name", "site")} == {
        "name": "Example portal",
        "site": "https://portal.example.com",
    }
    assert [entry["name"] for entry in second_page["data"]["vault_logins"]] == ["Second portal"]
    assert second_page["data"]["has_more"] is False
    serialized = json.dumps(first_page)
    assert ORG_VAULT_MASTER_PASSWORD not in serialized
    assert ORG_VAULT_ACCOUNT_EMAIL not in serialized


@pytest.mark.asyncio
async def test_an_unreadable_vault_is_named_without_its_error_text_and_read_again_next_call(
    org_vault: OrgVaultFake,
) -> None:
    org_vault.listing.side_effect = BitwardenLoginError(f"Invalid master password for {ORG_VAULT_ACCOUNT_EMAIL}")
    policy = RequestPolicy()

    result = await _list(policy, [])
    org_vault.listing.side_effect = None
    retried = await _list(policy, [])

    assert result["data"]["unreadable_vaults"] == ["bitwarden_connected"]
    assert result["data"]["vault_logins"] == []
    assert ORG_VAULT_ACCOUNT_EMAIL not in json.dumps(result)
    assert "unreadable_vaults" not in retried["data"]
    assert [login["name"] for login in retried["data"]["vault_logins"]] == ["Example portal"]


def _connect_onepassword(monkeypatch: pytest.MonkeyPatch, websites: list[str], *, failing_vault: bool = False) -> None:
    client = MagicMock()
    vault_ids = ["vault_ok", "vault_bad"] if failing_vault else ["vault_ok"]
    client.vaults.list = AsyncMock(return_value=[SimpleNamespace(id=vault_id) for vault_id in vault_ids])
    portal = SimpleNamespace(
        id="item_1",
        title="1Password portal",
        category=SimpleNamespace(value="LOGIN"),
        websites=[SimpleNamespace(url=url) for url in websites],
    )
    client.items.list = AsyncMock(side_effect=lambda vault_id: [portal] if vault_id == "vault_ok" else _raise())
    monkeypatch.setattr(
        onepassword_vault,
        "resolve_onepassword_token",
        AsyncMock(return_value=SimpleNamespace(source="organization", token="op_token")),
    )
    monkeypatch.setattr(onepassword_vault.OnePasswordClient, "authenticate", AsyncMock(return_value=client))
    monkeypatch.setitem(
        app.CREDENTIAL_VAULT_SERVICES,
        CredentialVaultType.ONEPASSWORD,
        onepassword_vault.OnePasswordCredentialVaultService(),
    )


@pytest.mark.asyncio
async def test_a_1password_vault_that_fails_to_list_makes_the_provider_unreadable(
    org_vault: OrgVaultFake, monkeypatch: pytest.MonkeyPatch
) -> None:
    _connect_onepassword(monkeypatch, ["https://portal.example.com/login"], failing_vault=True)
    policy = RequestPolicy()

    result = await _list(policy, [])

    assert result["data"]["unreadable_vaults"] == ["onepassword"]
    assert [login["name"] for login in result["data"]["vault_logins"]] == ["Example portal"]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("block_url", "imported"),
    [
        ("https://portal.example.com/signin", True),
        ("https://app.portal.example.com/login", False),
        ("https://second.example.com/login", False),
    ],
    ids=["first-website-origin", "sibling-host", "second-website"],
)
async def test_a_1password_login_is_bound_only_where_its_imported_row_will_fill(
    org_vault: OrgVaultFake, monkeypatch: pytest.MonkeyPatch, block_url: str, imported: bool
) -> None:
    _connect_onepassword(monkeypatch, ["https://portal.example.com/login", "https://second.example.com/login"])
    org_vault.listing.return_value = []
    policy = RequestPolicy()
    [login] = (await _list(policy, []))["data"]["vault_logins"]

    _, error = await import_bound_vault_logins(
        _draft_binding(login["credential_id"], block_url),
        SimpleNamespace(organization_id="o_test_org", request_policy=policy),
    )

    assert error is None if imported else "fills only on https://portal.example.com." in error
    assert [row.tested_url for row in await org_vault.imported_rows()] == (
        ["https://portal.example.com/login"] if imported else []
    )


def _raise() -> None:
    raise RuntimeError("vault not found")


@pytest.mark.asyncio
async def test_a_listed_vault_login_competes_for_a_page_while_another_vault_is_unreadable(
    org_vault: OrgVaultFake, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setitem(
        app.CREDENTIAL_VAULT_SERVICES,
        CredentialVaultType.ONEPASSWORD,
        SimpleNamespace(list_logins=AsyncMock(side_effect=TimeoutError())),
    )
    saved = _password_credential(credential_id="cred_page", tested_url="https://portal.example.com/login")
    policy = RequestPolicy()
    listed = (await _list(policy, [saved]))["data"]

    record = await resolve_credential_for_live_page(
        policy,
        organization_id="o_test_org",
        page_url="https://portal.example.com/login",
        load_org_credentials=AsyncMock(return_value=[saved]),
    )

    assert listed["unreadable_vaults"] == ["onepassword"]
    assert record.verdict == "ambiguous"
    assert policy.resolved_credentials == []


@pytest.mark.asyncio
@pytest.mark.parametrize("principal", [None, RequestPrincipal("o_test_org", AuthKind.ui_session, user_id="u_1")])
async def test_a_folder_restricted_requester_sees_no_vault_logins(
    org_vault: OrgVaultFake, monkeypatch: pytest.MonkeyPatch, principal: RequestPrincipal | None
) -> None:
    org_vault.folders_restricted.return_value = True
    monkeypatch.setattr(credential_vault_service_module, "get_request_principal", lambda: principal)
    monkeypatch.setattr(app.AGENT_FUNCTION, "accessible_credential_folder_ids", AsyncMock(return_value={"cfld_1"}))

    result = await _list(RequestPolicy(), [])

    assert result["data"]["vault_logins"] == []
    assert result["data"]["vault_logins_hidden_by_folder_access"] is True
    org_vault.listing.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("gap", "fact"),
    [
        ("unreadable", {"unreadable_vaults": ["bitwarden_connected"]}),
        ("hidden", {"vault_logins_hidden_by_folder_access": True}),
    ],
)
async def test_a_named_login_not_found_says_which_vault_logins_went_unseen(
    org_vault: OrgVaultFake, gap: str, fact: dict[str, object]
) -> None:
    if gap == "unreadable":
        org_vault.listing.side_effect = BitwardenLoginError("vault locked")
    else:
        org_vault.folders_restricted.return_value = True
    policy = RequestPolicy(canonical_user_message='sign in with "my portal login"')

    data = await _resolve("my portal login", policy, [])

    assert data["status"] == "not_found"
    assert {key: data[key] for key in ("unreadable_vaults", "vault_logins_hidden_by_folder_access") if key in data} == (
        fact
    )


@pytest.mark.asyncio
async def test_a_vault_login_the_user_names_by_title_resolves_to_its_listed_id(org_vault: OrgVaultFake) -> None:
    policy = RequestPolicy(canonical_user_message="sign in with my Bitwarden login Example portal")
    ref = await _listed_ref(RequestPolicy())

    result = await _resolve("Example portal", policy, [])

    assert result["status"] == "resolved"
    assert result["credential"]["credential_id"] == ref
    assert policy.current_turn_named_credential_ids == {ref}
    assert ORG_VAULT_MASTER_PASSWORD not in json.dumps(result)


@pytest.mark.asyncio
async def test_a_draft_binding_imports_the_vault_login_once_and_binds_its_saved_credential(
    org_vault: OrgVaultFake,
) -> None:
    policy = RequestPolicy()
    ctx = SimpleNamespace(organization_id="o_test_org", request_policy=policy)
    ref = await _listed_ref(policy)

    bound_yaml, error = await import_bound_vault_logins(_draft_binding(ref, "https://portal.example.com/login"), ctx)
    next_turn = RequestPolicy()
    rebound_yaml, rebind_error = await import_bound_vault_logins(
        _draft_binding(ref, "https://portal.example.com/login"),
        SimpleNamespace(organization_id="o_test_org", request_policy=next_turn),
    )

    [row] = await org_vault.imported_rows()
    assert error is None and rebind_error is None
    assert ref not in bound_yaml and row.credential_id in bound_yaml
    assert ref not in rebound_yaml and row.credential_id in rebound_yaml
    assert (row.vault_type, row.item_id, row.name, row.tested_url) == (
        "bitwarden_connected",
        ORG_VAULT_ITEM_ID,
        "Example portal",
        None,
    )
    assert row.username is None and not row.has_totp_seed
    listed = (await _list(next_turn, []))["data"]
    assert listed["vault_logins"] == []


@pytest.mark.asyncio
@pytest.mark.parametrize("block_url", ["https://other.example.com/login", "https://app.portal.example.com/login"])
async def test_a_draft_binding_off_the_logins_own_sites_imports_nothing(
    org_vault: OrgVaultFake, block_url: str
) -> None:
    policy = RequestPolicy()
    ctx = SimpleNamespace(organization_id="o_test_org", request_policy=policy)
    draft = _draft_binding(await _listed_ref(policy), block_url)

    bound_yaml, error = await import_bound_vault_logins(draft, ctx)

    assert error is not None
    assert bound_yaml == draft
    assert await org_vault.imported_rows() == []


@pytest.mark.asyncio
@pytest.mark.parametrize("shape", ["description-only", "block-without-url"])
async def test_a_vault_login_with_no_bound_login_page_imports_nothing(org_vault: OrgVaultFake, shape: str) -> None:
    policy = RequestPolicy()
    ctx = SimpleNamespace(organization_id="o_test_org", request_policy=policy)
    ref = await _listed_ref(policy)
    parsed = yaml.safe_load(_draft_binding(ref, "https://portal.example.com/login"))
    [block] = parsed["workflow_definition"]["blocks"]
    if shape == "description-only":
        parsed["workflow_definition"]["parameters"] = []
        block["parameter_keys"] = []
        block["navigation_goal"] = f"Sign in with {ref}"
    else:
        del block["url"]
    draft = yaml.safe_dump(parsed)

    bound_yaml, error = await import_bound_vault_logins(draft, ctx)

    assert (error is None) is (shape == "description-only")
    assert bound_yaml == draft
    assert await org_vault.imported_rows() == []


@pytest.mark.asyncio
async def test_a_draft_binding_rebinds_only_the_credential_slot(org_vault: OrgVaultFake) -> None:
    policy = RequestPolicy()
    ctx = SimpleNamespace(organization_id="o_test_org", request_policy=policy)
    ref = await _listed_ref(policy)
    parsed = yaml.safe_load(_draft_binding(ref, "https://portal.example.com/login"))
    parsed["workflow_definition"]["blocks"][0]["navigation_goal"] = f"Note: {ref}"

    bound_yaml, error = await import_bound_vault_logins(yaml.safe_dump(parsed), ctx)

    [row] = await org_vault.imported_rows()
    bound = yaml.safe_load(bound_yaml)["workflow_definition"]
    assert error is None
    assert bound["parameters"][0]["credential_id"] == row.credential_id
    assert bound["blocks"][0]["navigation_goal"] == f"Note: {ref}"


@pytest.mark.asyncio
async def test_a_draft_binding_by_a_requester_who_cannot_import_is_a_tool_error_that_imports_nothing(
    org_vault: OrgVaultFake, monkeypatch: pytest.MonkeyPatch
) -> None:
    policy = RequestPolicy()
    ctx = SimpleNamespace(organization_id="o_test_org", request_policy=policy)
    draft = _draft_binding(await _listed_ref(policy), "https://portal.example.com/login")
    monkeypatch.setattr(credential_creation_service, "get_request_principal", lambda: None)

    bound_yaml, error = await import_bound_vault_logins(draft, ctx)

    assert error is not None
    assert bound_yaml == draft
    assert await org_vault.imported_rows() == []


async def _imported_credential_id(org_vault: OrgVaultFake) -> str:
    policy = RequestPolicy()
    ref = await _listed_ref(policy)
    _, error = await import_bound_vault_logins(
        _draft_binding(ref, "https://portal.example.com/login"),
        SimpleNamespace(organization_id="o_test_org", request_policy=policy),
    )
    assert error is None
    [row] = await org_vault.imported_rows()
    return row.credential_id


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("block_url", "prior_origins", "refused"),
    [
        ("https://other.example.com/login", [], True),
        ("https://app.portal.example.com/login", [], True),
        ("https://portal.example.com/signin", [], False),
        ("https://other.example.com/login", ["https://other.example.com"], False),
    ],
    ids=["other-site", "subdomain-of-host-match", "own-host", "already-bound-there"],
)
async def test_an_imported_vault_login_is_newly_bound_only_on_its_own_sites(
    org_vault: OrgVaultFake, block_url: str, prior_origins: list[str], refused: bool
) -> None:
    credential_id = await _imported_credential_id(org_vault)
    next_turn = RequestPolicy(existing_workflow_credential_origins={credential_id: prior_origins})

    _, error = await import_bound_vault_logins(
        _draft_binding(credential_id, block_url),
        SimpleNamespace(organization_id="o_test_org", request_policy=next_turn),
    )

    assert (error is not None) is refused


@pytest.mark.asyncio
@pytest.mark.parametrize("unseen_by", ["unreadable", "hidden"])
async def test_an_imported_vault_login_is_not_newly_bound_while_its_vault_is_unseen(
    org_vault: OrgVaultFake, unseen_by: str
) -> None:
    credential_id = await _imported_credential_id(org_vault)
    if unseen_by == "unreadable":
        org_vault.listing.side_effect = BitwardenLoginError("vault locked")
    else:
        org_vault.folders_restricted.return_value = True

    _, error = await import_bound_vault_logins(
        _draft_binding(credential_id, "https://portal.example.com/login"),
        SimpleNamespace(organization_id="o_test_org", request_policy=RequestPolicy()),
    )

    assert error is not None and credential_id in error


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("item_id", "block_url", "refused"),
    [
        (ORG_VAULT_ITEM_ID, "https://other.example.com/login", True),
        (ORG_VAULT_ITEM_ID, "https://portal.example.com/signin", False),
        ("{{ portal_item }}", "https://portal.example.com/signin", True),
        ("{{ portal_item }}", "https://first.example.com/signin", False),
    ],
    ids=["other-site", "own-site", "unlisted-item", "already-bound-there"],
)
async def test_a_bitwarden_login_parameter_is_placed_on_a_new_site_only_within_its_item_sites(
    org_vault: OrgVaultFake, item_id: str, block_url: str, refused: bool
) -> None:
    legacy_id = f"bitwarden_login_credential:{item_id}"
    parsed = yaml.safe_load(_draft_binding("unused", block_url))
    parsed["workflow_definition"]["parameters"] = [
        {"key": "portal_login", "parameter_type": "bitwarden_login_credential", "bitwarden_item_id": item_id}
    ]
    policy = RequestPolicy(existing_workflow_credential_origins={legacy_id: ["https://first.example.com"]})

    _, error = await import_bound_vault_logins(
        yaml.safe_dump(parsed), SimpleNamespace(organization_id="o_test_org", request_policy=policy)
    )

    assert (error is not None) is refused


_RUN_INPUT_LOGIN_WORKFLOW_YAML = """
title: sign in
workflow_definition:
  parameters:
    - parameter_type: workflow
      workflow_parameter_type: credential_id
      key: login
  blocks:
    - block_type: login
      label: sign_in
      url: {url}
      parameter_keys:
        - login
"""


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("run_input", "block_url", "dispatched"),
    [
        ("cred_imported", "https://portal.example.com/login", True),
        ("cred_imported", "https://other.example.com/login", False),
        ("vlogin_0123456789abcdef", "https://portal.example.com/login", False),
    ],
    ids=["own-site", "other-site", "not-imported"],
)
async def test_a_run_input_carries_a_vault_login_only_to_its_own_sites(
    monkeypatch: pytest.MonkeyPatch, run_input: str, block_url: str, dispatched: bool
) -> None:
    harness = await install_run_blocks_harness(
        monkeypatch, workflow_yaml=_RUN_INPUT_LOGIN_WORKFLOW_YAML.format(url=block_url), polled_status="failed"
    )
    monkeypatch.setattr(run_execution_module, "_credential_ids_validation_error", AsyncMock(return_value=None))
    imported = VaultLogin(
        vault_type=CredentialVaultType.BITWARDEN_CONNECTED,
        vault_id="org_vault",
        item_id=ORG_VAULT_ITEM_ID,
        title="Example portal",
        urls=("https://portal.example.com/login",),
        sites=(LoginSite("https://portal.example.com", whole_site=False),),
        credential_id="cred_imported",
    )
    monkeypatch.setattr(
        vault_logins_module, "list_vault_logins", AsyncMock(return_value=VaultLoginListing(logins=(imported,)))
    )
    ctx = make_copilot_ctx(browser_session_id="pbs_chat")
    ctx.staged_workflow = harness["workflow"]
    ctx.frontier_resume_session_id = "pbs_run"
    ctx.request_policy = RequestPolicy(
        resolved_credentials=[_password_credential(credential_id=run_input, vault_type="bitwarden_connected")]
    )

    await _run_blocks_and_collect_debug({"block_labels": ["sign_in"], "parameters": {"login": run_input}}, ctx)

    assert workflow_service_module.prepare_workflow.called is dispatched


@pytest.mark.asyncio
async def test_a_draft_binding_a_login_not_listed_now_imports_nothing(org_vault: OrgVaultFake) -> None:
    ctx = SimpleNamespace(organization_id="o_test_org", request_policy=RequestPolicy())

    _, error = await import_bound_vault_logins(
        _draft_binding("vlogin_0123456789abcdef", "https://portal.example.com/login"), ctx
    )

    assert error is not None and "list_credentials" in error
    assert await org_vault.imported_rows() == []


@pytest.mark.parametrize(
    ("item_id", "persisted", "approved"),
    [
        (ORG_VAULT_ITEM_ID, True, True),
        (ORG_VAULT_ITEM_ID, False, False),
        ("{{ portal_item }}", True, True),
        ("{{ portal_item }}", False, False),
    ],
    ids=["saved-workflow", "unsaved-draft", "templated-saved", "templated-unsaved"],
)
def test_a_bitwarden_login_parameter_runs_only_once_saved(item_id: str, persisted: bool, approved: bool) -> None:
    legacy_id = f"bitwarden_login_credential:{item_id}"
    policy = RequestPolicy(persisted_workflow_credential_ids=[legacy_id] if persisted else [])

    error = _credential_run_approval_error([legacy_id], policy)

    assert (error is None) is approved
    if not approved:
        assert error is not None and "list_credentials" in error
