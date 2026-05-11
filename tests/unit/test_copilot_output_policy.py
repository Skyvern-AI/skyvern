from __future__ import annotations

import json
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from skyvern.forge.sdk.copilot import agent as agent_module
from skyvern.forge.sdk.copilot.context import CopilotContext
from skyvern.forge.sdk.copilot.output_policy import (
    CopilotOutputKind,
    OutputPolicyReason,
    OutputPolicyVerdict,
    derive_output_kind,
    evaluate_output_policy,
    hard_block_output_policy_verdict,
)
from skyvern.forge.sdk.copilot.request_policy import RequestPolicy


def _credential(credential_id: str = "cred_safe", tested_url: str = "https://login.example.test/login") -> object:
    return SimpleNamespace(credential_id=credential_id, name="Saved Login", tested_url=tested_url)


def _policy(**overrides: object) -> RequestPolicy:
    defaults = dict(resolved_credentials=[_credential()])
    defaults.update(overrides)
    return RequestPolicy(**defaults)


def _ctx(**overrides: object) -> CopilotContext:
    defaults = dict(
        organization_id="org-1",
        workflow_id="wf-1",
        workflow_permanent_id="wfp-1",
        workflow_yaml="",
        browser_session_id=None,
        stream=MagicMock(),
        request_policy=_policy(),
    )
    defaults.update(overrides)
    return CopilotContext(**defaults)


def _fake_run_result(payload: dict) -> SimpleNamespace:
    return SimpleNamespace(final_output=json.dumps(payload), new_items=[])


def _chat_request() -> SimpleNamespace:
    return SimpleNamespace(
        workflow_id="wf-1",
        workflow_permanent_id="wfp-1",
        workflow_copilot_chat_id="chat-1",
        workflow_yaml="title: Prior",
    )


def _workflow_yaml(
    *,
    url: str = "https://login.example.test/login",
    navigation_goal: str = "Log in.",
) -> str:
    return f"""
workflow_definition:
  parameters:
    - parameter_type: workflow
      workflow_parameter_type: credential_id
      key: login_credentials
      default_value: cred_safe
  blocks:
    - block_type: login
      label: login
      url: {url}
      navigation_goal: {navigation_goal}
      parameter_keys:
        - login_credentials
"""


def test_rejects_raw_secret_echo_in_user_response() -> None:
    verdict = evaluate_output_policy(
        request_policy=_policy(),
        user_response="I used password: hunter2 to test the login.",
    )

    assert not verdict.allowed
    assert OutputPolicyReason.RAW_SECRET_LEAK in verdict.reason_codes


@pytest.mark.parametrize(
    "user_response",
    [
        "I used hunter2 as the password.",
        "Type hunter2 into the password field.",
    ],
)
def test_does_not_infer_arbitrary_prose_secret_echoes(user_response: str) -> None:
    verdict = evaluate_output_policy(
        request_policy=_policy(),
        user_response=user_response,
    )

    assert verdict.allowed


def test_rejects_raw_secret_in_workflow_yaml() -> None:
    verdict = evaluate_output_policy(
        request_policy=_policy(),
        workflow_yaml=_workflow_yaml(navigation_goal="Log in with password: hunter2."),
    )

    assert not verdict.allowed
    assert OutputPolicyReason.RAW_SECRET_LEAK in verdict.reason_codes


def test_rejects_raw_secret_in_structured_tool_arguments() -> None:
    verdict = evaluate_output_policy(
        request_policy=_policy(),
        tool_arguments={"workflow_yaml": {"navigation_goal": "Type password: hunter2 into the field."}},
    )

    assert not verdict.allowed
    assert OutputPolicyReason.RAW_SECRET_LEAK in verdict.reason_codes


def test_does_not_hard_block_on_prior_global_context_secret() -> None:
    verdict = evaluate_output_policy(
        request_policy=_policy(),
        user_response="I drafted a safe workflow.",
        global_llm_context='{"prior_note": "User previously pasted password: hunter2."}',
    )

    assert verdict.allowed


def test_rejects_raw_secret_even_when_workflow_contains_jinja_placeholder() -> None:
    verdict = evaluate_output_policy(
        request_policy=_policy(),
        workflow_yaml="navigation_goal: Use {{ parameters.username }} and password: hunter2.",
    )

    assert not verdict.allowed
    assert OutputPolicyReason.RAW_SECRET_LEAK in verdict.reason_codes


def test_allows_sensitive_jinja_placeholder_in_navigation_goal() -> None:
    verdict = evaluate_output_policy(
        request_policy=_policy(),
        workflow_yaml=_workflow_yaml(
            navigation_goal="Type {{ parameters.password }} into the password field.",
        ),
    )

    assert verdict.allowed


def test_rejects_reply_when_request_policy_required_clarification() -> None:
    verdict = evaluate_output_policy(
        request_policy=_policy(user_response_policy="ask_clarification", allow_update_workflow=False),
        response_type="REPLY",
        user_response="I created the workflow with the credential.",
    )

    assert not verdict.allowed
    assert OutputPolicyReason.REQUEST_POLICY_CLARIFICATION_BYPASS in verdict.reason_codes


def test_classifies_unbacked_workflow_delivery_claim() -> None:
    verdict = evaluate_output_policy(
        request_policy=_policy(),
        response_type="REPLY",
        user_response="Here's the workflow.",
        has_workflow_proposal=False,
    )

    assert not verdict.allowed
    assert OutputPolicyReason.UNBACKED_WORKFLOW_DELIVERY_CLAIM in verdict.reason_codes
    assert OutputPolicyReason.MISSING_PROPOSAL_STATE in verdict.reason_codes


def test_allows_workflow_delivery_language_after_failed_workflow_attempt() -> None:
    verdict = evaluate_output_policy(
        request_policy=_policy(),
        response_type="REPLY",
        user_response="I created a draft workflow and tested it, but the test failed.",
        has_workflow_proposal=False,
        workflow_attempted=True,
    )

    assert verdict.allowed


def test_classifies_missing_unvalidated_proposal_affordance() -> None:
    verdict = evaluate_output_policy(
        request_policy=_policy(),
        response_type="REPLY",
        user_response="I drafted the workflow but did not finish testing.",
        has_workflow_proposal=True,
        unvalidated=True,
    )

    assert not verdict.allowed
    assert OutputPolicyReason.MISSING_UNVALIDATED_PROPOSAL_AFFORDANCE in verdict.reason_codes


def test_rejects_incidental_accept_discard_language_for_unvalidated_proposal() -> None:
    verdict = evaluate_output_policy(
        request_policy=_policy(),
        response_type="REPLY",
        user_response="I accept that the test failed, but I can't discard the login block without more info.",
        has_workflow_proposal=True,
        unvalidated=True,
    )

    assert not verdict.allowed
    assert OutputPolicyReason.MISSING_UNVALIDATED_PROPOSAL_AFFORDANCE in verdict.reason_codes


def test_allows_unvalidated_proposal_affordance_copy() -> None:
    verdict = evaluate_output_policy(
        request_policy=_policy(),
        response_type="REPLY",
        user_response=(
            "Use Review to inspect it, Accept to save it, or Reject to discard it. "
            "It has not been tested or verified end-to-end."
        ),
        has_workflow_proposal=True,
        unvalidated=True,
    )

    assert verdict.allowed


def test_allows_backend_authored_request_policy_clarification() -> None:
    verdict = evaluate_output_policy(
        request_policy=_policy(user_response_policy="ask_clarification", allow_update_workflow=False),
        response_type="ASK_QUESTION",
        user_response="Which saved credential should I use?",
    )

    assert verdict.allowed
    assert verdict.output_kind == CopilotOutputKind.CLARIFICATION_REQUEST


def test_rejects_unapproved_credential_id_in_final_text() -> None:
    verdict = evaluate_output_policy(
        request_policy=_policy(),
        user_response="I used cred_other for the login.",
    )

    assert not verdict.allowed
    assert OutputPolicyReason.UNAPPROVED_CREDENTIAL_REFERENCE in verdict.reason_codes


def test_rejects_credential_id_when_request_policy_approved_no_credentials() -> None:
    verdict = evaluate_output_policy(
        request_policy=_policy(resolved_credentials=[]),
        user_response="I used cred_some_existing for the login.",
    )

    assert not verdict.allowed
    assert OutputPolicyReason.UNAPPROVED_CREDENTIAL_REFERENCE in verdict.reason_codes


def test_rejects_unapproved_credential_id_in_structured_tool_arguments() -> None:
    verdict = evaluate_output_policy(
        request_policy=_policy(),
        tool_arguments={"workflow_yaml": {"parameters": [{"default_value": "cred_other"}]}},
    )

    assert not verdict.allowed
    assert OutputPolicyReason.UNAPPROVED_CREDENTIAL_REFERENCE in verdict.reason_codes


def test_allows_explicit_unresolved_credential_id_for_untested_draft() -> None:
    verdict = evaluate_output_policy(
        request_policy=_policy(
            resolved_credentials=[],
            credential_input_kind="credential_id",
            credential_refs=["cred_missing"],
            invalid_credential_ids=["cred_missing"],
            allow_missing_credentials_in_draft=True,
            allow_run_blocks=False,
        ),
        workflow_yaml=_workflow_yaml().replace("cred_safe", "cred_missing"),
    )

    assert verdict.allowed


def test_rejects_unrequested_credential_id_even_when_untested_draft_allows_missing_credentials() -> None:
    verdict = evaluate_output_policy(
        request_policy=_policy(
            resolved_credentials=[],
            credential_input_kind="credential_name",
            credential_refs=["Missing Login"],
            allow_missing_credentials_in_draft=True,
            allow_run_blocks=False,
        ),
        workflow_yaml=_workflow_yaml().replace("cred_safe", "cred_other"),
    )

    assert not verdict.allowed
    assert OutputPolicyReason.UNAPPROVED_CREDENTIAL_REFERENCE in verdict.reason_codes


def test_allows_approved_credential_id_in_workflow_yaml() -> None:
    verdict = evaluate_output_policy(
        request_policy=_policy(),
        workflow_yaml=_workflow_yaml(),
    )

    assert verdict.allowed


def test_rejects_approved_credential_on_different_login_origin() -> None:
    verdict = evaluate_output_policy(
        request_policy=_policy(),
        workflow_yaml=_workflow_yaml(url="https://evil.example.test/login"),
    )

    assert not verdict.allowed
    assert OutputPolicyReason.CREDENTIAL_SCOPE_BROADENED in verdict.reason_codes


def test_rejects_approved_credential_on_templated_login_origin() -> None:
    verdict = evaluate_output_policy(
        request_policy=_policy(),
        workflow_yaml=_workflow_yaml(url='"{{ parameters.target_url }}"'),
    )

    assert not verdict.allowed
    assert OutputPolicyReason.CREDENTIAL_SCOPE_BROADENED in verdict.reason_codes


def test_derive_output_kind_uses_state_over_response_type() -> None:
    assert (
        derive_output_kind(
            response_type="REPLY",
            request_policy=_policy(user_response_policy="ask_clarification"),
            updated_workflow=SimpleNamespace(),
            workflow_was_persisted=True,
            workflow_attempted=True,
            unvalidated=False,
        )
        == CopilotOutputKind.CLARIFICATION_REQUEST
    )
    assert (
        derive_output_kind(
            response_type="REPLY",
            request_policy=_policy(),
            updated_workflow=SimpleNamespace(),
            workflow_was_persisted=False,
            workflow_attempted=False,
            unvalidated=True,
        )
        == CopilotOutputKind.WORKFLOW_DRAFT_PROPOSAL
    )
    assert (
        derive_output_kind(
            response_type="REPLY",
            request_policy=_policy(),
            updated_workflow=SimpleNamespace(),
            workflow_was_persisted=True,
            workflow_attempted=True,
            unvalidated=False,
        )
        == CopilotOutputKind.WORKFLOW_RUN_RESULT
    )
    assert (
        derive_output_kind(
            response_type="REPLY",
            request_policy=_policy(),
            updated_workflow=None,
            workflow_was_persisted=True,
            workflow_attempted=False,
            unvalidated=False,
        )
        == CopilotOutputKind.INFORMATIONAL_ANSWER
    )


def test_plain_reply_after_prior_persist_stays_informational() -> None:
    verdict = evaluate_output_policy(
        request_policy=_policy(),
        response_type="REPLY",
        user_response="I can help with that.",
        has_workflow_proposal=False,
        workflow_was_persisted=True,
        workflow_attempted=False,
    )

    assert verdict.allowed
    assert verdict.output_kind == CopilotOutputKind.INFORMATIONAL_ANSWER


def test_persistence_state_mismatch_is_a_hard_block() -> None:
    verdict = evaluate_output_policy(
        request_policy=_policy(),
        user_response="I updated the workflow.",
        workflow_was_persisted=False,
        has_workflow_proposal=True,
        output_kind=CopilotOutputKind.WORKFLOW_UPDATE_PROPOSAL,
    )
    hard_verdict = hard_block_output_policy_verdict(verdict)

    assert not verdict.allowed
    assert OutputPolicyReason.PERSISTENCE_STATE_MISMATCH in verdict.reason_codes
    assert not hard_verdict.allowed
    assert hard_verdict.reason_codes == [OutputPolicyReason.PERSISTENCE_STATE_MISMATCH]


def test_output_policy_verdict_reason_codes_force_blocked_state() -> None:
    verdict = OutputPolicyVerdict(
        allowed=True,
        reason_codes=[OutputPolicyReason.REQUEST_POLICY_CLARIFICATION_BYPASS],
    )

    assert not verdict.allowed


def test_sdk_agent_guardrail_builders_use_policy_names() -> None:
    from agents import GuardrailFunctionOutput, InputGuardrail, OutputGuardrail

    input_guardrails = agent_module._build_copilot_input_guardrails(InputGuardrail, GuardrailFunctionOutput)
    output_guardrails = agent_module._build_copilot_output_guardrails(OutputGuardrail, GuardrailFunctionOutput)

    assert len(input_guardrails) == 1
    assert input_guardrails[0].name == "request_policy_guardrail"
    assert input_guardrails[0].run_in_parallel is False
    assert len(output_guardrails) == 1
    assert output_guardrails[0].name == "copilot_output_policy_guardrail"


@pytest.mark.asyncio
async def test_sdk_request_policy_guardrail_trips_on_required_clarification() -> None:
    from agents import GuardrailFunctionOutput, InputGuardrail
    from agents.run_context import RunContextWrapper

    input_guardrails = agent_module._build_copilot_input_guardrails(InputGuardrail, GuardrailFunctionOutput)
    result = await input_guardrails[0].run(
        SimpleNamespace(),
        "input",
        RunContextWrapper(
            context=_ctx(request_policy=_policy(user_response_policy="ask_clarification", allow_update_workflow=False))
        ),
    )

    assert result.output.tripwire_triggered is True
    assert result.output.output_info["user_response_policy"] == "ask_clarification"


def test_sdk_output_guardrail_hard_blocks_raw_secret_final_text() -> None:
    verdict, response_type = agent_module._evaluate_copilot_final_output_policy(
        _ctx(),
        {"type": "REPLY", "user_response": "I used password: hunter2."},
    )

    assert response_type == "REPLY"
    assert not verdict.allowed
    assert verdict.reason_codes == [OutputPolicyReason.RAW_SECRET_LEAK]


@pytest.mark.asyncio
async def test_workflow_mutation_tools_have_sdk_input_guardrails_and_reject_raw_secret() -> None:
    from agents import ToolInputGuardrailData
    from agents.tool_context import ToolContext

    from skyvern.forge.sdk.copilot.tools import _WORKFLOW_YAML_OUTPUT_POLICY_GUARDRAIL, NATIVE_TOOLS

    guarded_tools = {
        tool.name: tool for tool in NATIVE_TOOLS if tool.name in {"update_workflow", "update_and_run_blocks"}
    }
    assert guarded_tools.keys() == {"update_workflow", "update_and_run_blocks"}
    assert all(
        tool.tool_input_guardrails == [_WORKFLOW_YAML_OUTPUT_POLICY_GUARDRAIL] for tool in guarded_tools.values()
    )

    result = await _WORKFLOW_YAML_OUTPUT_POLICY_GUARDRAIL.run(
        ToolInputGuardrailData(
            context=ToolContext(
                context=_ctx(),
                tool_name="update_workflow",
                tool_call_id="call-1",
                tool_arguments=json.dumps(
                    {
                        "workflow_yaml": """
workflow_definition:
  blocks:
    - block_type: navigation
      label: login
      navigation_goal: Type password: hunter2 into the password field.
"""
                    }
                ),
            ),
            agent=SimpleNamespace(),
        )
    )

    assert result.behavior["type"] == "reject_content"
    assert "raw_secret_leak" in result.behavior["message"]


@pytest.mark.asyncio
async def test_update_workflow_rejects_raw_secret_before_processing(monkeypatch) -> None:
    from skyvern.forge.sdk.copilot.tools import _update_workflow

    process_mock = MagicMock()
    monkeypatch.setattr("skyvern.forge.sdk.copilot.tools._process_workflow_yaml", process_mock)

    result = await _update_workflow(
        {
            "workflow_yaml": """
workflow_definition:
  blocks:
    - block_type: navigation
      label: login
      navigation_goal: Type password: hunter2 into the password field.
"""
        },
        _ctx(),
        allow_missing_credentials=True,
    )

    assert result["ok"] is False
    assert "raw_secret_leak" in result["error"]
    process_mock.assert_not_called()


def test_inline_replace_workflow_rejects_raw_secret_before_processing(monkeypatch) -> None:
    process_mock = MagicMock()
    monkeypatch.setattr("skyvern.forge.sdk.copilot.tools._process_workflow_yaml", process_mock)
    result = _fake_run_result(
        {
            "type": "REPLACE_WORKFLOW",
            "user_response": "Here is the workflow.",
            "workflow_yaml": """
workflow_definition:
  blocks:
    - block_type: navigation
      label: login
      navigation_goal: Type password: hunter2 into the password field.
""",
        }
    )

    agent_result = agent_module._translate_to_agent_result(
        result,
        _ctx(),
        global_llm_context=None,
        chat_request=_chat_request(),
        organization_id="org-1",
    )

    assert agent_result.response_type == "ASK_QUESTION"
    assert agent_result.updated_workflow is None
    assert agent_result.clear_proposed_workflow is True
    process_mock.assert_not_called()


def test_translate_to_agent_result_blocks_raw_secret_final_text() -> None:
    result = _fake_run_result({"type": "REPLY", "user_response": "I used password: hunter2."})

    agent_result = agent_module._translate_to_agent_result(
        result,
        _ctx(),
        global_llm_context=None,
        chat_request=_chat_request(),
        organization_id="org-1",
    )

    assert agent_result.response_type == "ASK_QUESTION"
    assert agent_result.updated_workflow is None
    assert agent_result.clear_proposed_workflow is True
    assert "hunter2" not in agent_result.user_response
    assert "DO NOT PROVIDE RAW LOGIN/PASSWORD" in agent_result.user_response


def test_translate_to_agent_result_rewrites_unbacked_workflow_claim() -> None:
    result = _fake_run_result({"type": "REPLY", "user_response": "I've drafted a workflow for you."})

    agent_result = agent_module._translate_to_agent_result(
        result,
        _ctx(),
        global_llm_context=None,
        chat_request=_chat_request(),
        organization_id="org-1",
    )

    assert "wasn't able to produce a workflow proposal" in agent_result.user_response
    assert agent_result.updated_workflow is None


def test_translate_to_agent_result_adds_unvalidated_affordance() -> None:
    workflow = SimpleNamespace(name="draft")
    result = _fake_run_result({"type": "REPLY", "user_response": "I drafted the workflow."})

    agent_result = agent_module._translate_to_agent_result(
        result,
        _ctx(last_workflow=workflow, last_workflow_yaml="title: draft", last_test_ok=None),
        global_llm_context=None,
        chat_request=_chat_request(),
        organization_id="org-1",
    )

    assert agent_result.updated_workflow is workflow
    assert "Accept to save" in agent_result.user_response
    assert "Reject to discard" in agent_result.user_response
