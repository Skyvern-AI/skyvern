from __future__ import annotations

from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest
from pydantic import ValidationError

from skyvern.forge.sdk.copilot.agent import (
    RequestPolicyGuardrailInputs,
    _native_tools_for_turn,
    _store_request_policy_on_context,
)
from skyvern.forge.sdk.copilot.context import CopilotContext
from skyvern.forge.sdk.copilot.request_policy import RequestPolicy
from skyvern.forge.sdk.copilot.turn_intent import (
    RequiredContextKey,
    TurnIntent,
    TurnIntentAuthority,
    TurnIntentMode,
    TurnIntentReasonCode,
    build_turn_intent,
)
from skyvern.forge.sdk.schemas.workflow_copilot import (
    WorkflowCopilotChatHistoryMessage,
    WorkflowCopilotChatSender,
)


def _user_message(content: str) -> WorkflowCopilotChatHistoryMessage:
    return WorkflowCopilotChatHistoryMessage(
        sender=WorkflowCopilotChatSender.USER,
        content=content,
        created_at=datetime.now(timezone.utc),
    )


def test_turn_intent_defaults_to_unknown_shadow_contract() -> None:
    intent = TurnIntent()

    assert intent.mode == TurnIntentMode.UNKNOWN
    assert intent.user_goal == ""
    assert intent.authority == TurnIntentAuthority()
    assert intent.required_context == []
    assert intent.reason_codes == [TurnIntentReasonCode.DEFAULT_UNKNOWN]


def test_turn_intent_validates_confidence_bounds() -> None:
    with pytest.raises(ValidationError):
        TurnIntent(confidence=1.1)


def test_turn_intent_trace_data_omits_raw_goal_and_target_values() -> None:
    intent = TurnIntent(
        mode=TurnIntentMode.EDIT,
        user_goal="Use password: hunter2 to update the workflow",
        target_entities={"workflow": ["w_123"], "credential": ["cred_sensitive"]},
        required_context=[RequiredContextKey.CURRENT_WORKFLOW, RequiredContextKey.CREDENTIAL_METADATA],
        authority=TurnIntentAuthority(may_update_workflow=True, may_run_blocks=False),
        confidence=0.75,
        missing_context_question="Which saved credential should I use?",
        reason_codes=[TurnIntentReasonCode.REQUEST_POLICY_DERIVED],
    )

    trace_data = intent.to_trace_data()

    assert trace_data == {
        "mode": "edit",
        "expected_output": "workflow_update",
        "required_context": ["current_workflow", "credential_metadata"],
        "may_update_workflow": True,
        "may_run_blocks": False,
        "may_answer_without_mutation": True,
        "requires_user_input": False,
        "confidence": 0.75,
        "reason_codes": ["request_policy_derived"],
        "target_entity_types": ["credential", "workflow"],
        "has_missing_context_question": True,
    }
    assert "hunter2" not in repr(trace_data)
    assert "cred_sensitive" not in repr(trace_data)


def test_build_turn_intent_uses_request_policy_clarification_without_changing_policy() -> None:
    policy = RequestPolicy(
        user_response_policy="ask_clarification",
        allow_update_workflow=False,
        allow_run_blocks=False,
        clarification_question="Which page should I target?",
        clarification_reason="missing_target_context",
    )

    intent = build_turn_intent(
        user_message="Update this workflow",
        workflow_yaml="blocks: []",
        chat_history=[_user_message("Build a workflow")],
        global_llm_context="",
        request_policy=policy,
    )

    assert intent.mode == TurnIntentMode.CLARIFY
    assert intent.authority.requires_user_input is True
    assert intent.authority.may_update_workflow is False
    assert intent.authority.may_run_blocks is False
    assert intent.missing_context_question == "Which page should I target?"
    assert RequiredContextKey.CURRENT_WORKFLOW in intent.required_context
    assert TurnIntentReasonCode.REQUEST_POLICY_CLARIFICATION in intent.reason_codes


def test_build_turn_intent_routes_raw_secret_to_refuse_mode() -> None:
    policy = RequestPolicy(
        credential_input_kind="raw_secret",
        raw_secret_detected=True,
        user_response_policy="ask_clarification",
        allow_update_workflow=False,
        allow_run_blocks=False,
        clarification_question="Store the credential in the Credentials UI.",
    )

    intent = build_turn_intent(
        user_message="Use this password: hunter2 to sign in.",
        workflow_yaml="",
        chat_history=[],
        global_llm_context="",
        request_policy=policy,
    )

    assert intent.mode == TurnIntentMode.REFUSE
    assert intent.authority.may_update_workflow is False
    assert intent.authority.may_run_blocks is False
    assert intent.authority.requires_user_input is True
    assert intent.missing_context_question == "Store the credential in the Credentials UI."
    assert TurnIntentReasonCode.RAW_SECRET_REFUSAL in intent.reason_codes


def test_build_turn_intent_redacts_user_goal() -> None:
    intent = build_turn_intent(
        user_message="Use password: hunter2 and build the workflow",
        workflow_yaml="",
        chat_history=[],
        global_llm_context="",
        request_policy=RequestPolicy(),
    )

    assert "hunter2" not in intent.user_goal
    assert "[REDACTED_SECRET]" in intent.user_goal


def test_build_turn_intent_marks_docs_context_for_docs_answer() -> None:
    intent = build_turn_intent(
        user_message="Why does a loop need a condition?",
        workflow_yaml="",
        chat_history=[],
        global_llm_context="",
        request_policy=RequestPolicy(),
    )

    assert intent.mode == TurnIntentMode.DOCS_ANSWER
    assert intent.authority.may_update_workflow is False
    assert intent.authority.may_run_blocks is False
    assert RequiredContextKey.DOCS_CONTEXT in intent.required_context


def test_build_turn_intent_marks_platform_comparison_as_docs_answer() -> None:
    intent = build_turn_intent(
        user_message="I meant run with code vs run with agent",
        workflow_yaml="blocks: []",
        chat_history=[],
        global_llm_context="",
        request_policy=RequestPolicy(),
    )

    assert intent.mode == TurnIntentMode.DOCS_ANSWER
    assert intent.authority.may_update_workflow is False
    assert intent.authority.may_run_blocks is False
    assert RequiredContextKey.DOCS_CONTEXT in intent.required_context


def test_build_turn_intent_marks_blank_workflow_browser_task_as_build() -> None:
    intent = build_turn_intent(
        user_message="Go to https://en.wikipedia.org and search for Bauhaus.",
        workflow_yaml="",
        chat_history=[],
        global_llm_context="",
        request_policy=RequestPolicy(),
    )

    assert intent.mode == TurnIntentMode.BUILD
    assert intent.authority.may_update_workflow is True
    assert intent.authority.may_run_blocks is True


def test_build_turn_intent_marks_run_context_for_diagnose() -> None:
    intent = build_turn_intent(
        user_message="Diagnose the failed run",
        workflow_yaml="blocks: []",
        chat_history=[],
        global_llm_context="",
        request_policy=RequestPolicy(),
    )

    assert intent.mode == TurnIntentMode.DIAGNOSE
    assert intent.authority.may_update_workflow is False
    assert intent.authority.may_run_blocks is False
    assert RequiredContextKey.LATEST_RUN_RESULT in intent.required_context


def test_build_turn_intent_keeps_explicit_fix_as_edit() -> None:
    intent = build_turn_intent(
        user_message="Fix the error after login.",
        workflow_yaml="blocks: []",
        chat_history=[],
        global_llm_context="",
        request_policy=RequestPolicy(),
    )

    assert intent.mode == TurnIntentMode.EDIT
    assert intent.authority.may_update_workflow is True


def test_store_request_policy_attaches_turn_intent_to_context() -> None:
    ctx = CopilotContext(
        organization_id="org-1",
        workflow_id="wf-1",
        workflow_permanent_id="wfp-1",
        workflow_yaml="",
        browser_session_id=None,
        stream=MagicMock(),
    )
    policy = RequestPolicy(allow_update_workflow=True, allow_run_blocks=False)
    inputs = RequestPolicyGuardrailInputs(
        user_message="Explain why the last run failed",
        workflow_yaml="blocks: []",
        chat_history_text="",
        chat_history_messages=[],
        global_llm_context="",
        organization_id="org-1",
        handler=None,
        previous_user_message=None,
    )

    _store_request_policy_on_context(ctx, policy, inputs)

    assert ctx.turn_intent is not None
    assert ctx.turn_intent.authority.may_update_workflow is False
    assert ctx.turn_intent.authority.may_run_blocks is False


def test_answer_only_turn_intent_hides_get_run_results_tool() -> None:
    tools = [
        SimpleNamespace(name="update_workflow"),
        SimpleNamespace(name="get_run_results"),
        SimpleNamespace(name="list_credentials"),
    ]
    intent = TurnIntent(
        mode=TurnIntentMode.DIAGNOSE,
        authority=TurnIntentAuthority(may_update_workflow=False, may_run_blocks=False),
    )

    filtered = _native_tools_for_turn(tools, intent)

    assert filtered == []
