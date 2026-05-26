from __future__ import annotations

import functools
import re
from difflib import SequenceMatcher
from enum import StrEnum
from typing import Any

import yaml
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from skyvern.forge.sdk.copilot.context import StructuredContext
from skyvern.forge.sdk.copilot.request_policy import RequestPolicy, redact_raw_secrets_for_prompt
from skyvern.forge.sdk.schemas.workflow_copilot import (
    WorkflowCopilotChatHistoryMessage,
    WorkflowCopilotChatSender,
)
from skyvern.utils.yaml_loader import safe_load_no_dates

UNRESOLVED_BLOCK_REF_TARGET_ENTITY = "unresolved_block_ref"


class TurnIntentMode(StrEnum):
    BUILD = "build"
    EDIT = "edit"
    DIAGNOSE = "diagnose"
    DOCS_ANSWER = "docs_answer"
    DRAFT_ONLY = "draft_only"
    CLARIFY = "clarify"
    REFUSE = "refuse"
    UNKNOWN = "unknown"


NO_MUTATION_TURN_INTENT_MODES = frozenset(
    {
        TurnIntentMode.DOCS_ANSWER,
        TurnIntentMode.DIAGNOSE,
        TurnIntentMode.CLARIFY,
        TurnIntentMode.REFUSE,
    }
)

# Modes that have no legitimate use of run context. Used by tools.py to scope
# the intra-turn read-context override away from explicitly answer-only turns.
READ_CONTEXT_DENIED_MODES = frozenset(
    {
        TurnIntentMode.DOCS_ANSWER,
        TurnIntentMode.REFUSE,
        TurnIntentMode.CLARIFY,
    }
)


class RequiredContextKey(StrEnum):
    CURRENT_WORKFLOW = "current_workflow"
    PROPOSED_WORKFLOW = "proposed_workflow"
    LATEST_ASSISTANT_PROPOSAL = "latest_assistant_proposal"
    WORKFLOW_CHANGE = "workflow_change"
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
    CONFIRMATION_CARRYOVER = "confirmation_carryover"
    RAW_SECRET_REFUSAL = "raw_secret_refusal"
    USER_NON_PROGRESS = "user_non_progress"
    RECOVERY_FROM_RUN_CONTEXT = "recovery_from_run_context"


class TurnIntentAuthority(BaseModel):
    model_config = ConfigDict(extra="forbid")

    may_update_workflow: bool = False
    may_run_blocks: bool = False
    may_answer_without_mutation: bool = True
    requires_user_input: bool = False
    # Decoupled from mutation flags so DIAGNOSE turns may inspect run state without write authority.
    may_read_run_context: bool = False


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
            "may_read_run_context": self.authority.may_read_run_context,
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
_NEW_BROWSER_TASK_TERMS = ("go to", "navigate to", "open", "visit", "search for")
_EDIT_TERMS = ("edit", "update", "change", "modify", "replace", "fix")
_DIAGNOSE_TERMS = ("debug", "diagnose", "failed", "failure", "error", "result")
_RUN_CONTEXT_REQUEST_TERMS = ("get_run_results", "workflow_run_id", "run result", "run results")
# Subset of _DIAGNOSE_TERMS that name unambiguous diagnose intent. "result" is in
# _DIAGNOSE_TERMS but absent here: a browser task on a no-blocks workflow that
# mentions "results" is a build request, not a run-result inspection.
_CLEAR_DIAGNOSE_TERMS = ("debug", "diagnose", "failed", "failure", "error")
_DOCS_TERMS = (
    "explain",
    "how do",
    "how does",
    "what is",
    "what are",
    "why",
    "docs",
    "documentation",
    " vs ",
    " versus ",
    "difference between",
)
_IDENTIFIER_REF_RE = r"[A-Za-z][A-Za-z0-9]*(?:_[A-Za-z0-9]+)+"
_CODE_IDENTIFIER_REF_RE = re.compile(rf"`(?P<ref>{_IDENTIFIER_REF_RE})`")
_BLOCK_IDENTIFIER_REF_RE = re.compile(
    rf"\b(?:(?:block|step)\s+(?P<after>{_IDENTIFIER_REF_RE})|(?P<before>{_IDENTIFIER_REF_RE})\s+(?:block|step))\b",
    re.I,
)
_WF_IDENTIFIER_REF_RE = re.compile(r"\bWF_[A-Za-z0-9_]+\b")
_ANY_IDENTIFIER_REF_RE = re.compile(rf"\b{_IDENTIFIER_REF_RE}\b")


def _normalize_user_goal(user_message: str) -> str:
    goal = redact_raw_secrets_for_prompt((user_message or "").strip())
    if len(goal) <= _GOAL_MAX_CHARS:
        return goal
    return goal[: _GOAL_MAX_CHARS - 3].rstrip() + "..."


def _has_latest_assistant_turn(chat_history: list[WorkflowCopilotChatHistoryMessage]) -> bool:
    return any(
        message.sender == WorkflowCopilotChatSender.AI and (message.content or "").strip() for message in chat_history
    )


@functools.lru_cache(maxsize=None)
def _word_boundary_pattern(terms: tuple[str, ...]) -> re.Pattern[str]:
    alternation = "|".join(re.escape(term.strip()) for term in terms)
    # Boundaries are alphanumeric-only so `_` separates words: the edit
    # term `update` matches a block label like `update_card`. Optional
    # trailing `s` matches plurals (`errors`) but deliberately not `-ed`
    # forms (`fixed` must not match `fix`).
    return re.compile(rf"(?<![a-z0-9])(?:{alternation})s?(?![a-z0-9])")


def _contains_any(text: str, terms: tuple[str, ...]) -> bool:
    return _word_boundary_pattern(terms).search(text.lower()) is not None


def _is_explicit_run_context_request(user_message: str) -> bool:
    return _contains_any(user_message, _RUN_CONTEXT_REQUEST_TERMS)


def _workflow_definition_dict(workflow_yaml: str | None) -> dict | None:
    if not workflow_yaml:
        return None
    try:
        parsed = safe_load_no_dates(workflow_yaml)
    except yaml.YAMLError:
        return None
    if not isinstance(parsed, dict):
        return None
    workflow_definition = parsed.get("workflow_definition")
    return workflow_definition if isinstance(workflow_definition, dict) else None


def _workflow_has_blocks(workflow_yaml: str | None) -> bool:
    workflow_definition = _workflow_definition_dict(workflow_yaml)
    if workflow_definition is None:
        return False
    blocks = workflow_definition.get("blocks")
    if not isinstance(blocks, list):
        return False
    return any(isinstance(block, dict) for block in blocks)


def _workflow_block_labels(workflow_yaml: str | None) -> set[str]:
    workflow_definition = _workflow_definition_dict(workflow_yaml)
    if workflow_definition is None:
        return set()
    blocks = workflow_definition.get("blocks")
    if not isinstance(blocks, list):
        return set()
    labels: set[str] = set()
    for block in blocks:
        if not isinstance(block, dict):
            continue
        label = block.get("label")
        if isinstance(label, str) and label:
            labels.add(label)
    return labels


def _workflow_parameter_keys(workflow_yaml: str | None) -> set[str]:
    workflow_definition = _workflow_definition_dict(workflow_yaml)
    if workflow_definition is None:
        return set()
    parameters = workflow_definition.get("parameters")
    if not isinstance(parameters, list):
        return set()
    keys: set[str] = set()
    for parameter in parameters:
        if not isinstance(parameter, dict):
            continue
        key = parameter.get("key")
        if isinstance(key, str) and key:
            keys.add(key)
    return keys


def _explicit_identifier_refs(user_message: str) -> set[str]:
    refs = {match.group("ref") for match in _CODE_IDENTIFIER_REF_RE.finditer(user_message or "")}
    refs.update(
        match.group("after") or match.group("before")
        for match in _BLOCK_IDENTIFIER_REF_RE.finditer(user_message or "")
        if match.group("after") or match.group("before")
    )
    wf_refs = set(_WF_IDENTIFIER_REF_RE.findall(user_message or ""))
    refs.update(wf_refs)
    if wf_refs:
        # If the turn already contains a generated workflow-style ref, include the other
        # identifier-shaped tokens as companion refs. This catches "WF_x worked but y failed"
        # without treating every snake_case field name as a workflow block target.
        refs.update(_ANY_IDENTIFIER_REF_RE.findall(user_message or ""))
    return refs


def _unresolved_explicit_block_refs(user_message: str, workflow_yaml: str | None) -> list[str]:
    explicit_refs = _explicit_identifier_refs(user_message)
    if not explicit_refs:
        return []
    known_labels = {label.lower() for label in _workflow_block_labels(workflow_yaml)}
    parameter_keys = {key.lower() for key in _workflow_parameter_keys(workflow_yaml)}
    known_non_block_refs = {"workflow_run_id", "browser_session_id"}
    unresolved: list[str] = []
    for ref in sorted(explicit_refs):
        normalized = ref.lower()
        if normalized in known_labels or normalized in parameter_keys or normalized in known_non_block_refs:
            continue
        unresolved.append(ref)
    return unresolved


def _mode_from_keywords(
    user_message: str,
    *,
    has_workflow: bool,
    has_workflow_blocks: bool,
    has_prior_run_signal: bool,
) -> tuple[TurnIntentMode, TurnIntentExpectedOutput] | None:
    if has_workflow and _contains_any(user_message, _EDIT_TERMS):
        return TurnIntentMode.EDIT, TurnIntentExpectedOutput.WORKFLOW_UPDATE
    # A browser-task verb on a saved workflow with no buildable blocks is a new
    # build request even when the message mentions "results"; suppress only when
    # the user names diagnosis or docs intent, or a prior run is attached. The
    # no-yaml case still falls through to the trailing browser-task fallback.
    if (
        has_workflow
        and not has_workflow_blocks
        and not has_prior_run_signal
        and _contains_any(user_message, _NEW_BROWSER_TASK_TERMS)
        and not _contains_any(user_message, _CLEAR_DIAGNOSE_TERMS)
        and not _contains_any(user_message, _DOCS_TERMS)
    ):
        return TurnIntentMode.BUILD, TurnIntentExpectedOutput.WORKFLOW_DRAFT
    if _contains_any(user_message, _DIAGNOSE_TERMS):
        return TurnIntentMode.DIAGNOSE, TurnIntentExpectedOutput.RUN_RESULT
    if _contains_any(user_message, _DOCS_TERMS):
        return TurnIntentMode.DOCS_ANSWER, TurnIntentExpectedOutput.EXPLANATION
    if _contains_any(user_message, _BUILD_TERMS):
        return TurnIntentMode.BUILD, TurnIntentExpectedOutput.WORKFLOW_DRAFT
    if not has_workflow and _contains_any(user_message, _NEW_BROWSER_TASK_TERMS):
        return TurnIntentMode.BUILD, TurnIntentExpectedOutput.WORKFLOW_DRAFT
    return None


_PRIOR_RUN_OUTPUT_PREFIX = "  output:"
_RUN_CONTEXT_DECISION_PREFIXES = (
    "run_blocks_and_collect_debug:",
    "update_and_run_blocks:",
    "get_run_results:",
)
_PRIOR_RUN_FAILURE_MARKERS = (
    "Run ID:",
    "workflow_run_id",
    "Outcome is uncertain",
    "per-tool-call budget",
)


def _decision_records_prior_run(entry: str) -> bool:
    if entry.startswith(_PRIOR_RUN_OUTPUT_PREFIX):
        return True
    if not entry.startswith(_RUN_CONTEXT_DECISION_PREFIXES):
        return False
    if any(marker in entry for marker in _PRIOR_RUN_FAILURE_MARKERS):
        return True
    return entry.startswith(("run_blocks_and_collect_debug: Run ", "update_and_run_blocks: Run "))


def _has_structured_prior_run_signal(global_llm_context: str) -> bool:
    if not (global_llm_context or "").strip():
        return False
    structured = StructuredContext.from_json_str(global_llm_context)
    return any(isinstance(entry, str) and _decision_records_prior_run(entry) for entry in structured.decisions_made)


_AFFIRMATIVE_REPLIES = frozenset(
    {
        "yes",
        "y",
        "yeah",
        "yep",
        "yup",
        "sure",
        "ok",
        "okay",
        "confirm",
        "confirmed",
        "i confirm",
        "correct",
        "right",
        "thats right",
        "that's right",
        "go ahead",
        "do it",
        "please do",
        "sounds good",
        "affirmative",
    }
)


def _is_bare_affirmative(user_message: str) -> bool:
    """True when the message is only a confirmation token (no new instruction)."""
    normalized = (user_message or "").strip().lower().rstrip(".!,;: ")
    return normalized in _AFFIRMATIVE_REPLIES


_NON_PROGRESS_MARKER_RE = re.compile(
    r"\b(?:"
    r"i\s+can'?t\s+see\s+(?:it|that|them|those|the)"
    r"|still\s+(?:not|isn'?t|doesn'?t)\s+working"
    r"|still\s+(?:nothing|nope|none|no\s+luck)"
    r"|(?:still|it|that|this)\s+doesn'?t\s+work\b"
    r"|same\s+(?:problem|issue|answer|reply)"
    r"|didn'?t\s+(?:work|help)"
    r"|doesn'?t\s+help"
    r"|(?:still\s+)?can'?t\s+find\s+(?:it|that|them|those|the)\b"
    r"|where\s+(?:is|are|'?s)\s+(?:it|that|they|them)\b"
    r"|i\s+already\s+looked\b"
    r"|i\s+looked\s+(?:everywhere|already|but|and)"
    r"|not\s+(?:there|here|working)"
    r"|no\s+luck"
    r"|that\s+didn'?t\s+(?:work|help)"
    r")\b",
    re.IGNORECASE,
)
_NON_PROGRESS_SHORT_LIMIT = 80
_NON_PROGRESS_RESTATE_THRESHOLD = 0.7


def _user_signals_non_progress(user_message: str, chat_history: list[WorkflowCopilotChatHistoryMessage]) -> bool:
    """True when the current user turn re-engages on the same problem.

    Requires at least one prior AI reply — without that, marker phrases like
    "where is my X" on a first turn are genuine questions, not non-progress
    restatements of a stuck conversation.
    """
    text = (user_message or "").strip()
    if not text:
        return False
    has_prior_ai_reply = any(
        m.sender == WorkflowCopilotChatSender.AI and (m.content or "").strip() for m in chat_history
    )
    if not has_prior_ai_reply:
        return False
    if _NON_PROGRESS_MARKER_RE.search(text):
        return True
    if len(text) > _NON_PROGRESS_SHORT_LIMIT:
        return False
    prior = next(
        (
            (m.content or "").strip()
            for m in reversed(chat_history)
            if m.sender == WorkflowCopilotChatSender.USER and (m.content or "").strip()
        ),
        "",
    )
    if not prior:
        return False
    current_norm = re.sub(r"\s+", " ", text.lower()).strip()
    prior_norm = re.sub(r"\s+", " ", prior.lower()).strip()
    if not current_norm or not prior_norm:
        return False
    return SequenceMatcher(None, current_norm, prior_norm).ratio() >= _NON_PROGRESS_RESTATE_THRESHOLD


def _carryover_mode_from_prior_turn(
    chat_history: list[WorkflowCopilotChatHistoryMessage],
    *,
    has_workflow: bool,
    has_workflow_blocks: bool,
    has_prior_run_signal: bool,
) -> tuple[TurnIntentMode, TurnIntentExpectedOutput] | None:
    """Mode of the most recent prior user turn that keyword-classifies. A bare
    confirmation carries no keywords of its own, so it inherits the turn it confirms."""
    for message in reversed(chat_history):
        if message.sender != WorkflowCopilotChatSender.USER:
            continue
        content = (message.content or "").strip()
        # Skip earlier confirmations so a chain of "yes"/"confirm" turns walks
        # back to the substantive request, not to the nearest confirmation.
        if not content or _is_bare_affirmative(content):
            continue
        if carried := _mode_from_keywords(
            content,
            has_workflow=has_workflow,
            has_workflow_blocks=has_workflow_blocks,
            has_prior_run_signal=has_prior_run_signal,
        ):
            return carried
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
    has_workflow_blocks = _workflow_has_blocks(workflow_yaml)
    has_prior_context = bool((global_llm_context or "").strip())
    target_entities: dict[str, list[str]] = {}
    required_context: list[RequiredContextKey] = []
    reason_codes: list[TurnIntentReasonCode] = [TurnIntentReasonCode.REQUEST_POLICY_DERIVED]

    if workflow_id or workflow_permanent_id or has_workflow:
        workflow_targets = [value for value in (workflow_permanent_id, workflow_id) if value]
        target_entities["workflow"] = workflow_targets or ["current_workflow"]
    if workflow_run_id:
        target_entities["run"] = [workflow_run_id]
    if request_policy.credential_refs:
        target_entities["credential"] = list(request_policy.credential_refs)

    if has_workflow:
        required_context.append(RequiredContextKey.CURRENT_WORKFLOW)
        reason_codes.append(TurnIntentReasonCode.WORKFLOW_CONTEXT_PRESENT)
    if _has_latest_assistant_turn(chat_history):
        required_context.append(RequiredContextKey.LATEST_ASSISTANT_PROPOSAL)
        required_context.append(RequiredContextKey.WORKFLOW_CHANGE)
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

    has_prior_run_signal = workflow_run_id is not None or _has_structured_prior_run_signal(global_llm_context)

    if request_policy.raw_secret_detected and request_policy.raw_secret_handling != "redacted_draft":
        mode = TurnIntentMode.REFUSE
        expected_output = TurnIntentExpectedOutput.REFUSAL
        confidence = 0.9
        missing_context_question = request_policy.clarification_question
        authority.requires_user_input = True
        reason_codes.append(TurnIntentReasonCode.RAW_SECRET_REFUSAL)
    elif request_policy.user_response_policy == "ask_clarification":
        mode = TurnIntentMode.CLARIFY
        expected_output = TurnIntentExpectedOutput.CLARIFICATION
        confidence = 0.8
        missing_context_question = request_policy.clarification_question
        reason_codes.append(TurnIntentReasonCode.REQUEST_POLICY_CLARIFICATION)
    elif has_prior_run_signal and _is_explicit_run_context_request(user_message):
        mode = TurnIntentMode.DIAGNOSE
        expected_output = TurnIntentExpectedOutput.RUN_RESULT
        confidence = 0.45
        reason_codes.append(TurnIntentReasonCode.KEYWORD_HEURISTIC)
    elif request_policy.testing_intent == "skip_test":
        mode = TurnIntentMode.DRAFT_ONLY
        expected_output = TurnIntentExpectedOutput.WORKFLOW_DRAFT
        confidence = 0.6
        reason_codes.append(TurnIntentReasonCode.TESTING_INTENT_SKIP_TEST)
    elif keyword_mode := _mode_from_keywords(
        user_message,
        has_workflow=has_workflow,
        has_workflow_blocks=has_workflow_blocks,
        has_prior_run_signal=has_prior_run_signal,
    ):
        mode, expected_output = keyword_mode
        confidence = 0.35
        reason_codes.append(TurnIntentReasonCode.KEYWORD_HEURISTIC)
    elif _is_bare_affirmative(user_message) and (
        carried_mode := _carryover_mode_from_prior_turn(
            chat_history,
            has_workflow=has_workflow,
            has_workflow_blocks=has_workflow_blocks,
            has_prior_run_signal=has_prior_run_signal,
        )
    ):
        mode, expected_output = carried_mode
        confidence = 0.3
        reason_codes.append(TurnIntentReasonCode.CONFIRMATION_CARRYOVER)

    if _user_signals_non_progress(user_message, chat_history):
        reason_codes.append(TurnIntentReasonCode.USER_NON_PROGRESS)

    if mode == TurnIntentMode.EDIT:
        unresolved_block_refs = _unresolved_explicit_block_refs(user_message, workflow_yaml)
        if unresolved_block_refs:
            target_entities[UNRESOLVED_BLOCK_REF_TARGET_ENTITY] = unresolved_block_refs

    if mode == TurnIntentMode.UNKNOWN and has_prior_run_signal:
        mode = TurnIntentMode.DIAGNOSE
        expected_output = TurnIntentExpectedOutput.RUN_RESULT
        reason_codes.append(TurnIntentReasonCode.RECOVERY_FROM_RUN_CONTEXT)

    if mode == TurnIntentMode.DOCS_ANSWER:
        authority.may_update_workflow = False
        authority.may_run_blocks = False
        required_context.append(RequiredContextKey.DOCS_CONTEXT)
    elif mode == TurnIntentMode.DIAGNOSE and RequiredContextKey.LATEST_RUN_RESULT not in required_context:
        authority.may_update_workflow = False
        authority.may_run_blocks = False
        required_context.append(RequiredContextKey.LATEST_RUN_RESULT)
    elif mode == TurnIntentMode.DIAGNOSE:
        authority.may_update_workflow = False
        authority.may_run_blocks = False

    authority.may_read_run_context = mode == TurnIntentMode.DIAGNOSE

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
