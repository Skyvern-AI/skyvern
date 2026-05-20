from __future__ import annotations

from enum import StrEnum
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from skyvern.forge.sdk.copilot.request_policy import RequestPolicy, redact_raw_secrets_for_prompt
from skyvern.forge.sdk.schemas.workflow_copilot import (
    WorkflowCopilotChatHistoryMessage,
    WorkflowCopilotChatSender,
)


class TurnIntentMode(StrEnum):
    BUILD = "build"
    EDIT = "edit"
    DIAGNOSE = "diagnose"
    DOCS_ANSWER = "docs_answer"
    DRAFT_ONLY = "draft_only"
    CLARIFY = "clarify"
    REFUSE = "refuse"
    UNKNOWN = "unknown"


class RequiredContextKey(StrEnum):
    CURRENT_WORKFLOW = "current_workflow"
    PROPOSED_WORKFLOW = "proposed_workflow"
    LATEST_ASSISTANT_PROPOSAL = "latest_assistant_proposal"
    LATEST_RUN_RESULT = "latest_run_result"
    CREDENTIAL_METADATA = "credential_metadata"
    DOCS_CONTEXT = "docs_context"
    BROWSER_STATE = "browser_state"


class TurnIntentExpectedOutput(StrEnum):
    WORKFLOW_UPDATE = "workflow_update"
    WORKFLOW_DRAFT = "workflow_draft"
    RUN_RESULT = "run_result"
    EXPLANATION = "explanation"
    CLARIFICATION = "clarification"
    REFUSAL = "refusal"


class TurnIntentReasonCode(StrEnum):
    DEFAULT_UNKNOWN = "default_unknown"
    REQUEST_POLICY_DERIVED = "request_policy_derived"
    REQUEST_POLICY_CLARIFICATION = "request_policy_clarification"
    TESTING_INTENT_SKIP_TEST = "testing_intent_skip_test"
    WORKFLOW_CONTEXT_PRESENT = "workflow_context_present"
    CHAT_HISTORY_PRESENT = "chat_history_present"
    RUN_CONTEXT_PRESENT = "run_context_present"
    BROWSER_CONTEXT_PRESENT = "browser_context_present"
    KEYWORD_HEURISTIC = "keyword_heuristic"


class TurnIntentAuthority(BaseModel):
    model_config = ConfigDict(extra="forbid")

    may_update_workflow: bool = False
    may_run_blocks: bool = False
    may_answer_without_mutation: bool = True
    requires_user_input: bool = False


class TurnIntent(BaseModel):
    model_config = ConfigDict(extra="forbid")

    mode: TurnIntentMode = TurnIntentMode.UNKNOWN
    user_goal: str = ""
    target_entities: dict[str, list[str]] = Field(default_factory=dict)
    required_context: list[RequiredContextKey] = Field(default_factory=list)
    authority: TurnIntentAuthority = Field(default_factory=TurnIntentAuthority)
    expected_output: TurnIntentExpectedOutput = TurnIntentExpectedOutput.EXPLANATION
    confidence: float = Field(default=0.0, ge=0.0, le=1.0)
    reason_codes: list[TurnIntentReasonCode] = Field(default_factory=lambda: [TurnIntentReasonCode.DEFAULT_UNKNOWN])
    missing_context_question: str | None = None

    @field_validator("required_context", mode="after")
    @classmethod
    def _dedupe_required_context(cls, value: list[RequiredContextKey]) -> list[RequiredContextKey]:
        return list(dict.fromkeys(value))

    @field_validator("reason_codes", mode="after")
    @classmethod
    def _dedupe_reason_codes(cls, value: list[TurnIntentReasonCode]) -> list[TurnIntentReasonCode]:
        return list(dict.fromkeys(value)) or [TurnIntentReasonCode.DEFAULT_UNKNOWN]

    @field_validator("target_entities", mode="after")
    @classmethod
    def _dedupe_target_entities(cls, value: dict[str, list[str]]) -> dict[str, list[str]]:
        return {
            str(entity_type): list(dict.fromkeys(str(entity).strip() for entity in entities if str(entity).strip()))
            for entity_type, entities in value.items()
            if str(entity_type).strip()
        }

    @model_validator(mode="after")
    def _align_expected_output_with_mode(self) -> TurnIntent:
        if self.expected_output != TurnIntentExpectedOutput.EXPLANATION:
            return self
        expected_output_by_mode = {
            TurnIntentMode.BUILD: TurnIntentExpectedOutput.WORKFLOW_DRAFT,
            TurnIntentMode.EDIT: TurnIntentExpectedOutput.WORKFLOW_UPDATE,
            TurnIntentMode.DRAFT_ONLY: TurnIntentExpectedOutput.WORKFLOW_DRAFT,
            TurnIntentMode.CLARIFY: TurnIntentExpectedOutput.CLARIFICATION,
            TurnIntentMode.REFUSE: TurnIntentExpectedOutput.REFUSAL,
        }
        if mapped_expected_output := expected_output_by_mode.get(self.mode):
            self.expected_output = mapped_expected_output
        return self

    def to_trace_data(self) -> dict[str, Any]:
        data: dict[str, Any] = {
            "mode": self.mode.value,
            "expected_output": self.expected_output.value,
            "required_context": [key.value for key in self.required_context],
            "may_update_workflow": self.authority.may_update_workflow,
            "may_run_blocks": self.authority.may_run_blocks,
            "may_answer_without_mutation": self.authority.may_answer_without_mutation,
            "requires_user_input": self.authority.requires_user_input,
            "confidence": self.confidence,
            "reason_codes": [reason.value for reason in self.reason_codes],
        }
        if self.target_entities:
            data["target_entity_types"] = sorted(self.target_entities)
        if self.missing_context_question:
            data["has_missing_context_question"] = True
        return data


_GOAL_MAX_CHARS = 240
_BUILD_TERMS = ("build", "create", "make", "generate")
_EDIT_TERMS = ("edit", "update", "change", "modify", "replace", "fix")
_DIAGNOSE_TERMS = ("debug", "diagnose", "failed", "failure", "error", "result")
_DOCS_TERMS = ("explain", "how do", "how does", "what is", "what are", "why", "docs", "documentation")


def _normalize_user_goal(user_message: str) -> str:
    goal = redact_raw_secrets_for_prompt((user_message or "").strip())
    if len(goal) <= _GOAL_MAX_CHARS:
        return goal
    return goal[: _GOAL_MAX_CHARS - 3].rstrip() + "..."


def _has_latest_assistant_turn(chat_history: list[WorkflowCopilotChatHistoryMessage]) -> bool:
    return any(
        message.sender == WorkflowCopilotChatSender.AI and (message.content or "").strip() for message in chat_history
    )


def _contains_any(text: str, terms: tuple[str, ...]) -> bool:
    normalized = text.lower()
    return any(term in normalized for term in terms)


def _mode_from_keywords(
    user_message: str, *, has_workflow: bool
) -> tuple[TurnIntentMode, TurnIntentExpectedOutput] | None:
    if _contains_any(user_message, _DIAGNOSE_TERMS):
        return TurnIntentMode.DIAGNOSE, TurnIntentExpectedOutput.RUN_RESULT
    if _contains_any(user_message, _DOCS_TERMS):
        return TurnIntentMode.DOCS_ANSWER, TurnIntentExpectedOutput.EXPLANATION
    if has_workflow and _contains_any(user_message, _EDIT_TERMS):
        return TurnIntentMode.EDIT, TurnIntentExpectedOutput.WORKFLOW_UPDATE
    if _contains_any(user_message, _BUILD_TERMS):
        return TurnIntentMode.BUILD, TurnIntentExpectedOutput.WORKFLOW_DRAFT
    return None


def build_turn_intent(
    *,
    user_message: str,
    workflow_yaml: str,
    chat_history: list[WorkflowCopilotChatHistoryMessage],
    global_llm_context: str,
    request_policy: RequestPolicy,
    workflow_id: str | None = None,
    workflow_permanent_id: str | None = None,
    workflow_run_id: str | None = None,
    browser_session_id: str | None = None,
) -> TurnIntent:
    has_workflow = bool((workflow_yaml or "").strip())
    has_prior_context = bool((global_llm_context or "").strip())
    target_entities: dict[str, list[str]] = {}
    required_context: list[RequiredContextKey] = []
    reason_codes: list[TurnIntentReasonCode] = [TurnIntentReasonCode.REQUEST_POLICY_DERIVED]

    if workflow_id or workflow_permanent_id or has_workflow:
        target_entities["workflow"] = [value for value in (workflow_permanent_id, workflow_id) if value]
    if workflow_run_id:
        target_entities["run"] = [workflow_run_id]
    if request_policy.credential_refs:
        target_entities["credential"] = list(request_policy.credential_refs)

    if has_workflow:
        required_context.append(RequiredContextKey.CURRENT_WORKFLOW)
        reason_codes.append(TurnIntentReasonCode.WORKFLOW_CONTEXT_PRESENT)
    if _has_latest_assistant_turn(chat_history):
        required_context.append(RequiredContextKey.LATEST_ASSISTANT_PROPOSAL)
        reason_codes.append(TurnIntentReasonCode.CHAT_HISTORY_PRESENT)
    if workflow_run_id:
        required_context.append(RequiredContextKey.LATEST_RUN_RESULT)
        reason_codes.append(TurnIntentReasonCode.RUN_CONTEXT_PRESENT)
    if browser_session_id:
        required_context.append(RequiredContextKey.BROWSER_STATE)
        reason_codes.append(TurnIntentReasonCode.BROWSER_CONTEXT_PRESENT)
    if request_policy.credential_input_kind != "none" or request_policy.resolved_credentials:
        required_context.append(RequiredContextKey.CREDENTIAL_METADATA)

    authority = TurnIntentAuthority(
        may_update_workflow=request_policy.allow_update_workflow,
        may_run_blocks=request_policy.allow_run_blocks and request_policy.testing_intent != "skip_test",
        may_answer_without_mutation=True,
        requires_user_input=request_policy.user_response_policy == "ask_clarification",
    )
    mode = TurnIntentMode.UNKNOWN
    expected_output = TurnIntentExpectedOutput.EXPLANATION
    confidence = 0.2 if (has_workflow or has_prior_context or chat_history) else 0.0
    missing_context_question = None

    if request_policy.user_response_policy == "ask_clarification":
        mode = TurnIntentMode.CLARIFY
        expected_output = TurnIntentExpectedOutput.CLARIFICATION
        confidence = 0.8
        missing_context_question = request_policy.clarification_question
        reason_codes.append(TurnIntentReasonCode.REQUEST_POLICY_CLARIFICATION)
    elif request_policy.testing_intent == "skip_test":
        mode = TurnIntentMode.DRAFT_ONLY
        expected_output = TurnIntentExpectedOutput.WORKFLOW_DRAFT
        confidence = 0.6
        reason_codes.append(TurnIntentReasonCode.TESTING_INTENT_SKIP_TEST)
    elif keyword_mode := _mode_from_keywords(user_message, has_workflow=has_workflow):
        mode, expected_output = keyword_mode
        confidence = 0.35
        reason_codes.append(TurnIntentReasonCode.KEYWORD_HEURISTIC)

    if mode == TurnIntentMode.DOCS_ANSWER:
        required_context.append(RequiredContextKey.DOCS_CONTEXT)
    elif mode == TurnIntentMode.DIAGNOSE and RequiredContextKey.LATEST_RUN_RESULT not in required_context:
        required_context.append(RequiredContextKey.LATEST_RUN_RESULT)

    return TurnIntent(
        mode=mode,
        user_goal=_normalize_user_goal(user_message),
        target_entities=target_entities,
        required_context=required_context,
        authority=authority,
        expected_output=expected_output,
        confidence=confidence,
        reason_codes=reason_codes,
        missing_context_question=missing_context_question,
    )
