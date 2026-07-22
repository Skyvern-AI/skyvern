from __future__ import annotations

import textwrap
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest

from skyvern.config import settings
from skyvern.forge.sdk.copilot.context import (
    CredentialCheck,
    StructuredContext,
    record_approved_credentials_in_global_llm_context,
)
from skyvern.forge.sdk.copilot.request_policy import (
    CREDENTIAL_DEFERRED_DRAFT_REASONS,
    CREDENTIAL_PROMPT_CLARIFICATION_REASONS,
    RequestPolicy,
    _classification_from_raw,
    _workflow_credential_inputs_unbound,
    build_request_policy,
    credential_prompt_reason,
)
from skyvern.forge.sdk.copilot.tools.credentials import _credential_run_approval_error


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


@pytest.mark.asyncio
async def test_request_policy_resolver_still_blocks_raw_secret_with_author_time_log_only(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(settings, "ENV", "local")
    monkeypatch.setattr(settings, "WORKFLOW_COPILOT_AUTHOR_TIME_GATE_LOG_ONLY", True)

    policy = await build_request_policy(
        user_message="Use password: Hunter99! to sign in.",
        workflow_yaml="",
        chat_history=[],
        global_llm_context="",
        organization_id="o_test",
        handler=None,
    )

    assert policy.raw_secret_detected is True
    assert policy.user_response_policy == "ask_clarification"
    assert policy.allow_update_workflow is False
    assert policy.allow_run_blocks is False


def _cred(name: str, credential_id: str, tested_url: str | None = None) -> SimpleNamespace:
    return SimpleNamespace(name=name, credential_id=credential_id, tested_url=tested_url)


async def _build_with_forced_classifier(
    *,
    user_message: str,
    classifier_policy: RequestPolicy,
    org_credentials: list[SimpleNamespace],
    get_credentials: AsyncMock | None = None,
    get_credentials_by_ids: AsyncMock | None = None,
    workflow_yaml: str = "",
    global_llm_context: str = "",
) -> RequestPolicy:
    load_mock = get_credentials or AsyncMock(return_value=org_credentials)
    by_ids_mock = get_credentials_by_ids or AsyncMock(return_value=[])
    with (
        patch(
            "skyvern.forge.sdk.copilot.request_policy._classify_request",
            new=AsyncMock(return_value=classifier_policy),
        ),
        patch("skyvern.forge.app.DATABASE.credentials.get_credentials", new=load_mock),
        patch("skyvern.forge.app.DATABASE.credentials.get_credentials_by_ids", new=by_ids_mock),
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
@pytest.mark.parametrize("kind", ["credential_name", "none"])
async def test_success_empty_refs_resolves_named_credential(kind: str) -> None:
    credential = _cred("mock-portal-login", "cred_login")
    policy = await _build_with_forced_classifier(
        user_message="Sign in with the saved credential named mock-portal-login.",
        classifier_policy=RequestPolicy(credential_input_kind=kind, classifier_status="success"),
        org_credentials=[credential, _cred("other", "cred_other")],
    )

    assert policy.classifier_status == "success"
    assert policy.credential_input_kind == "credential_name"
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
async def test_success_website_kind_with_matching_url_keeps_url_credential() -> None:
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
    assert [c.credential_id for c in policy.resolved_credentials] == ["cred_url"]


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
async def test_success_populated_name_refs_are_not_narrowed_by_scan() -> None:
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
    assert policy.clarification_reason == "credential_name_unresolved"


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
