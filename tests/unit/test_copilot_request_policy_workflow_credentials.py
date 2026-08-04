from __future__ import annotations

import textwrap
from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest
from structlog.testing import capture_logs

from skyvern.forge.sdk.copilot.context import (
    CredentialCheck,
    StructuredContext,
    adopt_model_authored_context,
    record_approved_credentials_in_global_llm_context,
    record_signin_email_in_global_llm_context,
)
from skyvern.forge.sdk.copilot.credential_pause import credential_pause_reason
from skyvern.forge.sdk.copilot.credential_resolution import resolve_by_url
from skyvern.forge.sdk.copilot.request_policy import (
    _AMBIGUOUS_URL_CREDENTIAL_QUESTION,
    _AMBIGUOUS_URLLESS_CREDENTIAL_QUESTION,
    _LOGIN_CREDENTIAL_QUESTION,
    _SAVED_CREDENTIAL_NAME_QUESTION,
    _SAVED_CREDENTIAL_NAME_QUESTION_STABLE_PREFIX,
    _STORED_CREDENTIAL_URL_QUESTION,
    CREDENTIAL_DEFERRED_DRAFT_REASONS,
    CREDENTIAL_PROMPT_CLARIFICATION_REASONS,
    LiveCredentialAdmission,
    LivePageResolutionRecord,
    RequestPolicy,
    _can_defer_unresolved_credential_name_for_draft,
    _classification_from_raw,
    _clean_email_list,
    _exact_credential_name_candidates,
    _resolve_credentials,
    _saved_credential_names_mentioned,
    _seed_prior_approved_credentials,
    _should_defer_repeated_unresolved_credential_question,
    _workflow_credential_inputs_unbound,
    admit_credential_for_live_page,
    build_request_policy,
    credential_prompt_reason,
    resolve_credential_for_live_page,
)
from skyvern.forge.sdk.copilot.tools.credentials import _credential_run_approval_error
from skyvern.forge.sdk.schemas.credentials import CredentialType
from skyvern.forge.sdk.schemas.workflow_copilot import (
    WorkflowCopilotChatHistoryMessage,
    WorkflowCopilotChatSender,
)


def _yaml(body: str) -> str:
    return textwrap.dedent(body).strip() + "\n"


def test_detects_workflow_level_credential_referencing_empty_workflow_param() -> None:
    yaml = _yaml(
        """
        title: example
        workflow_definition:
          parameters:
          - key: username_key_vault_id
            parameter_type: workflow
            workflow_parameter_type: string
            default_value: ''
          - key: password_key_vault_id
            parameter_type: workflow
            workflow_parameter_type: string
            default_value: null
          - key: azure_credentials
            parameter_type: azure_vault_credential
            azure_vault_credential_parameter_id: azcp_528000000000000000
            vault_name: skyvern-secret-store
            username_key: '{{username_key_vault_id}}'
            password_key: '{{password_key_vault_id}}'
          blocks:
          - block_type: login
            label: login
            parameters: []
        """
    )

    findings = _workflow_credential_inputs_unbound(yaml)
    kinds = {(f["location"], f["field"], f["kind"]) for f in findings}

    assert ("workflow", "username_key", "credential_template_unbound") in kinds
    assert ("workflow", "password_key", "credential_template_unbound") in kinds


def test_resolves_when_workflow_param_has_non_empty_default_value() -> None:
    yaml = _yaml(
        """
        title: example
        workflow_definition:
          parameters:
          - key: username_key_vault_id
            parameter_type: workflow
            workflow_parameter_type: string
            default_value: user@example.com
          - key: password_key_vault_id
            parameter_type: workflow
            workflow_parameter_type: string
            default_value: stored
          - key: azure_credentials
            parameter_type: azure_vault_credential
            azure_vault_credential_parameter_id: azcp_real
            vault_name: skyvern-secret-store
            username_key: '{{username_key_vault_id}}'
            password_key: '{{password_key_vault_id}}'
          blocks: []
        """
    )

    assert _workflow_credential_inputs_unbound(yaml) == []


def test_jinja_reference_to_undefined_workflow_param_is_flagged() -> None:
    yaml = _yaml(
        """
        title: example
        workflow_definition:
          parameters:
          - key: azure_credentials
            parameter_type: azure_vault_credential
            azure_vault_credential_parameter_id: azcp_real
            vault_name: skyvern-secret-store
            username_key: '{{not_a_parameter}}'
            password_key: literal
          blocks: []
        """
    )

    findings = _workflow_credential_inputs_unbound(yaml)

    assert any(f["kind"] == "credential_template_undefined" and f["missing"] == "not_a_parameter" for f in findings)


def test_empty_literal_credential_key_flagged() -> None:
    yaml = _yaml(
        """
        title: example
        workflow_definition:
          parameters:
          - key: azure_credentials
            parameter_type: azure_vault_credential
            azure_vault_credential_parameter_id: azcp_real
            vault_name: skyvern-secret-store
            username_key: ''
            password_key: '   '
          blocks: []
        """
    )

    findings = _workflow_credential_inputs_unbound(yaml)

    assert {(f["field"], f["kind"]) for f in findings} == {
        ("username_key", "credential_empty"),
        ("password_key", "credential_empty"),
    }


def test_mock_eval_keys_with_placeholder_suffix_are_not_false_flagged() -> None:
    yaml = _yaml(
        """
        title: example
        workflow_definition:
          parameters:
          - key: username_key_placeholder
            parameter_type: workflow
            workflow_parameter_type: string
            default_value: mock-user
          - key: password_key_placeholder
            parameter_type: workflow
            workflow_parameter_type: string
            default_value: mock-pass
          - key: azure_credentials
            parameter_type: azure_vault_credential
            azure_vault_credential_parameter_id: azcp_placeholder
            vault_name: skyvern-secret-store
            username_key: '{{ username_key_placeholder }}'
            password_key: '{{ password_key_placeholder }}'
          blocks: []
        """
    )

    assert _workflow_credential_inputs_unbound(yaml) == []


def test_block_level_credential_inside_loop_blocks_is_walked() -> None:
    yaml = _yaml(
        """
        title: example
        workflow_definition:
          parameters: []
          blocks:
          - block_type: for_loop
            label: outer
            loop_blocks:
            - block_type: login
              label: inner_login
              parameters:
              - parameter_type: azure_vault_credential
                key: azure_credentials
                azure_vault_credential_parameter_id: azcp_real
                vault_name: skyvern-secret-store
                username_key: '{{missing_key}}'
                password_key: '{{missing_key}}'
        """
    )

    findings = _workflow_credential_inputs_unbound(yaml)

    assert any(
        f["location"] == "inner_login" and f["field"] == "username_key" and f["kind"] == "credential_template_undefined"
        for f in findings
    )


def test_non_login_credential_types_are_out_of_scope() -> None:
    """Only login-credential types have username/password key fields; secret-only
    types (AWS_SECRET, AZURE_SECRET, Bitwarden Sensitive/CreditCard) use different
    schemas and fall outside this guardrail."""
    yaml = _yaml(
        """
        title: example
        workflow_definition:
          parameters:
          - key: my_secret
            parameter_type: aws_secret
            aws_key: '{{ some_unbound_key }}'
          blocks: []
        """
    )

    assert _workflow_credential_inputs_unbound(yaml) == []


def test_workflow_without_credentials_returns_empty() -> None:
    yaml = _yaml(
        """
        title: example
        workflow_definition:
          parameters: []
          blocks:
          - block_type: navigation
            label: visit
            url: https://example.com/
            navigation_goal: open
        """
    )

    assert _workflow_credential_inputs_unbound(yaml) == []


def test_malformed_or_empty_yaml_is_inert() -> None:
    assert _workflow_credential_inputs_unbound("") == []
    assert _workflow_credential_inputs_unbound("- not a workflow yaml\n") == []
    assert _workflow_credential_inputs_unbound(":: broken yaml ::") == []


def _discovered_context(*credential_ids: str) -> str:
    structured = StructuredContext(
        credentials_checked=[
            CredentialCheck(credential_name=cid, credential_id=cid, found=True) for cid in credential_ids
        ]
    )
    return structured.to_json_str()


@pytest.mark.asyncio
async def test_discovered_credentials_seed_approved_set_on_none_turn() -> None:
    org_credentials = [
        SimpleNamespace(credential_id="cred_amazon"),
        SimpleNamespace(credential_id="cred_quicken"),
    ]
    with patch(
        "skyvern.forge.app.DATABASE.credentials.get_credentials_by_ids",
        new=AsyncMock(return_value=org_credentials),
    ):
        policy = await build_request_policy(
            user_message="yes, use both of those",
            workflow_yaml="",
            chat_history=[],
            global_llm_context=_discovered_context("cred_amazon", "cred_quicken"),
            organization_id="o_test",
            handler=None,
        )

    assert policy.credential_input_kind == "none"
    assert [c.credential_id for c in policy.discovered_credentials] == ["cred_amazon", "cred_quicken"]


@pytest.mark.asyncio
async def test_discovered_credential_absent_from_org_is_not_approved() -> None:
    with patch(
        "skyvern.forge.app.DATABASE.credentials.get_credentials_by_ids",
        new=AsyncMock(return_value=[SimpleNamespace(credential_id="cred_amazon")]),
    ):
        policy = await build_request_policy(
            user_message="yes",
            workflow_yaml="",
            chat_history=[],
            global_llm_context=_discovered_context("cred_amazon", "cred_ghost"),
            organization_id="o_test",
            handler=None,
        )

    assert [c.credential_id for c in policy.discovered_credentials] == ["cred_amazon"]


@pytest.mark.asyncio
async def test_no_discovered_credentials_leaves_approved_set_empty() -> None:
    get_by_ids = AsyncMock(return_value=[])
    with patch("skyvern.forge.app.DATABASE.credentials.get_credentials_by_ids", new=get_by_ids):
        policy = await build_request_policy(
            user_message="add a step to download the report",
            workflow_yaml="",
            chat_history=[],
            global_llm_context="",
            organization_id="o_test",
            handler=None,
        )

    assert policy.discovered_credentials == []
    get_by_ids.assert_not_awaited()


@pytest.mark.asyncio
async def test_fallback_code_block_credential_request_saves_draft_without_running() -> None:
    credential = SimpleNamespace(
        credential_id="cred_email_otp",
        name="mock-portal-login-email-otp",
        tested_url="http://localhost:8900/telco_billing/northwind/?mfa=email",
    )
    with patch(
        "skyvern.forge.app.DATABASE.credentials.get_credentials",
        new=AsyncMock(return_value=[credential]),
    ):
        policy = await build_request_policy(
            user_message=(
                "Build this as a Code block credential test using the saved credential named "
                "mock-portal-login-email-otp. Do not create a Login block for sign-in or MFA, "
                "and use await login_credentials.otp() for the email one-time-code."
            ),
            workflow_yaml="",
            chat_history=[],
            global_llm_context="",
            organization_id="o_test",
            handler=None,
        )

    assert policy.classifier_status == "fallback"
    assert policy.credential_input_kind == "credential_name"
    assert policy.credential_refs == ["mock-portal-login-email-otp"]
    assert policy.resolved_credentials == [credential]
    assert policy.allow_update_workflow is True
    assert policy.allow_run_blocks is False
    assert policy.allow_missing_credentials_in_draft is True
    assert policy.testing_intent == "skip_test"
    assert policy.requires_user_clarification is False


@pytest.mark.asyncio
async def test_fallback_code_block_generic_one_time_code_does_not_skip_run() -> None:
    policy = await build_request_policy(
        user_message="Build a code block that handles a one time code after sign in.",
        workflow_yaml="",
        chat_history=[],
        global_llm_context="",
        organization_id="o_test",
        handler=None,
    )

    assert policy.classifier_status == "fallback"
    assert policy.allow_run_blocks is True
    assert policy.allow_missing_credentials_in_draft is False
    assert policy.testing_intent == "unspecified"


def test_credential_prompt_clarification_reasons_membership() -> None:
    assert CREDENTIAL_PROMPT_CLARIFICATION_REASONS == {
        "raw_secret",
        "credential_name_unresolved",
        "credential_invention_requested",
        "workflow_credential_inputs_unbound",
        "login_credentials_unresolved",
    }


def test_credential_deferred_draft_reasons_is_narrower_than_prompt_reasons() -> None:
    assert CREDENTIAL_DEFERRED_DRAFT_REASONS < CREDENTIAL_PROMPT_CLARIFICATION_REASONS


def test_credential_prompt_reason_typed_reason_wins_over_deferred_draft_flag() -> None:
    policy = RequestPolicy(clarification_reason="credential_name_unresolved", allow_missing_credentials_in_draft=True)
    assert credential_prompt_reason(policy, None) == "credential_name_unresolved"


def test_credential_prompt_reason_deferred_draft_when_reason_cleared_but_flag_set() -> None:
    # Mirrors the explicit-defer path (_apply_explicit_code_block_credential_draft_policy),
    # which clears clarification_reason to "none" while leaving both flags set.
    policy = RequestPolicy(
        clarification_reason="none",
        allow_missing_credentials_in_draft=True,
        credential_draft_deferred_explicitly=True,
    )
    assert credential_prompt_reason(policy, None) == "credential_deferred_draft"


def test_credential_prompt_reason_ignores_generic_skip_test_with_no_credential_signal() -> None:
    # allow_missing_credentials_in_draft alone also fires for the generic skip_test
    # fallthrough (any "draft only, don't test" turn), independent of credentials;
    # only credential_draft_deferred_explicitly means a credential was really deferred.
    policy = RequestPolicy(
        testing_intent="skip_test",
        clarification_reason="none",
        allow_missing_credentials_in_draft=True,
        credential_draft_deferred_explicitly=False,
    )
    assert credential_prompt_reason(policy, None) is None


@pytest.mark.asyncio
async def test_explicit_code_block_login_block_ban_surfaces_credential_prompt_end_to_end() -> None:
    policy = await build_request_policy(
        user_message="Write this as a code block. Do not create a login block for this part.",
        workflow_yaml="",
        chat_history=[],
        global_llm_context="",
        organization_id="o_test",
        handler=None,
    )
    assert policy.credential_input_kind == "none"
    assert policy.testing_intent == "skip_test"
    assert credential_prompt_reason(policy, None) == "credential_deferred_draft"


@pytest.mark.parametrize(
    "text",
    [
        "Add it at https://app.skyvern.com/credentials.",
        "ADD IT AT HTTPS://APP.SKYVERN.COM/CREDENTIALS.",
        "Store it in the Credentials UI first.",
        "store it in the CREDENTIALS UI first.",
    ],
)
def test_credential_prompt_reason_marker_fallback_is_case_insensitive(text: str) -> None:
    assert credential_prompt_reason(None, text) == "assistant_directed"


def test_credential_prompt_reason_policy_none_is_safe() -> None:
    assert credential_prompt_reason(None, None) is None
    assert credential_prompt_reason(None, "Everything is set, no action needed.") is None


def _cred(
    name: str,
    credential_id: str,
    tested_url: str | None = None,
    credential_type: CredentialType = CredentialType.PASSWORD,
) -> SimpleNamespace:
    return SimpleNamespace(
        name=name, credential_id=credential_id, tested_url=tested_url, credential_type=credential_type
    )


async def _build_with_forced_classifier(
    *,
    user_message: str,
    classifier_policy: RequestPolicy,
    org_credentials: list[SimpleNamespace],
    get_credentials: AsyncMock | None = None,
    get_credentials_by_ids: AsyncMock | None = None,
    workflow_yaml: str = "",
    global_llm_context: str = "",
    connected_gmail: str | None = None,
    gmail_lookup: AsyncMock | None = None,
) -> RequestPolicy:
    load_mock = get_credentials or AsyncMock(return_value=org_credentials)
    by_ids_mock = get_credentials_by_ids or AsyncMock(return_value=[])
    gmail_mock = gmail_lookup or AsyncMock(return_value=connected_gmail)
    with (
        patch(
            "skyvern.forge.sdk.copilot.request_policy._classify_request",
            new=AsyncMock(return_value=classifier_policy),
        ),
        patch("skyvern.forge.app.DATABASE.credentials.get_credentials", new=load_mock),
        patch("skyvern.forge.app.DATABASE.credentials.get_credentials_by_ids", new=by_ids_mock),
        patch("skyvern.forge.sdk.copilot.request_policy.connected_gmail_address", new=gmail_mock),
    ):
        return await build_request_policy(
            user_message=user_message,
            workflow_yaml=workflow_yaml,
            chat_history=[],
            global_llm_context=global_llm_context,
            organization_id="o_test",
            handler=None,
        )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "kind",
    ["none", "credential_id", "credential_name", "website_stored_credential", "placeholder"],
)
async def test_success_empty_refs_resolves_named_credential(kind: str) -> None:
    credential = _cred("mock-portal-login", "cred_login")
    policy = await _build_with_forced_classifier(
        user_message="Sign in with the saved credential named mock-portal-login.",
        classifier_policy=RequestPolicy(credential_input_kind=kind, classifier_status="success"),
        org_credentials=[credential, _cred("other", "cred_other")],
    )

    assert policy.classifier_status == "success"
    assert policy.credential_input_kind == kind
    assert [c.credential_id for c in policy.resolved_credentials] == ["cred_login"]
    assert policy.clarification_reason != "credential_name_unresolved"


@pytest.mark.asyncio
async def test_success_website_kind_with_nonmatching_url_resolves_named_credential() -> None:
    credential = _cred("mock-portal-login", "cred_login", tested_url="https://saved.example.net/signin")
    policy = await _build_with_forced_classifier(
        user_message="Log in with the saved credential named mock-portal-login here.",
        classifier_policy=RequestPolicy(
            credential_input_kind="website_stored_credential",
            login_page_urls=["https://unrelated.example.com/login"],
            classifier_status="success",
        ),
        org_credentials=[credential],
    )

    assert [c.credential_id for c in policy.resolved_credentials] == ["cred_login"]
    assert policy.clarification_reason != "credential_name_unresolved"
    assert policy.clarification_question is None


@pytest.mark.asyncio
async def test_success_website_kind_prefers_named_credential_over_matching_url() -> None:
    url_credential = _cred("portal-cred", "cred_url", tested_url="https://portal.example.com/login")
    named_credential = _cred("other-login", "cred_named", tested_url="https://elsewhere.example.net/login")
    policy = await _build_with_forced_classifier(
        user_message="Log in with the saved credential named other-login.",
        classifier_policy=RequestPolicy(
            credential_input_kind="website_stored_credential",
            login_page_urls=["https://portal.example.com/login"],
            classifier_status="success",
        ),
        org_credentials=[url_credential, named_credential],
    )

    assert policy.credential_input_kind == "website_stored_credential"
    assert [c.credential_id for c in policy.resolved_credentials] == ["cred_named"]


async def _resolve_direct(
    policy: RequestPolicy,
    *,
    user_message: str,
    org_credentials: list[SimpleNamespace],
    get_credentials: AsyncMock | None = None,
    get_credentials_by_ids: AsyncMock | None = None,
) -> tuple[AsyncMock, AsyncMock]:
    load_mock = get_credentials or AsyncMock(return_value=org_credentials)
    by_ids_mock = get_credentials_by_ids or AsyncMock(return_value=[])
    with (
        patch("skyvern.forge.app.DATABASE.credentials.get_credentials", new=load_mock),
        patch("skyvern.forge.app.DATABASE.credentials.get_credentials_by_ids", new=by_ids_mock),
    ):
        await _resolve_credentials(policy, "o_test", user_message=user_message)
    return load_mock, by_ids_mock


@pytest.mark.parametrize(
    "user_message",
    ["Use credential ID cred_current.", "Use the saved credential named current-login."],
)
def test_draft_deferral_recognizes_current_turn_credential_scope(user_message: str) -> None:
    policy = RequestPolicy(
        credential_input_kind="none",
        clarification_reason="credential_name_unresolved",
    )

    assert _can_defer_unresolved_credential_name_for_draft(
        policy,
        global_llm_context="",
        user_message=user_message,
    )


@pytest.mark.parametrize(
    "user_message",
    ["Use credential ID cred_current.", "Use the saved credential named current-login."],
)
def test_repeated_question_deferral_preserves_current_turn_credential_scope(user_message: str) -> None:
    policy = RequestPolicy(
        credential_input_kind="none",
        clarification_reason="credential_name_unresolved",
    )
    history = [
        WorkflowCopilotChatHistoryMessage(
            sender=WorkflowCopilotChatSender.AI,
            content=_SAVED_CREDENTIAL_NAME_QUESTION_STABLE_PREFIX,
            created_at=datetime.now(UTC),
        )
    ]

    assert not _should_defer_repeated_unresolved_credential_question(
        policy,
        chat_history=history,
        user_message=user_message,
    )


@pytest.mark.asyncio
async def test_name_miss_does_not_advance_to_classifier_only_url() -> None:
    url_credential = _cred("url-login", "cred_url", tested_url="https://portal.example.com/login")
    policy = RequestPolicy(
        credential_input_kind="credential_name",
        credential_refs=["missing-name"],
        login_page_urls=["https://portal.example.com/login"],
    )

    load_mock, _ = await _resolve_direct(
        policy,
        user_message="Use the saved credential named missing-name.",
        org_credentials=[url_credential],
    )

    assert policy.resolved_credentials == []
    assert policy.clarification_reason == "credential_name_unresolved"
    assert "missing-name" in (policy.clarification_question or "")
    assert policy.credential_ask_card_answerable is True
    assert credential_pause_reason(SimpleNamespace(request_policy=policy)) == "login_credentials_unresolved"
    load_mock.assert_awaited_once()


@pytest.mark.asyncio
@pytest.mark.parametrize("name", ["abc", "spare-portal"], ids=["three_char", "twelve_char"])
async def test_url_less_ambiguity_is_independent_of_credential_name_length(name: str) -> None:
    policy = await _build_with_forced_classifier(
        user_message="Log into the portal at https://portal.example.com/login.",
        classifier_policy=RequestPolicy(
            credential_input_kind="website_stored_credential",
            login_page_urls=["https://portal.example.com/login"],
            login_intent=True,
            classifier_status="success",
        ),
        org_credentials=[_cred(name, "cred_one"), _cred("other-login", "cred_two")],
    )

    assert policy.credential_ask_card_answerable is True
    assert policy.credential_ask_candidate_ids == ["cred_one", "cred_two"]
    assert credential_pause_reason(SimpleNamespace(request_policy=policy)) == "login_credentials_unresolved"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "kind",
    ["none", "raw_secret", "credential_id", "credential_name", "website_stored_credential", "placeholder"],
)
@pytest.mark.parametrize("credential_refs", [[], ["mock-portal-login"]], ids=["without_refs", "with_refs"])
async def test_resolver_exact_name_is_independent_of_classifier_label(
    kind: str,
    credential_refs: list[str],
) -> None:
    credential = _cred("mock-portal-login", "cred_login")
    policy = RequestPolicy(credential_input_kind=kind, credential_refs=credential_refs)

    await _resolve_direct(
        policy,
        user_message="Sign in with the saved credential named mock-portal-login.",
        org_credentials=[credential],
    )

    assert policy.credential_input_kind == kind
    assert [candidate.credential_id for candidate in policy.resolved_credentials] == ["cred_login"]
    assert policy.clarification_question is None


@pytest.mark.asyncio
async def test_explicit_id_wins_and_skips_name_and_url_scan() -> None:
    by_ids = AsyncMock(return_value=[_cred("id-login", "cred_explicit")])
    load_mock = AsyncMock(side_effect=AssertionError("organization scan should not run after an ID hit"))
    policy = RequestPolicy(
        credential_input_kind="credential_id",
        credential_refs=["cred_explicit", "named-login"],
        login_page_urls=["https://portal.example.com/login"],
    )

    await _resolve_direct(
        policy,
        user_message="Use credential ID cred_explicit and the saved credential named named-login.",
        org_credentials=[],
        get_credentials=load_mock,
        get_credentials_by_ids=by_ids,
    )

    assert [candidate.credential_id for candidate in policy.resolved_credentials] == ["cred_explicit"]
    by_ids.assert_awaited_once_with(["cred_explicit"], organization_id="o_test")
    load_mock.assert_not_awaited()


@pytest.mark.asyncio
async def test_explicit_id_wins_for_non_id_label_and_competing_url_match() -> None:
    explicit = _cred("id-login", "cred_explicit")
    competing = _cred("url-login", "cred_url", tested_url="https://portal.example.com/login")
    by_ids = AsyncMock(return_value=[explicit])
    load_mock = AsyncMock(side_effect=AssertionError("organization scan should not run after an ID hit"))
    policy = RequestPolicy(
        credential_input_kind="website_stored_credential",
        credential_refs=["cred_explicit"],
        login_page_urls=["https://portal.example.com/login"],
    )

    await _resolve_direct(
        policy,
        user_message="Use credential ID cred_explicit for https://portal.example.com/login.",
        org_credentials=[competing],
        get_credentials=load_mock,
        get_credentials_by_ids=by_ids,
    )

    assert [candidate.credential_id for candidate in policy.resolved_credentials] == ["cred_explicit"]
    by_ids.assert_awaited_once_with(["cred_explicit"], organization_id="o_test")
    load_mock.assert_not_awaited()


@pytest.mark.asyncio
async def test_explicit_id_label_allows_colon() -> None:
    by_ids = AsyncMock(return_value=[_cred("id-login", "cred_explicit")])
    policy = RequestPolicy(credential_input_kind="credential_id")

    await _resolve_direct(
        policy,
        user_message="Use credential ID: cred_explicit.",
        org_credentials=[],
        get_credentials_by_ids=by_ids,
    )

    assert [candidate.credential_id for candidate in policy.resolved_credentials] == ["cred_explicit"]
    by_ids.assert_awaited_once_with(["cred_explicit"], organization_id="o_test")


@pytest.mark.asyncio
async def test_classifier_id_absent_from_current_message_does_not_resolve() -> None:
    by_ids = AsyncMock(side_effect=AssertionError("stale classifier IDs must not be resolved"))
    policy = RequestPolicy(
        credential_input_kind="credential_id",
        credential_refs=["cred_stale"],
    )

    await _resolve_direct(
        policy,
        user_message="Continue with the workflow.",
        org_credentials=[],
        get_credentials_by_ids=by_ids,
    )

    assert policy.resolved_credentials == []
    by_ids.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "user_message",
    [
        "Do not use cred_prod.",
        "Avoid using cred_prod.",
        "Exclude credential ID cred_prod.",
        "Do not use the old workflow because it still mentions credential ID cred_prod.",
        "Do not use this credential, credential ID cred_prod.",
    ],
)
async def test_negated_explicit_id_does_not_resolve(user_message: str) -> None:
    by_ids = AsyncMock(side_effect=AssertionError("negated credential IDs must not be resolved"))
    policy = RequestPolicy(
        credential_input_kind="credential_id",
        credential_refs=["cred_prod"],
    )

    await _resolve_direct(
        policy,
        user_message=user_message,
        org_credentials=[],
        get_credentials_by_ids=by_ids,
    )

    assert policy.resolved_credentials == []
    by_ids.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "user_message",
    [
        "Use this as a documentation example: cred_prod.",
        "Use the staging workflow; the notes mention cred_prod.",
        "Do not use the old workflow because it mentions cred_prod.",
        'Document the string "cred_prod" in the workflow notes.',
    ],
)
async def test_unrelated_explicit_id_mention_does_not_resolve(user_message: str) -> None:
    by_ids = AsyncMock(side_effect=AssertionError("unrelated credential IDs must not be resolved"))
    policy = RequestPolicy(
        credential_input_kind="credential_id",
        credential_refs=["cred_prod"],
    )

    await _resolve_direct(
        policy,
        user_message=user_message,
        org_credentials=[],
        get_credentials_by_ids=by_ids,
    )

    assert policy.resolved_credentials == []
    by_ids.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "user_message",
    [
        "Don't use cred_old; use cred_new.",
        "Don't use cred_old but use cred_new.",
        "Use cred_new instead of cred_old.",
        "Use cred_new rather than cred_old.",
        "Use cred_new instead of the credential cred_old.",
        "Use cred_new rather than credential ID cred_old.",
        "Use cred_new, not cred_old.",
    ],
)
async def test_later_affirmative_id_resolves_after_excluded_id(user_message: str) -> None:
    selected = _cred("new-login", "cred_new")
    by_ids = AsyncMock(return_value=[selected])
    policy = RequestPolicy(
        credential_input_kind="credential_id",
        credential_refs=["cred_old", "cred_new"],
    )

    await _resolve_direct(
        policy,
        user_message=user_message,
        org_credentials=[],
        get_credentials_by_ids=by_ids,
    )

    assert [credential.credential_id for credential in policy.resolved_credentials] == ["cred_new"]
    by_ids.assert_awaited_once_with(["cred_new"], organization_id="o_test")


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "user_message",
    [
        "Use cred_old; switch to cred_new instead.",
        "Use cred_old, actually replace it with cred_new.",
    ],
)
async def test_replacement_id_discards_superseded_candidate(user_message: str) -> None:
    selected = _cred("new-login", "cred_new")
    by_ids = AsyncMock(return_value=[selected])
    policy = RequestPolicy(credential_input_kind="credential_id")

    await _resolve_direct(
        policy,
        user_message=user_message,
        org_credentials=[],
        get_credentials_by_ids=by_ids,
    )

    assert [credential.credential_id for credential in policy.resolved_credentials] == ["cred_new"]
    by_ids.assert_awaited_once_with(["cred_new"], organization_id="o_test")


@pytest.mark.asyncio
async def test_chained_replacement_uses_final_affirmative_id() -> None:
    selected = _cred("final-login", "cred_final")
    by_ids = AsyncMock(return_value=[selected])
    policy = RequestPolicy(credential_input_kind="credential_id")

    await _resolve_direct(
        policy,
        user_message="Replace cred_old with cred_new, but do not use cred_new; use cred_final.",
        org_credentials=[],
        get_credentials_by_ids=by_ids,
    )

    assert [credential.credential_id for credential in policy.resolved_credentials] == ["cred_final"]
    by_ids.assert_awaited_once_with(["cred_final"], organization_id="o_test")


@pytest.mark.asyncio
async def test_unrelated_replacement_does_not_grant_credential_id_authority() -> None:
    alpha = _cred("alpha", "cred_alpha")
    by_ids = AsyncMock(side_effect=AssertionError("documentation ID must not gain credential authority"))
    policy = RequestPolicy(credential_input_kind="credential_name")

    await _resolve_direct(
        policy,
        user_message="Use credential alpha; replace the documentation credential example with cred_beta.",
        org_credentials=[alpha],
        get_credentials_by_ids=by_ids,
    )

    assert [credential.credential_id for credential in policy.resolved_credentials] == ["cred_alpha"]
    by_ids.assert_not_awaited()


@pytest.mark.asyncio
async def test_unrelated_replacement_does_not_grant_credential_name_authority() -> None:
    unrelated = _cred("new-login", "cred_new")
    policy = RequestPolicy(credential_input_kind="credential_name")

    await _resolve_direct(
        policy,
        user_message="Use credential alpha; replace this field with credential new-login.",
        org_credentials=[unrelated],
    )

    assert policy.resolved_credentials == []
    assert policy.clarification_reason == "credential_name_unresolved"
    assert "alpha" in (policy.clarification_question or "")


@pytest.mark.asyncio
async def test_unrelated_replacement_does_not_grant_postfix_credential_name_authority() -> None:
    unrelated = _cred("new-login", "cred_new")
    policy = RequestPolicy(credential_input_kind="credential_name")

    await _resolve_direct(
        policy,
        user_message="Use credential alpha; replace this field with the new-login credential.",
        org_credentials=[unrelated],
    )

    assert policy.resolved_credentials == []
    assert policy.clarification_reason == "credential_name_unresolved"
    assert "alpha" in (policy.clarification_question or "")


@pytest.mark.asyncio
async def test_negated_replacement_does_not_select_rejected_id() -> None:
    selected = _cred("old-login", "cred_old")
    by_ids = AsyncMock(return_value=[selected])
    policy = RequestPolicy(credential_input_kind="credential_id")

    await _resolve_direct(
        policy,
        user_message="Use cred_old; do not switch to cred_new.",
        org_credentials=[],
        get_credentials_by_ids=by_ids,
    )

    assert [credential.credential_id for credential in policy.resolved_credentials] == ["cred_old"]
    by_ids.assert_awaited_once_with(["cred_old"], organization_id="o_test")


@pytest.mark.asyncio
async def test_bare_id_appositive_negation_revokes_earlier_authority() -> None:
    by_ids = AsyncMock(side_effect=AssertionError("later rejection must revoke the earlier ID"))
    policy = RequestPolicy(credential_input_kind="credential_id")

    await _resolve_direct(
        policy,
        user_message="Use cred_good; do not use cred_bad, cred_good.",
        org_credentials=[],
        get_credentials_by_ids=by_ids,
    )

    assert policy.resolved_credentials == []
    by_ids.assert_not_awaited()


@pytest.mark.asyncio
async def test_invalid_explicit_id_does_not_fall_through_to_name() -> None:
    load_mock = AsyncMock(side_effect=AssertionError("organization scan should not run after an explicit ID"))
    policy = RequestPolicy(
        credential_input_kind="credential_id",
        credential_refs=["cred_missing", "named-login"],
    )

    await _resolve_direct(
        policy,
        user_message="Use credential ID cred_missing and the saved credential named named-login.",
        org_credentials=[],
        get_credentials=load_mock,
    )

    assert policy.invalid_credential_ids == ["cred_missing"]
    assert policy.clarification_reason == "credential_name_unresolved"
    assert policy.credential_ask_card_answerable is False
    assert credential_pause_reason(SimpleNamespace(request_policy=policy)) is None
    load_mock.assert_not_awaited()


@pytest.mark.asyncio
async def test_duplicate_name_hits_are_deduplicated_by_credential_id() -> None:
    credential = _cred("named-login", "cred_named")
    policy = RequestPolicy(credential_input_kind="placeholder")

    await _resolve_direct(
        policy,
        user_message="Use the saved credential named named-login.",
        org_credentials=[credential, credential],
    )

    assert [candidate.credential_id for candidate in policy.resolved_credentials] == ["cred_named"]
    assert policy.requires_user_clarification is False


@pytest.mark.asyncio
async def test_multiple_name_hits_keep_the_existing_picker_and_skip_url() -> None:
    policy = RequestPolicy(
        credential_input_kind="website_stored_credential",
        login_page_urls=["https://portal.example.com/login"],
        login_intent=True,
    )

    load_mock, _ = await _resolve_direct(
        policy,
        user_message="Use the credential named shared-login.",
        org_credentials=[
            _cred("shared-login", "cred_one"),
            _cred("shared-login", "cred_two"),
            _cred("url-login", "cred_url", tested_url="https://portal.example.com/login"),
        ],
    )

    assert policy.requires_user_clarification is True
    assert (
        policy.clarification_question
        == "I found multiple stored credentials with that exact name. Which one should I use?"
    )
    # No prose credential dump: neither the named matches nor the unrelated URL cred are listed.
    assert "cred_" not in policy.clarification_question
    load_mock.assert_awaited_once()


@pytest.mark.asyncio
async def test_name_collision_candidates_are_not_labelled_safe_matches() -> None:
    """A name-formed ask has no login page vouching for its candidates.

    Each candidate carrying some ``tested_url`` says nothing about *this* page, so the picker
    must not call them safe matches even when every one of them has been tested somewhere.
    """
    policy = RequestPolicy(
        credential_input_kind="website_stored_credential",
        login_page_urls=["https://portal.example.com/login"],
        login_intent=True,
    )

    await _resolve_direct(
        policy,
        user_message="Use the credential named shared-login.",
        org_credentials=[
            _cred("shared-login", "cred_one", tested_url="https://elsewhere.example.com/login"),
            _cred("shared-login", "cred_two", tested_url="https://other.example.com/login"),
        ],
    )

    assert policy.clarification_question is not None
    # No prose enumeration at all — the card renders the full-org picker, so candidates are never
    # labelled (neither "Safe matches" nor "Saved credentials"); they ride on the policy field.
    assert "Saved credentials" not in policy.clarification_question
    assert "Safe matches" not in policy.clarification_question
    assert policy.credential_ask_candidate_ids == ["cred_one", "cred_two"]
    assert policy.credential_ask_login_page_urls == []


@pytest.mark.asyncio
async def test_url_tier_deduplicates_and_keeps_existing_picker() -> None:
    first = _cred("first", "cred_first", tested_url="https://portal.example.com/login")
    second = _cred("second", "cred_second", tested_url="https://portal.example.com/login")
    policy = RequestPolicy(
        credential_input_kind="website_stored_credential",
        login_page_urls=["https://portal.example.com/login"],
        login_intent=True,
    )

    load_mock, _ = await _resolve_direct(
        policy,
        user_message="Use the portal login.",
        org_credentials=[first, first, second],
    )

    assert policy.clarification_question == _AMBIGUOUS_URL_CREDENTIAL_QUESTION
    # The "Safe matches" prose dump is gone; the deduplicated candidate set now rides only on
    # discovered_credentials (the FE renders the full org selector instead of a text list).
    assert {c.credential_id for c in policy.discovered_credentials} == {"cred_first", "cred_second"}
    assert len(policy.discovered_credentials) == 2
    load_mock.assert_awaited_once()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("kind", "expected"),
    [
        ("none", (False, "proceed", True, True, "none", None)),
        ("raw_secret", (False, "proceed", True, True, "none", None)),
        ("credential_id", (False, "proceed", True, True, "none", None)),
        (
            "credential_name",
            (True, "ask_clarification", False, False, "credential_name_unresolved", _SAVED_CREDENTIAL_NAME_QUESTION),
        ),
        (
            "website_stored_credential",
            (True, "ask_clarification", False, False, "missing_target_context", _STORED_CREDENTIAL_URL_QUESTION),
        ),
        ("placeholder", (False, "proceed", True, True, "none", None)),
    ],
)
async def test_zero_hit_policy_is_byte_identical_for_each_label(
    kind: str,
    expected: tuple[bool, str, bool, bool, str, str | None],
) -> None:
    policy = RequestPolicy(credential_input_kind=kind)

    load_mock, by_ids = await _resolve_direct(
        policy,
        user_message="Continue with the workflow.",
        org_credentials=[],
    )

    assert (
        policy.requires_user_clarification,
        policy.user_response_policy,
        policy.allow_update_workflow,
        policy.allow_run_blocks,
        policy.clarification_reason,
        policy.clarification_question,
    ) == expected
    load_mock.assert_not_awaited()
    by_ids.assert_not_awaited()


@pytest.mark.asyncio
async def test_build_path_named_candidate_overrides_only_credential_target_clarification() -> None:
    policy = await _build_with_forced_classifier(
        user_message="Sign in with the saved credential named mock-portal-login.",
        classifier_policy=RequestPolicy(
            credential_input_kind="website_stored_credential",
            requires_user_clarification=True,
            clarification_reason="missing_target_context",
            classifier_status="success",
        ),
        org_credentials=[_cred("mock-portal-login", "cred_login")],
    )

    assert [candidate.credential_id for candidate in policy.resolved_credentials] == ["cred_login"]
    assert policy.credential_input_kind == "website_stored_credential"
    assert policy.requires_user_clarification is False
    assert policy.clarification_reason == "none"
    assert policy.clarification_question is None
    assert policy.to_trace_data()["clarification_reason"] == "none"
    assert "clarification_reason: none" in policy.prompt_summary()


@pytest.mark.asyncio
async def test_classifier_stale_name_ref_without_current_message_anchor_does_not_resolve() -> None:
    load_mock = AsyncMock(side_effect=AssertionError("stale classifier refs must not scan credentials"))
    policy = await _build_with_forced_classifier(
        user_message="Continue with the workflow.",
        classifier_policy=RequestPolicy(
            credential_input_kind="none",
            credential_refs=["stale-login"],
            classifier_status="success",
        ),
        org_credentials=[],
        get_credentials=load_mock,
    )

    assert policy.resolved_credentials == []
    assert policy.clarification_reason == "none"
    load_mock.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "user_message",
    [
        "Continue the production workflow.",
        "Do not use credential prod.",
        "Do not log in with 'prod'.",
        "Avoid using credential prod.",
        "Exclude credential named prod.",
        "Never use the legacy workflow because it still points to credential named prod.",
        "Do not use the credential, credential named prod.",
        "Do not use the saved credential, `prod`.",
    ],
)
async def test_classifier_name_ref_requires_exact_affirmative_credential_context(user_message: str) -> None:
    load_mock = AsyncMock(side_effect=AssertionError("untrusted classifier refs must not scan credentials"))
    policy = RequestPolicy(
        credential_input_kind="credential_name",
        credential_refs=["prod"],
    )

    await _resolve_direct(
        policy,
        user_message=user_message,
        org_credentials=[],
        get_credentials=load_mock,
    )

    assert policy.resolved_credentials == []
    load_mock.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "user_message",
    [
        "Don't use credential old; use credential new.",
        "Don't use credential old but use credential new.",
    ],
)
async def test_later_affirmative_name_resolves_after_excluded_name(user_message: str) -> None:
    policy = RequestPolicy(
        credential_input_kind="credential_name",
        credential_refs=["old", "new"],
    )

    await _resolve_direct(
        policy,
        user_message=user_message,
        org_credentials=[_cred("old", "cred_old"), _cred("new", "cred_new")],
    )

    assert [credential.credential_id for credential in policy.resolved_credentials] == ["cred_new"]


@pytest.mark.asyncio
async def test_login_with_credential_token_is_an_exact_name_candidate() -> None:
    credential = _cred("mock-portal-login-totp", "cred_login")
    policy = RequestPolicy(
        credential_input_kind="website_stored_credential",
        credential_refs=["mock-portal-login-totp"],
    )

    await _resolve_direct(
        policy,
        user_message="Login with credential mock-portal-login-totp and continue.",
        org_credentials=[credential],
    )

    assert [candidate.credential_id for candidate in policy.resolved_credentials] == ["cred_login"]


@pytest.mark.asyncio
async def test_explicit_name_miss_does_not_fall_through_to_classifier_only_url() -> None:
    url_credential = _cred("url-login", "cred_url", tested_url="https://portal.example.com/login")
    policy = RequestPolicy(
        credential_input_kind="credential_name",
        credential_refs=["missing-name"],
        login_page_urls=["https://portal.example.com/login"],
        login_intent=False,
    )

    await _resolve_direct(
        policy,
        user_message="Use the saved credential named missing-name.",
        org_credentials=[url_credential],
    )

    assert policy.resolved_credentials == []
    assert policy.clarification_reason == "credential_name_unresolved"
    assert "missing-name" in (policy.clarification_question or "")


@pytest.mark.asyncio
async def test_explicit_name_miss_does_not_fall_through_to_message_url() -> None:
    url_credential = _cred("url-login", "cred_url", tested_url="https://portal.example.com/login")
    policy = RequestPolicy(
        credential_input_kind="credential_name",
        credential_refs=["missing-name"],
        login_intent=True,
    )

    await _resolve_direct(
        policy,
        user_message=("Sign in to https://portal.example.com/login with the saved credential named missing-name."),
        org_credentials=[url_credential],
    )

    assert policy.resolved_credentials == []
    assert policy.clarification_reason == "credential_name_unresolved"
    assert "missing-name" in (policy.clarification_question or "")


@pytest.mark.asyncio
async def test_credential_grammar_word_does_not_preempt_message_url_resolution() -> None:
    credential = _cred("portal-login", "cred_url", tested_url="https://portal.example.com/login")
    policy = RequestPolicy(
        credential_input_kind="website_stored_credential",
        login_intent=False,
    )

    await _resolve_direct(
        policy,
        user_message="Use the credential to log in at https://portal.example.com/login.",
        org_credentials=[credential],
    )

    assert [candidate.credential_id for candidate in policy.resolved_credentials] == ["cred_url"]
    assert policy.clarification_question is None


@pytest.mark.asyncio
async def test_replacement_name_discards_superseded_candidate() -> None:
    old = _cred("old-login", "cred_old")
    new = _cred("new-login", "cred_new")
    policy = RequestPolicy(credential_input_kind="credential_name")

    await _resolve_direct(
        policy,
        user_message="Use credential old-login; switch to credential new-login instead.",
        org_credentials=[old, new],
    )

    assert [credential.credential_id for credential in policy.resolved_credentials] == ["cred_new"]


@pytest.mark.asyncio
async def test_postfix_replacement_name_discards_superseded_candidate() -> None:
    old = _cred("old-login", "cred_old")
    new = _cred("new-login", "cred_new")
    policy = RequestPolicy(credential_input_kind="credential_name")

    await _resolve_direct(
        policy,
        user_message="Use credential old-login; switch to the new-login credential instead.",
        org_credentials=[old, new],
    )

    assert [credential.credential_id for credential in policy.resolved_credentials] == ["cred_new"]


@pytest.mark.asyncio
async def test_chained_replacement_name_uses_final_affirmative_candidate() -> None:
    old = _cred("old-login", "cred_old")
    new = _cred("new-login", "cred_new")
    final = _cred("final-login", "cred_final")
    policy = RequestPolicy(credential_input_kind="credential_name")

    await _resolve_direct(
        policy,
        user_message=(
            "Replace credential old-login with credential new-login, "
            "but do not use credential new-login; use credential final-login."
        ),
        org_credentials=[old, new, final],
    )

    assert [credential.credential_id for credential in policy.resolved_credentials] == ["cred_final"]


@pytest.mark.asyncio
async def test_unrelated_switch_preserves_credential_name_authority() -> None:
    credential = _cred("prod-login", "cred_prod")
    policy = RequestPolicy(credential_input_kind="credential_name")

    await _resolve_direct(
        policy,
        user_message="Use credential prod-login, then switch to the reports tab.",
        org_credentials=[credential],
    )

    assert [candidate.credential_id for candidate in policy.resolved_credentials] == ["cred_prod"]


@pytest.mark.asyncio
async def test_website_kind_stale_name_ref_keeps_original_zero_hit_question_and_reason() -> None:
    policy = await _build_with_forced_classifier(
        user_message="Continue with the workflow.",
        classifier_policy=RequestPolicy(
            credential_input_kind="website_stored_credential",
            credential_refs=["stale-login"],
            classifier_status="success",
        ),
        org_credentials=[_cred("stale-login", "cred_stale")],
    )

    assert policy.resolved_credentials == []
    assert policy.clarification_reason == "missing_target_context"
    assert policy.clarification_question == _STORED_CREDENTIAL_URL_QUESTION


@pytest.mark.asyncio
async def test_website_kind_resolves_sole_url_less_credential() -> None:
    policy = await _build_with_forced_classifier(
        user_message="Log into the portal at https://portal.example.com/login.",
        classifier_policy=RequestPolicy(
            credential_input_kind="website_stored_credential",
            login_page_urls=["https://portal.example.com/login"],
            login_intent=True,
            classifier_status="success",
        ),
        org_credentials=[_cred("saved-login", "cred_urlless")],
    )

    assert [c.credential_id for c in policy.resolved_credentials] == ["cred_urlless"]
    assert policy.clarification_question is None
    assert policy.clarification_reason != "credential_name_unresolved"


@pytest.mark.asyncio
async def test_website_kind_url_match_wins_over_url_less_credential() -> None:
    policy = await _build_with_forced_classifier(
        user_message="Log into the portal at https://portal.example.com/login.",
        classifier_policy=RequestPolicy(
            credential_input_kind="website_stored_credential",
            login_page_urls=["https://portal.example.com/login"],
            login_intent=True,
            classifier_status="success",
        ),
        org_credentials=[
            _cred("portal-cred", "cred_url", tested_url="https://portal.example.com/login"),
            _cred("saved-login", "cred_urlless"),
        ],
    )

    assert [c.credential_id for c in policy.resolved_credentials] == ["cred_url"]
    assert policy.clarification_question is None


@pytest.mark.asyncio
async def test_website_kind_multiple_url_less_credentials_ask_which_one() -> None:
    policy = await _build_with_forced_classifier(
        user_message="Log into the portal at https://portal.example.com/login.",
        classifier_policy=RequestPolicy(
            credential_input_kind="website_stored_credential",
            login_page_urls=["https://portal.example.com/login"],
            login_intent=True,
            classifier_status="success",
        ),
        org_credentials=[_cred("first-login", "cred_one"), _cred("second-login", "cred_two")],
    )

    assert policy.requires_user_clarification is True
    # One clean sentence, no prose credential dump; candidates ride on discovered_credentials / candidate_ids.
    assert policy.clarification_question == _AMBIGUOUS_URLLESS_CREDENTIAL_QUESTION
    assert "first-login" not in policy.clarification_question
    assert "second-login" not in policy.clarification_question
    assert policy.clarification_reason == "credential_name_unresolved"
    assert sorted(c.credential_id for c in policy.discovered_credentials) == ["cred_one", "cred_two"]
    assert not policy.resolved_credentials
    assert policy.credential_ask_card_answerable is True
    assert policy.credential_ask_candidate_ids == ["cred_one", "cred_two"]
    assert policy.credential_ask_login_page_urls == ["https://portal.example.com/login"]
    assert credential_pause_reason(SimpleNamespace(request_policy=policy)) == "login_credentials_unresolved"


@pytest.mark.asyncio
async def test_website_kind_multiple_url_matches_keep_disambiguation_reason() -> None:
    policy = await _build_with_forced_classifier(
        user_message="Log into the portal at https://portal.example.com/login.",
        classifier_policy=RequestPolicy(
            credential_input_kind="website_stored_credential",
            login_page_urls=["https://portal.example.com/login"],
            login_intent=True,
            classifier_status="success",
        ),
        org_credentials=[
            _cred("first-login", "cred_one", tested_url="https://portal.example.com/login"),
            _cred("second-login", "cred_two", tested_url="https://portal.example.com/login"),
        ],
    )

    # One clean sentence, no prose credential dump; candidates ride on discovered_credentials.
    assert policy.clarification_question == _AMBIGUOUS_URL_CREDENTIAL_QUESTION
    assert policy.clarification_reason == "credential_name_unresolved"
    assert policy.credential_ask_card_answerable is True
    assert sorted(c.credential_id for c in policy.discovered_credentials) == ["cred_one", "cred_two"]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "org_credentials",
    [
        pytest.param([], id="no_credentials"),
        pytest.param(
            [_cred("saved-card", "cred_card", credential_type=CredentialType.CREDIT_CARD)],
            id="url_less_card_only",
        ),
        pytest.param(
            [
                _cred(
                    "saved-card",
                    "cred_card",
                    tested_url="https://portal.example.com/login",
                    credential_type=CredentialType.CREDIT_CARD,
                )
            ],
            id="url_matched_card_only",
        ),
    ],
)
async def test_website_kind_without_login_credentials_still_blocks(
    org_credentials: list[SimpleNamespace],
) -> None:
    policy = await _build_with_forced_classifier(
        user_message="Log into the portal at https://portal.example.com/login.",
        classifier_policy=RequestPolicy(
            credential_input_kind="website_stored_credential",
            login_page_urls=["https://portal.example.com/login"],
            login_intent=True,
            classifier_status="success",
        ),
        org_credentials=org_credentials,
    )

    assert policy.requires_user_clarification is True
    assert policy.clarification_question is not None
    assert "could not find a stored credential" in policy.clarification_question
    assert not policy.resolved_credentials


@pytest.mark.asyncio
async def test_website_kind_explicit_name_wins_over_url_matched_card() -> None:
    policy = await _build_with_forced_classifier(
        user_message=(
            "Log into the portal at https://portal.example.com/login with the credential named analytics-login."
        ),
        classifier_policy=RequestPolicy(
            credential_input_kind="website_stored_credential",
            login_page_urls=["https://portal.example.com/login"],
            login_intent=True,
            classifier_status="success",
        ),
        org_credentials=[
            _cred(
                "saved-card",
                "cred_card",
                tested_url="https://portal.example.com/login",
                credential_type=CredentialType.CREDIT_CARD,
            ),
            _cred("analytics-login", "cred_named", tested_url="https://other.example.com/login"),
        ],
    )

    assert policy.credential_input_kind == "website_stored_credential"
    assert policy.credential_refs == []
    assert [c.credential_id for c in policy.resolved_credentials] == ["cred_named"]
    assert policy.requires_user_clarification is False
    assert policy.clarification_question is None


@pytest.mark.asyncio
async def test_success_credential_id_intent_is_not_overridden_by_name_scan() -> None:
    by_ids = AsyncMock(return_value=[_cred("real", "cred_real")])
    load_mock = AsyncMock(return_value=[_cred("mock-portal-login", "cred_login")])
    policy = await _build_with_forced_classifier(
        user_message="Use cred_real, the saved credential named mock-portal-login.",
        classifier_policy=RequestPolicy(
            credential_input_kind="credential_id",
            credential_refs=["cred_real"],
            classifier_status="success",
        ),
        org_credentials=[],
        get_credentials=load_mock,
        get_credentials_by_ids=by_ids,
    )

    assert policy.credential_input_kind == "credential_id"
    assert [c.credential_id for c in policy.resolved_credentials] == ["cred_real"]
    load_mock.assert_not_awaited()


@pytest.mark.asyncio
async def test_success_populated_name_refs_resolve_the_single_matching_candidate() -> None:
    policy = await _build_with_forced_classifier(
        user_message="Use the saved credential named alpha.",
        classifier_policy=RequestPolicy(
            credential_input_kind="credential_name",
            credential_refs=["alpha", "beta"],
            classifier_status="success",
        ),
        org_credentials=[_cred("alpha", "cred_alpha")],
    )

    assert policy.credential_refs == ["alpha", "beta"]
    assert [candidate.credential_id for candidate in policy.resolved_credentials] == ["cred_alpha"]
    assert policy.clarification_reason == "none"


@pytest.mark.asyncio
async def test_success_pre_resolution_clarification_is_preserved() -> None:
    policy = await _build_with_forced_classifier(
        user_message="Sign in with the saved credential named mock-portal-login.",
        classifier_policy=RequestPolicy(
            credential_input_kind="none",
            requires_user_clarification=True,
            clarification_reason="invalid_conditional_container",
            classifier_status="success",
        ),
        org_credentials=[_cred("mock-portal-login", "cred_login")],
    )

    assert policy.clarification_reason == "invalid_conditional_container"
    assert policy.resolved_credentials == []


@pytest.mark.asyncio
async def test_success_ambiguous_named_credentials_clarify_for_normal_request() -> None:
    policy = await _build_with_forced_classifier(
        user_message="Use the credential named alpha or the credential named beta to sign in.",
        classifier_policy=RequestPolicy(credential_input_kind="none", classifier_status="success"),
        org_credentials=[_cred("alpha", "cred_alpha"), _cred("beta", "cred_beta")],
    )

    assert policy.requires_user_clarification is True
    assert policy.clarification_reason == "credential_name_unresolved"
    assert policy.resolved_credentials == []


@pytest.mark.asyncio
async def test_success_ambiguous_named_credentials_draft_unbound_for_explicit_draft() -> None:
    policy = await _build_with_forced_classifier(
        user_message=(
            "Build this as a code block using the saved credential named alpha "
            "or the saved credential named beta. Do not create a login block."
        ),
        classifier_policy=RequestPolicy(credential_input_kind="none", classifier_status="success"),
        org_credentials=[_cred("alpha", "cred_alpha"), _cred("beta", "cred_beta")],
    )

    assert policy.resolved_credentials == []
    assert policy.allow_missing_credentials_in_draft is True
    assert policy.requires_user_clarification is False


@pytest.mark.asyncio
async def test_success_incidental_quote_out_of_credential_context_is_not_rewritten() -> None:
    load_mock = AsyncMock(return_value=[_cred("mock-portal-login", "cred_login")])
    policy = await _build_with_forced_classifier(
        user_message='Set the page title to "mock-portal-login" before saving.',
        classifier_policy=RequestPolicy(credential_input_kind="none", classifier_status="success"),
        org_credentials=[],
        get_credentials=load_mock,
    )

    assert policy.credential_input_kind == "none"
    assert policy.resolved_credentials == []
    load_mock.assert_not_awaited()


@pytest.mark.asyncio
async def test_success_quote_after_word_containing_login_substring_is_not_context() -> None:
    load_mock = AsyncMock(return_value=[_cred("mock-portal-login", "cred_login")])
    policy = await _build_with_forced_classifier(
        user_message='Publish the blog in the section titled "mock-portal-login".',
        classifier_policy=RequestPolicy(credential_input_kind="none", classifier_status="success"),
        org_credentials=[],
        get_credentials=load_mock,
    )

    assert policy.credential_input_kind == "none"
    assert policy.resolved_credentials == []
    load_mock.assert_not_awaited()


@pytest.mark.asyncio
async def test_success_quoted_name_in_credential_context_resolves() -> None:
    policy = await _build_with_forced_classifier(
        user_message='Sign in using the saved credential "mock-portal-login".',
        classifier_policy=RequestPolicy(credential_input_kind="credential_name", classifier_status="success"),
        org_credentials=[_cred("mock-portal-login", "cred_login")],
    )

    assert policy.credential_input_kind == "credential_name"
    assert [c.credential_id for c in policy.resolved_credentials] == ["cred_login"]


@pytest.mark.asyncio
async def test_success_zero_name_match_falls_through_to_clarification() -> None:
    policy = await _build_with_forced_classifier(
        user_message="Sign in with the saved credential named ghost-cred.",
        classifier_policy=RequestPolicy(credential_input_kind="credential_name", classifier_status="success"),
        org_credentials=[_cred("mock-portal-login", "cred_login")],
    )

    assert policy.resolved_credentials == []
    assert policy.clarification_reason == "credential_name_unresolved"


@pytest.mark.asyncio
async def test_success_raw_secret_blocks_name_scan_before_loading_credentials() -> None:
    load_mock = AsyncMock(return_value=[_cred("mock-portal-login", "cred_login")])
    policy = await _build_with_forced_classifier(
        user_message="Sign in with the saved credential named mock-portal-login.",
        classifier_policy=RequestPolicy(
            credential_input_kind="none",
            raw_secret_detected=True,
            raw_secret_handling="block",
            classifier_status="success",
        ),
        org_credentials=[],
        get_credentials=load_mock,
    )

    assert policy.resolved_credentials == []
    load_mock.assert_not_awaited()


@pytest.mark.asyncio
async def test_login_intent_with_no_reachable_credential_asks_before_any_build() -> None:
    policy = await _build_with_forced_classifier(
        user_message="Log in to https://portal.example.com/login and download this month's invoices.",
        classifier_policy=RequestPolicy(
            login_intent=True,
            credential_input_kind="website_stored_credential",
            login_page_urls=["https://portal.example.com/login"],
            classifier_status="success",
        ),
        org_credentials=[],
    )

    assert policy.clarification_reason == "login_credentials_unresolved"
    assert policy.user_response_policy == "ask_clarification"
    assert policy.requires_user_clarification is True
    assert policy.allow_run_blocks is False
    assert policy.allow_update_workflow is False
    assert policy.resolved_credentials == []


@pytest.mark.asyncio
async def test_first_touch_bare_login_task_without_a_concrete_target_draws_no_credential_ask() -> None:
    policy = await _build_with_forced_classifier(
        user_message="Let's log in to the webpage and export the latest statement.",
        classifier_policy=RequestPolicy(login_intent=True, classifier_status="success"),
        org_credentials=[],
    )

    assert policy.clarification_reason != "login_credentials_unresolved"
    assert policy.user_response_policy == "proceed"
    assert policy.clarification_question is None


@pytest.mark.asyncio
async def test_login_intent_without_credential_phrasing_asks_once_a_url_names_the_target() -> None:
    policy = await _build_with_forced_classifier(
        user_message="Sign in to https://billing.example.com and export the statement.",
        classifier_policy=RequestPolicy(login_intent=True, classifier_status="success"),
        org_credentials=[],
    )

    assert policy.clarification_reason == "login_credentials_unresolved"
    assert policy.user_response_policy == "ask_clarification"
    assert policy.clarification_question is not None


@pytest.mark.asyncio
async def test_login_intent_none_kind_one_message_url_with_single_match_resolves() -> None:
    credential = _cred("portal-cred", "cred_url", tested_url="http://localhost:8941/analytics_console/")
    policy = await _build_with_forced_classifier(
        user_message="Sign in to http://localhost:8941/analytics_console/ and continue.",
        classifier_policy=RequestPolicy(
            login_intent=True,
            credential_input_kind="none",
            login_page_urls=[],
            classifier_status="success",
        ),
        org_credentials=[credential],
    )

    assert policy.credential_input_kind == "none"
    assert [c.credential_id for c in policy.resolved_credentials] == ["cred_url"]
    assert policy.requires_user_clarification is False
    assert policy.clarification_question is None


@pytest.mark.asyncio
async def test_login_intent_none_kind_multiple_host_matches_asks_pick_question() -> None:
    first = _cred("portal-a", "cred_a", tested_url="http://localhost:8941/analytics_console/")
    second = _cred("portal-b", "cred_b", tested_url="http://localhost:8941/analytics_console/")
    policy = await _build_with_forced_classifier(
        user_message="Sign in to http://localhost:8941/analytics_console/ and continue.",
        classifier_policy=RequestPolicy(
            login_intent=True,
            credential_input_kind="none",
            login_page_urls=[],
            classifier_status="success",
        ),
        org_credentials=[first, second],
    )

    assert policy.credential_input_kind == "none"
    assert policy.requires_user_clarification is True
    # One clean sentence, no prose credential dump; the candidate set rides on discovered_credentials.
    assert policy.clarification_question == _AMBIGUOUS_URL_CREDENTIAL_QUESTION
    assert {c.credential_id for c in policy.discovered_credentials} == {"cred_a", "cred_b"}
    assert policy.clarification_reason == "credential_name_unresolved"


@pytest.mark.asyncio
async def test_login_intent_none_kind_prose_host_mention_does_not_bind() -> None:
    credential = _cred("portal-cred", "cred_url", tested_url="https://example.com/login")
    policy = await _build_with_forced_classifier(
        user_message="Log into my account — the steps are documented at https://example.com/help if you need them.",
        classifier_policy=RequestPolicy(
            login_intent=True,
            credential_input_kind="none",
            login_page_urls=[],
            classifier_status="success",
        ),
        org_credentials=[credential],
    )

    assert policy.resolved_credentials == []
    assert policy.clarification_reason == "login_credentials_unresolved"
    assert policy.clarification_question == _LOGIN_CREDENTIAL_QUESTION


@pytest.mark.asyncio
async def test_login_intent_none_kind_query_distinct_tenants_do_not_collapse() -> None:
    tenant_a = _cred("tenant-a", "cred_a", tested_url="https://portal.example.com/login?tenant=a")
    tenant_b = _cred("tenant-b", "cred_b", tested_url="https://portal.example.com/login?tenant=b")
    policy = await _build_with_forced_classifier(
        user_message="Sign in to https://portal.example.com/login?tenant=a and continue.",
        classifier_policy=RequestPolicy(
            login_intent=True,
            credential_input_kind="none",
            login_page_urls=[],
            classifier_status="success",
        ),
        org_credentials=[tenant_a, tenant_b],
    )

    assert [c.credential_id for c in policy.resolved_credentials] == ["cred_a"]
    assert policy.requires_user_clarification is False


@pytest.mark.asyncio
async def test_login_intent_none_kind_query_param_order_is_normalized() -> None:
    credential = _cred("portal-cred", "cred_url", tested_url="https://portal.example.com/login?b=2&a=1")
    policy = await _build_with_forced_classifier(
        user_message="Sign in to https://portal.example.com/login?a=1&b=2 and continue.",
        classifier_policy=RequestPolicy(
            login_intent=True,
            credential_input_kind="none",
            login_page_urls=[],
            classifier_status="success",
        ),
        org_credentials=[credential],
    )

    assert [c.credential_id for c in policy.resolved_credentials] == ["cred_url"]


@pytest.mark.asyncio
async def test_login_intent_none_kind_non_password_sole_match_does_not_bind() -> None:
    card = _cred(
        "corp-card",
        "cred_card",
        tested_url="https://portal.example.com/login",
        credential_type=CredentialType.CREDIT_CARD,
    )
    policy = await _build_with_forced_classifier(
        user_message="Sign in to https://portal.example.com/login and continue.",
        classifier_policy=RequestPolicy(
            login_intent=True,
            credential_input_kind="none",
            login_page_urls=[],
            classifier_status="success",
        ),
        org_credentials=[card],
    )

    assert policy.resolved_credentials == []
    assert policy.clarification_reason == "login_credentials_unresolved"
    assert policy.clarification_question == _LOGIN_CREDENTIAL_QUESTION


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("kind", "expected_question"),
    [("credential_name", _SAVED_CREDENTIAL_NAME_QUESTION), ("placeholder", _LOGIN_CREDENTIAL_QUESTION)],
)
async def test_login_classifier_url_non_password_sole_match_does_not_bind(
    kind: str,
    expected_question: str,
) -> None:
    card = _cred(
        "corp-card",
        "cred_card",
        tested_url="https://portal.example.com/login",
        credential_type=CredentialType.CREDIT_CARD,
    )
    policy = await _build_with_forced_classifier(
        user_message="Sign in to the portal and continue.",
        classifier_policy=RequestPolicy(
            login_intent=True,
            credential_input_kind=kind,
            login_page_urls=["https://portal.example.com/login"],
            classifier_status="success",
        ),
        org_credentials=[card],
    )

    assert policy.resolved_credentials == []
    assert policy.clarification_reason == "login_credentials_unresolved"
    assert policy.clarification_question == expected_question


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("kind", "expected_question"),
    [("credential_name", _SAVED_CREDENTIAL_NAME_QUESTION), ("placeholder", _LOGIN_CREDENTIAL_QUESTION)],
)
async def test_login_message_url_non_password_sole_match_does_not_bind(
    kind: str,
    expected_question: str,
) -> None:
    card = _cred(
        "corp-card",
        "cred_card",
        tested_url="https://portal.example.com/login",
        credential_type=CredentialType.CREDIT_CARD,
    )
    policy = await _build_with_forced_classifier(
        user_message="Sign in to https://portal.example.com/login and continue.",
        classifier_policy=RequestPolicy(
            login_intent=True,
            credential_input_kind=kind,
            login_page_urls=[],
            classifier_status="success",
        ),
        org_credentials=[card],
    )

    assert policy.resolved_credentials == []
    assert policy.clarification_reason == "login_credentials_unresolved"
    assert policy.clarification_question == expected_question


@pytest.mark.asyncio
async def test_login_intent_none_kind_classifier_url_keeps_host_fallback() -> None:
    credential = _cred("portal-cred", "cred_url", tested_url="https://example.com/login")
    policy = await _build_with_forced_classifier(
        user_message="Log into the portal and pull this week's report.",
        classifier_policy=RequestPolicy(
            login_intent=True,
            credential_input_kind="none",
            login_page_urls=["https://example.com/portal"],
            classifier_status="success",
        ),
        org_credentials=[credential],
    )

    assert [c.credential_id for c in policy.resolved_credentials] == ["cred_url"]
    assert policy.requires_user_clarification is False


@pytest.mark.asyncio
async def test_login_intent_none_kind_zero_url_matches_keeps_existing_login_unresolved_text() -> None:
    policy = await _build_with_forced_classifier(
        user_message="Sign in to http://localhost:8941/analytics_console/ and continue.",
        classifier_policy=RequestPolicy(
            login_intent=True,
            credential_input_kind="none",
            login_page_urls=[],
            classifier_status="success",
        ),
        org_credentials=[_cred("other", "cred_other", tested_url="http://localhost:8999/other")],
    )

    assert policy.resolved_credentials == []
    assert policy.clarification_reason == "login_credentials_unresolved"
    assert policy.clarification_question == _LOGIN_CREDENTIAL_QUESTION


@pytest.mark.asyncio
async def test_login_intent_none_kind_multi_host_message_skips_resolution() -> None:
    policy = await _build_with_forced_classifier(
        user_message="Sign in to http://localhost:8941/analytics_console/ then check https://example.org/login.",
        classifier_policy=RequestPolicy(
            login_intent=True,
            credential_input_kind="none",
            login_page_urls=[],
            classifier_status="success",
        ),
        org_credentials=[_cred("portal", "cred_url", tested_url="http://localhost:8941/analytics_console/")],
    )

    assert policy.resolved_credentials == []
    assert policy.clarification_reason == "login_credentials_unresolved"
    assert policy.clarification_question == _LOGIN_CREDENTIAL_QUESTION


@pytest.mark.asyncio
async def test_login_intent_none_kind_message_url_trailing_punctuation_still_matches() -> None:
    credential = _cred("portal-cred", "cred_url", tested_url="http://localhost:8941/analytics_console/")
    policy = await _build_with_forced_classifier(
        user_message="Sign in to http://localhost:8941/analytics_console/.",
        classifier_policy=RequestPolicy(
            login_intent=True,
            credential_input_kind="none",
            login_page_urls=[],
            classifier_status="success",
        ),
        org_credentials=[credential],
    )

    assert [c.credential_id for c in policy.resolved_credentials] == ["cred_url"]
    assert policy.credential_input_kind == "none"


@pytest.mark.asyncio
async def test_login_intent_none_kind_bare_host_message_does_not_trigger_url_resolution() -> None:
    policy = await _build_with_forced_classifier(
        user_message="Log into portal.example.com and continue.",
        classifier_policy=RequestPolicy(
            login_intent=True,
            credential_input_kind="none",
            login_page_urls=[],
            classifier_status="success",
        ),
        org_credentials=[_cred("portal", "cred_url", tested_url="https://portal.example.com/login")],
    )

    assert policy.resolved_credentials == []
    assert policy.clarification_reason != "login_credentials_unresolved"
    assert policy.user_response_policy == "proceed"


@pytest.mark.asyncio
async def test_login_intent_false_none_kind_never_loads_credentials_for_message_url() -> None:
    with patch(
        "skyvern.forge.sdk.copilot.request_policy._load_credentials",
        new=AsyncMock(side_effect=AssertionError("_load_credentials should not be called")),
    ):
        policy = await _build_with_forced_classifier(
            user_message="Go to http://localhost:8941/analytics_console/ and inspect the page.",
            classifier_policy=RequestPolicy(
                login_intent=False,
                credential_input_kind="none",
                login_page_urls=[],
                classifier_status="success",
            ),
            org_credentials=[],
        )

    assert policy.user_response_policy == "proceed"
    assert policy.resolved_credentials == []


@pytest.mark.asyncio
async def test_login_path_does_not_grant_credential_authority_without_login_intent() -> None:
    policy = RequestPolicy(
        login_intent=False,
        credential_input_kind="none",
    )

    await _resolve_direct(
        policy,
        user_message="Inspect https://portal.example.com/login and report its title.",
        org_credentials=[_cred("portal", "cred_url", tested_url="https://portal.example.com/login")],
    )

    assert policy.resolved_credentials == []
    assert policy.clarification_question is None


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("testing_intent", "allow_missing_credentials_in_draft"),
    [("skip_test", False), ("unspecified", True)],
)
async def test_login_intent_none_kind_skip_modes_do_not_lookup_credentials(
    testing_intent: str,
    allow_missing_credentials_in_draft: bool,
) -> None:
    with patch(
        "skyvern.forge.sdk.copilot.request_policy._load_credentials",
        new=AsyncMock(side_effect=AssertionError("_load_credentials should not be called")),
    ):
        policy = await _build_with_forced_classifier(
            user_message="Sign in to http://localhost:8941/analytics_console/ and continue.",
            classifier_policy=RequestPolicy(
                login_intent=True,
                credential_input_kind="none",
                login_page_urls=[],
                testing_intent=testing_intent,
                allow_missing_credentials_in_draft=allow_missing_credentials_in_draft,
                classifier_status="success",
            ),
            org_credentials=[],
        )

    assert policy.resolved_credentials == []


@pytest.mark.asyncio
async def test_stale_classifier_ref_does_not_suppress_unresolved_login_ask() -> None:
    policy = await _build_with_forced_classifier(
        user_message="Sign in to https://billing.example.com/login and export the statement.",
        classifier_policy=RequestPolicy(
            login_intent=True,
            credential_input_kind="none",
            credential_refs=["stale-login"],
            login_page_urls=["https://billing.example.com/login"],
            classifier_status="success",
        ),
        org_credentials=[],
    )

    assert policy.resolved_credentials == []
    assert policy.user_response_policy == "ask_clarification"
    assert policy.allow_run_blocks is False
    assert policy.clarification_reason == "login_credentials_unresolved"


@pytest.mark.asyncio
async def test_login_intent_none_kind_prefers_classifier_login_page_urls_over_message_urls() -> None:
    classifier_url_credential = _cred("classifier-url", "cred_classifier", tested_url="https://alpha.example.com/login")
    message_url_credential = _cred("message-url", "cred_message", tested_url="https://beta.example.com/login")
    policy = await _build_with_forced_classifier(
        user_message="Sign in to https://beta.example.com/login and continue.",
        classifier_policy=RequestPolicy(
            login_intent=True,
            credential_input_kind="none",
            login_page_urls=["https://alpha.example.com/login"],
            classifier_status="success",
        ),
        org_credentials=[classifier_url_credential, message_url_credential],
    )

    assert [c.credential_id for c in policy.resolved_credentials] == ["cred_classifier"]


@pytest.mark.asyncio
async def test_login_intent_on_an_existing_workflow_asks_without_a_url_in_the_message() -> None:
    policy = await _build_with_forced_classifier(
        user_message="Sign in to the billing portal and export the statement.",
        classifier_policy=RequestPolicy(login_intent=True, classifier_status="success"),
        org_credentials=[],
        workflow_yaml=_yaml(
            """
            title: Billing export
            workflow_definition:
              blocks:
                - block_type: task
                  label: export
            """
        ),
    )

    assert policy.clarification_reason == "login_credentials_unresolved"
    assert policy.user_response_policy == "ask_clarification"


@pytest.mark.asyncio
async def test_classifier_emitted_login_credentials_unresolved_is_not_trusted() -> None:
    policy = _classification_from_raw(
        {
            "testing_intent": "unspecified",
            "credential_input_kind": "none",
            "credential_refs": [],
            "login_page_urls": [],
            "requires_user_clarification": True,
            "clarification_reason": "login_credentials_unresolved",
            "completion_contract": None,
        }
    )

    assert policy.clarification_reason == "none"


@pytest.mark.asyncio
async def test_login_intent_satisfied_by_matching_org_credential_draws_no_ask() -> None:
    credential = _cred("portal-cred", "cred_url", tested_url="https://portal.example.com/login")
    policy = await _build_with_forced_classifier(
        user_message="Log in to https://portal.example.com/login and download this month's invoices.",
        classifier_policy=RequestPolicy(
            login_intent=True,
            credential_input_kind="website_stored_credential",
            login_page_urls=["https://portal.example.com/login"],
            classifier_status="success",
        ),
        org_credentials=[credential],
    )

    assert [c.credential_id for c in policy.resolved_credentials] == ["cred_url"]
    assert policy.clarification_reason != "login_credentials_unresolved"
    assert policy.user_response_policy == "proceed"
    assert policy.clarification_question is None


@pytest.mark.asyncio
async def test_login_intent_satisfied_by_a_workflow_bound_credential_draws_no_ask() -> None:
    yaml = _yaml(
        """
        title: example
        workflow_definition:
          parameters:
          - key: portal_login
            parameter_type: credential
            credential_id: cred_bound
          blocks: []
        """
    )
    policy = await _build_with_forced_classifier(
        user_message="Sign in to the portal and export the statement.",
        classifier_policy=RequestPolicy(login_intent=True, classifier_status="success"),
        org_credentials=[],
        workflow_yaml=yaml,
    )

    assert policy.existing_workflow_credential_ids == ["cred_bound"]
    assert policy.clarification_reason != "login_credentials_unresolved"
    assert policy.user_response_policy == "proceed"


@pytest.mark.asyncio
async def test_a_non_login_request_never_draws_the_login_credential_ask() -> None:
    policy = await _build_with_forced_classifier(
        user_message="Go to https://portal.example.com/login and tell me what fields the form has.",
        classifier_policy=RequestPolicy(login_intent=False, classifier_status="success"),
        org_credentials=[],
    )

    assert policy.clarification_reason != "login_credentials_unresolved"
    assert policy.user_response_policy == "proceed"
    assert policy.clarification_question is None


@pytest.mark.asyncio
async def test_a_user_named_credential_that_misses_keeps_the_name_unresolved_ask() -> None:
    policy = await _build_with_forced_classifier(
        user_message="Log in using my saved credential named portal-login.",
        classifier_policy=RequestPolicy(
            login_intent=True,
            credential_input_kind="credential_name",
            credential_refs=["portal-login"],
            classifier_status="success",
        ),
        org_credentials=[_cred("something-else", "cred_other")],
    )

    assert policy.clarification_reason == "credential_name_unresolved"


@pytest.mark.asyncio
async def test_an_explicit_draft_only_login_request_is_not_interrupted_for_a_credential() -> None:
    policy = await _build_with_forced_classifier(
        user_message="Draft a workflow that logs in to the portal. Don't run it, I'll test it myself.",
        classifier_policy=RequestPolicy(
            login_intent=True,
            testing_intent="skip_test",
            classifier_status="success",
        ),
        org_credentials=[],
    )

    assert policy.clarification_reason != "login_credentials_unresolved"
    assert policy.allow_missing_credentials_in_draft is True


@pytest.mark.asyncio
async def test_a_pasted_secret_under_login_intent_keeps_the_raw_secret_refusal() -> None:
    policy = await _build_with_forced_classifier(
        user_message="Log in with password hunter2",
        classifier_policy=RequestPolicy(
            login_intent=True,
            credential_input_kind="raw_secret",
            raw_secret_detected=True,
            classifier_status="success",
        ),
        org_credentials=[],
    )

    assert policy.clarification_reason == "raw_secret"
    assert policy.credential_ask_card_answerable is False
    assert credential_pause_reason(SimpleNamespace(request_policy=policy)) is None


def test_login_credentials_unresolved_surfaces_a_prompt_but_grants_no_deferred_draft_authority() -> None:
    assert "login_credentials_unresolved" in CREDENTIAL_PROMPT_CLARIFICATION_REASONS
    assert "login_credentials_unresolved" not in CREDENTIAL_DEFERRED_DRAFT_REASONS


async def _resolve_named_credential_turn(credential: SimpleNamespace) -> RequestPolicy:
    return await _build_with_forced_classifier(
        user_message=f"Sign in with the saved credential named {credential.name}.",
        classifier_policy=RequestPolicy(
            credential_input_kind="credential_name",
            credential_refs=[credential.name],
            classifier_status="success",
        ),
        org_credentials=[credential],
    )


@pytest.mark.asyncio
async def test_prior_approved_credential_carried_into_confirmation_turn() -> None:
    credential = _cred("mock-portal-login", "cred_portal", tested_url="http://localhost:8951/x")
    turn_one = await _resolve_named_credential_turn(credential)
    assert [c.credential_id for c in turn_one.resolved_credentials] == ["cred_portal"]

    recorded = record_approved_credentials_in_global_llm_context(SimpleNamespace(request_policy=turn_one), None)

    turn_two = await _build_with_forced_classifier(
        user_message="yes",
        classifier_policy=RequestPolicy(credential_input_kind="none", classifier_status="success"),
        org_credentials=[],
        get_credentials_by_ids=AsyncMock(return_value=[credential]),
        global_llm_context=recorded,
    )

    assert turn_two.credential_input_kind == "none"
    assert "cred_portal" in {c.credential_id for c in turn_two.resolved_credentials}
    assert _credential_run_approval_error(["cred_portal"], turn_two) is None


@pytest.mark.asyncio
async def test_carried_credential_metadata_matches_turn_one_resolution() -> None:
    credential = _cred("mock-portal-login", "cred_portal", tested_url="http://localhost:8951/x")
    turn_one = await _resolve_named_credential_turn(credential)
    recorded = record_approved_credentials_in_global_llm_context(SimpleNamespace(request_policy=turn_one), None)

    turn_two = await _build_with_forced_classifier(
        user_message="yes",
        classifier_policy=RequestPolicy(credential_input_kind="none", classifier_status="success"),
        org_credentials=[],
        get_credentials_by_ids=AsyncMock(return_value=[credential]),
        global_llm_context=recorded,
    )

    carried = next(c for c in turn_two.resolved_credentials if c.credential_id == "cred_portal")
    assert carried.tested_url == "http://localhost:8951/x"


@pytest.mark.asyncio
async def test_soft_deleted_prior_approved_credential_drops_out_of_carry() -> None:
    credential = _cred("mock-portal-login", "cred_portal")
    turn_one = await _resolve_named_credential_turn(credential)
    recorded = record_approved_credentials_in_global_llm_context(SimpleNamespace(request_policy=turn_one), None)

    turn_two = await _build_with_forced_classifier(
        user_message="yes",
        classifier_policy=RequestPolicy(credential_input_kind="none", classifier_status="success"),
        org_credentials=[],
        get_credentials_by_ids=AsyncMock(return_value=[]),
        global_llm_context=recorded,
    )

    assert turn_two.resolved_credentials == []
    assert _credential_run_approval_error(["cred_portal"], turn_two) is not None


@pytest.mark.asyncio
async def test_ambiguous_named_credential_still_blocks_despite_carried_state() -> None:
    approved = _cred("mock-portal-login", "cred_portal")
    recorded = record_approved_credentials_in_global_llm_context(
        SimpleNamespace(request_policy=RequestPolicy(resolved_credentials=[approved])), None
    )
    duplicates = [_cred("payroll", "cred_payroll_a"), _cred("payroll", "cred_payroll_b")]

    turn_two = await _build_with_forced_classifier(
        user_message="use my payroll login",
        classifier_policy=RequestPolicy(
            credential_input_kind="credential_name",
            credential_refs=["payroll"],
            classifier_status="success",
        ),
        org_credentials=duplicates,
        get_credentials_by_ids=AsyncMock(return_value=[approved]),
        global_llm_context=recorded,
    )

    assert turn_two.requires_user_clarification is True
    assert turn_two.clarification_reason == "credential_name_unresolved"


@pytest.mark.asyncio
async def test_carried_credential_satisfies_the_login_reachability_ask() -> None:
    # The carry must be seeded before _login_credentials_unresolved runs; seeding after it
    # would re-ask for a credential approved on an earlier turn (SKY-12812 via SKY-12760's ask).
    credential = _cred("mock-portal-login", "cred_portal", tested_url="https://portal.example.com/login")
    turn_one = await _resolve_named_credential_turn(credential)
    recorded = record_approved_credentials_in_global_llm_context(SimpleNamespace(request_policy=turn_one), None)

    turn_two = await _build_with_forced_classifier(
        user_message="Log in to https://portal.example.com/login and download this month's invoices.",
        classifier_policy=RequestPolicy(
            login_intent=True,
            credential_input_kind="website_stored_credential",
            login_page_urls=["https://portal.example.com/login"],
            classifier_status="success",
        ),
        org_credentials=[],
        get_credentials_by_ids=AsyncMock(return_value=[credential]),
        global_llm_context=recorded,
    )

    assert turn_two.clarification_reason != "login_credentials_unresolved"
    assert "cred_portal" in {c.credential_id for c in turn_two.resolved_credentials}


def _email_signin_policy(**overrides: object) -> RequestPolicy:
    defaults: dict[str, object] = {
        "login_intent": True,
        "email_signin_intent": True,
        "credential_input_kind": "none",
        "login_page_urls": ["https://console.example.com/signin"],
        "classifier_status": "success",
    }
    defaults.update(overrides)
    return RequestPolicy(**defaults)  # type: ignore[arg-type]


@pytest.mark.asyncio
async def test_an_address_named_in_the_request_outranks_the_connected_gmail_account() -> None:
    """Naming an address is an explicit instruction; a connected inbox is only the default
    for when none was named, and must not quietly substitute a different identity."""
    policy = await _build_with_forced_classifier(
        user_message="Sign in to https://console.example.com/signin with typed@example.com and extract the balance.",
        classifier_policy=_email_signin_policy(signin_email_candidates=["typed@example.com"]),
        org_credentials=[],
        connected_gmail="connected@example.com",
    )

    assert policy.resolved_signin_email == "typed@example.com"
    assert policy.clarification_reason == "none"
    assert policy.user_response_policy == "proceed"


@pytest.mark.asyncio
async def test_the_connected_account_is_not_even_looked_up_when_the_request_named_one() -> None:
    lookup = AsyncMock(return_value="connected@example.com")
    policy = await _build_with_forced_classifier(
        user_message="Sign in to https://console.example.com/signin with typed@example.com and extract it.",
        classifier_policy=_email_signin_policy(signin_email_candidates=["typed@example.com"]),
        org_credentials=[],
        gmail_lookup=lookup,
    )

    assert policy.resolved_signin_email == "typed@example.com"
    assert lookup.await_count == 0, "named address should short-circuit the Gmail network call"


@pytest.mark.asyncio
async def test_the_connected_gmail_account_is_used_when_the_request_named_no_address() -> None:
    policy = await _build_with_forced_classifier(
        user_message="Sign in to https://console.example.com/signin by email and extract the balance.",
        classifier_policy=_email_signin_policy(signin_email_candidates=[]),
        org_credentials=[],
        connected_gmail="connected@example.com",
    )

    assert policy.resolved_signin_email == "connected@example.com"
    assert policy.clarification_reason == "none"


@pytest.mark.asyncio
async def test_address_from_the_request_is_used_when_no_gmail_is_connected() -> None:
    policy = await _build_with_forced_classifier(
        user_message="Sign in to https://console.example.com/signin with typed@example.com and extract the balance.",
        classifier_policy=_email_signin_policy(signin_email_candidates=["typed@example.com"]),
        org_credentials=[],
        connected_gmail=None,
    )

    assert policy.resolved_signin_email == "typed@example.com"
    assert policy.clarification_reason == "none"
    assert policy.clarification_question is None


@pytest.mark.asyncio
async def test_neither_source_asks_for_an_address_and_never_for_a_saved_credential() -> None:
    policy = await _build_with_forced_classifier(
        user_message="Sign in to https://console.example.com/signin and extract the balance.",
        classifier_policy=_email_signin_policy(signin_email_candidates=[]),
        org_credentials=[],
        connected_gmail=None,
    )

    assert policy.resolved_signin_email is None
    assert policy.clarification_reason == "signin_email_unresolved"
    assert policy.clarification_question == "Which email address should I sign in with?"
    assert "cred_" not in (policy.clarification_question or "")
    assert policy.clarification_reason not in CREDENTIAL_PROMPT_CLARIFICATION_REASONS


@pytest.mark.asyncio
async def test_email_signin_with_no_org_credentials_never_draws_the_login_credential_ask() -> None:
    policy = await _build_with_forced_classifier(
        user_message="Sign in to https://console.example.com/signin with typed@example.com and extract the balance.",
        classifier_policy=_email_signin_policy(signin_email_candidates=["typed@example.com"]),
        org_credentials=[],
        connected_gmail=None,
    )

    assert policy.clarification_reason != "login_credentials_unresolved"
    assert not policy.requires_user_clarification


@pytest.mark.asyncio
async def test_a_password_login_with_no_credential_still_blocks_exactly_as_before() -> None:
    policy = await _build_with_forced_classifier(
        user_message="Log in to https://portal.example.com/login and download this month's invoices.",
        classifier_policy=RequestPolicy(
            login_intent=True,
            email_signin_intent=False,
            credential_input_kind="website_stored_credential",
            login_page_urls=["https://portal.example.com/login"],
            classifier_status="success",
        ),
        org_credentials=[],
        connected_gmail="connected@example.com",
    )

    assert policy.clarification_reason == "login_credentials_unresolved"
    assert "cred_" in (policy.clarification_question or "")
    assert policy.resolved_signin_email is None


@pytest.mark.asyncio
async def test_email_signin_does_not_reach_the_interactive_credential_card() -> None:
    # Sibling path: the card and the terminal ask both hang off login_credentials_unresolved,
    # so a fix that only silenced the terminal text would still demand a credential here.
    policy = await _build_with_forced_classifier(
        user_message="Sign in to https://console.example.com/signin with typed@example.com and extract the balance.",
        classifier_policy=_email_signin_policy(signin_email_candidates=["typed@example.com"]),
        org_credentials=[],
        connected_gmail=None,
    )

    assert credential_pause_reason(SimpleNamespace(request_policy=policy)) is None


@pytest.mark.asyncio
async def test_skip_test_email_signin_defers_without_asking_for_an_address() -> None:
    policy = await _build_with_forced_classifier(
        user_message="Draft a workflow that signs in to https://console.example.com/signin. Don't run it.",
        classifier_policy=_email_signin_policy(testing_intent="skip_test", signin_email_candidates=[]),
        org_credentials=[],
        connected_gmail=None,
    )

    assert policy.clarification_reason != "signin_email_unresolved"
    assert policy.resolved_signin_email is None


@pytest.mark.asyncio
async def test_a_named_saved_credential_keeps_password_resolution_even_with_email_intent() -> None:
    """Referenced credential material outranks email intent, so a misread flag cannot
    silently drop the credential requirement from a real password login."""
    policy = await _build_with_forced_classifier(
        user_message="Sign in to https://portal.example.com/login using my saved credential portal-login.",
        classifier_policy=_email_signin_policy(
            email_signin_intent=True,
            credential_input_kind="credential_name",
            credential_refs=["portal-login"],
            login_page_urls=["https://portal.example.com/login"],
        ),
        org_credentials=[],
        connected_gmail="connected@example.com",
    )

    assert policy.resolved_signin_email is None
    assert policy.clarification_reason != "signin_email_unresolved"


@pytest.mark.asyncio
async def test_first_touch_email_signin_with_no_site_yet_asks_nothing_about_an_address() -> None:
    """No site, workflow, or URL yet: the address is not the question the user still owes."""
    policy = await _build_with_forced_classifier(
        user_message="I want to sign in with my email address.",
        classifier_policy=_email_signin_policy(login_page_urls=[], signin_email_candidates=[]),
        org_credentials=[],
        connected_gmail=None,
    )

    assert policy.clarification_reason != "signin_email_unresolved"
    assert policy.resolved_signin_email is None


@pytest.mark.asyncio
async def test_the_email_signin_flag_is_the_only_thing_that_lifts_the_credential_ask() -> None:
    """The discriminator itself: identical policy but email_signin_intent=False must still
    block. Without this, the pass-path could be reached by any credential-less login."""
    policy = await _build_with_forced_classifier(
        user_message="Log in to https://console.example.com/signin as someone@example.com and extract the balance.",
        classifier_policy=_email_signin_policy(email_signin_intent=False),
        org_credentials=[],
        connected_gmail=None,
    )

    assert policy.clarification_reason == "login_credentials_unresolved"
    assert policy.resolved_signin_email is None


@pytest.mark.asyncio
async def test_email_signin_never_overwrites_a_clarification_the_user_already_owes() -> None:
    policy = await _build_with_forced_classifier(
        user_message="Put the invoice blocks in a loop and sign in with my email.",
        classifier_policy=_email_signin_policy(
            signin_email_candidates=[],
            clarification_reason="ambiguous_loop_edit",
            requires_user_clarification=True,
        ),
        org_credentials=[],
        connected_gmail=None,
    )

    assert policy.clarification_reason == "ambiguous_loop_edit"
    assert policy.clarification_reason != "signin_email_unresolved"


def test_classifier_supplied_addresses_are_shape_checked_before_use() -> None:
    assert _clean_email_list(["  ok@example.com  "]) == ["ok@example.com"]
    assert _clean_email_list(["ok@example.com", "ok@example.com"]) == ["ok@example.com"]
    # A workflow parameter default is built from this value, so template and markup
    # syntax must not survive the boundary.
    assert _clean_email_list(["{{evil}}@example.com"]) == []
    assert _clean_email_list(["<img src=x>@example.com"]) == []
    assert _clean_email_list([f"{'a' * 300}@example.com"]) == []
    assert _clean_email_list(["not an email", "", "@nope.com", None, 123]) == []


@pytest.mark.asyncio
async def test_a_matching_org_credential_does_not_pull_a_passwordless_turn_into_a_credential_ask() -> None:
    """A stored credential for the same host must not reroute the turn into the URL
    credential tier, which asks for a saved credential under a different reason."""
    policy = await _build_with_forced_classifier(
        user_message="Sign in to https://console.example.com/signin with typed@example.com and extract it.",
        classifier_policy=_email_signin_policy(signin_email_candidates=["typed@example.com"]),
        org_credentials=[
            _cred("console-one", "cred_one", tested_url="https://console.example.com/signin"),
            _cred("console-two", "cred_two", tested_url="https://console.example.com/signin"),
        ],
        connected_gmail=None,
    )

    assert policy.clarification_reason == "none"
    assert policy.clarification_question is None
    assert policy.resolved_signin_email == "typed@example.com"
    assert not policy.resolved_credentials


@pytest.mark.asyncio
async def test_a_follow_up_turn_keeps_the_sign_in_identity_the_earlier_turn_resolved() -> None:
    """The classifier reads a terse follow-up as an ordinary login, so without a carry the
    original bug returns one turn later."""
    carried = record_signin_email_in_global_llm_context(
        SimpleNamespace(
            request_policy=RequestPolicy(
                resolved_signin_email="typed@example.com",
                resolved_signin_host="https://console.example.com",
            )
        ),
        None,
    )

    policy = await _build_with_forced_classifier(
        user_message="also download the invoices from https://console.example.com/invoices",
        classifier_policy=RequestPolicy(
            login_intent=True,
            email_signin_intent=False,
            credential_input_kind="none",
            classifier_status="success",
        ),
        org_credentials=[],
        connected_gmail=None,
        global_llm_context=carried or "",
    )

    assert policy.clarification_reason != "login_credentials_unresolved"
    assert policy.resolved_signin_email == "typed@example.com"


@pytest.mark.asyncio
async def test_a_failed_resolution_asks_for_the_address_rather_than_building_without_one() -> None:
    """The credential requirement is already lifted for this shape, so a silent fall-through
    would author a login with no identity and no question asked."""
    policy = await _build_with_forced_classifier(
        user_message="Sign in to https://console.example.com/signin by email and extract the balance.",
        classifier_policy=_email_signin_policy(signin_email_candidates=[]),
        org_credentials=[],
        gmail_lookup=AsyncMock(side_effect=RuntimeError("boom")),
    )

    assert policy.clarification_reason == "signin_email_unresolved"
    assert policy.resolved_signin_email is None


@pytest.mark.asyncio
async def test_a_carried_identity_does_not_follow_the_user_to_a_different_site() -> None:
    """Reusing it anywhere else would suppress the password ask on a site that never
    established it was passwordless."""
    carried = record_signin_email_in_global_llm_context(
        SimpleNamespace(
            request_policy=RequestPolicy(
                resolved_signin_email="typed@example.com",
                resolved_signin_host="https://console.example.com",
            )
        ),
        None,
    )

    policy = await _build_with_forced_classifier(
        user_message="Log in to https://portal.example.com/login and download this month's invoices.",
        classifier_policy=RequestPolicy(
            login_intent=True,
            email_signin_intent=False,
            credential_input_kind="none",
            classifier_status="success",
        ),
        org_credentials=[],
        connected_gmail=None,
        global_llm_context=carried or "",
    )

    assert policy.clarification_reason == "login_credentials_unresolved"
    assert policy.resolved_signin_email is None


def test_a_model_authored_signin_identity_is_discarded_in_favour_of_the_trusted_one() -> None:
    """The model can author the context blob; a signin_email it invents would mark the next
    turn passwordless and suppress the password ask for a site that needs one."""
    trusted = record_signin_email_in_global_llm_context(
        SimpleNamespace(
            request_policy=RequestPolicy(
                resolved_signin_email="trusted@example.com",
                resolved_signin_host="https://console.example.com",
            )
        ),
        None,
    )

    adopted = adopt_model_authored_context(
        trusted,
        {"signin_email": "attacker@example.com", "signin_email_host": "https://portal.example.com"},
    )

    assert adopted.signin_email == "trusted@example.com"
    assert adopted.signin_email_host == "https://console.example.com"


def test_a_model_authored_identity_cannot_appear_where_the_server_resolved_none() -> None:
    adopted = adopt_model_authored_context(
        None,
        {"signin_email": "attacker@example.com", "signin_email_host": "https://portal.example.com"},
    )

    assert adopted.signin_email == ""
    assert adopted.signin_email_host == ""


def test_a_named_credential_resolves_without_the_word_credential() -> None:
    """ "use skyvern-datadog to login" names the credential outright. The pattern-based candidates
    only fire when the message also says "credential", which left this resolving nothing."""
    credentials = [SimpleNamespace(name="skyvern-datadog"), SimpleNamespace(name="skyvern-posthog")]

    assert _saved_credential_names_mentioned("use skyvern-datadog to login.", credentials) == ["skyvern-datadog"]


def test_contraction_apostrophe_does_not_open_a_bogus_quoted_name() -> None:
    """A contraction's apostrophe once paired with a later quote and swallowed the span between them as
    a credential name (e.g. from "I've connected the credential 'hex'"). It must extract only 'hex'."""
    candidates = _exact_credential_name_candidates("I've connected the credential 'hex' — continue.")

    assert "hex" in candidates
    assert not any("connected" in candidate for candidate in candidates)


@pytest.mark.asyncio
async def test_a_named_credential_resolves_when_the_classifier_misses_login_intent() -> None:
    """`login_intent` is a model-authored hint that varies run to run. Gating the saved-name scan on
    it alone let the classifier veto a message that says "log in" outright — the very case this
    resolves. The deterministic login phrasing must reach the scan on its own."""
    named = SimpleNamespace(credential_id="cred_datadog", name="skyvern-datadog", credential_type="password")
    policy = RequestPolicy(credential_input_kind="skyvern_stored_credential", login_intent=False)

    await _resolve_direct(policy, user_message="use skyvern-datadog to login.", org_credentials=[named])

    assert [c.credential_id for c in policy.resolved_credentials] == ["cred_datadog"]


def test_a_negated_saved_name_is_not_treated_as_a_reference() -> None:
    credentials = [SimpleNamespace(name="skyvern-datadog")]

    assert _saved_credential_names_mentioned("do not use skyvern-datadog", credentials) == []


async def _admit(
    policy: RequestPolicy,
    *,
    credential_id: str,
    page_url: str,
    org_credentials: list[SimpleNamespace],
) -> tuple[LiveCredentialAdmission, AsyncMock]:
    load_mock = AsyncMock(return_value=org_credentials)
    with patch("skyvern.forge.app.DATABASE.credentials.get_credentials", new=load_mock):
        admission = await admit_credential_for_live_page(
            policy,
            organization_id="o_test",
            credential_id=credential_id,
            page_url=page_url,
        )
    return admission, load_mock


_LIVE_LOGIN_URL = "https://analytics.example.com/login?next=%2Fweb"
_SAVED_LOGIN_URL = "https://analytics.example.com/login"


class TestLivePageCredentialAdmission:
    """A login wall the scout reached is authority evidence no turn-start classification carried.

    The turn-start resolved set is empty in every case here, which is the state a bare-task
    prompt ("go to <deep link> and output <metric>") leaves behind.
    """

    @pytest.mark.asyncio
    async def test_admits_the_credential_the_live_login_page_vouches_for(self) -> None:
        policy = RequestPolicy()
        admission, _ = await _admit(
            policy,
            credential_id="cred_analytics",
            page_url=_LIVE_LOGIN_URL,
            org_credentials=[_cred("analytics", "cred_analytics", _SAVED_LOGIN_URL)],
        )

        assert admission.admitted is True
        assert admission.steer is None
        assert [c.credential_id for c in policy.resolved_credentials] == ["cred_analytics"]

    @pytest.mark.asyncio
    async def test_admission_is_additive_and_does_not_duplicate(self) -> None:
        already = _cred("other", "cred_other", "https://billing.example.com/login")
        policy = RequestPolicy(resolved_credentials=[already])
        await _admit(
            policy,
            credential_id="cred_analytics",
            page_url=_LIVE_LOGIN_URL,
            org_credentials=[_cred("analytics", "cred_analytics", _SAVED_LOGIN_URL), already],
        )
        await _admit(
            policy,
            credential_id="cred_analytics",
            page_url=_LIVE_LOGIN_URL,
            org_credentials=[_cred("analytics", "cred_analytics", _SAVED_LOGIN_URL), already],
        )

        assert [c.credential_id for c in policy.resolved_credentials] == ["cred_other", "cred_analytics"]

    @pytest.mark.asyncio
    async def test_declines_a_credential_the_live_page_does_not_vouch_for(self) -> None:
        policy = RequestPolicy()
        admission, _ = await _admit(
            policy,
            credential_id="cred_unrelated",
            page_url=_LIVE_LOGIN_URL,
            org_credentials=[_cred("unrelated", "cred_unrelated", "https://billing.example.com/login")],
        )

        assert admission.admitted is False
        assert policy.resolved_credentials == []

    @pytest.mark.asyncio
    async def test_a_wrong_credential_is_refused_without_naming_the_right_one(self) -> None:
        """The tool answers a model-authored id; naming the match would enumerate the org for it."""
        policy = RequestPolicy()
        admission, _ = await _admit(
            policy,
            credential_id="cred_unrelated",
            page_url=_LIVE_LOGIN_URL,
            org_credentials=[
                _cred("analytics", "cred_analytics", _SAVED_LOGIN_URL),
                _cred("unrelated", "cred_unrelated", "https://billing.example.com/login"),
            ],
        )

        assert admission.admitted is False
        assert admission.steer is not None
        assert "cred_analytics" not in admission.steer
        assert policy.resolved_credentials == []

    @pytest.mark.asyncio
    async def test_several_page_matches_ask_instead_of_binding(self) -> None:
        policy = RequestPolicy()
        admission, _ = await _admit(
            policy,
            credential_id="cred_one",
            page_url=_LIVE_LOGIN_URL,
            org_credentials=[
                _cred("analytics one", "cred_one", _SAVED_LOGIN_URL),
                _cred("analytics two", "cred_two", _SAVED_LOGIN_URL),
            ],
        )

        assert admission.admitted is False
        assert admission.steer is not None
        assert _AMBIGUOUS_URL_CREDENTIAL_QUESTION in admission.steer
        assert "cred_one" not in admission.steer
        assert policy.resolved_credentials == []
        assert [c.credential_id for c in policy.discovered_credentials] == ["cred_one", "cred_two"]

    @pytest.mark.asyncio
    async def test_a_credential_saved_without_a_login_url_is_evidence_about_no_page(self) -> None:
        """The sole-URL-less tier is turn-start-only: reaching a login wall says a login is needed,
        not which credential this page belongs to."""
        policy = RequestPolicy()
        admission, _ = await _admit(
            policy,
            credential_id="cred_urlless",
            page_url=_LIVE_LOGIN_URL,
            org_credentials=[_cred("analytics", "cred_urlless")],
        )

        assert admission.admitted is False
        assert policy.resolved_credentials == []

    @pytest.mark.asyncio
    async def test_a_url_matched_credential_is_named_when_a_urlless_one_was_requested(self) -> None:
        policy = RequestPolicy()
        admission, _ = await _admit(
            policy,
            credential_id="cred_urlless",
            page_url=_LIVE_LOGIN_URL,
            org_credentials=[
                _cred("tested", "cred_tested", _SAVED_LOGIN_URL),
                _cred("urlless", "cred_urlless"),
            ],
        )

        assert admission.admitted is False
        assert admission.steer is not None
        assert "cred_tested" not in admission.steer

    @pytest.mark.asyncio
    async def test_a_card_credential_never_admits_as_a_login(self) -> None:
        policy = RequestPolicy()
        admission, _ = await _admit(
            policy,
            credential_id="cred_card",
            page_url=_LIVE_LOGIN_URL,
            org_credentials=[_cred("card", "cred_card", _SAVED_LOGIN_URL, CredentialType.CREDIT_CARD)],
        )

        assert admission.admitted is False
        assert policy.resolved_credentials == []

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        "policy",
        [
            RequestPolicy(raw_secret_detected=True),
            RequestPolicy(allow_run_blocks=False),
        ],
    )
    async def test_a_turn_without_run_authority_never_reads_credentials(self, policy: RequestPolicy) -> None:
        admission, load_mock = await _admit(
            policy,
            credential_id="cred_analytics",
            page_url=_LIVE_LOGIN_URL,
            org_credentials=[_cred("analytics", "cred_analytics", _SAVED_LOGIN_URL)],
        )

        assert admission.admitted is False
        assert policy.resolved_credentials == []
        load_mock.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_no_live_page_url_never_reads_credentials(self) -> None:
        policy = RequestPolicy()
        admission, load_mock = await _admit(
            policy,
            credential_id="cred_analytics",
            page_url="",
            org_credentials=[_cred("analytics", "cred_analytics", _SAVED_LOGIN_URL)],
        )

        assert admission.admitted is False
        load_mock.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_the_admission_fingerprint_names_the_fill_seam(self) -> None:
        with capture_logs() as logs:
            await _admit(
                RequestPolicy(),
                credential_id="cred_analytics",
                page_url=_LIVE_LOGIN_URL,
                org_credentials=[_cred("analytics", "cred_analytics", _SAVED_LOGIN_URL)],
            )

        emitted = [entry for entry in logs if entry["event"] == "copilot credential live-page admission"]
        assert [(entry["seam"], entry["outcome"]) for entry in emitted] == [("fill", "admitted")]


async def _resolve_live_page(
    policy: RequestPolicy,
    *,
    page_url: str,
    org_credentials: list[SimpleNamespace],
) -> tuple[LivePageResolutionRecord, AsyncMock]:
    load_mock = AsyncMock(return_value=org_credentials)
    with patch("skyvern.forge.app.DATABASE.credentials.get_credentials", new=load_mock):
        record = await resolve_credential_for_live_page(
            policy,
            organization_id="o_test",
            page_url=page_url,
        )
    return record, load_mock


class TestLivePageCredentialObservation:
    """The observation seam asks the same question with no credential requested: whose page is this?"""

    @pytest.mark.asyncio
    async def test_a_sole_page_match_binds_and_writes_the_fill_seam_records(self) -> None:
        policy = RequestPolicy()
        record, _ = await _resolve_live_page(
            policy,
            page_url=_LIVE_LOGIN_URL,
            org_credentials=[
                _cred("analytics", "cred_analytics", _SAVED_LOGIN_URL),
                _cred("billing", "cred_billing", "https://billing.example.com/login"),
                _cred("urlless", "cred_urlless"),
            ],
        )

        assert record.verdict == "resolved"
        assert record.tier == "url_path"
        assert [c.credential_id for c in record.candidates] == ["cred_analytics"]
        assert [c.credential_id for c in policy.resolved_credentials] == ["cred_analytics"]
        assert policy.live_page_admitted_urls == {"cred_analytics": _LIVE_LOGIN_URL}

    @pytest.mark.asyncio
    async def test_two_page_matches_bind_nothing_and_record_both_candidates(self) -> None:
        policy = RequestPolicy()
        record, _ = await _resolve_live_page(
            policy,
            page_url=_LIVE_LOGIN_URL,
            org_credentials=[
                _cred("analytics one", "cred_one", _SAVED_LOGIN_URL),
                _cred("analytics two", "cred_two", _SAVED_LOGIN_URL),
            ],
        )

        assert record.verdict == "ambiguous"
        assert [c.credential_id for c in record.candidates] == ["cred_one", "cred_two"]
        assert policy.resolved_credentials == []
        assert policy.live_page_admitted_urls == {}
        assert policy.live_page_resolution is record

    @pytest.mark.asyncio
    async def test_decoys_only_bind_nothing(self) -> None:
        policy = RequestPolicy()
        record, _ = await _resolve_live_page(
            policy,
            page_url=_LIVE_LOGIN_URL,
            org_credentials=[
                _cred("billing", "cred_billing", "https://billing.example.com/login"),
                _cred("urlless", "cred_urlless"),
            ],
        )

        assert record.verdict == "no_match"
        assert policy.resolved_credentials == []
        assert policy.live_page_admitted_urls == {}

    @pytest.mark.asyncio
    async def test_a_card_credential_never_binds_as_a_login(self) -> None:
        policy = RequestPolicy()
        record, _ = await _resolve_live_page(
            policy,
            page_url=_LIVE_LOGIN_URL,
            org_credentials=[_cred("card", "cred_card", _SAVED_LOGIN_URL, CredentialType.CREDIT_CARD)],
        )

        assert record.verdict == "no_match"
        assert policy.resolved_credentials == []

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        "policy",
        [
            RequestPolicy(raw_secret_detected=True),
            RequestPolicy(allow_run_blocks=False),
            RequestPolicy(email_signin_intent=True, login_intent=True),
        ],
    )
    async def test_a_turn_without_run_authority_never_reads_credentials(self, policy: RequestPolicy) -> None:
        record, load_mock = await _resolve_live_page(
            policy,
            page_url=_LIVE_LOGIN_URL,
            org_credentials=[_cred("analytics", "cred_analytics", _SAVED_LOGIN_URL)],
        )

        assert record.verdict == "declined"
        assert policy.resolved_credentials == []
        assert policy.live_page_admitted_urls == {}
        load_mock.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_the_admission_fingerprint_fires_once_per_page(self) -> None:
        policy = RequestPolicy()
        org_credentials = [_cred("analytics", "cred_analytics", _SAVED_LOGIN_URL)]
        with capture_logs() as logs:
            await _resolve_live_page(policy, page_url=_LIVE_LOGIN_URL, org_credentials=org_credentials)
            await _resolve_live_page(policy, page_url=_LIVE_LOGIN_URL, org_credentials=org_credentials)
            await _resolve_live_page(
                policy, page_url="https://other.example.com/login", org_credentials=org_credentials
            )

        emitted = [entry for entry in logs if entry["event"] == "copilot credential live-page admission"]
        assert [entry["seam"] for entry in emitted] == ["page_observation", "page_observation"]
        assert emitted[0]["outcome"] == "admitted"
        assert emitted[0]["tier"] == "url_path"

    @pytest.mark.asyncio
    async def test_a_passwordless_email_turn_declines_under_the_observation_seam(self) -> None:
        policy = RequestPolicy(email_signin_intent=True, login_intent=True)
        with capture_logs() as logs:
            record, _ = await _resolve_live_page(
                policy,
                page_url=_LIVE_LOGIN_URL,
                org_credentials=[_cred("analytics", "cred_analytics", _SAVED_LOGIN_URL)],
            )

        emitted = [entry for entry in logs if entry["event"] == "copilot credential live-page admission"]
        assert record.verdict == "declined"
        # Both lines emit: the guard keys on outcome, so a decline cannot consume the token a later
        # grant on the same URL needs.
        assert [(entry["seam"], entry["outcome"]) for entry in emitted] == [
            ("page_observation", "passwordless_declined"),
            ("page_observation", "declined"),
        ]

    @pytest.mark.asyncio
    async def test_a_later_unmatched_page_does_not_erase_the_login_pages_answer(self) -> None:
        policy = RequestPolicy()
        ambiguous_credentials = [
            _cred("analytics one", "cred_one", _SAVED_LOGIN_URL),
            _cred("analytics two", "cred_two", _SAVED_LOGIN_URL),
        ]
        await _resolve_live_page(policy, page_url=_LIVE_LOGIN_URL, org_credentials=ambiguous_credentials)
        await _resolve_live_page(
            policy, page_url="https://help.example.com/docs", org_credentials=ambiguous_credentials
        )

        assert policy.live_page_resolution is not None
        assert policy.live_page_resolution.verdict == "ambiguous"


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", ["website_stored_credential", "credential_name"])
async def test_a_credential_labelled_turn_looks_a_written_name_up_by_name(kind: str) -> None:
    """Which credential label the classifier chose must not decide whether a name the user wrote
    is searched by name (SKY-12917). The message carries no login phrase and no quoted name, so
    only the classifier-ref route can resolve it."""
    policy = RequestPolicy(credential_input_kind=kind, credential_refs=["analytics-login"])
    await _resolve_direct(
        policy,
        user_message="Use analytics-login here.",
        org_credentials=[_cred("analytics-login", "cred_analytics")],
    )

    assert [c.credential_id for c in policy.resolved_credentials] == ["cred_analytics"]
    assert policy.clarification_question is None


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", ["none", "placeholder"])
async def test_a_turn_the_classifier_never_called_a_credential_request_does_not_scan(kind: str) -> None:
    policy = RequestPolicy(credential_input_kind=kind, credential_refs=["analytics-login"])
    load_mock, _ = await _resolve_direct(
        policy,
        user_message="Use analytics-login here.",
        org_credentials=[_cred("analytics-login", "cred_analytics")],
        get_credentials=AsyncMock(side_effect=AssertionError("a non-credential turn must not scan")),
    )

    assert policy.resolved_credentials == []
    load_mock.assert_not_awaited()


@pytest.mark.asyncio
async def test_a_degraded_classifier_never_enumerates_saved_credentials(kind: str = "credential_name") -> None:
    policy = RequestPolicy(
        credential_input_kind=kind, credential_refs=["analytics-login"], classifier_status="fallback"
    )
    load_mock, _ = await _resolve_direct(
        policy,
        user_message="Use analytics-login here.",
        org_credentials=[_cred("analytics-login", "cred_analytics")],
        get_credentials=AsyncMock(side_effect=AssertionError("a degraded turn must not scan")),
    )

    assert policy.resolved_credentials == []
    load_mock.assert_not_awaited()


@pytest.mark.asyncio
async def test_a_stale_ref_absent_from_the_message_still_authorizes_nothing() -> None:
    policy = RequestPolicy(credential_input_kind="website_stored_credential", credential_refs=["analytics-login"])
    load_mock, by_ids_mock = await _resolve_direct(
        policy,
        user_message="Continue with the workflow.",
        org_credentials=[_cred("analytics-login", "cred_analytics")],
        get_credentials=AsyncMock(side_effect=AssertionError("a stale ref must not scan credentials")),
    )

    assert policy.resolved_credentials == []
    load_mock.assert_not_awaited()


def _passwordless_email_policy() -> RequestPolicy:
    """The shape `_is_passwordless_email_signin` recognises: sign-in by email, no credential referenced."""
    return RequestPolicy(
        login_intent=True,
        email_signin_intent=True,
        credential_input_kind="none",
    )


class TestPasswordlessEmailTurnMeetsLivePageAdmission:
    """A passwordless-email turn must not be pulled into a password-credential ask (SKY-13030 / :2559).

    Turn-start resolution excludes this shape from the URL tiers; these pin what the mid-turn
    seam does with the same shape, which is the interaction the turn-start rule cannot cover.
    """

    @pytest.mark.asyncio
    async def test_a_sole_host_match_is_neither_admitted_nor_asked_about(self) -> None:
        policy = _passwordless_email_policy()
        admission, load_mock = await _admit(
            policy,
            credential_id="cred_analytics",
            page_url=_LIVE_LOGIN_URL,
            org_credentials=[_cred("analytics", "cred_analytics", _SAVED_LOGIN_URL)],
        )

        assert admission.admitted is False
        assert admission.steer is None
        assert policy.resolved_credentials == []
        assert policy.clarification_question is None
        load_mock.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_several_host_matches_do_not_pull_the_turn_into_a_credential_ask(self) -> None:
        policy = _passwordless_email_policy()
        admission, _ = await _admit(
            policy,
            credential_id="cred_one",
            page_url=_LIVE_LOGIN_URL,
            org_credentials=[
                _cred("analytics one", "cred_one", _SAVED_LOGIN_URL),
                _cred("analytics two", "cred_two", _SAVED_LOGIN_URL),
            ],
        )

        assert admission.admitted is False
        assert admission.steer is None
        assert policy.discovered_credentials == []
        assert policy.requires_user_clarification is False


@pytest.mark.asyncio
async def test_a_live_page_admission_does_not_carry_to_the_next_turn() -> None:
    """The evidence is a page the next turn has not seen, so the grant is re-earned there."""
    policy = RequestPolicy()
    await _admit(
        policy,
        credential_id="cred_analytics",
        page_url=_LIVE_LOGIN_URL,
        org_credentials=[_cred("analytics", "cred_analytics", _SAVED_LOGIN_URL)],
    )
    carried_context = record_approved_credentials_in_global_llm_context(
        SimpleNamespace(request_policy=policy, credential_pause_connected_credential_id=None), None
    )

    next_turn = RequestPolicy()
    with patch(
        "skyvern.forge.app.DATABASE.credentials.get_credentials_by_ids",
        new=AsyncMock(return_value=[_cred("analytics", "cred_analytics", _SAVED_LOGIN_URL)]),
    ):
        await _seed_prior_approved_credentials(
            next_turn,
            organization_id="o_test",
            global_llm_context=carried_context or "",
        )

    assert next_turn.resolved_credentials == []


@pytest.mark.asyncio
async def test_session_reuse_language_grants_no_authority_at_either_seam() -> None:
    """ "Use the tab where I'm signed in" is session context, not stored-credential authority
    (SKY-10131) — and the live-page seam must not convert it into authority either, because
    the tool gate, not the wording, is what reaches admission."""
    policy = RequestPolicy(login_intent=False, credential_input_kind="none")
    load_mock, _ = await _resolve_direct(
        policy,
        user_message="Use the tab where I'm signed in at https://analytics.example.com/web and read the visitor count.",
        org_credentials=[_cred("analytics", "cred_analytics", "https://analytics.example.com/login")],
    )
    assert policy.resolved_credentials == []
    assert policy.clarification_question is None

    blocked = RequestPolicy(allow_run_blocks=False)
    admission, admit_load = await _admit(
        blocked,
        credential_id="cred_analytics",
        page_url="https://analytics.example.com/login",
        org_credentials=[_cred("analytics", "cred_analytics", "https://analytics.example.com/login")],
    )
    assert admission.admitted is False
    admit_load.assert_not_awaited()


@pytest.mark.asyncio
async def test_the_passwordless_decline_is_named_in_the_admission_log() -> None:
    """QA counts a run only when this exact value appears, so the string is a contract, not a label.

    Captured through structlog rather than caplog: a sibling test module reconfigures structlog's
    factory, which empties caplog here depending on test order.
    """
    policy = _passwordless_email_policy()
    with capture_logs() as entries:
        await _admit(
            policy,
            credential_id="cred_one",
            page_url=_LIVE_LOGIN_URL,
            org_credentials=[
                _cred("analytics one", "cred_one", _SAVED_LOGIN_URL),
                _cred("analytics two", "cred_two", _SAVED_LOGIN_URL),
            ],
        )

    assert any(entry.get("outcome") == "passwordless_declined" for entry in entries)


class TestLivePageTierBoundaries:
    """The path tier exists for the live page's transient query, not to cross an identity."""

    def test_an_exact_match_wins_however_the_caller_orders_the_tiers(self) -> None:
        credentials = [
            _cred("tenant a", "cred_exact", "https://analytics.example.com/login?tenant=a"),
            _cred("no tenant", "cred_loose", "https://analytics.example.com/login"),
        ]
        page = ["https://analytics.example.com/login?tenant=a"]

        for tiers in (("url_exact", "url_path"), ("url_path", "url_exact")):
            resolution = resolve_by_url(credentials, page, tiers=tiers, password_only=True)
            assert resolution.tier == "url_exact"
            assert [c.credential_id for c in resolution.candidates] == ["cred_exact"]

    @pytest.mark.asyncio
    async def test_a_tenant_query_on_the_saved_url_must_match_exactly(self) -> None:
        policy = RequestPolicy()
        admission, _ = await _admit(
            policy,
            credential_id="cred_tenant_a",
            page_url="https://analytics.example.com/login?tenant=b",
            org_credentials=[_cred("tenant a", "cred_tenant_a", "https://analytics.example.com/login?tenant=a")],
        )

        assert admission.admitted is False
        assert policy.resolved_credentials == []

    @pytest.mark.asyncio
    async def test_the_live_pages_transient_query_does_not_block_the_match(self) -> None:
        policy = RequestPolicy()
        admission, _ = await _admit(
            policy,
            credential_id="cred_analytics",
            page_url=_LIVE_LOGIN_URL,
            org_credentials=[_cred("analytics", "cred_analytics", _SAVED_LOGIN_URL)],
        )

        assert admission.admitted is True

    @pytest.mark.asyncio
    async def test_another_page_on_the_same_host_is_not_the_login_page(self) -> None:
        policy = RequestPolicy()
        admission, _ = await _admit(
            policy,
            credential_id="cred_analytics",
            page_url="https://analytics.example.com/u/someone/uploaded-note",
            org_credentials=[_cred("analytics", "cred_analytics", _SAVED_LOGIN_URL)],
        )

        assert admission.admitted is False
        assert policy.resolved_credentials == []


@pytest.mark.asyncio
async def test_basic_auth_in_a_login_url_never_reaches_the_admission_log() -> None:
    """A sign-in URL can carry `user:pass@`, and the matching key keeps it deliberately."""
    policy = RequestPolicy()
    with capture_logs() as entries:
        await _admit(
            policy,
            credential_id="cred_analytics",
            page_url="https://admin:hunter2@analytics.example.com/login?code=abc",
            org_credentials=[_cred("analytics", "cred_analytics", _SAVED_LOGIN_URL)],
        )

    logged = " ".join(str(entry) for entry in entries)
    assert "hunter2" not in logged
    assert "admin:" not in logged
    assert "code=abc" not in logged
