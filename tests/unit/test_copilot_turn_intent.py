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
    PROMPT_NAME,
    UNRESOLVED_BLOCK_REF_TARGET_ENTITY,
    RequiredContextKey,
    TurnIntent,
    TurnIntentAuthority,
    TurnIntentClassification,
    TurnIntentExpectedOutput,
    TurnIntentMode,
    TurnIntentReasonCode,
    _has_structured_prior_run_signal,
    _turn_intent_classification_from_raw,
    build_turn_intent,
    classify_turn_intent,
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


def _classification(
    mode: TurnIntentMode,
    *,
    expected_output: TurnIntentExpectedOutput | None = None,
    required_context: list[RequiredContextKey] | None = None,
    confidence: float = 0.8,
    target_entities: dict[str, list[str]] | None = None,
    missing_context_question: str | None = None,
    reason_codes: list[TurnIntentReasonCode] | None = None,
) -> TurnIntentClassification:
    return TurnIntentClassification(
        mode=mode,
        expected_output=expected_output,
        required_context=required_context or [],
        confidence=confidence,
        target_entities=target_entities or {},
        missing_context_question=missing_context_question,
        reason_codes=reason_codes or [],
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


def test_build_turn_intent_does_not_keyword_classify_without_llm_result() -> None:
    intent = build_turn_intent(
        user_message="Build a workflow that opens https://example.test and downloads invoices.",
        workflow_yaml="",
        chat_history=[],
        global_llm_context="",
        request_policy=RequestPolicy(),
    )

    assert intent.mode == TurnIntentMode.UNKNOWN
    assert TurnIntentReasonCode.LLM_CLASSIFIER not in intent.reason_codes


def test_build_turn_intent_applies_llm_build_classification() -> None:
    intent = build_turn_intent(
        user_message="Create a workflow from the page I have open.",
        workflow_yaml="",
        chat_history=[],
        global_llm_context="",
        request_policy=RequestPolicy(),
        browser_session_id="pbs_123",
        classification=_classification(
            TurnIntentMode.BUILD,
            required_context=[RequiredContextKey.BROWSER_STATE],
            confidence=0.84,
        ),
    )

    assert intent.mode == TurnIntentMode.BUILD
    assert intent.expected_output == TurnIntentExpectedOutput.WORKFLOW_DRAFT
    assert intent.authority.may_update_workflow is True
    assert intent.authority.may_run_blocks is True
    assert RequiredContextKey.BROWSER_STATE in intent.required_context
    assert TurnIntentReasonCode.BROWSER_CONTEXT_PRESENT in intent.reason_codes
    assert TurnIntentReasonCode.LLM_CLASSIFIER in intent.reason_codes


def test_build_turn_intent_applies_docs_classification_without_mutation() -> None:
    intent = build_turn_intent(
        user_message="What does run with code mean?",
        workflow_yaml="title: Existing\nworkflow_definition:\n  blocks: []\n",
        chat_history=[],
        global_llm_context="",
        request_policy=RequestPolicy(),
        classification=_classification(TurnIntentMode.DOCS_ANSWER),
    )

    assert intent.mode == TurnIntentMode.DOCS_ANSWER
    assert intent.authority.may_update_workflow is False
    assert intent.authority.may_run_blocks is False
    assert RequiredContextKey.CURRENT_WORKFLOW in intent.required_context
    assert RequiredContextKey.DOCS_CONTEXT in intent.required_context


def test_build_turn_intent_applies_diagnose_classification_and_read_authority() -> None:
    intent = build_turn_intent(
        user_message="Tell me why the run failed.",
        workflow_yaml="title: Existing\nworkflow_definition:\n  blocks: []\n",
        chat_history=[],
        global_llm_context="",
        request_policy=RequestPolicy(allow_update_workflow=True, allow_run_blocks=True),
        workflow_run_id="wr_123",
        classification=_classification(TurnIntentMode.DIAGNOSE),
    )

    assert intent.mode == TurnIntentMode.DIAGNOSE
    assert intent.expected_output == TurnIntentExpectedOutput.RUN_RESULT
    assert intent.authority.may_read_run_context is True
    assert intent.authority.may_update_workflow is False
    assert intent.authority.may_run_blocks is False
    assert RequiredContextKey.LATEST_RUN_RESULT in intent.required_context
    assert intent.target_entities["run"] == ["wr_123"]


def test_build_turn_intent_applies_draft_only_classification_without_run_authority() -> None:
    intent = build_turn_intent(
        user_message="Draft only; no validation run.",
        workflow_yaml="",
        chat_history=[],
        global_llm_context="",
        request_policy=RequestPolicy(allow_update_workflow=True, allow_run_blocks=True),
        classification=_classification(TurnIntentMode.DRAFT_ONLY),
    )

    assert intent.mode == TurnIntentMode.DRAFT_ONLY
    assert intent.authority.may_update_workflow is True
    assert intent.authority.may_run_blocks is False


def test_build_turn_intent_skip_test_policy_is_fallback_when_classifier_missing() -> None:
    intent = build_turn_intent(
        user_message="Draft only; no validation run.",
        workflow_yaml="",
        chat_history=[],
        global_llm_context="",
        request_policy=RequestPolicy(testing_intent="skip_test", allow_update_workflow=True, allow_run_blocks=False),
    )

    assert intent.mode == TurnIntentMode.DRAFT_ONLY
    assert intent.authority.may_run_blocks is False
    assert TurnIntentReasonCode.TESTING_INTENT_SKIP_TEST in intent.reason_codes


def test_build_turn_intent_llm_diagnose_outranks_skip_test_policy() -> None:
    intent = build_turn_intent(
        user_message="Call get_run_results with workflow_run_id wr_123. Do not run blocks.",
        workflow_yaml="title: Existing\nworkflow_definition:\n  blocks: []\n",
        chat_history=[],
        global_llm_context="",
        request_policy=RequestPolicy(testing_intent="skip_test", allow_update_workflow=True, allow_run_blocks=False),
        workflow_run_id="wr_123",
        classification=_classification(TurnIntentMode.DIAGNOSE),
    )

    assert intent.mode == TurnIntentMode.DIAGNOSE
    assert intent.authority.may_read_run_context is True
    assert intent.authority.may_update_workflow is False
    assert TurnIntentReasonCode.TESTING_INTENT_SKIP_TEST not in intent.reason_codes


def test_build_turn_intent_diagnose_with_require_test_keeps_run_authority() -> None:
    intent = build_turn_intent(
        user_message="Test it again and confirm what it extracted.",
        workflow_yaml="title: Existing\nworkflow_definition:\n  blocks: []\n",
        chat_history=[],
        global_llm_context="",
        request_policy=RequestPolicy(testing_intent="require_test", allow_update_workflow=True, allow_run_blocks=True),
        workflow_run_id="wr_123",
        classification=_classification(TurnIntentMode.DIAGNOSE),
    )

    assert intent.mode == TurnIntentMode.DIAGNOSE
    assert intent.authority.may_run_blocks is True
    assert intent.authority.may_update_workflow is False
    assert intent.authority.may_read_run_context is True
    assert RequiredContextKey.LATEST_RUN_RESULT in intent.required_context
    assert TurnIntentReasonCode.TESTING_INTENT_RUN_OVERRIDES_DIAGNOSE in intent.reason_codes


def test_build_turn_intent_diagnose_without_require_test_stays_answer_only() -> None:
    intent = build_turn_intent(
        user_message="What did the last run extract?",
        workflow_yaml="title: Existing\nworkflow_definition:\n  blocks: []\n",
        chat_history=[],
        global_llm_context="",
        request_policy=RequestPolicy(allow_update_workflow=False, allow_run_blocks=False),
        workflow_run_id="wr_123",
        classification=_classification(TurnIntentMode.DIAGNOSE),
    )

    assert intent.mode == TurnIntentMode.DIAGNOSE
    assert intent.authority.may_run_blocks is False
    assert intent.authority.may_update_workflow is False
    assert intent.authority.may_read_run_context is True
    assert TurnIntentReasonCode.TESTING_INTENT_RUN_OVERRIDES_DIAGNOSE not in intent.reason_codes


def test_build_turn_intent_uses_request_policy_clarification_over_llm_classification() -> None:
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
        classification=_classification(TurnIntentMode.EDIT),
    )

    assert intent.mode == TurnIntentMode.CLARIFY
    assert intent.authority.requires_user_input is True
    assert intent.authority.may_update_workflow is False
    assert intent.authority.may_run_blocks is False
    assert intent.missing_context_question == "Which page should I target?"
    assert RequiredContextKey.CURRENT_WORKFLOW in intent.required_context
    assert TurnIntentReasonCode.REQUEST_POLICY_CLARIFICATION in intent.reason_codes
    assert TurnIntentReasonCode.LLM_CLASSIFIER not in intent.reason_codes


def test_build_turn_intent_routes_raw_secret_to_refuse_over_llm_classification() -> None:
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
        classification=_classification(TurnIntentMode.BUILD),
    )

    assert intent.mode == TurnIntentMode.REFUSE
    assert intent.authority.may_update_workflow is False
    assert intent.authority.may_run_blocks is False
    assert intent.authority.requires_user_input is True
    assert intent.missing_context_question == "Store the credential in the Credentials UI."
    assert TurnIntentReasonCode.RAW_SECRET_REFUSAL in intent.reason_codes
    assert TurnIntentReasonCode.LLM_CLASSIFIER not in intent.reason_codes


def test_build_turn_intent_redacts_user_goal() -> None:
    intent = build_turn_intent(
        user_message="Use password: hunter2 and build the workflow",
        workflow_yaml="",
        chat_history=[],
        global_llm_context="",
        request_policy=RequestPolicy(),
        classification=_classification(TurnIntentMode.BUILD),
    )

    assert "hunter2" not in intent.user_goal
    assert "[REDACTED_SECRET]" in intent.user_goal


@pytest.mark.parametrize("mode", [TurnIntentMode.BUILD, TurnIntentMode.EDIT, TurnIntentMode.DRAFT_ONLY])
def test_build_turn_intent_low_confidence_mutating_classification_clarifies(mode: TurnIntentMode) -> None:
    intent = build_turn_intent(
        user_message="Update it.",
        workflow_yaml="title: Existing\nworkflow_definition:\n  blocks: []\n",
        chat_history=[],
        global_llm_context="",
        request_policy=RequestPolicy(),
        classification=_classification(mode, confidence=0.35, target_entities={"workflow_change": ["update_it"]}),
    )

    assert intent.mode == TurnIntentMode.CLARIFY
    assert intent.authority.may_update_workflow is False
    assert intent.authority.may_run_blocks is False
    assert TurnIntentReasonCode.LOW_CONFIDENCE_CLARIFICATION in intent.reason_codes


def test_build_turn_intent_targetless_edit_classification_clarifies() -> None:
    intent = build_turn_intent(
        user_message="Update it.",
        workflow_yaml="title: Existing\nworkflow_definition:\n  blocks: []\n",
        chat_history=[],
        global_llm_context="",
        request_policy=RequestPolicy(),
        classification=_classification(TurnIntentMode.EDIT, confidence=0.82),
    )

    assert intent.mode == TurnIntentMode.CLARIFY
    assert intent.authority.may_update_workflow is False
    assert intent.authority.may_run_blocks is False
    assert intent.missing_context_question == "What change should I make to this workflow?"
    assert TurnIntentReasonCode.MISSING_EDIT_TARGET in intent.reason_codes


def test_build_turn_intent_allows_clear_workflow_change_edit_classification() -> None:
    intent = build_turn_intent(
        user_message="Add a step that downloads the invoice PDF.",
        workflow_yaml="title: Existing\nworkflow_definition:\n  blocks: []\n",
        chat_history=[],
        global_llm_context="",
        request_policy=RequestPolicy(allow_run_blocks=False),
        classification=_classification(
            TurnIntentMode.EDIT,
            confidence=0.82,
            target_entities={"workflow_change": ["add_invoice_download_step"]},
        ),
    )

    assert intent.mode == TurnIntentMode.EDIT
    assert intent.authority.may_update_workflow is True
    assert intent.target_entities["workflow_change"] == ["add_invoice_download_step"]


@pytest.mark.parametrize(
    ("block_ref", "expected_label"),
    [
        ("login block", "login_block"),
        ("login_block", "login_block"),
        ("login step", "login_step"),
    ],
)
def test_build_turn_intent_resolves_classifier_block_targets(block_ref: str, expected_label: str) -> None:
    intent = build_turn_intent(
        user_message=f"Update the {block_ref}.",
        workflow_yaml=(
            "title: Existing\n"
            "workflow_definition:\n"
            "  blocks:\n"
            "    - block_type: navigation\n"
            "      label: login_block\n"
            "    - block_type: action\n"
            "      label: login_step\n"
        ),
        chat_history=[],
        global_llm_context="",
        request_policy=RequestPolicy(allow_run_blocks=False),
        classification=_classification(
            TurnIntentMode.EDIT,
            confidence=0.65,
            target_entities={"block": [block_ref]},
            reason_codes=[TurnIntentReasonCode.TARGET_ENTITY_RESOLVED],
        ),
    )

    assert intent.mode == TurnIntentMode.EDIT
    assert intent.target_entities["block"] == [expected_label]
    assert intent.authority.may_update_workflow is True


def test_build_turn_intent_preserves_classifier_unresolved_block_targets() -> None:
    intent = build_turn_intent(
        user_message="Remove the login block.",
        workflow_yaml=(
            "title: Existing\n"
            "workflow_definition:\n"
            "  blocks:\n"
            "    - block_type: navigation\n"
            "      label: download_invoice\n"
        ),
        chat_history=[],
        global_llm_context="",
        request_policy=RequestPolicy(allow_run_blocks=False),
        classification=_classification(
            TurnIntentMode.CLARIFY,
            confidence=0.35,
            target_entities={"block": ["login block"]},
            missing_context_question="Which existing block should I change?",
            reason_codes=[TurnIntentReasonCode.LOW_CONFIDENCE_CLARIFICATION],
        ),
    )

    assert intent.mode == TurnIntentMode.CLARIFY
    assert intent.authority.requires_user_input is True
    assert intent.target_entities[UNRESOLVED_BLOCK_REF_TARGET_ENTITY] == ["login block"]


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


def _structured_context_with_run_decision() -> str:
    return StructuredContext(
        decisions_made=[
            "run_blocks_and_collect_debug: ran 3 blocks, hit per-tool-call budget",
            "  output: block_extract: {'rbt_certified': true}",
        ],
    ).to_json_str()


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


def test_has_structured_prior_run_signal_ignores_workflow_state_only() -> None:
    payload = StructuredContext(workflow_state="updated workflow yaml diff summary").to_json_str()
    assert _has_structured_prior_run_signal(payload) is False


def test_user_non_progress_fires_on_stuck_marker() -> None:
    intent = build_turn_intent(
        user_message="i can't see it",
        workflow_yaml="",
        chat_history=[_user_message("where is my downloaded file"), _ai_message("Check the Artifacts panel.")],
        global_llm_context="",
        request_policy=RequestPolicy(),
    )

    assert TurnIntentReasonCode.USER_NON_PROGRESS in intent.reason_codes


def test_user_non_progress_does_not_fire_on_first_turn_even_when_marker_matches() -> None:
    intent = build_turn_intent(
        user_message="where is my API key stored?",
        workflow_yaml="",
        chat_history=[],
        global_llm_context="",
        request_policy=RequestPolicy(),
    )

    assert TurnIntentReasonCode.USER_NON_PROGRESS not in intent.reason_codes


def test_docs_answer_turn_directive_renders_only_for_docs_answer_mode() -> None:
    docs_directive = _docs_answer_turn_directive(TurnIntent(mode=TurnIntentMode.DOCS_ANSWER))
    assert "TURN INTENT: docs_answer" in docs_directive
    assert "Answer it inline in the user's language" in docs_directive
    assert "do not offer to build an example workflow" in docs_directive

    assert _docs_answer_turn_directive(TurnIntent(mode=TurnIntentMode.BUILD)) == ""
    assert _docs_answer_turn_directive(TurnIntent(mode=TurnIntentMode.EDIT)) == ""
    assert _docs_answer_turn_directive(None) == ""


def test_store_request_policy_attaches_classified_turn_intent_to_context() -> None:
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

    _store_request_policy_on_context(
        ctx,
        policy,
        inputs,
        turn_intent_classification=_classification(TurnIntentMode.DIAGNOSE),
    )

    assert ctx.turn_intent is not None
    assert ctx.turn_intent.mode == TurnIntentMode.DIAGNOSE
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


def test_turn_intent_classification_parser_normalizes_supported_llm_payloads() -> None:
    payload = {
        "mode": "edit",
        "expected_output": "workflow_update",
        "required_context": ["current_workflow"],
        "confidence": 1.2,
        "target_entities": {"block": ["login block"]},
        "reason_codes": ["target_entity_resolved", "not_a_reason"],
    }

    for raw_payload in (
        payload,
        (
            '{"mode":"edit","expected_output":"workflow_update","required_context":["current_workflow"],'
            '"confidence":1.2,"target_entities":{"block":["login block"]},'
            '"reason_codes":["target_entity_resolved","not_a_reason"]}'
        ),
    ):
        classification = _turn_intent_classification_from_raw(raw_payload)

        assert classification is not None
        assert classification.mode == TurnIntentMode.EDIT
        assert classification.expected_output == TurnIntentExpectedOutput.WORKFLOW_UPDATE
        assert classification.required_context == [RequiredContextKey.CURRENT_WORKFLOW]
        assert classification.confidence == 1.0
        assert classification.target_entities == {"block": ["login block"]}
        assert classification.reason_codes == [TurnIntentReasonCode.TARGET_ENTITY_RESOLVED]


def test_turn_intent_classification_parser_rejects_malformed_payload() -> None:
    assert _turn_intent_classification_from_raw({"mode": "not_a_mode"}) is None
    assert _turn_intent_classification_from_raw("not json") is None


@pytest.mark.asyncio
async def test_classify_turn_intent_calls_llm_handler_with_prompt_contract() -> None:
    calls: list[dict[str, str]] = []

    async def handler(prompt: str, prompt_name: str, **_: object) -> dict[str, object]:
        calls.append({"prompt": prompt, "prompt_name": prompt_name})
        return {
            "mode": "build",
            "expected_output": "workflow_draft",
            "required_context": ["browser_state"],
            "confidence": 0.82,
            "target_entities": {"workflow": ["current_workflow"]},
            "missing_context_question": None,
            "reason_codes": [],
        }

    classification = await classify_turn_intent(
        user_message="Create a workflow from the page I have open.",
        workflow_yaml="",
        chat_history=[],
        global_llm_context="",
        request_policy=RequestPolicy(),
        handler=handler,
    )

    assert classification is not None
    assert classification.mode == TurnIntentMode.BUILD
    assert classification.required_context == [RequiredContextKey.BROWSER_STATE]
    assert calls[0]["prompt_name"] == PROMPT_NAME
    assert "Create a workflow from the page I have open." in calls[0]["prompt"]
    assert "Allowed modes" in calls[0]["prompt"]
