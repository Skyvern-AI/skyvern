from __future__ import annotations

from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest
from pydantic import ValidationError

from skyvern.forge.sdk.copilot.agent import (
    RequestPolicyGuardrailInputs,
    _docs_answer_turn_directive,
    _native_tools_for_turn,
    _store_request_policy_on_context,
)
from skyvern.forge.sdk.copilot.context import CopilotContext, StructuredContext
from skyvern.forge.sdk.copilot.request_policy import RequestPolicy
from skyvern.forge.sdk.copilot.turn_intent import (
    UNRESOLVED_BLOCK_REF_TARGET_ENTITY,
    RequiredContextKey,
    TurnIntent,
    TurnIntentAuthority,
    TurnIntentMode,
    TurnIntentReasonCode,
    _has_structured_prior_run_signal,
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


def _ai_message(content: str) -> WorkflowCopilotChatHistoryMessage:
    return WorkflowCopilotChatHistoryMessage(
        sender=WorkflowCopilotChatSender.AI,
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
        "may_read_run_context": False,
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


_BLANK_SAVED_WORKFLOW_YAML = "title: Saved blank workflow\nworkflow_definition:\n  blocks: []\n  parameters: []\n"


def test_build_turn_intent_marks_blank_saved_workflow_browser_task_with_results_word_as_build() -> None:
    intent = build_turn_intent(
        user_message=(
            "Go to https://example.test/registry, search for ABC-1234, expand the entry, "
            "and report the credential type and expiration date from the results."
        ),
        workflow_yaml=_BLANK_SAVED_WORKFLOW_YAML,
        chat_history=[],
        global_llm_context="",
        request_policy=RequestPolicy(),
    )

    assert intent.mode == TurnIntentMode.BUILD
    assert intent.authority.may_update_workflow is True
    assert intent.authority.may_run_blocks is True
    assert RequiredContextKey.LATEST_RUN_RESULT not in intent.required_context


def test_build_turn_intent_marks_create_workflow_with_browser_task_and_results_as_build() -> None:
    intent = build_turn_intent(
        user_message=("Create a workflow that opens https://example.test and reports the results."),
        workflow_yaml=_BLANK_SAVED_WORKFLOW_YAML,
        chat_history=[],
        global_llm_context="",
        request_policy=RequestPolicy(),
    )

    assert intent.mode == TurnIntentMode.BUILD
    assert intent.authority.may_update_workflow is True
    assert intent.authority.may_run_blocks is True
    assert RequiredContextKey.LATEST_RUN_RESULT not in intent.required_context


def test_build_turn_intent_keeps_clear_diagnose_on_blank_saved_workflow_with_browser_task_verb() -> None:
    intent = build_turn_intent(
        user_message="Open the failed page and tell me what went wrong.",
        workflow_yaml=_BLANK_SAVED_WORKFLOW_YAML,
        chat_history=[],
        global_llm_context="",
        request_policy=RequestPolicy(),
    )

    assert intent.mode == TurnIntentMode.DIAGNOSE


def test_build_turn_intent_keeps_run_result_diagnose_with_build_keyword_on_existing_workflow() -> None:
    existing_workflow_yaml = (
        "title: Existing\nworkflow_definition:\n  blocks:\n    - block_type: navigation\n      label: nav_to_site\n"
    )
    intent = build_turn_intent(
        user_message="Generate a report from the last result.",
        workflow_yaml=existing_workflow_yaml,
        chat_history=[],
        global_llm_context="",
        request_policy=RequestPolicy(),
    )

    assert intent.mode == TurnIntentMode.DIAGNOSE


def test_build_turn_intent_keeps_run_result_diagnose_when_no_browser_task_verb() -> None:
    intent = build_turn_intent(
        user_message="What is the result of the last run?",
        workflow_yaml=_BLANK_SAVED_WORKFLOW_YAML,
        chat_history=[],
        global_llm_context="",
        request_policy=RequestPolicy(),
    )

    assert intent.mode == TurnIntentMode.DIAGNOSE


def test_build_turn_intent_keeps_diagnose_when_run_id_attached_on_blank_saved_workflow() -> None:
    intent = build_turn_intent(
        user_message="Open the result.",
        workflow_yaml=_BLANK_SAVED_WORKFLOW_YAML,
        chat_history=[],
        global_llm_context="",
        request_policy=RequestPolicy(),
        workflow_run_id="wr_abc123",
    )

    assert intent.mode == TurnIntentMode.DIAGNOSE
    assert RequiredContextKey.LATEST_RUN_RESULT in intent.required_context


def test_build_turn_intent_routes_docs_question_with_browser_task_verb_on_blank_saved_workflow() -> None:
    intent = build_turn_intent(
        user_message="How do I navigate to my workflow?",
        workflow_yaml=_BLANK_SAVED_WORKFLOW_YAML,
        chat_history=[],
        global_llm_context="",
        request_policy=RequestPolicy(),
    )

    assert intent.mode == TurnIntentMode.DOCS_ANSWER
    assert intent.authority.may_update_workflow is False
    assert intent.authority.may_run_blocks is False


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
    assert intent.target_entities["workflow"] == ["current_workflow"]


def test_build_turn_intent_records_unresolved_explicit_block_refs() -> None:
    intent = build_turn_intent(
        user_message="WF_trigger_SSO_login worked but update_card is not receiving browser state.",
        workflow_yaml="""
title: Public SSO login cleanup
workflow_definition:
  blocks:
    - block_type: goto_url
      label: navigate_to_SSO
    - block_type: navigation
      label: block_placeholder
""",
        chat_history=[],
        global_llm_context="",
        request_policy=RequestPolicy(),
    )

    assert intent.mode == TurnIntentMode.EDIT
    assert intent.target_entities[UNRESOLVED_BLOCK_REF_TARGET_ENTITY] == [
        "WF_trigger_SSO_login",
        "update_card",
    ]


def test_build_turn_intent_does_not_treat_snake_case_fields_as_unresolved_block_refs() -> None:
    intent = build_turn_intent(
        user_message="Update the workflow so the last_name field is required.",
        workflow_yaml="""
title: Existing
workflow_definition:
  blocks:
    - block_type: navigation
      label: update_form
""",
        chat_history=[],
        global_llm_context="",
        request_policy=RequestPolicy(),
    )

    assert intent.mode == TurnIntentMode.EDIT
    assert UNRESOLVED_BLOCK_REF_TARGET_ENTITY not in intent.target_entities


def test_build_turn_intent_carries_docs_mode_onto_bare_confirmation() -> None:
    intent = build_turn_intent(
        user_message="I confirm.",
        workflow_yaml="blocks: []",
        chat_history=[
            _user_message("Explain how workflow parameters and webhooks work."),
            _ai_message("Do you want me to explain parameters, or modify the workflow?"),
        ],
        global_llm_context="",
        request_policy=RequestPolicy(),
    )

    assert intent.mode == TurnIntentMode.DOCS_ANSWER
    assert intent.authority.may_update_workflow is False
    assert intent.authority.may_run_blocks is False
    assert RequiredContextKey.DOCS_CONTEXT in intent.required_context
    assert TurnIntentReasonCode.CONFIRMATION_CARRYOVER in intent.reason_codes


def test_build_turn_intent_carries_mode_from_most_recent_classifiable_prior_turn() -> None:
    intent = build_turn_intent(
        user_message="yes",
        workflow_yaml="blocks: []",
        chat_history=[
            _user_message("Explain how I can call this workflow from an external tool."),
            _ai_message("Is your goal to modify the workflow, or do you want information?"),
            _user_message("I just want to know whether the parameters are fixed."),
            _ai_message("Do you confirm you want an explanation of parameters and webhooks?"),
        ],
        global_llm_context="",
        request_policy=RequestPolicy(),
    )

    assert intent.mode == TurnIntentMode.DOCS_ANSWER
    assert TurnIntentReasonCode.CONFIRMATION_CARRYOVER in intent.reason_codes


def test_build_turn_intent_carries_build_mode_onto_confirmation() -> None:
    intent = build_turn_intent(
        user_message="go ahead",
        workflow_yaml="",
        chat_history=[_user_message("Build a workflow that downloads my invoices.")],
        global_llm_context="",
        request_policy=RequestPolicy(),
    )

    assert intent.mode == TurnIntentMode.BUILD
    assert TurnIntentReasonCode.CONFIRMATION_CARRYOVER in intent.reason_codes


def test_build_turn_intent_does_not_carry_over_when_message_is_not_bare_affirmative() -> None:
    intent = build_turn_intent(
        user_message="yes, but change the target URL first",
        workflow_yaml="blocks: []",
        chat_history=[_user_message("Explain how parameters work.")],
        global_llm_context="",
        request_policy=RequestPolicy(),
    )

    assert intent.mode != TurnIntentMode.DOCS_ANSWER
    assert TurnIntentReasonCode.CONFIRMATION_CARRYOVER not in intent.reason_codes


def test_build_turn_intent_confirmation_without_classifiable_prior_stays_unknown() -> None:
    intent = build_turn_intent(
        user_message="I confirm.",
        workflow_yaml="blocks: []",
        chat_history=[_user_message("I confirm.")],
        global_llm_context="",
        request_policy=RequestPolicy(),
    )

    assert intent.mode == TurnIntentMode.UNKNOWN
    assert TurnIntentReasonCode.CONFIRMATION_CARRYOVER not in intent.reason_codes


def test_build_turn_intent_raw_secret_outranks_confirmation_carryover() -> None:
    policy = RequestPolicy(
        credential_input_kind="raw_secret",
        raw_secret_detected=True,
        user_response_policy="ask_clarification",
        allow_update_workflow=False,
        allow_run_blocks=False,
    )
    intent = build_turn_intent(
        user_message="yes",
        workflow_yaml="blocks: []",
        chat_history=[_user_message("Build a workflow that downloads invoices.")],
        global_llm_context="",
        request_policy=policy,
    )

    assert intent.mode == TurnIntentMode.REFUSE


def test_docs_answer_turn_directive_renders_only_for_docs_answer_mode() -> None:
    docs_directive = _docs_answer_turn_directive(TurnIntent(mode=TurnIntentMode.DOCS_ANSWER))
    assert "TURN INTENT: docs_answer" in docs_directive
    assert "Answer it inline in the user's language" in docs_directive
    assert "do not offer to build an example workflow" in docs_directive

    assert _docs_answer_turn_directive(TurnIntent(mode=TurnIntentMode.BUILD)) == ""
    assert _docs_answer_turn_directive(TurnIntent(mode=TurnIntentMode.EDIT)) == ""
    assert _docs_answer_turn_directive(None) == ""


def test_build_turn_intent_requests_workflow_change_when_prior_assistant_turn_exists() -> None:
    ai_turn = WorkflowCopilotChatHistoryMessage(
        sender=WorkflowCopilotChatSender.AI,
        content="Drafted v1",
        created_at=datetime.now(timezone.utc),
    )
    intent = build_turn_intent(
        user_message="I did it myself, does this look right?",
        workflow_yaml="blocks: []",
        chat_history=[_user_message("Build a workflow"), ai_turn],
        global_llm_context="",
        request_policy=RequestPolicy(),
    )

    assert RequiredContextKey.WORKFLOW_CHANGE in intent.required_context


def test_build_turn_intent_omits_workflow_change_on_first_turn() -> None:
    intent = build_turn_intent(
        user_message="Build a workflow",
        workflow_yaml="",
        chat_history=[],
        global_llm_context="",
        request_policy=RequestPolicy(),
    )

    assert RequiredContextKey.WORKFLOW_CHANGE not in intent.required_context


def _ai_message(text: str) -> WorkflowCopilotChatHistoryMessage:
    return WorkflowCopilotChatHistoryMessage(
        sender=WorkflowCopilotChatSender.AI,
        content=text,
        created_at=datetime.now(timezone.utc),
    )


def test_user_non_progress_fires_on_stuck_marker() -> None:
    intent = build_turn_intent(
        user_message="i can't see it",
        workflow_yaml="",
        chat_history=[_user_message("where is my downloaded file"), _ai_message("Check the Artifacts panel.")],
        global_llm_context="",
        request_policy=RequestPolicy(),
    )
    assert TurnIntentReasonCode.USER_NON_PROGRESS in intent.reason_codes


def test_user_non_progress_fires_on_short_restatement() -> None:
    intent = build_turn_intent(
        user_message="where's my downloaded file?",
        workflow_yaml="",
        chat_history=[_user_message("where is my downloaded file"), _ai_message("Check the Artifacts panel.")],
        global_llm_context="",
        request_policy=RequestPolicy(),
    )
    assert TurnIntentReasonCode.USER_NON_PROGRESS in intent.reason_codes


def test_user_non_progress_does_not_fire_on_topic_switch_after_ai_reply() -> None:
    """Regression: 'where is my X' is a topic switch, not non-progress, even with prior AI."""
    intent = build_turn_intent(
        user_message="where is my API key stored?",
        workflow_yaml="",
        chat_history=[_user_message("build me a workflow"), _ai_message("Drafted v1.")],
        global_llm_context="",
        request_policy=RequestPolicy(),
    )
    assert TurnIntentReasonCode.USER_NON_PROGRESS not in intent.reason_codes


def test_user_non_progress_does_not_fire_on_first_turn_even_when_marker_matches() -> None:
    """Regression: a first-turn 'where is my X' is a real question, not non-progress."""
    intent = build_turn_intent(
        user_message="where is my API key stored?",
        workflow_yaml="",
        chat_history=[],
        global_llm_context="",
        request_policy=RequestPolicy(),
    )
    assert TurnIntentReasonCode.USER_NON_PROGRESS not in intent.reason_codes


def test_user_non_progress_does_not_fire_on_topic_switch() -> None:
    intent = build_turn_intent(
        user_message="now build me a workflow for invoicing",
        workflow_yaml="",
        chat_history=[_user_message("where is my downloaded file"), _ai_message("Check the Artifacts panel.")],
        global_llm_context="",
        request_policy=RequestPolicy(),
    )
    assert TurnIntentReasonCode.USER_NON_PROGRESS not in intent.reason_codes


def test_user_non_progress_does_not_fire_on_legitimate_followup() -> None:
    intent = build_turn_intent(
        user_message="where is the documentation for loop blocks?",
        workflow_yaml="",
        chat_history=[_user_message("build me a workflow"), _ai_message("Drafted v1.")],
        global_llm_context="",
        request_policy=RequestPolicy(),
    )
    assert TurnIntentReasonCode.USER_NON_PROGRESS not in intent.reason_codes


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


def test_answer_only_turn_intent_keeps_native_tools_registered() -> None:
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

    assert filtered == tools


def test_build_turn_intent_marks_declarative_step_request_as_edit() -> None:
    intent = build_turn_intent(
        user_message="I need a step where the page scrolls to the right.",
        workflow_yaml="blocks: []",
        chat_history=[],
        global_llm_context="",
        request_policy=RequestPolicy(allow_run_blocks=False),
    )

    assert intent.mode == TurnIntentMode.EDIT


def test_build_turn_intent_marks_create_step_request_as_edit() -> None:
    intent = build_turn_intent(
        user_message="create a step in my workflow where the page scrolls to the right",
        workflow_yaml="blocks: []",
        chat_history=[],
        global_llm_context="",
        request_policy=RequestPolicy(allow_run_blocks=False),
    )

    assert intent.mode == TurnIntentMode.EDIT


def test_build_turn_intent_marks_declarative_plural_step_request_as_edit() -> None:
    intent = build_turn_intent(
        user_message="I need steps that log in and download the report.",
        workflow_yaml="blocks: []",
        chat_history=[],
        global_llm_context="",
        request_policy=RequestPolicy(allow_run_blocks=False),
    )

    assert intent.mode == TurnIntentMode.EDIT


def test_build_turn_intent_marks_another_step_request_as_edit() -> None:
    intent = build_turn_intent(
        user_message="I need another step after the login block.",
        workflow_yaml="blocks: []",
        chat_history=[],
        global_llm_context="",
        request_policy=RequestPolicy(allow_run_blocks=False),
    )

    assert intent.mode == TurnIntentMode.EDIT


def test_build_turn_intent_marks_declarative_step_request_as_build_without_workflow() -> None:
    intent = build_turn_intent(
        user_message="I want a step that clicks the login button.",
        workflow_yaml="",
        chat_history=[],
        global_llm_context="",
        request_policy=RequestPolicy(),
    )

    assert intent.mode == TurnIntentMode.BUILD


def test_build_turn_intent_marks_leading_add_step_as_edit() -> None:
    intent = build_turn_intent(
        user_message="add a step that downloads the invoice PDF",
        workflow_yaml="blocks: []",
        chat_history=[],
        global_llm_context="",
        request_policy=RequestPolicy(allow_run_blocks=False),
    )

    assert intent.mode == TurnIntentMode.EDIT


def test_build_turn_intent_does_not_classify_leading_remove_named_block_as_edit() -> None:
    intent = build_turn_intent(
        user_message="remove the login block",
        workflow_yaml="blocks: []",
        chat_history=[],
        global_llm_context="",
        request_policy=RequestPolicy(allow_run_blocks=False),
    )

    assert intent.mode == TurnIntentMode.UNKNOWN


def test_build_turn_intent_leading_add_with_failure_clause_is_edit() -> None:
    intent = build_turn_intent(
        user_message="Add a step that fails if the invoice total is missing.",
        workflow_yaml="blocks: []",
        chat_history=[],
        global_llm_context="",
        request_policy=RequestPolicy(allow_run_blocks=False),
    )

    assert intent.mode == TurnIntentMode.EDIT


def test_build_turn_intent_leading_nouny_docs_question_stays_docs_answer() -> None:
    intent = build_turn_intent(
        user_message="delete is a step type, what does it do?",
        workflow_yaml="blocks: []",
        chat_history=[],
        global_llm_context="",
        request_policy=RequestPolicy(),
    )

    assert intent.mode == TurnIntentMode.DOCS_ANSWER
    assert intent.authority.may_update_workflow is False


def test_build_turn_intent_leading_delete_step_docs_question_stays_docs_answer() -> None:
    intent = build_turn_intent(
        user_message="delete step - what does it do?",
        workflow_yaml="blocks: []",
        chat_history=[],
        global_llm_context="",
        request_policy=RequestPolicy(),
    )

    assert intent.mode == TurnIntentMode.DOCS_ANSWER
    assert intent.authority.may_update_workflow is False


def test_build_turn_intent_docs_question_with_nouny_edit_word_stays_docs_answer() -> None:
    intent = build_turn_intent(
        user_message="What does the delete step do?",
        workflow_yaml="blocks: []",
        chat_history=[],
        global_llm_context="",
        request_policy=RequestPolicy(),
    )

    assert intent.mode == TurnIntentMode.DOCS_ANSWER
    assert intent.authority.may_update_workflow is False


def test_build_turn_intent_failure_report_with_nouny_step_name_is_diagnose() -> None:
    intent = build_turn_intent(
        user_message="The delete step failed.",
        workflow_yaml="blocks: []",
        chat_history=[],
        global_llm_context="",
        request_policy=RequestPolicy(allow_update_workflow=False, allow_run_blocks=False),
    )

    assert intent.mode == TurnIntentMode.DIAGNOSE


def test_build_turn_intent_leading_nouny_failure_report_is_diagnose() -> None:
    intent = build_turn_intent(
        user_message="delete step failed.",
        workflow_yaml="blocks: []",
        chat_history=[],
        global_llm_context="",
        request_policy=RequestPolicy(allow_update_workflow=False, allow_run_blocks=False),
    )

    assert intent.mode == TurnIntentMode.DIAGNOSE


def test_build_turn_intent_leading_nouny_block_failure_report_is_diagnose() -> None:
    intent = build_turn_intent(
        user_message="Remove block failed after login.",
        workflow_yaml="blocks: []",
        chat_history=[],
        global_llm_context="",
        request_policy=RequestPolicy(allow_update_workflow=False, allow_run_blocks=False),
    )

    assert intent.mode == TurnIntentMode.DIAGNOSE


def test_build_turn_intent_does_not_mutate_on_existing_block_reference() -> None:
    intent = build_turn_intent(
        user_message="thanks, that step looks great",
        workflow_yaml="blocks: []",
        chat_history=[],
        global_llm_context="",
        request_policy=RequestPolicy(),
    )

    assert intent.mode not in (TurnIntentMode.EDIT, TurnIntentMode.BUILD)


def test_build_turn_intent_docs_question_with_structure_noun_stays_docs_answer() -> None:
    intent = build_turn_intent(
        user_message="Can you tell me what a block is supposed to do?",
        workflow_yaml="blocks: []",
        chat_history=[],
        global_llm_context="",
        request_policy=RequestPolicy(),
    )

    assert intent.mode == TurnIntentMode.DOCS_ANSWER
    assert intent.authority.may_update_workflow is False


def test_build_turn_intent_structure_reference_question_does_not_mutate() -> None:
    intent = build_turn_intent(
        user_message="Where should a step go in the workflow?",
        workflow_yaml="blocks: []",
        chat_history=[],
        global_llm_context="",
        request_policy=RequestPolicy(),
    )

    assert intent.mode not in (TurnIntentMode.EDIT, TurnIntentMode.BUILD)


def _structured_context_with_run_decision() -> str:
    return StructuredContext(
        decisions_made=[
            "run_blocks_and_collect_debug: ran 3 blocks, hit per-tool-call budget",
            "  output: block_extract: {'rbt_certified': true}",
        ],
    ).to_json_str()


def test_build_turn_intent_unknown_with_workflow_run_id_upgrades_to_diagnose() -> None:
    intent = build_turn_intent(
        user_message="please continue",
        workflow_yaml="",
        chat_history=[],
        global_llm_context="",
        request_policy=RequestPolicy(),
        workflow_run_id="wr_test_123",
    )

    assert intent.mode == TurnIntentMode.DIAGNOSE
    assert TurnIntentReasonCode.RECOVERY_FROM_RUN_CONTEXT in intent.reason_codes
    assert intent.authority.may_read_run_context is True
    assert intent.authority.may_update_workflow is False
    assert intent.authority.may_run_blocks is False


def test_build_turn_intent_unknown_with_persisted_prior_run_signal_upgrades_to_diagnose() -> None:
    intent = build_turn_intent(
        user_message="please continue",
        workflow_yaml="",
        chat_history=[],
        global_llm_context=_structured_context_with_run_decision(),
        request_policy=RequestPolicy(),
    )

    assert intent.mode == TurnIntentMode.DIAGNOSE
    assert TurnIntentReasonCode.RECOVERY_FROM_RUN_CONTEXT in intent.reason_codes
    assert intent.authority.may_read_run_context is True


def test_build_turn_intent_unknown_with_persisted_prior_failed_run_upgrades_to_diagnose() -> None:
    structured_context = StructuredContext()
    structured_context.merge_turn_summary(
        [
            {
                "tool": "update_and_run_blocks",
                "summary": "Failed: The run exceeded the 240s per-tool-call budget. Run ID: wr_budget_123.",
            }
        ]
    )
    global_llm_context = structured_context.to_json_str()
    assert _has_structured_prior_run_signal(global_llm_context) is True

    intent = build_turn_intent(
        user_message="please continue",
        workflow_yaml="",
        chat_history=[],
        global_llm_context=global_llm_context,
        request_policy=RequestPolicy(),
    )

    assert intent.mode == TurnIntentMode.DIAGNOSE
    assert TurnIntentReasonCode.RECOVERY_FROM_RUN_CONTEXT in intent.reason_codes
    assert intent.authority.may_read_run_context is True


def test_build_turn_intent_unknown_without_run_signals_stays_unknown() -> None:
    intent = build_turn_intent(
        user_message="please continue",
        workflow_yaml="",
        chat_history=[],
        global_llm_context="",
        request_policy=RequestPolicy(),
    )

    assert intent.mode == TurnIntentMode.UNKNOWN
    assert TurnIntentReasonCode.RECOVERY_FROM_RUN_CONTEXT not in intent.reason_codes
    assert intent.authority.may_read_run_context is False


def test_build_turn_intent_docs_answer_keeps_read_context_false_even_with_run_id() -> None:
    intent = build_turn_intent(
        user_message="How do parameters work?",
        workflow_yaml="",
        chat_history=[],
        global_llm_context="",
        request_policy=RequestPolicy(),
        workflow_run_id="wr_test_123",
    )

    assert intent.mode == TurnIntentMode.DOCS_ANSWER
    assert intent.authority.may_read_run_context is False


def test_build_turn_intent_explicit_run_results_outranks_skip_test_policy() -> None:
    intent = build_turn_intent(
        user_message="Call get_run_results with workflow_run_id wr_test_123. Do not run blocks.",
        workflow_yaml="title: Existing workflow\nworkflow_definition:\n  blocks: []\n",
        chat_history=[],
        global_llm_context="",
        request_policy=RequestPolicy(testing_intent="skip_test", allow_update_workflow=True, allow_run_blocks=False),
        workflow_run_id="wr_test_123",
    )

    assert intent.mode == TurnIntentMode.DIAGNOSE
    assert intent.authority.may_read_run_context is True
    assert intent.authority.may_update_workflow is False
    assert intent.authority.may_run_blocks is False


def test_build_turn_intent_diagnose_grants_may_read_run_context_without_run_id() -> None:
    intent = build_turn_intent(
        user_message="Diagnose the failure.",
        workflow_yaml="",
        chat_history=[],
        global_llm_context="",
        request_policy=RequestPolicy(),
    )

    assert intent.mode == TurnIntentMode.DIAGNOSE
    assert intent.authority.may_read_run_context is True


def test_has_structured_prior_run_signal_ignores_workflow_state_only() -> None:
    payload = StructuredContext(workflow_state="updated workflow yaml diff summary").to_json_str()
    assert _has_structured_prior_run_signal(payload) is False


def test_has_structured_prior_run_signal_ignores_unrelated_decisions() -> None:
    payload = StructuredContext(
        decisions_made=["click: clicked submit", "evaluate: returned true"],
    ).to_json_str()
    assert _has_structured_prior_run_signal(payload) is False


def test_has_structured_prior_run_signal_handles_malformed_json() -> None:
    assert _has_structured_prior_run_signal("{not valid json") is False
    assert _has_structured_prior_run_signal("") is False


def test_has_structured_prior_run_signal_ignores_failed_tool_attempt_without_output() -> None:
    # The merger appends a `<tool>: Failed: ...` entry for blocked/loop-rejected
    # calls, but no `  output:` line (hooks.py only sets output_preview on
    # ok=True). Failed attempts must not trigger recovery.
    payload = StructuredContext(
        decisions_made=["run_blocks_and_collect_debug: Failed: blocked by policy"],
    ).to_json_str()
    assert _has_structured_prior_run_signal(payload) is False


def test_has_structured_prior_run_signal_matches_output_preview_line() -> None:
    payload = StructuredContext(
        decisions_made=[
            "get_run_results: workflow completed",
            "  output: block_extract: {'result': 'ok'}",
        ],
    ).to_json_str()
    assert _has_structured_prior_run_signal(payload) is True
