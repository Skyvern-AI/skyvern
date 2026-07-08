from __future__ import annotations

import textwrap
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest

from skyvern.config import settings
from skyvern.forge.sdk.copilot.context import CredentialCheck, StructuredContext
from skyvern.forge.sdk.copilot.request_policy import (
    CREDENTIAL_DEFERRED_DRAFT_REASONS,
    CREDENTIAL_PROMPT_CLARIFICATION_REASONS,
    RequestPolicy,
    _workflow_credential_inputs_unbound,
    build_request_policy,
    credential_prompt_reason,
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
