from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest

from skyvern.forge import app
from skyvern.forge.sdk.copilot.request_policy import RequestPolicy, build_request_policy_trust_floor
from skyvern.forge.sdk.copilot.tools.credentials import _list_credentials
from skyvern.forge.sdk.schemas.credentials import CredentialType


def _cred(name: str, credential_id: str) -> SimpleNamespace:
    return SimpleNamespace(
        name=name,
        credential_id=credential_id,
        tested_url=None,
        credential_type=CredentialType.PASSWORD,
        username="user@example.test",
        totp_type=None,
        card_last4=None,
        card_brand=None,
        secret_label=None,
    )


def _ctx(policy: RequestPolicy, *credential_targets: str) -> SimpleNamespace:
    return SimpleNamespace(
        organization_id="org-1",
        user_message="expanded agent input",
        request_policy=policy,
    )


@pytest.mark.asyncio
async def test_request_policy_trust_floor_makes_only_the_narrow_safety_call() -> None:
    handler = AsyncMock(return_value={"version": "1", "state": "clean", "handling": "none", "citations": []})

    policy = await build_request_policy_trust_floor(
        user_message="Build a workflow for https://example.com/report",
        workflow_yaml="",
        chat_history=[],
        global_llm_context="",
        organization_id="org-1",
        handler=handler,
    )

    handler.assert_awaited_once()
    assert handler.await_args.kwargs["prompt_name"] == "workflow-copilot-raw-secret-safety"
    assert policy.classifier_status == "not_run"
    assert policy.completion_criteria == []
    assert policy.user_provided_site_urls == ["https://example.com/report"]


@pytest.mark.asyncio
async def test_request_policy_trust_floor_survives_a_url_with_a_malformed_authority() -> None:
    handler = AsyncMock(return_value={"version": "1", "state": "clean", "handling": "none", "citations": []})

    policy = await build_request_policy_trust_floor(
        user_message="log into [https://broken.example](https://broken.example) then https://example.com/report",
        workflow_yaml="",
        chat_history=[],
        global_llm_context="",
        organization_id="org-1",
        handler=handler,
    )

    assert policy.user_provided_site_urls == ["https://example.com/report"]


@pytest.mark.asyncio
async def test_request_policy_trust_floor_redacts_raw_secret_in_canonical_message() -> None:
    literal = "password=hunter2-secret-value"
    handler = AsyncMock(return_value={"version": "1", "state": "clean", "handling": "none", "citations": []})

    policy = await build_request_policy_trust_floor(
        user_message=f"Make a draft using {literal}",
        workflow_yaml="",
        chat_history=[],
        global_llm_context="",
        organization_id="org-1",
        handler=handler,
    )

    handler.assert_awaited_once()
    assert policy.raw_secret_detected is False
    assert policy.raw_secret_handling == "none"
    assert policy.raw_secret_safety_status == "clean"
    assert policy.raw_secret_safety_citation_count == 0
    assert policy.allow_run_blocks is True
    assert literal not in policy.canonical_user_message
    assert "hunter2-secret-value" not in policy.canonical_user_message


@pytest.mark.asyncio
async def test_list_credentials_exact_mode_binds_one_grounded_name() -> None:
    policy = RequestPolicy(canonical_user_message="Use saved-login for this workflow")
    ctx = _ctx(policy, "saved-login")
    credential = _cred("saved-login", "cred_one")

    with patch(
        "skyvern.forge.sdk.copilot.tools.credentials.load_credentials",
        AsyncMock(return_value=[credential]),
    ):
        result = await _list_credentials({"exact_reference": "saved-login"}, ctx)

    assert result["data"]["status"] == "resolved"
    assert policy.current_turn_named_credential_ids == {"cred_one"}
    assert [item.credential_id for item in policy.resolved_credentials] == ["cred_one"]


@pytest.mark.asyncio
@pytest.mark.parametrize("classifier_targets", [(), ("different-login",)])
async def test_list_credentials_exact_mode_uses_literal_provenance_not_classifier_targets(
    classifier_targets: tuple[str, ...],
) -> None:
    policy = RequestPolicy(
        canonical_user_message="Please build the workflow with the saved credential saved-login for this site."
    )
    ctx = _ctx(policy, *classifier_targets)
    credential = _cred("saved-login", "cred_one")

    with patch(
        "skyvern.forge.sdk.copilot.tools.credentials.load_credentials",
        AsyncMock(return_value=[credential]),
    ):
        result = await _list_credentials({"exact_reference": "saved-login"}, ctx)

    assert result["data"]["status"] == "resolved"
    assert policy.current_turn_named_credential_ids == {"cred_one"}


@pytest.mark.asyncio
async def test_list_credentials_exact_mode_does_not_let_classifier_choose_between_literal_references() -> None:
    credentials = [_cred("Prod", "cred_prod"), _cred("Backup", "cred_backup")]
    policy = RequestPolicy(canonical_user_message="Use Prod or Backup for this workflow")
    ctx = _ctx(policy, "Prod")

    with patch(
        "skyvern.forge.sdk.copilot.tools.credentials.load_credentials",
        AsyncMock(return_value=credentials),
    ):
        result = await _list_credentials({"exact_reference": "Prod"}, ctx)

    assert result["data"]["status"] == "resolved"
    assert policy.current_turn_named_credential_ids == {"cred_prod"}


@pytest.mark.asyncio
async def test_list_credentials_exact_mode_denies_ungrounded_model_reference() -> None:
    policy = RequestPolicy(canonical_user_message="Use my saved credential")
    ctx = _ctx(policy)
    loader = AsyncMock(return_value=[_cred("invented-login", "cred_one")])

    with patch("skyvern.forge.sdk.copilot.tools.credentials.load_credentials", loader):
        result = await _list_credentials({"exact_reference": "invented-login"}, ctx)

    assert result["data"]["status"] == "denied"
    loader.assert_awaited_once()
    assert policy.resolved_credentials == []


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("latest_user_message", "reference"),
    [
        ("Use Production for this workflow", "Prod"),
        ("Use cred_shared_backup for this workflow", "cred_shared"),
        ("Use saved-login-old for this workflow", "saved-login"),
    ],
)
async def test_list_credentials_exact_mode_denies_reference_embedded_in_larger_identifier(
    latest_user_message: str,
    reference: str,
) -> None:
    policy = RequestPolicy(canonical_user_message=latest_user_message)
    ctx = _ctx(policy, reference)
    loader = AsyncMock(return_value=[_cred(reference, "cred_one")])

    with patch("skyvern.forge.sdk.copilot.tools.credentials.load_credentials", loader):
        result = await _list_credentials({"exact_reference": reference}, ctx)

    assert result["data"]["status"] == "denied"
    loader.assert_awaited_once()
    assert policy.resolved_credentials == []


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("latest_user_message", "reference", "credentials"),
    [
        ("Use Prod Login for this workflow", "Prod", [_cred("Prod", "cred_prod"), _cred("Prod Login", "cred_login")]),
        ("Use prod.example for this workflow", "prod", [_cred("prod", "cred_prod")]),
    ],
)
async def test_list_credentials_exact_mode_prefers_complete_saved_name_over_partial_name(
    latest_user_message: str,
    reference: str,
    credentials: list[SimpleNamespace],
) -> None:
    policy = RequestPolicy(canonical_user_message=latest_user_message)
    ctx = _ctx(policy, reference)

    with patch(
        "skyvern.forge.sdk.copilot.tools.credentials.load_credentials",
        AsyncMock(return_value=credentials),
    ):
        result = await _list_credentials({"exact_reference": reference}, ctx)

    assert result["data"]["status"] == "denied"
    assert policy.resolved_credentials == []


@pytest.mark.asyncio
@pytest.mark.parametrize("message", ['Use "Prod Login", please', "Use (Prod Login).", "Use `Prod Login`"])
async def test_list_credentials_exact_mode_accepts_quoted_name_with_sentence_punctuation(message: str) -> None:
    credential = _cred("Prod Login", "cred_login")
    policy = RequestPolicy(canonical_user_message=message)
    ctx = _ctx(policy, "Prod Login")

    with patch(
        "skyvern.forge.sdk.copilot.tools.credentials.load_credentials",
        AsyncMock(return_value=[credential]),
    ):
        result = await _list_credentials({"exact_reference": "Prod Login"}, ctx)

    assert result["data"]["status"] == "resolved"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "message",
    ["Replace Prod with Prod Login", "Do not use Prod; use Prod Login"],
)
async def test_list_credentials_exact_mode_leaves_selection_semantics_to_the_agent(message: str) -> None:
    credentials = [_cred("Prod", "cred_prod"), _cred("Prod Login", "cred_login")]
    policy = RequestPolicy(canonical_user_message=message)
    ctx = _ctx(policy, "Prod Login")

    with patch(
        "skyvern.forge.sdk.copilot.tools.credentials.load_credentials",
        AsyncMock(return_value=credentials),
    ):
        result = await _list_credentials({"exact_reference": "Prod"}, ctx)

    assert result["data"]["status"] == "resolved"
    assert policy.current_turn_named_credential_ids == {"cred_prod"}


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("latest_user_message", "reference", "credentials", "expected_status"),
    [
        ("Use missing-login", "missing-login", [], "not_found"),
        ("Use Saved-Login", "Saved-Login", [_cred("saved-login", "cred_one")], "not_found"),
        (
            "Use duplicate-login",
            "duplicate-login",
            [_cred("duplicate-login", "cred_one"), _cred("duplicate-login", "cred_two")],
            "ambiguous",
        ),
        (
            "Use cred_shared",
            "cred_shared",
            [_cred("other", "cred_shared"), _cred("cred_shared", "cred_two")],
            "ambiguous",
        ),
    ],
)
async def test_list_credentials_exact_mode_fails_closed_on_non_unique_exact_match(
    latest_user_message: str,
    reference: str,
    credentials: list[SimpleNamespace],
    expected_status: str,
) -> None:
    policy = RequestPolicy(canonical_user_message=latest_user_message)
    ctx = _ctx(policy, reference)

    with patch(
        "skyvern.forge.sdk.copilot.tools.credentials.load_credentials",
        AsyncMock(return_value=credentials),
    ):
        result = await _list_credentials({"exact_reference": reference}, ctx)

    assert result["data"]["status"] == expected_status
    assert policy.resolved_credentials == []


@pytest.mark.asyncio
async def test_list_credentials_exact_mode_accepts_typed_resume_reference() -> None:
    credential = _cred("saved-login", "cred_one")
    policy = RequestPolicy(
        resolved_credentials=[credential],
        current_turn_named_credential_ids={"cred_one"},
        canonical_user_message="continue",
    )
    ctx = _ctx(policy)

    with patch(
        "skyvern.forge.sdk.copilot.tools.credentials.load_credentials",
        AsyncMock(return_value=[credential]),
    ):
        result = await _list_credentials({"exact_reference": "saved-login"}, ctx)

    assert result["data"]["status"] == "resolved"
    assert [item.credential_id for item in policy.resolved_credentials] == ["cred_one"]


@pytest.mark.asyncio
async def test_list_credentials_discovery_does_not_grant_authority(monkeypatch: pytest.MonkeyPatch) -> None:
    policy = RequestPolicy()
    ctx = SimpleNamespace(organization_id="org-1", user_message="List credentials", request_policy=policy)
    credential = _cred("saved-login", "cred_one")
    database = SimpleNamespace(credentials=SimpleNamespace(get_credentials=AsyncMock(return_value=[credential])))

    monkeypatch.setattr(object.__getattribute__(app, "_inst"), "DATABASE", database, raising=False)
    result = await _list_credentials({"page": 1, "page_size": 10}, ctx)

    assert result["ok"] is True
    assert policy.resolved_credentials == []
    assert policy.current_turn_named_credential_ids == set()
    assert [item.credential_id for item in policy.discovered_credentials] == ["cred_one"]
