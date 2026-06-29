from __future__ import annotations

import asyncio
import re
import time
from collections.abc import Mapping
from difflib import SequenceMatcher
from enum import StrEnum

import structlog
import yaml
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from skyvern.config import settings
from skyvern.forge.prompts import prompt_engine
from skyvern.forge.sdk.api.llm.api_handler import LLMAPIHandler
from skyvern.forge.sdk.copilot.context import StructuredContext, sanitize_global_llm_context_for_prompt
from skyvern.forge.sdk.copilot.output_utils import parse_final_response
from skyvern.forge.sdk.copilot.request_policy import (
    RequestPolicy,
    build_transcript_context,
    redact_raw_secrets_for_prompt,
)
from skyvern.forge.sdk.copilot.tracing_setup import copilot_span
from skyvern.forge.sdk.schemas.workflow_copilot import (
    WorkflowCopilotChatHistoryMessage,
    WorkflowCopilotChatSender,
)
from skyvern.utils.strings import escape_code_fences
from skyvern.utils.yaml_loader import safe_load_no_dates

LOG = structlog.get_logger()
PROMPT_NAME = "workflow-copilot-turn-intent"
UNRESOLVED_BLOCK_REF_TARGET_ENTITY = "unresolved_block_ref"
_WORKFLOW_YAML_PROMPT_MAX_CHARS = 4096
_GLOBAL_CONTEXT_PROMPT_MAX_CHARS = 2048
_TARGET_ENTITY_MAX_VALUES = 12
_TARGET_ENTITY_VALUE_MAX_CHARS = 160


class TurnIntentMode(StrEnum):
    BUILD = "build"
    EDIT = "edit"
    DIAGNOSE = "diagnose"
    DOCS_ANSWER = "docs_answer"
    DRAFT_ONLY = "draft_only"
    CLARIFY = "clarify"
    REFUSE = "refuse"
    UNKNOWN = "unknown"


_LOW_CONFIDENCE_MUTATION_THRESHOLD = 0.5
_MUTATING_CLASSIFIER_MODES = frozenset((TurnIntentMode.BUILD, TurnIntentMode.EDIT, TurnIntentMode.DRAFT_ONLY))
_EDIT_SPECIFIC_TARGET_ENTITY_TYPES = frozenset(
    ("block", "run", "proposed_workflow", "latest_assistant_proposal", "proposal", "workflow_change")
)


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
    TESTING_INTENT_RUN_OVERRIDES_DIAGNOSE = "testing_intent_run_overrides_diagnose"
    WORKFLOW_CONTEXT_PRESENT = "workflow_context_present"
    CHAT_HISTORY_PRESENT = "chat_history_present"
    RUN_CONTEXT_PRESENT = "run_context_present"
    BROWSER_CONTEXT_PRESENT = "browser_context_present"
    CONFIRMATION_CARRYOVER = "confirmation_carryover"
    RAW_SECRET_REFUSAL = "raw_secret_refusal"
    USER_NON_PROGRESS = "user_non_progress"
    RECOVERY_FROM_RUN_CONTEXT = "recovery_from_run_context"
    LLM_CLASSIFIER = "llm_classifier"
    LOW_CONFIDENCE_CLARIFICATION = "low_confidence_clarification"
    TARGET_ENTITY_RESOLVED = "target_entity_resolved"
    MISSING_EDIT_TARGET = "missing_edit_target"
    STRUCTURALLY_INFEASIBLE = "structurally_infeasible"
    TRANSIENT_CLASSIFIER_FALLBACK = "transient_classifier_fallback"


class TurnIntentClassifierFailureKind(StrEnum):
    TIMEOUT = "timeout"
    PROVIDER_ERROR = "provider_error"
    MISSING_HANDLER = "missing_handler"
    EMPTY_MESSAGE = "empty_message"
    PROMPT_RENDER_ERROR = "prompt_render_error"
    MALFORMED_OUTPUT = "malformed_output"


_CLASSIFIER_REASON_CODES = tuple(
    reason for reason in TurnIntentReasonCode if reason != TurnIntentReasonCode.TRANSIENT_CLASSIFIER_FALLBACK
)


_DEFAULT_EXPECTED_OUTPUT_BY_MODE: dict[TurnIntentMode, TurnIntentExpectedOutput] = {
    TurnIntentMode.BUILD: TurnIntentExpectedOutput.WORKFLOW_DRAFT,
    TurnIntentMode.EDIT: TurnIntentExpectedOutput.WORKFLOW_UPDATE,
    TurnIntentMode.DIAGNOSE: TurnIntentExpectedOutput.RUN_RESULT,
    TurnIntentMode.DOCS_ANSWER: TurnIntentExpectedOutput.EXPLANATION,
    TurnIntentMode.DRAFT_ONLY: TurnIntentExpectedOutput.WORKFLOW_DRAFT,
    TurnIntentMode.CLARIFY: TurnIntentExpectedOutput.CLARIFICATION,
    TurnIntentMode.REFUSE: TurnIntentExpectedOutput.REFUSAL,
    TurnIntentMode.UNKNOWN: TurnIntentExpectedOutput.EXPLANATION,
}


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
        if mapped_expected_output := _DEFAULT_EXPECTED_OUTPUT_BY_MODE.get(self.mode):
            self.expected_output = mapped_expected_output
        return self

    def to_trace_data(self) -> dict[str, object]:
        data: dict[str, object] = {
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


class TurnIntentClassification(BaseModel):
    """Typed, model-produced task-shape classification.

    Deterministic code consumes this as an input contract; it still decides
    authority, context availability, and safety overrides.
    """

    model_config = ConfigDict(extra="forbid")

    mode: TurnIntentMode = TurnIntentMode.UNKNOWN
    expected_output: TurnIntentExpectedOutput | None = None
    required_context: list[RequiredContextKey] = Field(default_factory=list)
    confidence: float = Field(default=0.0, ge=0.0, le=1.0)
    target_entities: dict[str, list[str]] = Field(default_factory=dict)
    missing_context_question: str | None = None
    reason_codes: list[TurnIntentReasonCode] = Field(default_factory=list)

    @field_validator("required_context", mode="after")
    @classmethod
    def _dedupe_required_context(cls, value: list[RequiredContextKey]) -> list[RequiredContextKey]:
        return list(dict.fromkeys(value))

    @field_validator("reason_codes", mode="after")
    @classmethod
    def _dedupe_reason_codes(cls, value: list[TurnIntentReasonCode]) -> list[TurnIntentReasonCode]:
        return list(dict.fromkeys(value))

    @field_validator("target_entities", mode="after")
    @classmethod
    def _dedupe_target_entities(cls, value: dict[str, list[str]]) -> dict[str, list[str]]:
        return {
            str(entity_type): list(dict.fromkeys(str(entity).strip() for entity in entities if str(entity).strip()))
            for entity_type, entities in value.items()
            if str(entity_type).strip()
        }

    def expected_output_or_default(self) -> TurnIntentExpectedOutput:
        return self.expected_output or _DEFAULT_EXPECTED_OUTPUT_BY_MODE[self.mode]

    def to_trace_data(self) -> dict[str, object]:
        data: dict[str, object] = {
            "mode": self.mode.value,
            "expected_output": self.expected_output_or_default().value,
            "required_context": [key.value for key in self.required_context],
            "confidence": self.confidence,
            "reason_codes": [reason.value for reason in self.reason_codes],
        }
        if self.target_entities:
            data["target_entity_types"] = sorted(self.target_entities)
        if self.missing_context_question:
            data["has_missing_context_question"] = True
        return data


class TurnIntentClassifierResult(BaseModel):
    model_config = ConfigDict(extra="forbid")

    classification: TurnIntentClassification | None = None
    failure_kind: TurnIntentClassifierFailureKind | None = None

    @model_validator(mode="after")
    def _validate_result_shape(self) -> TurnIntentClassifierResult:
        if (self.classification is None) == (self.failure_kind is None):
            raise ValueError("classifier result must contain exactly one of classification or failure_kind")
        return self

    @classmethod
    def success(cls, classification: TurnIntentClassification) -> TurnIntentClassifierResult:
        return cls(classification=classification)

    @classmethod
    def failure(cls, failure_kind: TurnIntentClassifierFailureKind) -> TurnIntentClassifierResult:
        return cls(failure_kind=failure_kind)

    @property
    def is_success(self) -> bool:
        return self.classification is not None

    @property
    def is_transient_failure(self) -> bool:
        return self.failure_kind in (
            TurnIntentClassifierFailureKind.TIMEOUT,
            TurnIntentClassifierFailureKind.PROVIDER_ERROR,
        )


_GOAL_MAX_CHARS = 240


def _normalize_user_goal(user_message: str) -> str:
    goal = redact_raw_secrets_for_prompt((user_message or "").strip())
    if len(goal) <= _GOAL_MAX_CHARS:
        return goal
    return goal[: _GOAL_MAX_CHARS - 3].rstrip() + "..."


def _has_latest_assistant_turn(chat_history: list[WorkflowCopilotChatHistoryMessage]) -> bool:
    return any(
        message.sender == WorkflowCopilotChatSender.AI and (message.content or "").strip() for message in chat_history
    )


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


def _normalize_ref_text(value: str) -> str:
    return re.sub(r"[^a-z0-9]+", " ", value.lower()).strip()


def _add_block_label_lookup(
    lookup: dict[tuple[str, str | None], list[str]],
    key: tuple[str, str | None],
    label: str,
) -> None:
    lookup.setdefault(key, [])
    if label not in lookup[key]:
        lookup[key].append(label)


def _workflow_block_label_lookup(workflow_yaml: str | None) -> dict[tuple[str, str | None], list[str]]:
    lookup: dict[tuple[str, str | None], list[str]] = {}
    for label in sorted(_workflow_block_labels(workflow_yaml)):
        normalized = _normalize_ref_text(label)
        if not normalized:
            continue
        _add_block_label_lookup(lookup, (normalized, None), label)
        for kind in ("block", "step"):
            suffix = f" {kind}"
            if normalized.endswith(suffix):
                alias = normalized[: -len(suffix)].strip()
                if alias:
                    _add_block_label_lookup(lookup, (alias, kind), label)
    return lookup


def _lookup_block_label(
    lookup: dict[tuple[str, str | None], list[str]],
    normalized_ref: str,
    *,
    kind: str | None = None,
) -> str | None:
    keys: list[tuple[str, str | None]] = []
    if kind:
        keys.append((normalized_ref, kind))
    keys.append((normalized_ref, None))
    for key in keys:
        labels = lookup.get(key, [])
        if len(labels) == 1:
            return labels[0]
    return None


def _merge_target_entities(target_entities: dict[str, list[str]], additions: dict[str, list[str]]) -> None:
    for entity_type, entities in additions.items():
        target_entities[entity_type] = list(dict.fromkeys([*target_entities.get(entity_type, []), *entities]))


def _clean_string_list(raw: object, *, max_values: int = _TARGET_ENTITY_MAX_VALUES) -> list[str]:
    if not isinstance(raw, list):
        return []
    values: list[str] = []
    for item in raw:
        value = str(item).strip()[:_TARGET_ENTITY_VALUE_MAX_CHARS]
        if value:
            values.append(value)
        if len(values) >= max_values:
            break
    return list(dict.fromkeys(values))


def _coerce_confidence(value: object) -> float:
    if isinstance(value, bool) or not isinstance(value, (float, int, str)):
        return 0.0
    try:
        confidence = float(value)
    except ValueError:
        return 0.0
    return min(1.0, max(0.0, confidence))


def _coerce_mode(value: object) -> TurnIntentMode | None:
    try:
        return TurnIntentMode(str(value))
    except ValueError:
        return None


def _coerce_expected_output(value: object) -> TurnIntentExpectedOutput | None:
    if value is None:
        return None
    try:
        return TurnIntentExpectedOutput(str(value))
    except ValueError:
        return None


def _coerce_required_context(raw: object) -> list[RequiredContextKey]:
    values = _clean_string_list(raw, max_values=len(RequiredContextKey))
    required_context: list[RequiredContextKey] = []
    for value in values:
        try:
            required_context.append(RequiredContextKey(value))
        except ValueError:
            continue
    return list(dict.fromkeys(required_context))


def _coerce_reason_codes(raw: object) -> list[TurnIntentReasonCode]:
    values = _clean_string_list(raw, max_values=len(TurnIntentReasonCode))
    reason_codes: list[TurnIntentReasonCode] = []
    for value in values:
        try:
            reason_code = TurnIntentReasonCode(value)
        except ValueError:
            continue
        if reason_code in _CLASSIFIER_REASON_CODES:
            reason_codes.append(reason_code)
    return list(dict.fromkeys(reason_codes))


def _coerce_target_entities(raw: object) -> dict[str, list[str]]:
    if not isinstance(raw, Mapping):
        return {}
    entities: dict[str, list[str]] = {}
    for entity_type, values in raw.items():
        key = str(entity_type).strip()
        if not key:
            continue
        cleaned_values = _clean_string_list(values)
        if cleaned_values:
            entities[key] = cleaned_values
    return entities


def _decode_classifier_response(raw: object) -> dict[str, object] | None:
    if isinstance(raw, str):
        raw = parse_final_response(raw)
    if not isinstance(raw, Mapping):
        return None
    return {str(key): value for key, value in raw.items()}


def _turn_intent_classification_from_raw(raw: object) -> TurnIntentClassification | None:
    payload = _decode_classifier_response(raw)
    if payload is None:
        return None
    mode = _coerce_mode(payload.get("mode"))
    if mode is None:
        return None
    missing_context_question = payload.get("missing_context_question")
    return TurnIntentClassification(
        mode=mode,
        expected_output=_coerce_expected_output(payload.get("expected_output")),
        required_context=_coerce_required_context(payload.get("required_context")),
        confidence=_coerce_confidence(payload.get("confidence")),
        target_entities=_coerce_target_entities(payload.get("target_entities")),
        missing_context_question=missing_context_question.strip()
        if isinstance(missing_context_question, str) and missing_context_question.strip()
        else None,
        reason_codes=_coerce_reason_codes(payload.get("reason_codes")),
    )


def _block_ref_candidates(value: str) -> list[tuple[str, str | None]]:
    normalized = _normalize_ref_text(value)
    if not normalized:
        return []
    candidates: list[tuple[str, str | None]] = []
    for article in ("the ", "a ", "an "):
        if normalized.startswith(article):
            normalized = normalized[len(article) :].strip()
            break
    for kind in ("block", "step"):
        suffix = f" {kind}"
        if normalized.endswith(suffix):
            alias = normalized[: -len(suffix)].strip()
            if alias:
                candidates.append((alias, kind))
    candidates.append((normalized, None))
    return list(dict.fromkeys(candidates))


def _resolve_classified_block_targets(values: list[str], workflow_yaml: str | None) -> tuple[list[str], list[str]]:
    lookup = _workflow_block_label_lookup(workflow_yaml)
    resolved: list[str] = []
    unresolved: list[str] = []
    for value in values:
        label = None
        for normalized_ref, kind in _block_ref_candidates(value):
            label = _lookup_block_label(lookup, normalized_ref, kind=kind)
            if label:
                break
        if label:
            resolved.append(label)
        else:
            unresolved.append(value)
    return list(dict.fromkeys(resolved)), list(dict.fromkeys(unresolved))


def _normalize_classified_target_entities(
    target_entities: dict[str, list[str]],
    workflow_yaml: str | None,
) -> dict[str, list[str]]:
    normalized_entities = {key: list(values) for key, values in target_entities.items() if values}
    block_values = normalized_entities.pop("block", [])
    if block_values:
        resolved, unresolved = _resolve_classified_block_targets(block_values, workflow_yaml)
        if resolved:
            _merge_target_entities(normalized_entities, {"block": resolved})
        if unresolved:
            _merge_target_entities(normalized_entities, {UNRESOLVED_BLOCK_REF_TARGET_ENTITY: unresolved})
    return normalized_entities


def _has_specific_edit_target(target_entities: dict[str, list[str]]) -> bool:
    if any(target_entities.get(entity_type) for entity_type in _EDIT_SPECIFIC_TARGET_ENTITY_TYPES):
        return True
    return any(target != "current_workflow" for target in target_entities.get("workflow", []))


async def classify_turn_intent(
    *,
    user_message: str,
    workflow_yaml: str,
    chat_history: list[WorkflowCopilotChatHistoryMessage],
    global_llm_context: str,
    request_policy: RequestPolicy,
    handler: LLMAPIHandler | None,
) -> TurnIntentClassifierResult:
    if not isinstance(user_message, str) or not user_message.strip():
        LOG.info("turn-intent classifier skipped empty user message")
        return TurnIntentClassifierResult.failure(TurnIntentClassifierFailureKind.EMPTY_MESSAGE)
    if handler is None:
        LOG.info("turn-intent classifier has no LLM handler available")
        return TurnIntentClassifierResult.failure(TurnIntentClassifierFailureKind.MISSING_HANDLER)

    safe_user_message = redact_raw_secrets_for_prompt(user_message)
    safe_global_llm_context = sanitize_global_llm_context_for_prompt(global_llm_context)
    transcript = build_transcript_context(chat_history, safe_user_message)
    try:
        prompt = prompt_engine.load_prompt(
            template=PROMPT_NAME,
            mode_values=", ".join(mode.value for mode in TurnIntentMode),
            expected_output_values=", ".join(output.value for output in TurnIntentExpectedOutput),
            required_context_values=", ".join(key.value for key in RequiredContextKey),
            reason_code_values=", ".join(reason.value for reason in _CLASSIFIER_REASON_CODES),
            user_message=escape_code_fences(safe_user_message),
            request_policy_summary=escape_code_fences(request_policy.prompt_summary()),
            workflow_yaml=escape_code_fences(
                redact_raw_secrets_for_prompt(workflow_yaml)[:_WORKFLOW_YAML_PROMPT_MAX_CHARS]
            ),
            earliest_user_turn=transcript.earliest_user_turn,
            latest_prior_user_turn=transcript.latest_prior_user_turn,
            latest_assistant_turn=transcript.latest_assistant_turn,
            retained_history=transcript.retained_history,
            global_llm_context=escape_code_fences(
                redact_raw_secrets_for_prompt(safe_global_llm_context)[:_GLOBAL_CONTEXT_PROMPT_MAX_CHARS]
            ),
        )
    except Exception as exc:
        LOG.warning("turn-intent classifier prompt render failed", error=str(exc))
        return TurnIntentClassifierResult.failure(TurnIntentClassifierFailureKind.PROMPT_RENDER_ERROR)

    timeout = settings.COPILOT_TURN_INTENT_CLASSIFIER_TIMEOUT_SECONDS
    started_at = time.monotonic()
    try:
        raw = await asyncio.wait_for(
            handler(prompt=prompt, prompt_name=PROMPT_NAME),
            timeout=timeout,
        )
    except asyncio.TimeoutError:
        LOG.warning(
            "turn-intent classifier timed out",
            failure_kind=TurnIntentClassifierFailureKind.TIMEOUT.value,
            timeout=timeout,
        )
        return TurnIntentClassifierResult.failure(TurnIntentClassifierFailureKind.TIMEOUT)
    except Exception as exc:
        elapsed_seconds = time.monotonic() - started_at
        timeout_margin = min(0.25, max(0.001, timeout * 0.05))
        if elapsed_seconds + timeout_margin >= timeout:
            LOG.warning(
                "turn-intent classifier timed out",
                failure_kind=TurnIntentClassifierFailureKind.TIMEOUT.value,
                timeout=timeout,
                elapsed_seconds=elapsed_seconds,
                converted_error_type=type(exc).__name__,
                error=str(exc),
            )
            return TurnIntentClassifierResult.failure(TurnIntentClassifierFailureKind.TIMEOUT)
        LOG.warning(
            "turn-intent classifier provider failed",
            failure_kind=TurnIntentClassifierFailureKind.PROVIDER_ERROR.value,
            error=str(exc),
        )
        return TurnIntentClassifierResult.failure(TurnIntentClassifierFailureKind.PROVIDER_ERROR)

    classification = _turn_intent_classification_from_raw(raw)
    if classification is None:
        LOG.warning(
            "turn-intent classifier returned malformed output",
            failure_kind=TurnIntentClassifierFailureKind.MALFORMED_OUTPUT.value,
        )
        return TurnIntentClassifierResult.failure(TurnIntentClassifierFailureKind.MALFORMED_OUTPUT)

    with copilot_span("turn_intent_classifier", data=classification.to_trace_data()):
        LOG.info("turn-intent classifier decision", **classification.to_trace_data())
    return TurnIntentClassifierResult.success(classification)


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
    classifier_result: TurnIntentClassifierResult | None = None,
) -> TurnIntent:
    has_workflow = bool((workflow_yaml or "").strip())
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

    classification = classifier_result.classification if classifier_result and classifier_result.is_success else None
    has_prior_run_signal = workflow_run_id is not None or _has_structured_prior_run_signal(global_llm_context)

    if classification is not None and TurnIntentReasonCode.STRUCTURALLY_INFEASIBLE in classification.reason_codes:
        infeasibility_question = (classification.missing_context_question or "").strip()
        if not infeasibility_question:
            # Questionless infeasibility fails open: drop the verdict and proceed at request-policy
            # authority rather than stranding the turn in an answerless CLARIFY.
            LOG.warning("turn-intent dropped questionless structural-infeasibility verdict, proceeding")
            classification = classification.model_copy(
                update={
                    "mode": TurnIntentMode.UNKNOWN,
                    "reason_codes": [
                        code
                        for code in classification.reason_codes
                        if code != TurnIntentReasonCode.STRUCTURALLY_INFEASIBLE
                    ],
                }
            )
        elif classification.mode != TurnIntentMode.CLARIFY:
            # Force CLARIFY so the pre-loop bail fires; otherwise the turn enters the agent loop with
            # mutation authority on a blocked request. Clear edit targets that don't belong on a CLARIFY.
            classification = classification.model_copy(
                update={
                    "mode": TurnIntentMode.CLARIFY,
                    "expected_output": TurnIntentExpectedOutput.CLARIFICATION,
                    "target_entities": {},
                }
            )

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
    elif classification is not None and classification.mode != TurnIntentMode.UNKNOWN:
        mode = classification.mode
        expected_output = classification.expected_output_or_default()
        confidence = classification.confidence
        missing_context_question = classification.missing_context_question
        required_context.extend(classification.required_context)
        _merge_target_entities(
            target_entities,
            _normalize_classified_target_entities(classification.target_entities, workflow_yaml),
        )
        reason_codes.append(TurnIntentReasonCode.LLM_CLASSIFIER)
        reason_codes.extend(classification.reason_codes)
    elif request_policy.testing_intent == "skip_test":
        mode = TurnIntentMode.DRAFT_ONLY
        expected_output = TurnIntentExpectedOutput.WORKFLOW_DRAFT
        confidence = 0.6
        reason_codes.append(TurnIntentReasonCode.TESTING_INTENT_SKIP_TEST)
    elif classification is not None:
        confidence = classification.confidence
        required_context.extend(classification.required_context)
        _merge_target_entities(
            target_entities,
            _normalize_classified_target_entities(classification.target_entities, workflow_yaml),
        )
        reason_codes.append(TurnIntentReasonCode.LLM_CLASSIFIER)
        reason_codes.extend(classification.reason_codes)

    if (
        classification is not None
        and mode in _MUTATING_CLASSIFIER_MODES
        and confidence < _LOW_CONFIDENCE_MUTATION_THRESHOLD
    ):
        mode = TurnIntentMode.CLARIFY
        expected_output = TurnIntentExpectedOutput.CLARIFICATION
        missing_context_question = missing_context_question or "What workflow should I build or change?"
        reason_codes.append(TurnIntentReasonCode.LOW_CONFIDENCE_CLARIFICATION)

    if mode == TurnIntentMode.EDIT and not _has_specific_edit_target(target_entities):
        mode = TurnIntentMode.CLARIFY
        expected_output = TurnIntentExpectedOutput.CLARIFICATION
        confidence = min(confidence, _LOW_CONFIDENCE_MUTATION_THRESHOLD - 0.01)
        missing_context_question = missing_context_question or "What change should I make to this workflow?"
        reason_codes.append(TurnIntentReasonCode.MISSING_EDIT_TARGET)

    if _user_signals_non_progress(user_message, chat_history):
        reason_codes.append(TurnIntentReasonCode.USER_NON_PROGRESS)

    has_request_policy_update_or_run_authority = authority.may_update_workflow or authority.may_run_blocks
    suppress_prior_run_recovery = (
        mode == TurnIntentMode.UNKNOWN
        and has_prior_run_signal
        and classifier_result is not None
        and classifier_result.is_transient_failure
        and has_request_policy_update_or_run_authority
    )
    if suppress_prior_run_recovery:
        mode = TurnIntentMode.BUILD
        expected_output = (
            TurnIntentExpectedOutput.WORKFLOW_UPDATE
            if has_workflow or has_prior_context or workflow_id or workflow_permanent_id
            else TurnIntentExpectedOutput.WORKFLOW_DRAFT
        )
        confidence = max(confidence, 0.6)
        reason_codes.append(TurnIntentReasonCode.TRANSIENT_CLASSIFIER_FALLBACK)
    elif mode == TurnIntentMode.UNKNOWN and has_prior_run_signal:
        mode = TurnIntentMode.DIAGNOSE
        expected_output = TurnIntentExpectedOutput.RUN_RESULT
        reason_codes.append(TurnIntentReasonCode.RECOVERY_FROM_RUN_CONTEXT)

    if mode == TurnIntentMode.DOCS_ANSWER:
        authority.may_update_workflow = False
        authority.may_run_blocks = False
        required_context.append(RequiredContextKey.DOCS_CONTEXT)
    elif mode == TurnIntentMode.DRAFT_ONLY:
        authority.may_run_blocks = False
    elif mode == TurnIntentMode.CLARIFY:
        authority.may_update_workflow = False
        authority.may_run_blocks = False
        authority.requires_user_input = True
    elif mode == TurnIntentMode.REFUSE:
        authority.may_update_workflow = False
        authority.may_run_blocks = False
        authority.requires_user_input = True
    elif mode == TurnIntentMode.DIAGNOSE:
        authority.may_update_workflow = False
        retest_mandated = (
            request_policy.testing_intent == "require_test"
            and request_policy.allow_run_blocks
            and classification is not None
            and classification.mode == TurnIntentMode.DIAGNOSE
        )
        if retest_mandated:
            # RequestPolicy owns testing policy; an explicit require_test re-run must not be inverted
            # by the diagnose classification.
            authority.may_run_blocks = True
            reason_codes.append(TurnIntentReasonCode.TESTING_INTENT_RUN_OVERRIDES_DIAGNOSE)
        else:
            authority.may_run_blocks = False
        if RequiredContextKey.LATEST_RUN_RESULT not in required_context:
            required_context.append(RequiredContextKey.LATEST_RUN_RESULT)

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
