from __future__ import annotations

import asyncio
import json
import re
import time
from dataclasses import dataclass, field, replace
from typing import Any, Literal, cast, get_args
from urllib.parse import urlparse

import structlog

from skyvern.config import settings
from skyvern.forge import app
from skyvern.forge.prompts import prompt_engine
from skyvern.forge.sdk.copilot.config import CopilotConfig
from skyvern.forge.sdk.copilot.context import StructuredContext, sanitize_global_llm_context_for_prompt
from skyvern.forge.sdk.copilot.llm_errors import is_retriable_llm_error
from skyvern.forge.sdk.copilot.output_utils import parse_final_response
from skyvern.forge.sdk.copilot.request_slots import (
    CanonicalRequestSlotV1,
    RequestSlotContractV1,
    RequestSlotProducerInputV1,
    produce_request_slots,
    request_slot_source_text,
    request_slot_sources,
)
from skyvern.forge.sdk.copilot.secret_redaction import (
    RAW_SECRET_PATTERNS,
    contains_email_password_pair,
    redact_raw_secrets_for_prompt,
)
from skyvern.forge.sdk.copilot.tracing_setup import copilot_span
from skyvern.forge.sdk.copilot.workflow_credential_utils import workflow_credential_ids, workflow_credential_origins
from skyvern.forge.sdk.schemas.credentials import Credential
from skyvern.forge.sdk.schemas.workflow_copilot import (
    WorkflowCopilotChatHistoryMessage,
    WorkflowCopilotChatSender,
)
from skyvern.forge.sdk.workflow.models.parameter import ParameterType
from skyvern.utils.strings import escape_code_fences
from skyvern.utils.yaml_loader import safe_load_no_dates

LOG = structlog.get_logger()
PROMPT_NAME = "workflow-copilot-request-policy"
_TESTING_INTENTS = {"require_test", "skip_test", "unspecified"}
_AUTHORING_INTENTS = {"author_now", "defer_authoring"}
DEFER_AUTHORING_DURABLE_FILL_CRITERION_ID = "defer_authoring_durable_fill"
_KINDS = {"none", "raw_secret", "credential_id", "credential_name", "website_stored_credential", "placeholder"}
_RAW_SECRET_HANDLINGS = {"none", "block", "redacted_draft"}
_CLASSIFIER_FAILURE_KINDS = {
    "none",
    "missing_handler",
    "raw_secret_no_handler",
    "timeout",
    "transient_error",
    "provider_error",
}
_CLASSIFICATION_RESPONSE_FIELDS = {
    "testing_intent",
    "authoring_intent",
    "credential_input_kind",
    "credential_refs",
    "login_page_urls",
    "requires_user_clarification",
    "completion_contract",
    "completion_criteria",
    "raw_secret_evidence",
    "raw_secret_handling",
    "clarification_reason",
}
REGISTERED_DOWNLOAD_REQUESTED_OUTPUT_PATHS = frozenset(
    {
        "output.downloaded_files",
        "output.downloaded_file_urls",
        "output.downloaded_file_artifact_ids",
    }
)
_LONE_REGISTERED_DOWNLOAD_OUTPUT_PATH = "output.downloaded_files"
ClarificationReason = Literal[
    "none",
    "raw_secret",
    "credential_name_unresolved",
    "credential_invention_requested",
    "ambiguous_loop_edit",
    "invalid_conditional_container",
    "missing_conditional_condition",
    "missing_target_context",
    "workflow_credential_inputs_unbound",
]
RawSecretHandling = Literal["none", "block", "redacted_draft"]
_VALID_CLARIFICATION_REASONS: frozenset[ClarificationReason] = frozenset(get_args(ClarificationReason))
# Gates guardrails.py's deferred-draft tool authority — narrower than the prompt set below.
CREDENTIAL_DEFERRED_DRAFT_REASONS: frozenset[ClarificationReason] = frozenset(
    {"workflow_credential_inputs_unbound", "credential_name_unresolved"}
)
# Broader: any reason credential_prompt_reason() should surface an add-credential CTA for.
CREDENTIAL_PROMPT_CLARIFICATION_REASONS: frozenset[ClarificationReason] = frozenset(
    {"raw_secret", "credential_name_unresolved", "credential_invention_requested", "workflow_credential_inputs_unbound"}
)
_PRE_RESOLUTION_CLARIFICATION_REASONS = {
    "credential_invention_requested",
    "ambiguous_loop_edit",
    "invalid_conditional_container",
    "missing_conditional_condition",
    "missing_target_context",
}
_REASONS_OVERRIDDEN_BY_CREDENTIAL_REFS = {
    "ambiguous_loop_edit",
    "invalid_conditional_container",
    "missing_conditional_condition",
    "missing_target_context",
}
_CREDENTIALS_UI_DIRECTIONS = (
    f"You can find or add saved credentials at {settings.SKYVERN_APP_URL.rstrip('/')}/credentials."
)
# Matches any final reply containing these substrings, not just credential-blocking
# ones; safe today because every such emitter routes through _CREDENTIALS_UI_DIRECTIONS.
_CREDENTIAL_PROMPT_TEXT_MARKERS = ("/credentials", "credentials ui")
# Stable tail of every raw-secret refusal; transcript redaction keys off it, so all refusal emitters must keep it verbatim.
RAW_SECRET_REFUSAL_SENTINEL = "DO NOT PROVIDE RAW LOGIN/PASSWORD"
_RAW_SECRET_QUESTION = (
    "Please do not paste raw login credentials or secrets in chat because they can enter model telemetry and execution traces. "
    "Store the credential in the Skyvern Credentials UI and reply with its exact saved credential name or a credential ID beginning with cred_. "
    f"{_CREDENTIALS_UI_DIRECTIONS} "
    f"{RAW_SECRET_REFUSAL_SENTINEL}."
)
_SAVED_CREDENTIAL_NAME_QUESTION_STABLE_PREFIX = "Which saved credential should I use? Please provide the exact credential name or a credential ID beginning with cred_."
_SAVED_CREDENTIAL_NAME_QUESTION = f"{_SAVED_CREDENTIAL_NAME_QUESTION_STABLE_PREFIX} {_CREDENTIALS_UI_DIRECTIONS}"
_STORED_CREDENTIAL_URL_QUESTION_STABLE_PREFIX = (
    "Which website or login page should I use to look up the stored credential?"
)
_STORED_CREDENTIAL_URL_QUESTION = f"{_STORED_CREDENTIAL_URL_QUESTION_STABLE_PREFIX} {_CREDENTIALS_UI_DIRECTIONS}"
_CREDENTIAL_ID_RE = re.compile(r"\bcred_[A-Za-z0-9][A-Za-z0-9_-]*\b")
# A credential ID typed with the wrong separator (`cred 530…`, `cred-530…`). The
# digit-only body and length floor keep this off prose like `cred and the password`.
_MALFORMED_CREDENTIAL_ID_RE = re.compile(r"\bcred[ \t\-]+([0-9]{12,})\b")
_JINJA_TEMPLATE_VAR_RE = re.compile(r"\{\{\s*([a-zA-Z_][a-zA-Z0-9_]*)\s*\}\}")
_CREDENTIAL_PARAM_METADATA_FIELDS = frozenset(
    {
        "parameter_type",
        "key",
        "description",
        "workflow_id",
        "created_at",
        "modified_at",
        "deleted_at",
    }
)
_LOGIN_CREDENTIAL_REQUIRED_KEY_FIELDS = ("username_key", "password_key")
_WORKFLOW_CREDENTIAL_INPUTS_UNBOUND_QUESTION = (
    "I couldn't find the required credentials for the existing workflow. "
    f"Please add them via the Credentials UI and I can try again. {_CREDENTIALS_UI_DIRECTIONS}"
)
_INVALID_CONDITIONAL_CONTAINER_MARKERS = (
    "into the conditional",
    "inside the conditional",
    "within the conditional",
    "into conditional",
    "inside conditional",
    "within conditional",
)
_QUOTED_CREDENTIAL_NAME_RE = re.compile(r"(?:`([^`]{1,100})`|\"([^\"]{1,100})\"|'([^']{1,100})')")
_NAMED_CREDENTIAL_TOKEN_RE = re.compile(
    r"\b(?:saved\s+credential|credential)\s+(?:named|called)\s+([A-Za-z0-9_.@:-]{2,100})\b",
    re.I,
)
_CREDENTIAL_QUOTE_CONTEXT_RE = re.compile(r"\b(?:credentials?|log[\s-]?in)\b", re.I)
_CODE_BLOCK_AUTHORING_MARKERS = ("code block", "code-block", "codeblock")
_LOGIN_BLOCK_BAN_MARKERS = ("do not create a login block", "don't create a login block", "no login block")
_CREDENTIAL_CODE_MARKERS = ("saved credential", "login_credentials", ".otp()", "one-time-code")


_MAX_COMPLETION_CRITERIA = 8
_MAX_TRACE_COMPLETION_CRITERIA = 8
_COMPLETION_CRITERION_OUTCOME_MAX_CHARS = 200
_COMPLETION_CRITERION_CONTINGENT_ON_MAX_CHARS = 200
_COMPLETION_CRITERION_EXPECTED_VALUE_MAX_CHARS = 500
_COMPLETION_CRITERION_CLASSIFICATION_TARGET_MAX_CHARS = 120
_CLASSIFICATION_OUTPUT_KEY_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
_CONTINGENT_ANTECEDENT_OUTPUT_PATH_RE = re.compile(r"^output\.[A-Za-z_][A-Za-z0-9_]*$")
_REQUESTED_OUTPUT_CRITERION_ID_PREFIX = "__copilot_requested_output__"
_VALIDATION_CLASSIFICATION_BOOLEAN_OUTPUT_TARGETS: dict[str, tuple[str, bool]] = {
    "output.login_only": ("login_only", True),
    "output.login_gated": ("login_gated", True),
}
_VALIDATION_CLASSIFICATION_LABEL_OUTPUT_TARGETS: dict[str, str] = {
    "output.path_classification": "path_classification",
}

FALLBACK_FLOOR_CRITERION_ID_PREFIX = "__copilot_fallback_floor__"
_FALLBACK_FLOOR_BASE_ID = f"{FALLBACK_FLOOR_CRITERION_ID_PREFIX}run"
_FALLBACK_FLOOR_CREDENTIAL_ID = f"{FALLBACK_FLOOR_CRITERION_ID_PREFIX}credential"
_FALLBACK_FLOOR_BASE_OUTCOME = "The workflow runs to its intended end state with the expected output."
_FALLBACK_FLOOR_CREDENTIAL_OUTCOME = "The credentialed step authenticates and reaches the post-login state."

CriterionLevel = Literal["definition", "run"]
_CRITERION_LEVELS: frozenset[str] = frozenset({"definition", "run"})
CriterionKind = Literal["outcome", "terminal_action", "validation_classification"]
TerminalActionFamily = Literal["request", "application", "form", "order"]
ClassificationTarget = str | bool
ExpectedOutputValue = str | bool
ExpectedOutputShape = Literal[
    "reference_code",
    "numeric_identifier",
    "date",
    "address",
    "status_label",
    "money_amount",
    "owner_label",
    "goal_judgment_boolean",
]
RequestedOutputEvidenceSource = Literal[
    "runtime_output",
    "independent_run_evidence",
    "registered_output_parameter",
    "registered_artifact_content",
]
JudgmentPredicate = Literal["login_gate_blocks_target"]
MintDisposition = Literal["pending", "decidable", "degraded"]
Pinability = Literal["pinned", "shapeless_valid", "unpinnable"]
_JUDGMENT_PREDICATES: frozenset[str] = frozenset(get_args(JudgmentPredicate))
MintDegrade = Literal[
    "turn_unsatisfiable_fallback",
    "contingent_missing_antecedent",
    "undecidable_judgment",
]
MINT_DEGRADE_VALUES: frozenset[str] = frozenset(get_args(MintDegrade))
_CRITERION_KINDS: frozenset[str] = frozenset({"outcome", "terminal_action", "validation_classification"})
_TERMINAL_ACTION_FAMILIES: frozenset[str] = frozenset({"request", "application", "form", "order"})
_EXPECTED_OUTPUT_SHAPES: frozenset[str] = frozenset(get_args(ExpectedOutputShape))
_REQUESTED_OUTPUT_EVIDENCE_SOURCES: frozenset[str] = frozenset(get_args(RequestedOutputEvidenceSource))
RequestedOutputPathMintSource = Literal["classifier_default"]
REQUESTED_OUTPUT_PATH_MINT_SOURCES: frozenset[str] = frozenset(get_args(RequestedOutputPathMintSource))

_OUTPUT_INTENT_RE = re.compile(
    r"\b(?:read|capture|extract|output|return|returns|returned|include|includes|including|"
    r"final\s+(?:extracted\s+)?fields?|result\s+records?|returned\s+records?)\b",
    re.I,
)
_OUTPUT_SPAN_END_RE = re.compile(r"[\n!?]")
_OUTPUT_METHOD_TAIL_RE = re.compile(
    r"\b(?:by|via|using|after|before|then|click(?:ing)?|open(?:ing)?|select(?:ing)?|"
    r"choose|choosing|search(?:ing)?|navigate|go\s+to)\b.*",
    re.I,
)
_OUTPUT_SPLIT_RE = re.compile(r",|;|\band\b|\bplus\b|&", re.I)
_OUTPUT_FIELD_CONNECTOR_RE = re.compile(
    r"\b(?:with|including|include|includes|containing|contains|fields?|result)\b[:\s]+",
    re.I,
)
_OUTPUT_NEGATED_INTENT_PREFIX_RE = re.compile(
    r"(?:\b(?:do|does|did|should|must|can)\s+not\s+|\b(?:don't|doesn't|didn't|shouldn't|mustn't|can't|cannot|never|without)\s+)$",
    re.I,
)
_OUTPUT_EXPLICIT_FIELD_KEY_RE = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")
_OUTPUT_NAMED_FIELD_RE = re.compile(
    r"\b(?:"
    r"(?:(?:requested[-\s]+output|output|return(?:ed)?|final|structured)\s+)?fields?\s+(?:named|called)"
    r"|(?:requested[-\s]+)?output\s+(?:named|called)"
    r"|named\s+(?:requested[-\s]+)?output"
    r")\s+`?([A-Za-z_][A-Za-z0-9_]*)`?\b",
    re.I,
)
_OUTPUT_ACRONYM_RE = re.compile(r"\b[A-Z][A-Z0-9]{1,10}\b")
_OUTPUT_FIELD_WORDS = frozenset(
    "address addresses date dates email emails id identifier identifiers license licenses location locations "
    "amount amounts domain domains name names number numbers owner owners phone phones rate rates specialties specialty "
    "status statuses taxonomy total totals url urls website websites".split()
)
# Intentionally distinct from enforcement._COVERAGE_GENERIC_TOKENS: this list filters intent prose when
# parsing requested-output field names, so it drops phrase-level noise ("each", "profile", "structured");
# the coverage list filters path leaf tokens only. Not unified — the consumers differ.
_OUTPUT_GENERIC_WORDS = frozenset(
    "a all an data detail details each entity final for information its of output outputs profile record records "
    "result results structured the value values".split()
)
_OUTPUT_METHOD_WORDS = frozenset("choose click open plan search select setup show".split())
_OUTPUT_LEADING_FIELD_WORDS = frozenset(
    "capture captured extract extracted include included includes output read return returned".split()
)
_OUTPUT_INPUT_ONLY_WORDS = frozenset({"input", "inputs", "parameter", "parameters", "reusable"})
_OUTPUT_OUTCOME_WORDS = frozenset(
    "capture captured extract extracted final include included includes output read record result return returned".split()
)


@dataclass(frozen=True)
class JudgmentTruthCondition:
    """Per-polarity page-evidence predicate a judgment boolean is decidable by. ``polarity_when_holds``
    is the emitted boolean value that corresponds to the predicate holding on the independent packet."""

    predicate: JudgmentPredicate
    polarity_when_holds: bool


@dataclass(frozen=True)
class CompletionCriterion:
    id: str
    outcome: str
    contingent_on: str | None = None
    contingent_antecedent_output_path: str | None = None
    deliverable_kind: Literal["registered_download"] | None = None
    # Author-time seam signal only: unlike ``deliverable_kind`` it survives canonicalization onto
    # non-canonical output paths, so it is never rendered to the completion verifier.
    declared_deliverable_kind: Literal["registered_download"] | None = None
    implicit: bool = False
    method_mandated: bool = False
    # "definition": a property of the workflow definition itself, graded against the
    # YAML; "run": an end state only a run can evidence. Invalid input coerces to "run".
    level: CriterionLevel = "run"
    output_path: str | None = None
    expected_output_value: ExpectedOutputValue | None = None
    expected_output_shape: ExpectedOutputShape | None = None
    requested_output_evidence_source: RequestedOutputEvidenceSource = "runtime_output"
    requested_output_path_mint_source: RequestedOutputPathMintSource | None = None
    kind: CriterionKind = "outcome"
    terminal_action_family: TerminalActionFamily | None = None
    classification_output_key: str | None = None
    expected_classification: ClassificationTarget | None = None
    requested_output_corroborator: bool = False
    mint_degrade: MintDegrade | None = None
    judgment_truth_condition: JudgmentTruthCondition | None = None
    requested_output_floor_rekeyed: bool = False
    floor_rekeyed_from_path: str | None = None
    # Typed request-slot metadata. Assertion identity, deduplication, and reconciliation
    # intentionally exclude these fields.
    request_slot_id: str | None = None
    pinability: Pinability | None = None
    mint_disposition: MintDisposition = "decidable"


@dataclass
class RequestPolicy:
    testing_intent: str = "unspecified"
    authoring_intent: str = "author_now"
    credential_input_kind: str = "none"
    credential_refs: list[str] = field(default_factory=list)
    login_page_urls: list[str] = field(default_factory=list)
    requires_user_clarification: bool = False
    allow_update_workflow: bool = True
    allow_run_blocks: bool = True
    allow_missing_credentials_in_draft: bool = False
    # Narrower than the flag above: True only when a credential-specific path (an explicit
    # code-block credential draft, or a redacted raw secret) set it, not the generic
    # skip_test fallthrough that fires for any untested draft regardless of credentials.
    credential_draft_deferred_explicitly: bool = False
    user_response_policy: str = "proceed"
    completion_contract: str | None = None
    completion_criteria: list[CompletionCriterion] = field(default_factory=list)
    resolved_credentials: list[Credential] = field(default_factory=list)
    # Approves persisting a bound credential, not running it: run authority stays
    # scoped to resolved_credentials (ADR 0002).
    discovered_credentials: list[Credential] = field(default_factory=list)
    invalid_credential_ids: list[str] = field(default_factory=list)
    clarification_question: str | None = None
    raw_secret_detected: bool = False
    raw_secret_evidence: str | None = None
    raw_secret_handling: RawSecretHandling = "none"
    clarification_reason: ClarificationReason = "none"
    existing_workflow_credential_ids: list[str] = field(default_factory=list)
    # Sorted at the trace/JSON boundary; YAML traversal uses sets.
    existing_workflow_credential_origins: dict[str, list[str]] = field(default_factory=dict)
    classifier_status: str = "not_run"
    classifier_failure_kind: str = "none"
    classifier_retry_count: int = 0
    classifier_non_runtime_requested_output_evidence_sources: list[str] = field(default_factory=list)
    completion_contract_status: str = "absent"
    request_slot_failure_kind: str | None = None

    def graded_completion_criteria(self) -> list[CompletionCriterion]:
        return [criterion for criterion in self.completion_criteria if not criterion.method_mandated]

    def to_trace_data(self) -> dict[str, Any]:
        data: dict[str, Any] = {
            "testing_intent": self.testing_intent,
            "authoring_intent": self.authoring_intent,
            "credential_input_kind": self.credential_input_kind,
            "clarification_reason": self.clarification_reason,
            "allow_update_workflow": self.allow_update_workflow,
            "allow_run_blocks": self.allow_run_blocks,
            "allow_missing_credentials_in_draft": self.allow_missing_credentials_in_draft,
            "credential_draft_deferred_explicitly": self.credential_draft_deferred_explicitly,
            "resolved_credential_count": len(self.resolved_credentials),
            "has_completion_contract": bool(self.completion_contract),
            "completion_criteria_count": len(self.graded_completion_criteria()),
            "completion_criteria_implicit_count": sum(
                1 for criterion in self.completion_criteria if criterion.implicit
            ),
            "completion_criteria_method_mandated_count": sum(
                1 for criterion in self.completion_criteria if criterion.method_mandated
            ),
            "raw_secret_detected": self.raw_secret_detected,
            "has_raw_secret_evidence": self.raw_secret_evidence is not None,
            "raw_secret_handling": self.raw_secret_handling,
            "classifier_status": self.classifier_status,
            "classifier_failure_kind": self.classifier_failure_kind,
            "classifier_retry_count": self.classifier_retry_count,
            "classifier_non_runtime_requested_output_evidence_source_count": len(
                self.classifier_non_runtime_requested_output_evidence_sources
            ),
            "classifier_non_runtime_requested_output_evidence_sources": list(
                self.classifier_non_runtime_requested_output_evidence_sources
            ),
            "completion_contract_status": self.completion_contract_status,
            "existing_workflow_credential_id_count": len(self.existing_workflow_credential_ids),
            "existing_workflow_credential_origin_count": sum(
                len(origins) for origins in self.existing_workflow_credential_origins.values()
            ),
        }
        requested_output_criteria = [
            criterion for criterion in self.graded_completion_criteria() if criterion.output_path is not None
        ]
        data["requested_output_criteria_count"] = len(requested_output_criteria)
        if self.request_slot_failure_kind is not None:
            data["request_slot_failure_kind"] = self.request_slot_failure_kind
        for index, criterion in enumerate(requested_output_criteria[:_MAX_TRACE_COMPLETION_CRITERIA]):
            prefix = f"requested_output_criterion_{index}"
            data[f"{prefix}_id"] = criterion.id
            data[f"{prefix}_output_path"] = criterion.output_path
            data[f"{prefix}_grounding_mode"] = _criterion_grounding_mode(criterion)
            data[f"{prefix}_has_exact_value"] = criterion.expected_output_value is not None
            if criterion.mint_degrade is not None:
                data[f"{prefix}_mint_degrade"] = criterion.mint_degrade
            data[f"{prefix}_mint_disposition"] = criterion.mint_disposition
            data[f"{prefix}_evidence_source"] = criterion.requested_output_evidence_source
            if criterion.request_slot_id is not None:
                data[f"{prefix}_request_slot_id"] = criterion.request_slot_id
            if criterion.pinability is not None:
                data[f"{prefix}_pinability"] = criterion.pinability
            if criterion.expected_output_shape:
                data[f"{prefix}_expected_output_shape"] = criterion.expected_output_shape
        mint_degraded_criteria = [
            criterion for criterion in self.completion_criteria if criterion.mint_degrade is not None
        ]
        data["mint_degraded_criterion_count"] = len(mint_degraded_criteria)
        for index, criterion in enumerate(mint_degraded_criteria[:_MAX_TRACE_COMPLETION_CRITERIA]):
            prefix = f"mint_degraded_criterion_{index}"
            data[f"{prefix}_id"] = criterion.id
            data[f"{prefix}_mint_degrade"] = criterion.mint_degrade
        return data

    def prompt_summary(self) -> str:
        lines = [
            f"testing_intent: {self.testing_intent}",
            f"authoring_intent: {self.authoring_intent}",
            f"credential_input_kind: {self.credential_input_kind}",
            f"clarification_reason: {self.clarification_reason}",
            f"allow_update_workflow: {self.allow_update_workflow}",
            f"allow_run_blocks: {self.allow_run_blocks}",
            f"allow_missing_credentials_in_draft: {self.allow_missing_credentials_in_draft}",
            f"raw_secret_handling: {self.raw_secret_handling}",
            f"classifier_status: {self.classifier_status}",
            f"completion_contract_status: {self.completion_contract_status}",
        ]
        if self.completion_contract:
            lines.append(f"completion_contract: {self.completion_contract}")
        if self.raw_secret_detected:
            lines.append(f"raw_secret_detected: {self.raw_secret_detected}")
        validation_classification_criteria = [
            criterion
            for criterion in self.graded_completion_criteria()
            if criterion.kind == "validation_classification"
            and criterion.classification_output_key
            and criterion.expected_classification is not None
        ]
        if validation_classification_criteria:
            lines.append("validation_classification_output_contracts:")
            for criterion in validation_classification_criteria:
                lines.append(
                    f"- criterion_id: {criterion.id}; "
                    f"return_key: {criterion.classification_output_key}; "
                    f"expected_value: {json.dumps(criterion.expected_classification)}; "
                    "return_location: top_level_block_output"
                )
        if self.resolved_credentials:
            lines += [
                "resolved_credentials:",
                *[f"- {_safe_label(credential)}" for credential in self.resolved_credentials],
            ]
        if self.invalid_credential_ids:
            lines.append("invalid_credential_ids: " + ", ".join(f"`{cid}`" for cid in self.invalid_credential_ids))
        return "\n".join(lines)


def request_policy_has_present_completion_contract(request_policy: RequestPolicy | None) -> bool:
    if request_policy is None:
        return False
    return request_policy.completion_contract_status == "present" or bool(request_policy.completion_criteria)


def is_defer_authoring_durable_fill_criterion(criterion: CompletionCriterion) -> bool:
    return criterion.id == DEFER_AUTHORING_DURABLE_FILL_CRITERION_ID


def _defer_authoring_durable_fill_criterion() -> CompletionCriterion:
    return CompletionCriterion(
        id=DEFER_AUTHORING_DURABLE_FILL_CRITERION_ID,
        outcome="the live form is filled on the page this turn",
        kind="terminal_action",
        terminal_action_family="form",
        method_mandated=True,
        level="run",
    )


def credential_prompt_reason(policy: RequestPolicy | None, final_text: str | None) -> str | None:
    # Typed clarification_reason wins, then the explicit-defer flag — narrowly, since
    # allow_missing_credentials_in_draft alone also covers the generic skip_test
    # fallthrough with no credential involvement — then a text marker.
    if isinstance(policy, RequestPolicy):
        if policy.clarification_reason in CREDENTIAL_PROMPT_CLARIFICATION_REASONS:
            return policy.clarification_reason
        if policy.credential_draft_deferred_explicitly:
            return "credential_deferred_draft"
    normalized = " ".join((final_text or "").lower().split())
    if any(marker in normalized for marker in _CREDENTIAL_PROMPT_TEXT_MARKERS):
        return "assistant_directed"
    return None


def _is_judgment_boolean_criterion(criterion: CompletionCriterion) -> bool:
    return (
        isinstance(criterion.expected_output_value, bool) or criterion.expected_output_shape == "goal_judgment_boolean"
    )


def is_judgment_finalization_candidate(criterion: CompletionCriterion) -> bool:
    """Whether accepted-artifact evidence must decide or degrade this criterion."""
    if criterion.pinability in {"shapeless_valid", "unpinnable"}:
        return False
    return (
        criterion.judgment_truth_condition is not None
        or _is_judgment_boolean_criterion(criterion)
        or (
            criterion.kind == "validation_classification"
            and criterion.classification_output_key is not None
            and (isinstance(criterion.expected_classification, bool) or criterion.mint_disposition == "pending")
        )
    )


def typed_expected_output_value_key(value: ExpectedOutputValue | None) -> str:
    if isinstance(value, bool):
        return f"bool:{value}"
    if isinstance(value, str):
        return f"str:{value}"
    return ""


def judgment_truth_condition_key(condition: JudgmentTruthCondition | None) -> str:
    if condition is None:
        return ""
    return f"{condition.predicate}:{'t' if condition.polarity_when_holds else 'f'}"


def _criterion_grounding_mode(
    criterion: CompletionCriterion,
) -> Literal["exact_value", "shape", "missing", "judgment_boolean"]:
    if _is_judgment_boolean_criterion(criterion):
        return "judgment_boolean"
    if criterion.expected_output_value is not None:
        return "exact_value"
    if criterion.expected_output_shape is not None:
        return "shape"
    return "missing"


_TRANSCRIPT_TOTAL_CHAR_BUDGET = 2048
TRANSCRIPT_ANCHOR_CHAR_CAP = 512
_TRANSCRIPT_RETAINED_MIN_CHARS = 512
_TRANSCRIPT_MARKER_RESERVE = 32
_EMPTY_SLOT_SENTINEL = "(none)"
_REDACTED_REFUSED_SECRET_TURN = "[raw credentials redacted — this turn was refused]"


@dataclass(frozen=True)
class RequestPolicyTranscriptContext:
    earliest_user_turn: str
    latest_prior_user_turn: str
    latest_assistant_turn: str
    retained_history: str
    omitted_any: bool


def _middle_truncate(text: str, cap: int) -> str:
    if len(text) <= cap:
        return text
    keep = max(cap - 32, 16)
    head_len = keep // 2
    tail_len = keep - head_len
    omitted = len(text) - keep
    return f"{text[:head_len]}<…{omitted} chars truncated…>{text[-tail_len:]}"


def _safe_slot(text: str | None, cap: int) -> str:
    if not text:
        return _EMPTY_SLOT_SENTINEL
    # Truncate the raw text first to bound regex work on pasted large messages.
    # A secret that straddles the head/tail splice would not be caught here, but
    # `_middle_truncate` later collapses the rendered output anyway, so any such
    # value already cannot leak intact to the prompt.
    bounded = text if len(text) <= cap * 4 else text[: cap * 2] + text[-cap * 2 :]
    return _middle_truncate(escape_code_fences(redact_raw_secrets_for_prompt(bounded)), cap)


def _redact_refused_secret_turns(
    messages: list[WorkflowCopilotChatHistoryMessage],
) -> list[WorkflowCopilotChatHistoryMessage]:
    """Replace the content of any user turn answered with the raw-secret refusal.

    A confirmed raw-credential paste is redacted by conversation position — the
    refusal is a deterministic marker — so a leaked secret cannot bias a later
    turn's classification regardless of the syntax the user pasted it in.
    """
    redacted = list(messages)
    for i in range(len(redacted) - 1):
        current, following = redacted[i], redacted[i + 1]
        if (
            current.sender == WorkflowCopilotChatSender.USER
            and following.sender == WorkflowCopilotChatSender.AI
            and RAW_SECRET_REFUSAL_SENTINEL in (following.content or "")
        ):
            redacted[i] = current.model_copy(update={"content": _REDACTED_REFUSED_SECRET_TURN})
    return redacted


def build_transcript_context(
    messages: list[WorkflowCopilotChatHistoryMessage],
    current_user_message: str,
    *,
    total_char_budget: int = _TRANSCRIPT_TOTAL_CHAR_BUDGET,
    anchor_char_cap: int = TRANSCRIPT_ANCHOR_CHAR_CAP,
    retained_min_chars: int = _TRANSCRIPT_RETAINED_MIN_CHARS,
) -> RequestPolicyTranscriptContext:
    """Shape chat history into structural anchors for the request-policy classifier.

    The chat window is already truncated upstream (see
    `CHAT_HISTORY_CONTEXT_MESSAGES` in `skyvern/forge/sdk/routes/workflow_copilot.py`),
    so `earliest_user_turn` is the head of the retained tail rather than the
    original conversation goal — the prompt header surfaces that caveat.
    """
    filtered: list[WorkflowCopilotChatHistoryMessage] = [m for m in messages if (m.content or "").strip()]
    current_stripped = (current_user_message or "").strip()
    if (
        filtered
        and filtered[-1].sender == WorkflowCopilotChatSender.USER
        and (filtered[-1].content or "").strip() == current_stripped
        and current_stripped
    ):
        filtered = filtered[:-1]

    filtered = _redact_refused_secret_turns(filtered)

    user_indices = [i for i, m in enumerate(filtered) if m.sender == WorkflowCopilotChatSender.USER]
    ai_indices = [i for i, m in enumerate(filtered) if m.sender == WorkflowCopilotChatSender.AI]

    earliest_idx = user_indices[0] if user_indices else None
    latest_user_idx = user_indices[-1] if user_indices else None
    latest_ai_idx = ai_indices[-1] if ai_indices else None
    anchor_indices = {idx for idx in (earliest_idx, latest_user_idx, latest_ai_idx) if idx is not None}

    earliest_text = filtered[earliest_idx].content if earliest_idx is not None else None
    latest_user_text = filtered[latest_user_idx].content if latest_user_idx is not None else None
    latest_ai_text = filtered[latest_ai_idx].content if latest_ai_idx is not None else None

    earliest_slot = _safe_slot(earliest_text, anchor_char_cap)
    latest_user_slot = _safe_slot(latest_user_text, anchor_char_cap)
    latest_ai_slot = _safe_slot(latest_ai_text, anchor_char_cap)

    consumed = sum(len(slot) for slot in (earliest_slot, latest_user_slot, latest_ai_slot))
    retained_budget = max(total_char_budget - consumed, retained_min_chars)
    # Inner max() floors the retained block at `retained_min_chars // 2` when
    # the marker reserve would otherwise drive it negative; outer min() caps it
    # by total_char_budget so retained_history cannot exceed the declared ceiling
    # even when retained_min_chars is configured above total_char_budget.
    content_budget = min(
        max(retained_budget - _TRANSCRIPT_MARKER_RESERVE, retained_min_chars // 2),
        total_char_budget,
    )

    candidate_lines: list[tuple[int, str]] = []
    for i, message in enumerate(filtered):
        if i in anchor_indices:
            continue
        role = "user" if message.sender == WorkflowCopilotChatSender.USER else "assistant"
        candidate_lines.append((i, f"[{i + 1}] {role}: {_safe_slot(message.content, anchor_char_cap)}"))

    keep: list[str] = []
    used = 0
    # break (not continue) on overflow: drops the oldest tail entirely instead
    # of skipping a large recent turn to fit smaller older ones.
    for _i, line in reversed(candidate_lines):
        cost = len(line) + 1
        if used + cost > content_budget:
            break
        keep.append(line)
        used += cost
    omitted_count = len(candidate_lines) - len(keep)
    keep.reverse()

    if omitted_count:
        keep.insert(0, f"<omitted {omitted_count} entries>")

    retained = "\n".join(keep) if keep else _EMPTY_SLOT_SENTINEL
    return RequestPolicyTranscriptContext(
        earliest_user_turn=earliest_slot,
        latest_prior_user_turn=latest_user_slot,
        latest_assistant_turn=latest_ai_slot,
        retained_history=retained,
        omitted_any=bool(omitted_count),
    )


def _clean_list(values: list[Any]) -> list[str]:
    return list(dict.fromkeys(str(value).strip() for value in values if str(value).strip()))


def _credential_ids(text: str) -> list[str]:
    text = text or ""
    canonical = _CREDENTIAL_ID_RE.findall(text)
    normalized = [f"cred_{body}" for body in _MALFORMED_CREDENTIAL_ID_RE.findall(text)]
    return list(dict.fromkeys(canonical + normalized))


def _canonicalize_credential_ref(ref: str) -> str:
    malformed = _MALFORMED_CREDENTIAL_ID_RE.fullmatch(ref.strip())
    return f"cred_{malformed.group(1)}" if malformed else ref


def _raw_secret_detected(text: str) -> bool:
    return any(pattern.search(text or "") for pattern in RAW_SECRET_PATTERNS) or contains_email_password_pair(text)


_MIN_RAW_SECRET_EVIDENCE_CHARS = 4


def _verify_raw_secret_evidence(evidence: str | None, user_message: str) -> bool:
    if not evidence or len(evidence) < _MIN_RAW_SECRET_EVIDENCE_CHARS:
        return False
    if evidence not in (user_message or ""):
        return False
    return any(c.isdigit() or (not c.isalnum() and not c.isspace()) for c in evidence)


def _coerce_clarification_reason(value: Any) -> ClarificationReason:
    if value in _VALID_CLARIFICATION_REASONS:
        return cast(ClarificationReason, value)
    return "none"


def _coerce_raw_secret_handling(value: Any) -> RawSecretHandling:
    if value in _RAW_SECRET_HANDLINGS:
        return cast(RawSecretHandling, value)
    return "none"


def _coerce_criterion_kind(value: Any) -> CriterionKind:
    if isinstance(value, str) and value in _CRITERION_KINDS:
        return cast(CriterionKind, value)
    return "outcome"


def _coerce_terminal_action_family(value: Any, kind: CriterionKind) -> TerminalActionFamily | None:
    if kind == "terminal_action" and isinstance(value, str) and value in _TERMINAL_ACTION_FAMILIES:
        return cast(TerminalActionFamily, value)
    return None


def _coerce_expected_output_value(value: Any) -> ExpectedOutputValue | None:
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        collapsed = " ".join(value.split())[:_COMPLETION_CRITERION_EXPECTED_VALUE_MAX_CHARS].strip()
        return collapsed or None
    return None


def _canonical_bool_string(value: str) -> bool | None:
    collapsed = value.strip().casefold()
    if collapsed == "true":
        return True
    if collapsed == "false":
        return False
    return None


def _coerce_expected_output_shape(value: Any) -> ExpectedOutputShape | None:
    if isinstance(value, str) and value in _EXPECTED_OUTPUT_SHAPES:
        return cast(ExpectedOutputShape, value)
    return None


def _coerce_judgment_truth_condition(predicate: Any, polarity: Any) -> JudgmentTruthCondition | None:
    if not isinstance(predicate, str) or predicate not in _JUDGMENT_PREDICATES:
        return None
    if not isinstance(polarity, bool):
        return None
    return JudgmentTruthCondition(predicate=cast(JudgmentPredicate, predicate), polarity_when_holds=polarity)


def _coerce_requested_output_evidence_source(value: Any) -> RequestedOutputEvidenceSource:
    if isinstance(value, str) and value in _REQUESTED_OUTPUT_EVIDENCE_SOURCES:
        return cast(RequestedOutputEvidenceSource, value)
    return "runtime_output"


def _coerce_classification_output_key(value: Any) -> str | None:
    if not isinstance(value, str):
        return None
    key = value.strip()
    return key if _CLASSIFICATION_OUTPUT_KEY_RE.fullmatch(key) else None


def _coerce_expected_classification(value: Any) -> ClassificationTarget | None:
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        collapsed = " ".join(value.split())[:_COMPLETION_CRITERION_CLASSIFICATION_TARGET_MAX_CHARS].strip()
        return collapsed or None
    return None


def _coerce_classifier_payload(raw: Any) -> dict[str, Any] | None:
    if isinstance(raw, str):
        raw = parse_final_response(raw)
    if not isinstance(raw, dict):
        return None
    if not any(field in raw for field in _CLASSIFICATION_RESPONSE_FIELDS):
        return None
    return raw


def _request_slot_anchor(item: dict[str, Any]) -> tuple[str, str] | None:
    source_id = item.get("request_slot_source_id")
    source_quote = item.get("request_slot_source_quote")
    if not isinstance(source_id, str) or not isinstance(source_quote, str) or not source_quote:
        return None
    return source_id, source_quote


def _item_claims_request_slot_fields(item: dict[str, Any]) -> bool:
    return any(
        item.get(field) is not None
        for field in (
            "output_path",
            "classification_output_key",
            "expected_output_value",
            "expected_output_shape",
            "expected_classification",
        )
    )


def _item_claims_request_slot(item: dict[str, Any]) -> bool:
    return any(item.get(field) is not None for field in ("request_slot_source_id", "request_slot_source_quote")) or (
        _item_claims_request_slot_fields(item)
    )


def _request_slot_anchor_is_valid(
    item: dict[str, Any],
    *,
    request_slot_request: RequestSlotProducerInputV1,
) -> bool:
    anchor = _request_slot_anchor(item)
    if anchor is None:
        return False
    source_id, source_quote = anchor
    source = next(
        (source for source in request_slot_sources(request_slot_request) if source.source_id == source_id),
        None,
    )
    if source is None:
        return False
    start = source.text.find(source_quote)
    return start >= 0 and source.text.find(source_quote, start + 1) < 0


def _request_slot_anchor_matches_criterion_datum(item: dict[str, Any]) -> bool:
    anchor = _request_slot_anchor(item)
    if anchor is None:
        return False
    _source_id, source_quote = anchor
    quote_tokens = re.findall(r"[a-z0-9]+", source_quote.casefold())
    for field_name in ("output_path", "classification_output_key"):
        value = item.get(field_name)
        if not isinstance(value, str) or not value:
            continue
        datum = value.rsplit(".", 1)[-1]
        datum_tokens = re.findall(r"[a-z0-9]+", datum.casefold())
        if not datum_tokens:
            continue
        width = len(datum_tokens)
        return any(
            quote_tokens[index : index + width] == datum_tokens for index in range(len(quote_tokens) - width + 1)
        )
    return True


def _request_slot_anchor_is_admissible(
    item: dict[str, Any],
    *,
    request_slot_request: RequestSlotProducerInputV1,
) -> bool:
    return _request_slot_anchor_is_valid(
        item,
        request_slot_request=request_slot_request,
    ) and _request_slot_anchor_matches_criterion_datum(item)


def _request_slot_claims_need_anchor_correction(
    raw_criteria: Any,
    *,
    request_slot_request: RequestSlotProducerInputV1,
) -> bool:
    return isinstance(raw_criteria, list) and any(
        isinstance(item, dict)
        and _item_claims_request_slot(item)
        and not _request_slot_anchor_is_admissible(item, request_slot_request=request_slot_request)
        for item in raw_criteria
    )


def _accept_request_slot_anchor_correction(
    original: dict[str, Any],
    corrected: dict[str, Any],
    *,
    request_slot_request: RequestSlotProducerInputV1,
) -> dict[str, Any] | None:
    original_criteria = original.get("completion_criteria")
    corrected_criteria = corrected.get("completion_criteria")
    if not isinstance(original_criteria, list) or not isinstance(corrected_criteria, list):
        return None
    if len(original_criteria) != len(corrected_criteria):
        return None

    anchor_fields = {"request_slot_source_id", "request_slot_source_quote"}
    if _classifier_payload_semantics(original) != _classifier_payload_semantics(corrected):
        return None
    original_semantic_criteria = [
        {key: value for key, value in item.items() if key not in anchor_fields}
        for item in original_criteria
        if isinstance(item, dict)
    ]
    corrected_semantic_criteria = [
        {key: value for key, value in item.items() if key not in anchor_fields}
        for item in corrected_criteria
        if isinstance(item, dict)
    ]
    if _parse_completion_criteria(original_semantic_criteria, emit_mint_events=False) != _parse_completion_criteria(
        corrected_semantic_criteria, emit_mint_events=False
    ):
        return None

    accepted_criteria: list[dict[str, Any]] = []
    for original_item, corrected_item in zip(original_criteria, corrected_criteria, strict=True):
        if not isinstance(original_item, dict) or not isinstance(corrected_item, dict):
            return None
        original_without_anchors = {key: value for key, value in original_item.items() if key not in anchor_fields}
        corrected_without_anchors = {key: value for key, value in corrected_item.items() if key not in anchor_fields}
        if _classifier_criterion_semantics(original_without_anchors) != _classifier_criterion_semantics(
            corrected_without_anchors
        ):
            return None
        accepted_item = dict(original_item)
        if _item_claims_request_slot(original_item):
            original_quote = original_item.get("request_slot_source_quote")
            corrected_anchor = _request_slot_anchor(corrected_item)
            if corrected_anchor is None:
                return None
            corrected_source_id, corrected_quote = corrected_anchor
            if isinstance(original_quote, str) and original_quote:
                corrected_source_for_original_quote = {
                    **original_item,
                    "request_slot_source_id": corrected_source_id,
                }
                if not _request_slot_anchor_is_admissible(
                    corrected_source_for_original_quote,
                    request_slot_request=request_slot_request,
                ):
                    return None
                accepted_item["request_slot_source_id"] = corrected_source_id
            else:
                if not _item_claims_request_slot_fields(original_item):
                    return None
                corrected_anchor_for_original_datum = {
                    **original_item,
                    "request_slot_source_id": corrected_source_id,
                    "request_slot_source_quote": corrected_quote,
                }
                if not _request_slot_anchor_is_admissible(
                    corrected_anchor_for_original_datum,
                    request_slot_request=request_slot_request,
                ):
                    return None
                accepted_item["request_slot_source_id"] = corrected_source_id
                accepted_item["request_slot_source_quote"] = corrected_quote
        elif any(corrected_item.get(field) != original_item.get(field) for field in anchor_fields):
            return None
        accepted_criteria.append(accepted_item)
    return {**original, "completion_criteria": accepted_criteria}


def _classifier_payload_semantics(payload: dict[str, Any]) -> RequestPolicy:
    return _classification_from_raw({**payload, "completion_criteria": []})


def _classifier_criterion_semantics(item: dict[str, Any]) -> CompletionCriterion | None:
    entries = _parse_completion_criterion_entries([item], emit_mint_events=False)
    return entries[0][1] if entries else None


def _request_slot_anchor_correction_prompt(
    prompt: str,
    *,
    raw_payload: dict[str, Any],
) -> str:
    correction = {
        "instruction": (
            "Return the same JSON object with identical criterion count and identical non-anchor fields. "
            "For each criterion that already has request_slot_source_quote, preserve that quote byte-for-byte "
            "and only add or replace request_slot_source_id with the source containing that exact unique quote. "
            "For a criterion with structured requested-output fields but no request_slot_source_quote, add "
            "request_slot_source_id and one exact unique source quote that contains the full datum named by its "
            "output_path or classification_output_key. Do not add anchors to any other criterion."
        ),
        "original_payload": raw_payload,
    }
    return f"{prompt}\n\nREQUEST SLOT ANCHOR CORRECTION (one pass):\n{json.dumps(correction, ensure_ascii=True, separators=(',', ':'))}"


def _request_slot_has_exact_requirement(criterion: CompletionCriterion) -> bool:
    if criterion.kind == "validation_classification":
        return criterion.expected_classification is not None
    return criterion.expected_output_value is not None or criterion.expected_output_shape is not None


def _degrade_unbound_request_slot_criterion(criterion: CompletionCriterion) -> CompletionCriterion:
    return replace(
        criterion,
        output_path=None,
        expected_output_value=None,
        expected_output_shape=None,
        classification_output_key=None,
        expected_classification=None,
        judgment_truth_condition=None,
        kind="outcome",
        mint_disposition="degraded",
        mint_degrade="undecidable_judgment",
        requested_output_floor_rekeyed=True,
        floor_rekeyed_from_path=criterion.output_path,
    )


def _request_slot_for_anchor(
    anchor: tuple[str, str] | None,
    *,
    request_slot_request: RequestSlotProducerInputV1,
    request_slot_contract: RequestSlotContractV1,
) -> CanonicalRequestSlotV1 | None:
    if anchor is None:
        return None
    source_id, source_quote = anchor
    source = next((item for item in request_slot_sources(request_slot_request) if item.source_id == source_id), None)
    if source is None:
        return None
    start = source.text.find(source_quote)
    if start < 0 or source.text.find(source_quote, start + 1) >= 0:
        return None
    end = start + len(source_quote)
    matches = [
        slot
        for slot in request_slot_contract.slots
        if slot.source_id == source_id and slot.source_start < end and start < slot.source_end
    ]
    return matches[0] if len(matches) == 1 else None


def _bind_criterion_to_request_slot(
    criterion: CompletionCriterion,
    *,
    slot: CanonicalRequestSlotV1,
    source_quote: str,
) -> CompletionCriterion:
    pinability = cast(Pinability, slot.pinability.value)
    has_exact_requirement = _request_slot_has_exact_requirement(criterion)
    degraded = pinability == "unpinnable" or (pinability == "pinned" and not has_exact_requirement)
    rekeyed = degraded or pinability == "shapeless_valid"

    expected_output_value = criterion.expected_output_value
    expected_output_shape = criterion.expected_output_shape
    expected_classification = criterion.expected_classification
    kind = criterion.kind
    classification_output_key = criterion.classification_output_key
    judgment_truth_condition = criterion.judgment_truth_condition
    original_output_path = (
        criterion.output_path
        or (f"output.{classification_output_key}" if classification_output_key is not None else None)
        or slot.canonical_path
    )
    if pinability != "pinned" or degraded:
        expected_output_value = None
        expected_output_shape = None
        expected_classification = None
        judgment_truth_condition = None
    if rekeyed and kind == "validation_classification":
        kind = "outcome"
        classification_output_key = None
    output_path = None if rekeyed else criterion.output_path

    pending = pinability == "pinned" and (
        isinstance(expected_output_value, bool)
        or expected_output_shape == "goal_judgment_boolean"
        or isinstance(expected_classification, bool)
        or judgment_truth_condition is not None
    )
    return replace(
        criterion,
        id=slot.slot_id,
        outcome=criterion.outcome or source_quote,
        level=cast(CriterionLevel, slot.plane.value),
        output_path=output_path,
        expected_output_value=expected_output_value,
        expected_output_shape=expected_output_shape,
        kind=kind,
        classification_output_key=classification_output_key,
        expected_classification=expected_classification,
        judgment_truth_condition=judgment_truth_condition,
        request_slot_id=slot.slot_id,
        pinability=pinability,
        mint_disposition="degraded" if degraded else ("pending" if pending else "decidable"),
        mint_degrade="undecidable_judgment" if degraded else None,
        requested_output_floor_rekeyed=rekeyed,
        floor_rekeyed_from_path=original_output_path if rekeyed else None,
    )


def _parse_fresh_request_slot_criteria(
    raw: Any,
    *,
    request_slot_request: RequestSlotProducerInputV1,
    request_slot_contract: RequestSlotContractV1,
) -> list[CompletionCriterion]:
    if request_slot_contract.version != "1" or not isinstance(raw, list):
        return []
    bound_by_slot_id: dict[str, CompletionCriterion] = {}
    non_slot_criteria: list[CompletionCriterion] = []
    for item, criterion in _parse_completion_criterion_entries(raw):
        anchor = _request_slot_anchor(item)
        slot = (
            _request_slot_for_anchor(
                anchor,
                request_slot_request=request_slot_request,
                request_slot_contract=request_slot_contract,
            )
            if _request_slot_anchor_is_admissible(item, request_slot_request=request_slot_request)
            else None
        )
        if anchor is None or slot is None:
            if _item_claims_request_slot(item):
                criterion = _degrade_unbound_request_slot_criterion(criterion)
            non_slot_criteria.append(
                replace(
                    criterion,
                    id=f"c{len(non_slot_criteria)}",
                )
            )
            continue
        if slot.slot_id in bound_by_slot_id:
            # The producer owns slot membership and one slot can yield only one criterion.
            # Preserve the first classifier row in source order; later aliases cannot widen
            # or replace the already-bound contract member.
            continue
        bound_by_slot_id[slot.slot_id] = _bind_criterion_to_request_slot(
            criterion,
            slot=slot,
            source_quote=anchor[1],
        )

    for slot in request_slot_contract.slots:
        if slot.slot_id in bound_by_slot_id:
            continue
        source_quote = request_slot_source_text(request_slot_request, slot)
        bound_by_slot_id[slot.slot_id] = _bind_criterion_to_request_slot(
            CompletionCriterion(id="c0", outcome=source_quote),
            slot=slot,
            source_quote=source_quote,
        )
    return ([bound_by_slot_id[slot.slot_id] for slot in request_slot_contract.slots] + non_slot_criteria)[
        :_MAX_COMPLETION_CRITERIA
    ]


def _fresh_request_slot_failure_criteria(raw: Any) -> list[CompletionCriterion]:
    if not isinstance(raw, list):
        return []
    criteria: list[CompletionCriterion] = []
    for item, criterion in _parse_completion_criterion_entries(raw):
        if _item_claims_request_slot(item):
            criterion = _degrade_unbound_request_slot_criterion(criterion)
        criteria.append(replace(criterion, id=f"c{len(criteria)}"))
    return criteria


def _classification_from_raw(
    raw: Any,
    *,
    request_slot_request: RequestSlotProducerInputV1 | None = None,
    request_slot_contract: RequestSlotContractV1 | None = None,
    request_slot_failure_kind: str | None = None,
) -> RequestPolicy:
    raw = _coerce_classifier_payload(raw)
    if raw is None:
        return RequestPolicy()
    testing_intent = raw.get("testing_intent")
    authoring_intent = raw.get("authoring_intent")
    credential_input_kind = raw.get("credential_input_kind")
    completion_contract_raw = raw.get("completion_contract")
    completion_contract = completion_contract_raw.strip() if isinstance(completion_contract_raw, str) else None
    evidence_raw = raw.get("raw_secret_evidence")
    raw_secret_evidence = evidence_raw if isinstance(evidence_raw, str) and evidence_raw.strip() else None
    raw_criteria = raw.get("completion_criteria")
    claims_request_slots = isinstance(raw_criteria, list) and any(
        isinstance(item, dict) and _item_claims_request_slot(item) for item in raw_criteria
    )
    if request_slot_request is not None and request_slot_contract is not None:
        completion_criteria = _parse_fresh_request_slot_criteria(
            raw_criteria,
            request_slot_request=request_slot_request,
            request_slot_contract=request_slot_contract,
        )
    elif claims_request_slots:
        completion_criteria = _fresh_request_slot_failure_criteria(raw_criteria)
        request_slot_failure_kind = request_slot_failure_kind or "missing_request_slot_contract"
    else:
        completion_criteria = _parse_completion_criteria(raw_criteria)
    policy = RequestPolicy(
        testing_intent=testing_intent if testing_intent in _TESTING_INTENTS else "unspecified",
        authoring_intent=authoring_intent if authoring_intent in _AUTHORING_INTENTS else "author_now",
        credential_input_kind=credential_input_kind if credential_input_kind in _KINDS else "none",
        credential_refs=_clean_list(raw.get("credential_refs") or []),
        login_page_urls=_clean_list(raw.get("login_page_urls") or []),
        requires_user_clarification=bool(raw.get("requires_user_clarification")),
        completion_contract=completion_contract or None,
        completion_criteria=completion_criteria,
        raw_secret_evidence=raw_secret_evidence,
        raw_secret_handling=_coerce_raw_secret_handling(raw.get("raw_secret_handling")),
        clarification_reason=_coerce_clarification_reason(raw.get("clarification_reason")),
        classifier_status="success",
        classifier_failure_kind="none",
        request_slot_failure_kind=request_slot_failure_kind,
    )
    if policy.credential_input_kind == "raw_secret":
        policy.clarification_reason = "raw_secret"
        if policy.raw_secret_handling == "none":
            policy.raw_secret_handling = "block"
    if policy.clarification_reason in _PRE_RESOLUTION_CLARIFICATION_REASONS:
        policy.requires_user_clarification = True
    return policy


def _structural_clarification_reason(user_message: str) -> ClarificationReason:
    normalized = " ".join((user_message or "").lower().split())
    if "loop" in normalized and "conditional" in normalized:
        if any(marker in normalized for marker in _INVALID_CONDITIONAL_CONTAINER_MARKERS):
            return "invalid_conditional_container"
    return "none"


def _ground_completion_contract(user_message: str, value: str | None) -> str | None:
    if not value or not value.strip():
        return None

    contract = value.strip()
    message = user_message or ""
    if contract.lower() in message.lower():
        return contract

    return None


def _parse_completion_criterion_entries(
    raw: Any,
    *,
    emit_mint_events: bool = True,
) -> list[tuple[dict[str, Any], CompletionCriterion]]:
    """Build outcome criteria from the classifier output.

    IDs are assigned server-side by index after de-duplication; any id the
    model emits is discarded because the downstream satisfaction check keys on
    a unique id set.
    """
    if not isinstance(raw, list):
        return []
    entries: list[tuple[dict[str, Any], CompletionCriterion]] = []
    seen: set[tuple[str, ...]] = set()
    registered_download_item_count = sum(
        1
        for candidate in raw
        if isinstance(candidate, dict)
        and _normalize_deliverable_kind(candidate.get("deliverable_kind")) == "registered_download"
        and _coerce_criterion_kind(candidate.get("kind")) != "validation_classification"
    )
    for item in raw:
        if not isinstance(item, dict):
            continue
        outcome_raw = item.get("outcome")
        if not isinstance(outcome_raw, str):
            continue
        outcome = " ".join(outcome_raw.split())[:_COMPLETION_CRITERION_OUTCOME_MAX_CHARS].strip()
        if not outcome:
            continue
        output_path_raw = item.get("output_path")
        output_path = output_path_raw.strip() if isinstance(output_path_raw, str) and output_path_raw.strip() else None
        expected_output_value: ExpectedOutputValue | None = _coerce_expected_output_value(
            item.get("expected_output_value")
        )
        expected_output_shape = _coerce_expected_output_shape(item.get("expected_output_shape"))
        requested_output_evidence_source = _coerce_requested_output_evidence_source(
            item.get("requested_output_evidence_source")
        )
        if isinstance(expected_output_value, str) and (
            requested_output_evidence_source == "independent_run_evidence"
            or expected_output_shape == "goal_judgment_boolean"
        ):
            coerced_judgment_bool = _canonical_bool_string(expected_output_value)
            if coerced_judgment_bool is not None:
                expected_output_value = coerced_judgment_bool
        judgment_truth_condition = _coerce_judgment_truth_condition(
            item.get("judgment_predicate"), item.get("judgment_polarity_when_holds")
        )
        contingent_on_raw = item.get("contingent_on")
        contingent_on = (
            " ".join(contingent_on_raw.split())[:_COMPLETION_CRITERION_CONTINGENT_ON_MAX_CHARS].strip()
            if isinstance(contingent_on_raw, str)
            else None
        )
        contingent_on = contingent_on or None
        contingent_antecedent_output_path = _normalize_contingent_antecedent_output_path(
            item.get("contingent_antecedent_output_path")
        )
        deliverable_kind = _normalize_deliverable_kind(item.get("deliverable_kind"))
        kind = _coerce_criterion_kind(item.get("kind"))
        classification_output_key = (
            _coerce_classification_output_key(item.get("classification_output_key"))
            if kind == "validation_classification"
            else None
        )
        expected_classification = (
            _coerce_expected_classification(item.get("expected_classification"))
            if kind == "validation_classification"
            else None
        )
        boolean_classification = kind == "validation_classification" and (
            isinstance(expected_classification, bool)
            or (expected_classification is None and expected_output_shape == "goal_judgment_boolean")
        )
        if kind == "validation_classification":
            if boolean_classification and classification_output_key is not None:
                outcome = f"The run classifies whether {classification_output_key.replace('_', ' ')}."
                expected_classification = None
            output_path = None
            expected_output_value = None
            expected_output_shape = "goal_judgment_boolean" if boolean_classification else None
            requested_output_evidence_source = (
                "independent_run_evidence" if boolean_classification else "runtime_output"
            )
        elif isinstance(expected_output_value, bool) or expected_output_shape == "goal_judgment_boolean":
            requested_output_evidence_source = "independent_run_evidence"
        requested_output_path_mint_source: RequestedOutputPathMintSource | None = None
        if (
            output_path is None
            and expected_output_value is None
            and deliverable_kind == "registered_download"
            and kind != "validation_classification"
            and registered_download_item_count == 1
        ):
            output_path = _LONE_REGISTERED_DOWNLOAD_OUTPUT_PATH
            requested_output_path_mint_source = "classifier_default"
            if emit_mint_events:
                LOG.info(
                    "copilot_registered_download_requested_output_minted",
                    output_path=output_path,
                    requested_output_path_mint_source=requested_output_path_mint_source,
                    criterion_id=f"c{len(entries)}",
                )
        key = (
            contingent_on or "",
            contingent_antecedent_output_path or "",
            output_path or classification_output_key or normalized_criterion_outcome_key(outcome),
            deliverable_kind or "",
            kind,
            str(expected_classification) if expected_classification is not None else "",
            typed_expected_output_value_key(expected_output_value),
            expected_output_shape or "",
            requested_output_evidence_source,
            judgment_truth_condition_key(judgment_truth_condition),
        )
        if key in seen:
            continue
        seen.add(key)
        level_raw = item.get("level")
        entries.append(
            (
                item,
                CompletionCriterion(
                    id=f"c{len(entries)}",
                    outcome=outcome,
                    contingent_on=contingent_on,
                    contingent_antecedent_output_path=contingent_antecedent_output_path,
                    deliverable_kind=deliverable_kind,
                    declared_deliverable_kind=deliverable_kind,
                    implicit=bool(item.get("implicit")),
                    method_mandated=bool(item.get("method_mandated")),
                    level=cast(CriterionLevel, level_raw)
                    if isinstance(level_raw, str) and level_raw in _CRITERION_LEVELS
                    else "run",
                    output_path=output_path,
                    expected_output_value=expected_output_value,
                    expected_output_shape=expected_output_shape,
                    requested_output_evidence_source=requested_output_evidence_source,
                    requested_output_path_mint_source=requested_output_path_mint_source,
                    kind=kind,
                    terminal_action_family=_coerce_terminal_action_family(item.get("terminal_action_family"), kind),
                    classification_output_key=classification_output_key,
                    expected_classification=expected_classification,
                    judgment_truth_condition=judgment_truth_condition,
                    mint_disposition=(
                        "pending"
                        if (
                            isinstance(expected_output_value, bool)
                            or expected_output_shape == "goal_judgment_boolean"
                            or boolean_classification
                        )
                        else "decidable"
                    ),
                ),
            )
        )
        if judgment_truth_condition is not None and emit_mint_events:
            LOG.info(
                "copilot_judgment_truth_condition_minted",
                mint_source="classifier",
                predicate=judgment_truth_condition.predicate,
                polarity_when_holds=judgment_truth_condition.polarity_when_holds,
                criterion_id=entries[-1][1].id,
            )
        if len(entries) >= _MAX_COMPLETION_CRITERIA:
            break
    return entries


def _parse_completion_criteria(raw: Any, *, emit_mint_events: bool = True) -> list[CompletionCriterion]:
    return [
        criterion for _item, criterion in _parse_completion_criterion_entries(raw, emit_mint_events=emit_mint_events)
    ]


def _normalize_contingent_antecedent_output_path(raw: Any) -> str | None:
    if not isinstance(raw, str):
        return None
    path = raw.strip()
    return path if _CONTINGENT_ANTECEDENT_OUTPUT_PATH_RE.fullmatch(path) else None


def _normalize_deliverable_kind(raw: Any) -> Literal["registered_download"] | None:
    return "registered_download" if raw == "registered_download" else None


def normalized_criterion_outcome_key(outcome: str) -> str:
    collapsed = " ".join((outcome or "").split())[:_COMPLETION_CRITERION_OUTCOME_MAX_CHARS]
    return collapsed.strip().lower().rstrip(".!?;:,").strip()


def _output_intent_spans(user_message: str, *, negated: bool = False) -> list[str]:
    message = user_message or ""
    spans: list[str] = []
    for match in _OUTPUT_INTENT_RE.finditer(message):
        if _output_intent_is_negated(message, match.start()) != negated:
            continue
        tail = message[match.start() :]
        span = tail[: _output_span_end_index(tail)]
        span = _OUTPUT_METHOD_TAIL_RE.sub("", span).strip(" :,-")
        if span:
            spans.append(span)
    return spans


def _output_intent_is_negated(message: str, match_start: int) -> bool:
    return bool(_OUTPUT_NEGATED_INTENT_PREFIX_RE.search(message[max(0, match_start - 32) : match_start]))


def _output_span_end_index(text: str) -> int:
    for index, char in enumerate(text):
        if _OUTPUT_SPAN_END_RE.fullmatch(char):
            return index
        if char != ".":
            continue
        next_char = text[index + 1] if index + 1 < len(text) else ""
        if next_char and not next_char.isspace():
            continue
        return index + 1
    return len(text)


def _normalize_requested_output_aliases(aliases: dict[str, str] | None) -> dict[str, str]:
    normalized: dict[str, str] = {}
    for field_name, output_path in (aliases or {}).items():
        if not isinstance(field_name, str) or not isinstance(output_path, str):
            continue
        path = output_path.strip()
        if not path.startswith("output."):
            continue
        key = " ".join(_word_tokens(field_name))
        if key:
            normalized[key] = path
    return normalized


# Shared within the Copilot subsystem; not a general public API.
def lookup_requested_output_path_alias(field_name: str, aliases: dict[str, str] | None) -> str | None:
    normalized_aliases = _normalize_requested_output_aliases(aliases)
    field_key = " ".join(_word_tokens(field_name))
    if not field_key:
        return None
    if field_key in normalized_aliases:
        return normalized_aliases[field_key]
    field_words = field_key.split()
    for alias_key, output_path in normalized_aliases.items():
        alias_words = alias_key.split()
        if _tokens_contain_sequence(field_words, alias_words) or _tokens_contain_sequence(alias_words, field_words):
            return output_path
    return None


def schema_output_path_aliases_from_criteria(criteria: list[CompletionCriterion]) -> dict[str, str]:
    aliases: dict[str, str] = {}
    for criterion in criteria:
        if criterion.level == "definition" or criterion.method_mandated or not criterion.output_path:
            continue
        path_tokens = _word_tokens(criterion.output_path.removeprefix("output."))
        keep_generic_words = (
            {"record"} if any(token in {"id", "identifier", "identifiers"} for token in path_tokens) else set()
        )
        path_phrase = " ".join(
            token for token in path_tokens if token not in _OUTPUT_GENERIC_WORDS or token in keep_generic_words
        )
        if path_phrase:
            aliases.setdefault(path_phrase, criterion.output_path)
        for token in path_tokens:
            if token and token not in _OUTPUT_GENERIC_WORDS:
                aliases.setdefault(token, criterion.output_path)
    return aliases


def _clean_requested_output_candidate(segment: str, aliases: dict[str, str] | None = None) -> str | None:
    candidate = _OUTPUT_METHOD_TAIL_RE.sub("", segment).strip(" :-")
    if not candidate:
        return None

    candidate = _normalize_named_output_field_delimiter(candidate)
    named_field = _OUTPUT_NAMED_FIELD_RE.search(candidate)
    if named_field is not None:
        return named_field.group(1)
    connector_matches = list(_OUTPUT_FIELD_CONNECTOR_RE.finditer(candidate))
    if connector_matches:
        candidate = candidate[connector_matches[-1].end() :]
    candidate = re.sub(r"\b([A-Za-z0-9]+)'s\b", r"\1", candidate)
    candidate = " ".join(re.sub(r"[^A-Za-z0-9 _/-]+", " ", candidate).split()).strip(" :-")
    if not candidate:
        return None
    if "_" in candidate and _OUTPUT_EXPLICIT_FIELD_KEY_RE.fullmatch(candidate):
        return candidate
    normalized_aliases = _normalize_requested_output_aliases(aliases)
    if " ".join(_word_tokens(candidate)) in normalized_aliases:
        return candidate
    candidate_tokens = _word_tokens(candidate)
    for alias_key in sorted(normalized_aliases, key=lambda key: len(key.split()), reverse=True):
        if _tokens_contain_sequence(candidate_tokens, alias_key.split()):
            return _matched_alias_phrase(candidate, alias_key) or alias_key
    candidate_words = candidate.split()
    for word in candidate_words:
        if " ".join(_word_tokens(word)) in normalized_aliases:
            return word
    if lookup_requested_output_path_alias(candidate, aliases) is not None:
        return candidate

    acronyms = [
        token
        for token in _OUTPUT_ACRONYM_RE.findall(candidate)
        if token.casefold() not in _OUTPUT_METHOD_WORDS
        and token.casefold() not in _OUTPUT_GENERIC_WORDS
        and lookup_requested_output_path_alias(token, aliases) is not None
    ]
    if acronyms:
        return acronyms[0]

    words = candidate.split()
    normalized_words = [word.casefold().strip("_-/") for word in words if word.strip("_-/")]
    if not normalized_words:
        return None
    while normalized_words and normalized_words[0] in _OUTPUT_LEADING_FIELD_WORDS:
        words = words[1:]
        normalized_words = normalized_words[1:]
    if not normalized_words:
        return None
    if len(words) == 1 and "_" in words[0] and _OUTPUT_EXPLICIT_FIELD_KEY_RE.fullmatch(words[0]):
        return words[0]
    if any(word in _OUTPUT_METHOD_WORDS for word in normalized_words):
        return None
    if all(word in _OUTPUT_GENERIC_WORDS for word in normalized_words):
        return None
    field_indexes = [i for i, word in enumerate(normalized_words) if word in _OUTPUT_FIELD_WORDS]
    if not field_indexes:
        return None
    status_indexes = [i for i, word in enumerate(normalized_words) if word in {"status", "statuses"}]
    if status_indexes:
        field_indexes = status_indexes
    last_field_index = field_indexes[-1]
    start = max(0, last_field_index - 2)
    keep_generic_words = (
        {"record"} if normalized_words[last_field_index] in {"id", "identifier", "identifiers"} else set()
    )
    phrase_words = [
        word
        for word in words[start : last_field_index + 1]
        if word.casefold().strip("_-/") not in _OUTPUT_GENERIC_WORDS
        or word.casefold().strip("_-/") in keep_generic_words
    ]
    if not phrase_words:
        return None
    return " ".join(phrase_words)


def _normalize_named_output_field_delimiter(candidate: str) -> str:
    lowered = candidate.casefold()
    for anchor in (" named", " called"):
        token_start = lowered.find(anchor)
        if token_start == -1:
            continue
        token_start += len(anchor)
        while token_start < len(candidate) and candidate[token_start].isspace():
            token_start += 1
        if token_start >= len(candidate) or candidate[token_start] not in "'\"`":
            continue
        quote = candidate[token_start]
        token_end = candidate.find(quote, token_start + 1)
        if token_end == -1:
            continue
        field_name = candidate[token_start + 1 : token_end]
        if _OUTPUT_EXPLICIT_FIELD_KEY_RE.fullmatch(field_name):
            return f"{candidate[:token_start]}{field_name}{candidate[token_end + 1 :]}"
    return candidate


def _matched_alias_phrase(candidate: str, alias_key: str) -> str | None:
    alias_words = alias_key.split()
    if not alias_words:
        return None
    pattern = r"\b" + r"[\W_]+".join(re.escape(word) for word in alias_words) + r"\b"
    match = re.search(pattern, candidate, re.I)
    return match.group(0).strip() if match is not None else None


def _requested_output_fields(
    user_message: str, aliases: dict[str, str] | None = None, *, negated: bool = False
) -> list[str]:
    fields: list[str] = []
    seen: set[str] = set()
    for span in _output_intent_spans(user_message, negated=negated):
        if not negated:
            for match in _OUTPUT_INTENT_RE.finditer(span):
                if _output_intent_is_negated(span, match.start()):
                    span = span[: match.start()]
                    break
        for segment in _OUTPUT_SPLIT_RE.split(span):
            field_name = _clean_requested_output_candidate(segment, aliases)
            if field_name is None:
                continue
            key = field_name.casefold()
            if key in seen:
                continue
            seen.add(key)
            fields.append(field_name)
    return fields


def _requested_output_path_for_detected_field(
    field_name: str, schema_aliases: dict[str, str], config_aliases: dict[str, str]
) -> str:
    if "_" in field_name and _OUTPUT_EXPLICIT_FIELD_KEY_RE.fullmatch(field_name.strip()):
        return requested_output_path_for_field(field_name)
    return (
        lookup_requested_output_path_alias(field_name, schema_aliases)
        or lookup_requested_output_path_alias(field_name, config_aliases)
        or requested_output_path_for_field(field_name)
    )


def requested_output_path_for_field(field_name: str, aliases: dict[str, str] | None = None) -> str:
    alias_path = lookup_requested_output_path_alias(field_name, aliases)
    if alias_path is not None:
        return alias_path
    words = _word_tokens(field_name)
    slug = "_".join(words) or "field"
    return f"output.{slug}"


def _requested_output_field_label(field_name: str, output_path: str) -> str:
    stripped = field_name.strip()
    if stripped.isupper():
        return stripped
    return " ".join(_word_tokens(field_name))


def _tokens_contain_sequence(haystack: list[str], needle: list[str]) -> bool:
    if not needle or len(needle) > len(haystack):
        return False
    for index in range(len(haystack) - len(needle) + 1):
        if haystack[index : index + len(needle)] == needle:
            return True
    return False


def _criterion_text_covers_requested_output(criterion: CompletionCriterion, field_name: str) -> bool:
    if criterion.level == "definition" or criterion.method_mandated:
        return False
    outcome_words = _word_tokens(criterion.outcome)
    field_words = _word_tokens(field_name)
    if not _tokens_contain_sequence(outcome_words, field_words):
        return False
    if any(word in _OUTPUT_INPUT_ONLY_WORDS for word in outcome_words) and not any(
        word in _OUTPUT_OUTCOME_WORDS for word in outcome_words
    ):
        return False
    return True


def _criterion_covers_requested_output(
    criterion: CompletionCriterion, field_name: str, aliases: dict[str, str] | None = None
) -> bool:
    if criterion.level == "definition" or criterion.method_mandated:
        return False
    output_path = requested_output_path_for_field(field_name, aliases)
    return criterion.output_path == output_path or _criterion_text_covers_requested_output(criterion, field_name)


def _criterion_text_covers_any_requested_output(criterion: CompletionCriterion, requested_fields: list[str]) -> bool:
    return any(_criterion_text_covers_requested_output(criterion, field_name) for field_name in requested_fields)


def _requested_output_expected_values_from_criteria(
    criteria: list[CompletionCriterion],
    requested_specs: list[tuple[str, str, str]],
) -> dict[str, ExpectedOutputValue]:
    values: dict[str, ExpectedOutputValue] = {}
    for criterion in criteria:
        if criterion.level == "definition" or criterion.method_mandated:
            continue
        for field_name, output_path, _field_label in requested_specs:
            if criterion.output_path != output_path and not _criterion_text_covers_requested_output(
                criterion, field_name
            ):
                continue
            value = criterion.expected_output_value
            if value is not None:
                values.setdefault(output_path, value)
    return values


def _requested_output_shapes_from_criteria(
    criteria: list[CompletionCriterion],
    requested_specs: list[tuple[str, str, str]],
) -> dict[str, ExpectedOutputShape]:
    shapes: dict[str, ExpectedOutputShape] = {}
    for criterion in criteria:
        if criterion.level == "definition" or criterion.method_mandated or criterion.expected_output_shape is None:
            continue
        for field_name, output_path, _field_label in requested_specs:
            if criterion.output_path != output_path and not _criterion_text_covers_requested_output(
                criterion, field_name
            ):
                continue
            shapes.setdefault(output_path, criterion.expected_output_shape)
    return shapes


def _requested_output_evidence_sources_from_criteria(
    criteria: list[CompletionCriterion],
    requested_specs: list[tuple[str, str, str]],
) -> dict[str, RequestedOutputEvidenceSource]:
    sources: dict[str, RequestedOutputEvidenceSource] = {}
    eligible_source_criteria: list[CompletionCriterion] = []
    for criterion in criteria:
        if criterion.level == "definition" or criterion.method_mandated:
            continue
        if criterion.requested_output_evidence_source == "runtime_output":
            continue
        if criterion.kind == "outcome" and not _validation_classification_output_path(criterion.output_path):
            eligible_source_criteria.append(criterion)
        for field_name, output_path, _field_label in requested_specs:
            if criterion.output_path != output_path and not _criterion_text_covers_requested_output(
                criterion, field_name
            ):
                continue
            sources.setdefault(output_path, criterion.requested_output_evidence_source)
    if len(requested_specs) == 1 and len(eligible_source_criteria) == 1:
        _field_name, output_path, _field_label = requested_specs[0]
        sources.setdefault(output_path, eligible_source_criteria[0].requested_output_evidence_source)
    return sources


def _requested_output_judgment_conditions_from_criteria(
    criteria: list[CompletionCriterion],
    requested_specs: list[tuple[str, str, str]],
) -> dict[str, JudgmentTruthCondition]:
    conditions: dict[str, JudgmentTruthCondition] = {}
    for criterion in criteria:
        if criterion.level == "definition" or criterion.method_mandated or criterion.judgment_truth_condition is None:
            continue
        for field_name, output_path, _field_label in requested_specs:
            if criterion.output_path != output_path and not _criterion_text_covers_requested_output(
                criterion, field_name
            ):
                continue
            conditions.setdefault(output_path, criterion.judgment_truth_condition)
    return conditions


def _requested_output_criterion_id(output_path: str) -> str:
    slug = "_".join(_word_tokens(output_path)) or "field"
    return f"{_REQUESTED_OUTPUT_CRITERION_ID_PREFIX}{slug}"


def _requested_output_mint_state(
    expected_value: ExpectedOutputValue | None,
    expected_shape: ExpectedOutputShape | None,
    judgment_condition: JudgmentTruthCondition | None,
) -> tuple[MintDisposition, MintDegrade | None]:
    if isinstance(expected_value, bool) or expected_shape == "goal_judgment_boolean" or judgment_condition is not None:
        return "pending", None
    if expected_shape == "status_label" or (expected_value is None and expected_shape is not None):
        return "degraded", "undecidable_judgment"
    return "decidable", None


def _generic_completion_criterion(criterion: CompletionCriterion) -> bool:
    key = normalized_criterion_outcome_key(criterion.outcome)
    if is_fallback_floor_criterion(criterion):
        return True
    generic_markers = (
        "intended end state",
        "expected output",
        "profile details",
        "profile information",
        "requested identifier-like value",
        "target entity",
        "grouped row entries",
    )
    return any(marker in key for marker in generic_markers)


def _criterion_drop_priority(criterion: CompletionCriterion, requested_output_paths: set[str]) -> int:
    if criterion.output_path in requested_output_paths:
        return 0
    if criterion.requested_output_corroborator:
        return 0
    if criterion.level == "definition":
        return 0
    if is_fallback_floor_criterion(criterion):
        return 4
    if criterion.method_mandated:
        return 0
    if _generic_completion_criterion(criterion):
        return 3
    return 2


def _cap_completion_criteria(
    criteria: list[CompletionCriterion], requested_output_paths: set[str]
) -> list[CompletionCriterion]:
    capped = list(criteria)
    while len(capped) > _MAX_COMPLETION_CRITERIA:
        drop_index = max(
            range(len(capped)),
            key=lambda index: (_criterion_drop_priority(capped[index], requested_output_paths), index),
        )
        if _criterion_drop_priority(capped[drop_index], requested_output_paths) == 0:
            break
        del capped[drop_index]
    return capped


def _preserve_after_requested_output_canonicalization(
    criterion: CompletionCriterion,
    requested_fields: list[str],
    requested_output_paths: set[str],
    forbidden_fields: list[str] | None = None,
    forbidden_output_paths: set[str] | None = None,
) -> bool:
    if criterion.level == "definition" or criterion.method_mandated:
        return True
    if criterion.output_path in (forbidden_output_paths or set()) or _criterion_text_covers_any_requested_output(
        criterion, forbidden_fields or []
    ):
        return False
    if criterion.output_path in requested_output_paths or _criterion_text_covers_any_requested_output(
        criterion, requested_fields
    ):
        return False
    if (
        requested_output_paths
        and not criterion.output_path
        and criterion.kind == "outcome"
        and _generic_completion_criterion(criterion)
    ):
        return False
    return True


def _non_requested_output_run_corroborator(criterion: CompletionCriterion) -> bool:
    return (
        criterion.level == "run"
        and criterion.kind == "outcome"
        and not criterion.method_mandated
        and criterion.output_path is None
    )


def is_presence_only_requested_output_criterion(criterion: CompletionCriterion) -> bool:
    return (
        criterion.expected_output_value is None
        and criterion.expected_output_shape is None
        and criterion.deliverable_kind is None
        and criterion.mint_degrade is None
    )


def _validation_classification_output_path(output_path: str | None) -> bool:
    return (
        output_path in _VALIDATION_CLASSIFICATION_BOOLEAN_OUTPUT_TARGETS
        or output_path in _VALIDATION_CLASSIFICATION_LABEL_OUTPUT_TARGETS
    )


def completion_criterion_requires_active_run_terminal_monitor(criterion: CompletionCriterion) -> bool:
    if criterion.requested_output_corroborator:
        return False
    if (
        criterion.output_path
        and criterion.level != "definition"
        and not criterion.method_mandated
        and criterion.kind != "validation_classification"
    ):
        return False
    return True


def _source_requested_output_corroborator(
    criteria: list[CompletionCriterion],
    requested_fields: list[str],
    requested_output_paths: set[str],
) -> CompletionCriterion | None:
    if len(requested_output_paths) == 1:
        for criterion in criteria:
            if (
                criterion.level != "run"
                or criterion.kind != "outcome"
                or criterion.method_mandated
                or criterion.output_path is None
            ):
                continue
            if is_judgment_finalization_candidate(criterion):
                continue
            if criterion.requested_output_evidence_source == "independent_run_evidence":
                continue
            if _validation_classification_output_path(criterion.output_path):
                continue
            if criterion.output_path in requested_output_paths or _criterion_text_covers_any_requested_output(
                criterion, requested_fields
            ):
                return replace(
                    criterion,
                    output_path=None,
                    expected_output_value=None,
                    expected_output_shape=None,
                    requested_output_corroborator=True,
                )
    for criterion in criteria:
        if is_fallback_floor_base_criterion(criterion):
            return replace(criterion, requested_output_corroborator=True)
    return None


def _requested_output_corroborator_id(source_id: str, used_ids: set[str]) -> str:
    base_id = f"{source_id}__requested_output_corroborator"
    if base_id not in used_ids:
        return base_id
    suffix = 2
    while f"{base_id}_{suffix}" in used_ids:
        suffix += 1
    return f"{base_id}_{suffix}"


def _apply_classifier_typed_requested_output_corroborators(policy: RequestPolicy) -> None:
    if any(_non_requested_output_run_corroborator(criterion) for criterion in policy.completion_criteria):
        return
    source_criteria = [
        criterion
        for criterion in policy.completion_criteria
        if criterion.level == "run"
        and criterion.kind == "outcome"
        and not criterion.method_mandated
        and criterion.output_path is not None
        and criterion.requested_output_evidence_source != "independent_run_evidence"
        and not _validation_classification_output_path(criterion.output_path)
        and not is_judgment_finalization_candidate(criterion)
        and not criterion.id.startswith(_REQUESTED_OUTPUT_CRITERION_ID_PREFIX)
    ]
    if not source_criteria:
        return

    used_ids = {criterion.id for criterion in policy.completion_criteria}
    corroborators: list[CompletionCriterion] = []
    for criterion in source_criteria:
        corroborator_id = _requested_output_corroborator_id(criterion.id, used_ids)
        used_ids.add(corroborator_id)
        corroborators.append(
            replace(
                criterion,
                id=corroborator_id,
                output_path=None,
                expected_output_value=None,
                expected_output_shape=None,
                requested_output_corroborator=True,
            )
        )

    requested_output_paths = {criterion.output_path for criterion in source_criteria if criterion.output_path}
    policy.completion_criteria = _cap_completion_criteria(
        policy.completion_criteria + corroborators,
        requested_output_paths,
    )


def _apply_requested_output_completion_criteria(
    policy: RequestPolicy, user_message: str, aliases: dict[str, str] | None = None, *, extract_literals: bool = False
) -> None:
    schema_aliases = schema_output_path_aliases_from_criteria(policy.completion_criteria)
    config_aliases = _normalize_requested_output_aliases(aliases)
    schema_aliases = _normalize_requested_output_aliases(schema_aliases)
    detection_aliases = {**config_aliases, **schema_aliases}
    requested_fields = _requested_output_fields(user_message, detection_aliases)
    forbidden_fields = _requested_output_fields(user_message, detection_aliases, negated=True)
    if not requested_fields and not forbidden_fields:
        return

    requested_specs: list[tuple[str, str, str]] = []
    requested_output_paths: set[str] = set()
    for field_name in requested_fields:
        output_path = _requested_output_path_for_detected_field(field_name, schema_aliases, config_aliases)
        field_label = _requested_output_field_label(field_name, output_path)
        if output_path in requested_output_paths:
            continue
        requested_output_paths.add(output_path)
        requested_specs.append((field_name, output_path, field_label))

    forbidden_output_paths = {
        _requested_output_path_for_detected_field(field_name, schema_aliases, config_aliases)
        for field_name in forbidden_fields
    }

    if not requested_output_paths and not forbidden_output_paths:
        return

    value_by_output_path = _requested_output_expected_values_from_criteria(policy.completion_criteria, requested_specs)
    if extract_literals:
        for output_path, literal in _fallback_literal_expected_values(user_message, requested_specs).items():
            value_by_output_path.setdefault(output_path, literal)
    shape_by_output_path = _requested_output_shapes_from_criteria(policy.completion_criteria, requested_specs)
    source_by_output_path = _requested_output_evidence_sources_from_criteria(
        policy.completion_criteria, requested_specs
    )
    judgment_condition_by_output_path = _requested_output_judgment_conditions_from_criteria(
        policy.completion_criteria, requested_specs
    )

    metadata_by_output_path: dict[str, tuple[str | None, str | None, Literal["registered_download"] | None]] = {}
    declared_kind_by_output_path: dict[str, Literal["registered_download"]] = {}
    for criterion in policy.completion_criteria:
        if criterion.level == "definition" or criterion.method_mandated:
            continue
        if (
            not criterion.contingent_on
            and not criterion.contingent_antecedent_output_path
            and not criterion.deliverable_kind
            and not criterion.declared_deliverable_kind
        ):
            continue
        for field_name, output_path, _field_label in requested_specs:
            if criterion.output_path == output_path or _criterion_text_covers_requested_output(criterion, field_name):
                deliverable_kind = (
                    criterion.deliverable_kind if output_path in REGISTERED_DOWNLOAD_REQUESTED_OUTPUT_PATHS else None
                )
                if criterion.output_path == output_path and criterion.declared_deliverable_kind:
                    declared_kind_by_output_path.setdefault(output_path, criterion.declared_deliverable_kind)
                metadata_by_output_path.setdefault(
                    output_path,
                    (
                        criterion.contingent_on,
                        criterion.contingent_antecedent_output_path,
                        deliverable_kind,
                    ),
                )

    preserved_criteria = [
        criterion
        for criterion in policy.completion_criteria
        if _preserve_after_requested_output_canonicalization(
            criterion,
            requested_fields,
            requested_output_paths,
            forbidden_fields,
            forbidden_output_paths,
        )
    ]
    canonical_requested_criteria: list[CompletionCriterion] = []
    for _field_name, output_path, field_label in requested_specs:
        expected_value = value_by_output_path.get(output_path)
        expected_shape = shape_by_output_path.get(output_path)
        judgment_condition = judgment_condition_by_output_path.get(output_path)
        mint_disposition, mint_degrade = _requested_output_mint_state(
            expected_value,
            expected_shape,
            judgment_condition,
        )
        canonical_requested_criteria.append(
            CompletionCriterion(
                id=_requested_output_criterion_id(output_path),
                outcome=f"The returned record includes {field_label}.",
                level="run",
                output_path=output_path,
                expected_output_value=expected_value,
                expected_output_shape=expected_shape,
                requested_output_evidence_source=source_by_output_path.get(output_path, "runtime_output"),
                contingent_on=metadata_by_output_path.get(output_path, (None, None, None))[0],
                contingent_antecedent_output_path=metadata_by_output_path.get(output_path, (None, None, None))[1],
                deliverable_kind=metadata_by_output_path.get(output_path, (None, None, None))[2],
                declared_deliverable_kind=declared_kind_by_output_path.get(output_path),
                judgment_truth_condition=judgment_condition,
                mint_degrade=mint_degrade,
                mint_disposition=mint_disposition,
            )
        )
    criteria = preserved_criteria + canonical_requested_criteria
    if not any(_non_requested_output_run_corroborator(criterion) for criterion in criteria):
        corroborator = _source_requested_output_corroborator(
            policy.completion_criteria,
            requested_fields,
            requested_output_paths,
        )
        if corroborator is not None:
            criteria = preserved_criteria + [corroborator] + canonical_requested_criteria
    policy.completion_criteria = _cap_completion_criteria(
        criteria,
        requested_output_paths,
    )


def _validation_classification_target_for_legacy_criterion(
    criterion: CompletionCriterion,
) -> tuple[str, ClassificationTarget] | None:
    if criterion.kind != "outcome" or criterion.level == "definition" or criterion.method_mandated:
        return None
    if not criterion.output_path:
        return None
    boolean_target = _VALIDATION_CLASSIFICATION_BOOLEAN_OUTPUT_TARGETS.get(criterion.output_path)
    if boolean_target is not None:
        return boolean_target
    label_key = _VALIDATION_CLASSIFICATION_LABEL_OUTPUT_TARGETS.get(criterion.output_path)
    if label_key is None or criterion.expected_output_value is None:
        return None
    return (label_key, criterion.expected_output_value)


def _apply_validation_classification_completion_criteria(policy: RequestPolicy) -> None:
    """Promote legacy classifier output carriers into typed validation-classification contracts."""

    criteria: list[CompletionCriterion] = []
    seen_classification_targets: set[tuple[str, str]] = set()
    for criterion in policy.completion_criteria:
        target = _validation_classification_target_for_legacy_criterion(criterion)
        if target is not None:
            output_key, expected = target
            boolean_classification = isinstance(expected, bool)
            criterion = replace(
                criterion,
                kind="validation_classification",
                output_path=None,
                expected_output_value=None,
                expected_output_shape="goal_judgment_boolean" if boolean_classification else None,
                requested_output_evidence_source=(
                    "independent_run_evidence" if boolean_classification else "runtime_output"
                ),
                deliverable_kind=None,
                declared_deliverable_kind=None,
                terminal_action_family=None,
                classification_output_key=output_key,
                expected_classification=expected,
                mint_disposition="pending" if boolean_classification else "decidable",
            )
        if criterion.kind == "validation_classification":
            target_key = (
                criterion.classification_output_key or "",
                str(criterion.expected_classification) if criterion.expected_classification is not None else "",
            )
            if target_key in seen_classification_targets:
                continue
            seen_classification_targets.add(target_key)
        criteria.append(criterion)
    policy.completion_criteria = criteria


def _render_active_criteria_for_prompt(criteria: list[CompletionCriterion] | None) -> str:
    if not criteria:
        return ""
    rendered: list[dict[str, Any]] = []
    for criterion in criteria:
        item: dict[str, Any] = {
            "outcome": criterion.outcome,
            "implicit": criterion.implicit,
            "method_mandated": criterion.method_mandated,
            "level": criterion.level,
            "kind": criterion.kind,
            "terminal_action_family": criterion.terminal_action_family,
        }
        if criterion.contingent_on:
            item["contingent_on"] = criterion.contingent_on
        if criterion.contingent_antecedent_output_path:
            item["contingent_antecedent_output_path"] = criterion.contingent_antecedent_output_path
        if criterion.deliverable_kind:
            item["deliverable_kind"] = criterion.deliverable_kind
        if criterion.output_path:
            item["output_path"] = criterion.output_path
        if criterion.expected_output_value is not None:
            item["expected_output_value"] = criterion.expected_output_value
        if criterion.expected_output_shape:
            item["expected_output_shape"] = criterion.expected_output_shape
        if criterion.requested_output_evidence_source != "runtime_output":
            item["requested_output_evidence_source"] = criterion.requested_output_evidence_source
        if criterion.judgment_truth_condition is not None:
            item["judgment_predicate"] = criterion.judgment_truth_condition.predicate
            item["judgment_polarity_when_holds"] = criterion.judgment_truth_condition.polarity_when_holds
        if criterion.classification_output_key:
            item["classification_output_key"] = criterion.classification_output_key
        if criterion.expected_classification is not None:
            item["expected_classification"] = criterion.expected_classification
        if criterion.requested_output_corroborator:
            item["requested_output_corroborator"] = True
        rendered.append(item)
    return json.dumps(rendered)


def is_fallback_floor_criterion(criterion: CompletionCriterion) -> bool:
    return criterion.id.startswith(FALLBACK_FLOOR_CRITERION_ID_PREFIX)


def is_fallback_floor_base_criterion(criterion: CompletionCriterion) -> bool:
    return criterion.id == _FALLBACK_FLOOR_BASE_ID


def is_turn_unsatisfiable_fallback_degraded(criterion: CompletionCriterion) -> bool:
    return criterion.mint_degrade == "turn_unsatisfiable_fallback"


def is_contingent_missing_antecedent_degraded(criterion: CompletionCriterion) -> bool:
    return criterion.mint_degrade == "contingent_missing_antecedent"


def resolve_mint_degrade(
    stored_value: object,
    contingent_on: str | None,
    contingent_antecedent_output_path: str | None,
) -> MintDegrade | None:
    if isinstance(stored_value, str) and stored_value in MINT_DEGRADE_VALUES:
        return cast(MintDegrade, stored_value)
    if contingent_on and contingent_antecedent_output_path is None:
        return "contingent_missing_antecedent"
    return None


_FALLBACK_LITERAL_MIN_CHARS = 4
_FALLBACK_LITERAL_BINDER_RE = r"(?:equal to|equals|should be|must be|is expected to be|expected to be|will be|is|:|=)"
_FALLBACK_LITERAL_MAX_SCAN_CHARS = 4000


def _fallback_literal_field_surface_forms(field_name: str, field_label: str) -> list[str]:
    forms: set[str] = set()
    for source in (field_name, field_label):
        collapsed = " ".join(source.split()).strip()
        if collapsed:
            forms.add(collapsed)
            forms.add(collapsed.replace(" ", "_"))
    return [form for form in sorted(forms, key=len, reverse=True) if form]


def _fallback_literal_excluded_forms(field_name: str, field_label: str, output_path: str) -> set[str]:
    forms = {
        " ".join(_word_tokens(field_name)),
        " ".join(_word_tokens(field_label)),
        " ".join(_word_tokens(output_path)),
    }
    return {form for form in forms if form}


def _fallback_literal_candidates_for_field(user_message: str, field_name: str, field_label: str) -> list[str]:
    bounded_message = user_message[:_FALLBACK_LITERAL_MAX_SCAN_CHARS]
    candidates: list[str] = []
    for surface in _fallback_literal_field_surface_forms(field_name, field_label):
        pattern = re.compile(
            r"\b"
            + re.escape(surface)
            + r"\b\s{0,8}+(?:"
            + _FALLBACK_LITERAL_BINDER_RE
            + r"\s{0,8}+)?(?P<quote>['\"`])(?P<quoted>[^'\"`]{1,80}+)(?P=quote)",
            re.IGNORECASE,
        )
        for match in pattern.finditer(bounded_message):
            quoted = match.group("quoted")
            if quoted is not None:
                candidates.append(quoted)
    return candidates


def _fallback_literal_expected_values(
    user_message: str, requested_specs: list[tuple[str, str, str]]
) -> dict[str, ExpectedOutputValue]:
    if not user_message.strip():
        return {}
    values: dict[str, ExpectedOutputValue] = {}
    for field_name, output_path, field_label in requested_specs:
        excluded = _fallback_literal_excluded_forms(field_name, field_label, output_path)
        accepted: list[str] = []
        for raw in _fallback_literal_candidates_for_field(user_message, field_name, field_label):
            coerced = _coerce_expected_output_value(raw)
            if not isinstance(coerced, str) or len(coerced) < _FALLBACK_LITERAL_MIN_CHARS:
                continue
            if " ".join(_word_tokens(coerced)) in excluded:
                continue
            accepted.append(coerced)
        unique = list(dict.fromkeys(accepted))
        if len(unique) == 1:
            values[output_path] = unique[0]
    return values


def _mark_turn_unsatisfiable_fallback_criteria(policy: RequestPolicy) -> None:
    marked: list[CompletionCriterion] = []
    for criterion in policy.completion_criteria:
        value_less = criterion.expected_output_value is None and criterion.expected_output_shape is None
        is_floor_base = is_fallback_floor_base_criterion(criterion)
        is_requested_output = criterion.id.startswith(_REQUESTED_OUTPUT_CRITERION_ID_PREFIX)
        if value_less and (is_floor_base or is_requested_output):
            marked.append(replace(criterion, mint_degrade="turn_unsatisfiable_fallback"))
        else:
            marked.append(criterion)
    policy.completion_criteria = marked


def _degrade_pathless_contingent_criteria(policy: RequestPolicy) -> None:
    swept: list[CompletionCriterion] = []
    degraded_ids: list[str] = []
    for criterion in policy.completion_criteria:
        resolved = resolve_mint_degrade(
            criterion.mint_degrade, criterion.contingent_on, criterion.contingent_antecedent_output_path
        )
        if resolved != criterion.mint_degrade:
            criterion = replace(criterion, mint_degrade=resolved)
            degraded_ids.append(criterion.id)
        swept.append(criterion)
    policy.completion_criteria = swept
    if degraded_ids:
        LOG.info(
            "copilot_contingent_criterion_mint_degraded",
            criterion_ids=degraded_ids,
            mint_degrade="contingent_missing_antecedent",
        )


def build_classifier_fallback_floor(ids: list[str]) -> list[CompletionCriterion]:
    floor = [
        CompletionCriterion(
            id=_FALLBACK_FLOOR_BASE_ID,
            outcome=_FALLBACK_FLOOR_BASE_OUTCOME,
            implicit=True,
            method_mandated=False,
            level="run",
        )
    ]
    if ids:
        floor.append(
            CompletionCriterion(
                id=_FALLBACK_FLOOR_CREDENTIAL_ID,
                outcome=_FALLBACK_FLOOR_CREDENTIAL_OUTCOME,
                implicit=True,
                method_mandated=True,
                level="run",
            )
        )
    return floor[:_MAX_COMPLETION_CRITERIA]


def _classifier_fallback_policy(
    ids: list[str],
    *,
    raw_secret_present: bool,
    failure_kind: str,
    retry_count: int = 0,
    user_message: str = "",
    requested_output_path_aliases: dict[str, str] | None = None,
) -> RequestPolicy:
    if failure_kind not in _CLASSIFIER_FAILURE_KINDS:
        failure_kind = "provider_error"
    if raw_secret_present:
        return RequestPolicy(
            credential_input_kind="raw_secret",
            credential_refs=ids,
            raw_secret_detected=True,
            raw_secret_handling="block",
            clarification_reason="raw_secret",
            classifier_status="fallback",
            classifier_failure_kind=failure_kind,
            classifier_retry_count=retry_count,
            completion_contract_status="unknown",
        )
    fallback_criteria = _fallback_structured_record_completion_criteria(user_message)
    if fallback_criteria:
        LOG.info(
            "copilot request policy synthesized fallback structured-record criteria",
            classifier_failure_kind=failure_kind,
            completion_criterion_ids=[criterion.id for criterion in fallback_criteria],
        )
    policy = RequestPolicy(
        credential_input_kind="credential_id" if ids else "none",
        credential_refs=ids,
        completion_criteria=fallback_criteria or build_classifier_fallback_floor(ids),
        classifier_status="fallback",
        classifier_failure_kind=failure_kind,
        classifier_retry_count=retry_count,
        completion_contract_status="present" if fallback_criteria else "unknown",
    )
    _apply_requested_output_completion_criteria(
        policy, user_message, requested_output_path_aliases, extract_literals=True
    )
    _apply_classifier_typed_requested_output_corroborators(policy)
    _mark_turn_unsatisfiable_fallback_criteria(policy)
    _degrade_pathless_contingent_criteria(policy)
    if policy.graded_completion_criteria():
        policy.completion_contract_status = "present"
    return policy


def _word_tokens(text: str) -> list[str]:
    return "".join(char if char.isalnum() else " " for char in text.casefold()).split()


def _fallback_structured_record_completion_criteria(user_message: str) -> list[CompletionCriterion]:
    """Conservative structured-record criteria when the request-policy classifier is unavailable."""

    words = _word_tokens(user_message)
    if not words:
        return []
    haystack = f" {' '.join(words)} "

    def _mentions(*terms: str) -> bool:
        # Whole-word/phrase match so "id" can't fire on "consider" nor "name" on "filename".
        return any(f" {' '.join(_word_tokens(term))} " in haystack for term in terms)

    has_long_number = any(word.isdigit() and len(word) >= 6 for word in words)
    if not _mentions("return", "record", "capture", "read", "extract"):
        return []
    if not _mentions("name", "entity", "person"):
        return []
    if not _mentions("identifier", "id", "number") and not has_long_number:
        return []
    if not _mentions("status"):
        return []
    if not _mentions("locations", "items", "rows", "groups", "entries", "per location", "per item", "per row"):
        return []
    return [
        CompletionCriterion(
            id="fallback_record_identity",
            outcome="The returned record identifies the target entity.",
            implicit=True,
            level="run",
        ),
        CompletionCriterion(
            id="fallback_record_identifier",
            outcome="The returned record includes the requested identifier-like value.",
            implicit=True,
            level="run",
        ),
        CompletionCriterion(
            id="fallback_record_groups",
            outcome="The returned record includes the requested grouped row entries.",
            implicit=True,
            level="run",
        ),
        CompletionCriterion(
            id="fallback_record_status",
            outcome=(
                "The returned record's per-row statuses and summary status are present and internally consistent."
            ),
            implicit=True,
            level="run",
        ),
    ]


def _explicit_code_block_credential_draft_requested(user_message: str) -> bool:
    normalized = " ".join((user_message or "").lower().split())
    if not normalized:
        return False
    has_code_marker = any(marker in normalized for marker in _CODE_BLOCK_AUTHORING_MARKERS)
    if not has_code_marker:
        return False
    blocks_login = any(marker in normalized for marker in _LOGIN_BLOCK_BAN_MARKERS)
    mentions_credential_code = any(marker in normalized for marker in _CREDENTIAL_CODE_MARKERS)
    return blocks_login or mentions_credential_code


def _apply_explicit_code_block_credential_draft_policy(policy: RequestPolicy, user_message: str) -> None:
    if policy.raw_secret_detected:
        return
    if not _explicit_code_block_credential_draft_requested(user_message):
        return
    policy.testing_intent = "skip_test"
    policy.allow_update_workflow = True
    policy.allow_run_blocks = False
    policy.allow_missing_credentials_in_draft = True
    policy.credential_draft_deferred_explicitly = True
    policy.requires_user_clarification = False
    policy.user_response_policy = "proceed"
    policy.clarification_reason = "none"
    policy.clarification_question = None


async def _run_request_policy_classifier(handler: Any, prompt: str) -> tuple[Any | None, str, int]:
    # Diverges from turn-intent on purpose: a retriable provider error (429/5xx) retries once
    # within the budget, while a timeout is never retried (retrying cannot beat the budget).
    deadline = time.monotonic() + settings.COPILOT_REQUEST_POLICY_CLASSIFIER_TIMEOUT_SECONDS
    retry_count = 0
    while True:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            # Only reachable after a retriable error consumed the budget before the retry ran.
            return None, "transient_error", retry_count
        try:
            raw = await asyncio.wait_for(
                handler(prompt=prompt, prompt_name=PROMPT_NAME),
                timeout=remaining,
            )
            return raw, "none", retry_count
        except asyncio.TimeoutError:
            return None, "timeout", retry_count
        except Exception as exc:
            if retry_count == 0 and is_retriable_llm_error(exc):
                retry_count += 1
                continue
            return None, "provider_error", retry_count


async def _classify_request(
    user_message: str,
    workflow_yaml: str,
    chat_history: list[WorkflowCopilotChatHistoryMessage],
    global_llm_context: str,
    handler: Any,
    *,
    active_criteria: list[CompletionCriterion] | None = None,
    config: CopilotConfig | None = None,
) -> RequestPolicy:
    requested_output_path_aliases = config.requested_output_path_aliases if config is not None else {}
    ids = _credential_ids(user_message)
    raw_secret_present = _raw_secret_detected(user_message)
    if raw_secret_present and handler is None:
        return _classifier_fallback_policy(
            ids,
            raw_secret_present=True,
            failure_kind="raw_secret_no_handler",
            user_message=user_message,
            requested_output_path_aliases=requested_output_path_aliases,
        )
    structural_reason = _structural_clarification_reason(user_message)
    if structural_reason != "none" and not raw_secret_present:
        return RequestPolicy(
            credential_input_kind="credential_id" if ids else "none",
            credential_refs=ids,
            requires_user_clarification=True,
            clarification_reason=structural_reason,
        )
    if handler is None:
        return _classifier_fallback_policy(
            ids,
            raw_secret_present=False,
            failure_kind="missing_handler",
            user_message=user_message,
            requested_output_path_aliases=requested_output_path_aliases,
        )

    # Raw-secret turns intentionally reach the classifier when available so it
    # can distinguish unsafe secret use from redacted draft/spec conversion.
    # Timeout and exception fallbacks remain conservative blocks below.
    safe_user_message = redact_raw_secrets_for_prompt(user_message) if raw_secret_present else user_message
    safe_global_llm_context = sanitize_global_llm_context_for_prompt(global_llm_context)
    transcript = build_transcript_context(chat_history, safe_user_message)
    request_slot_request = RequestSlotProducerInputV1(
        version="1",
        latest_request=safe_user_message[:16_384],
        workflow_context=workflow_yaml[:32_768],
        earliest_user_turn=""
        if transcript.earliest_user_turn == _EMPTY_SLOT_SENTINEL
        else transcript.earliest_user_turn,
        latest_prior_user_turn=(
            "" if transcript.latest_prior_user_turn == _EMPTY_SLOT_SENTINEL else transcript.latest_prior_user_turn
        ),
        latest_assistant_turn=(
            "" if transcript.latest_assistant_turn == _EMPTY_SLOT_SENTINEL else transcript.latest_assistant_turn
        ),
        retained_history=(
            () if transcript.retained_history == _EMPTY_SLOT_SENTINEL else (transcript.retained_history,)
        ),
        global_context=safe_global_llm_context[:32_768],
    )
    prompt = prompt_engine.load_prompt(
        template=PROMPT_NAME,
        user_message=escape_code_fences(safe_user_message),
        raw_secret_present=str(raw_secret_present).lower(),
        workflow_yaml=escape_code_fences(redact_raw_secrets_for_prompt(workflow_yaml)[:2048]),
        earliest_user_turn=transcript.earliest_user_turn,
        latest_prior_user_turn=transcript.latest_prior_user_turn,
        latest_assistant_turn=transcript.latest_assistant_turn,
        retained_history=transcript.retained_history,
        global_llm_context=escape_code_fences(redact_raw_secrets_for_prompt(safe_global_llm_context)[:2048]),
        active_completion_criteria=escape_code_fences(_render_active_criteria_for_prompt(active_criteria)),
        request_slot_sources=json.dumps(
            # The classifier needs the same bounded, byte-exact source set as the
            # producer so it can nominate an unambiguous source ID + quote. This is
            # intentionally repeated alongside the role-labelled transcript context;
            # the latter supplies conversational meaning, while this block supplies
            # server-verifiable identity.
            [source.model_dump(mode="json") for source in request_slot_sources(request_slot_request)],
            ensure_ascii=True,
            separators=(",", ":"),
        ),
    )
    raw, failure_kind, retry_count = await _run_request_policy_classifier(handler, prompt)
    if raw is None:
        LOG.warning("request-policy classifier failed", failure_kind=failure_kind, retry_count=retry_count)
        return _classifier_fallback_policy(
            ids,
            raw_secret_present=raw_secret_present,
            failure_kind=failure_kind,
            retry_count=retry_count,
            user_message=user_message,
            requested_output_path_aliases=requested_output_path_aliases,
        )

    raw_payload = _coerce_classifier_payload(raw)
    if raw_payload is None:
        LOG.warning("request-policy classifier returned malformed payload")
        return _classifier_fallback_policy(
            ids,
            raw_secret_present=raw_secret_present,
            failure_kind=failure_kind if failure_kind != "none" else "provider_error",
            retry_count=retry_count,
            user_message=user_message,
            requested_output_path_aliases=requested_output_path_aliases,
        )

    request_slot_contract: RequestSlotContractV1 | None = None
    request_slot_failure_kind: str | None = None
    raw_criteria = raw_payload.get("completion_criteria")
    if _request_slot_claims_need_anchor_correction(
        raw_criteria,
        request_slot_request=request_slot_request,
    ):
        correction_prompt = _request_slot_anchor_correction_prompt(
            prompt,
            raw_payload=raw_payload,
        )
        corrected_raw, correction_failure_kind, correction_retry_count = await _run_request_policy_classifier(
            handler,
            correction_prompt,
        )
        retry_count += correction_retry_count
        corrected_payload = _coerce_classifier_payload(corrected_raw)
        accepted_payload = (
            _accept_request_slot_anchor_correction(
                raw_payload,
                corrected_payload,
                request_slot_request=request_slot_request,
            )
            if corrected_payload is not None
            else None
        )
        if accepted_payload is not None:
            raw_payload = accepted_payload
            raw_criteria = raw_payload.get("completion_criteria")
        else:
            request_slot_failure_kind = "invalid_anchor_correction"
            LOG.warning(
                "request-policy request-slot anchor correction failed",
                failure_kind=correction_failure_kind,
            )
    declared_request_slots = (
        request_slot_failure_kind is None
        and isinstance(raw_criteria, list)
        and any(
            isinstance(item, dict)
            and _item_claims_request_slot(item)
            and _request_slot_anchor_is_admissible(item, request_slot_request=request_slot_request)
            for item in raw_criteria
        )
    )
    if declared_request_slots:
        # This validation hop is deliberately sequential: the request-policy
        # classifier first declares which criteria are request-owned slots, then
        # the independent producer types their identity, plane, and pinability.
        request_slot_result = await produce_request_slots(request=request_slot_request, handler=handler)
        if request_slot_result.status == "success":
            request_slot_contract = request_slot_result.contract
        else:
            request_slot_failure_kind = (
                request_slot_result.failure_kind.value if request_slot_result.failure_kind is not None else "unknown"
            )
            LOG.warning(
                "request-policy request-slot producer failed",
                failure_kind=request_slot_failure_kind,
                attempts=request_slot_result.attempts,
            )
    policy = _classification_from_raw(
        raw_payload,
        request_slot_request=request_slot_request,
        request_slot_contract=request_slot_contract,
        request_slot_failure_kind=request_slot_failure_kind,
    )
    policy.classifier_retry_count = retry_count
    policy.classifier_non_runtime_requested_output_evidence_sources = sorted(
        {
            criterion.requested_output_evidence_source
            for criterion in policy.completion_criteria
            if criterion.requested_output_evidence_source != "runtime_output"
        }
    )
    policy.completion_contract = _ground_completion_contract(user_message, policy.completion_contract)
    policy.completion_contract_status = (
        "present" if policy.completion_contract or policy.completion_criteria else "absent"
    )
    classifier_credential_refs = [_canonicalize_credential_ref(ref) for ref in policy.credential_refs]
    policy.credential_refs = _clean_list(classifier_credential_refs + ids)
    if raw_secret_present:
        policy.raw_secret_detected = True
        if policy.raw_secret_handling == "redacted_draft":
            if policy.credential_input_kind == "raw_secret":
                policy.credential_input_kind = "placeholder"
            policy.raw_secret_evidence = None
        else:
            policy.credential_input_kind = "raw_secret"
            policy.raw_secret_handling = "block"
            policy.clarification_reason = structural_reason if structural_reason != "none" else "raw_secret"
    if policy.testing_intent == "skip_test" and policy.completion_contract:
        policy.testing_intent = "unspecified"
    if ids and policy.credential_input_kind != "raw_secret":
        # A deterministically-extracted `cred_`-shaped token overrides a non-ID kind, unless the
        # classifier pointed at another resolvable target — a saved name (an ID-shaped ref does
        # not count) or a login-page URL — leaving the cred_ token as contextual.
        classifier_named_a_credential = any(not _credential_ids(ref) for ref in classifier_credential_refs)
        classifier_target_wins = (
            policy.credential_input_kind == "credential_name" and classifier_named_a_credential
        ) or (policy.credential_input_kind == "website_stored_credential" and bool(policy.login_page_urls))
        if not classifier_target_wins:
            policy.credential_input_kind = "credential_id"
    if (
        policy.credential_input_kind == "raw_secret"
        and not raw_secret_present
        and not _verify_raw_secret_evidence(policy.raw_secret_evidence, user_message)
    ):
        # The classifier claimed raw_secret but cited no verifiable secret in
        # the latest message — typically a token carried over from a prior turn.
        # Clear the claim so the turn classifies on its own merits downstream.
        LOG.warning(
            "request-policy raw_secret claim failed evidence verification; clearing",
            evidence_cited=policy.raw_secret_evidence is not None,
        )
        policy.credential_input_kind = "credential_id" if ids else "none"
        policy.clarification_reason = "none"
        policy.requires_user_clarification = False
        policy.raw_secret_evidence = None
    _degrade_pathless_contingent_criteria(policy)
    policy.completion_contract_status = (
        "present" if policy.completion_contract or policy.graded_completion_criteria() else "absent"
    )
    return policy


async def _load_credentials(organization_id: str) -> list[Credential]:
    page = 1
    credentials: list[Credential] = []
    while True:
        items = await app.DATABASE.credentials.get_credentials(organization_id=organization_id, page=page, page_size=50)
        credentials.extend(items)
        if len(items) < 50:
            return sorted(credentials, key=lambda c: getattr(c, "created_at", None) or "", reverse=True)
        page += 1


def _quote_in_credential_context(user_message: str, quote_start: int) -> bool:
    return bool(_CREDENTIAL_QUOTE_CONTEXT_RE.search(user_message[max(0, quote_start - 48) : quote_start]))


def _exact_credential_name_candidates(user_message: str) -> list[str]:
    text = user_message or ""
    candidates: list[str] = []
    for match in _QUOTED_CREDENTIAL_NAME_RE.finditer(text):
        value = next((group for group in match.groups() if group), "").strip()
        if value and _quote_in_credential_context(text, match.start()):
            candidates.append(value)
    for match in _NAMED_CREDENTIAL_TOKEN_RE.finditer(text):
        value = match.group(1).strip()
        if value:
            candidates.append(value)
    return _clean_list(candidates)


def _exact_credential_name_scan_eligible(policy: RequestPolicy) -> bool:
    if policy.raw_secret_detected:
        return False
    if policy.clarification_reason in _PRE_RESOLUTION_CLARIFICATION_REASONS:
        return False
    if policy.credential_input_kind == "credential_name" and policy.credential_refs:
        return False
    return policy.credential_input_kind in ("none", "credential_name", "website_stored_credential")


async def _apply_exact_credential_name_scope(
    policy: RequestPolicy,
    *,
    user_message: str,
    organization_id: str,
) -> None:
    if not _exact_credential_name_scan_eligible(policy):
        return
    candidates = _exact_credential_name_candidates(user_message)
    if not candidates:
        return
    credentials = await _load_credentials(organization_id)
    if (
        policy.credential_input_kind == "website_stored_credential"
        and policy.login_page_urls
        and _match_by_url(credentials, policy.login_page_urls)
    ):
        return
    matched_names = _clean_list(
        [candidate for candidate in candidates if any(credential.name == candidate for credential in credentials)]
    )
    if len(matched_names) == 1:
        policy.credential_input_kind = "credential_name"
        policy.credential_refs = matched_names
        policy.requires_user_clarification = False
        policy.clarification_reason = "none"
        policy.clarification_question = None
    elif len(matched_names) > 1:
        matches = [credential for credential in credentials if credential.name in matched_names]
        _block(
            policy,
            "I found multiple saved credentials named in your request. Which one should I use?",
            matches,
            reason="credential_name_unresolved",
        )


def _safe_label(credential: Credential) -> str:
    parts = [f"`{credential.credential_id}`", credential.name]
    parts += [f"Login Page URL: {credential.tested_url}"] if credential.tested_url else []
    return " - ".join(parts)


def _block(
    policy: RequestPolicy,
    question: str,
    candidates: list[Credential] | None = None,
    *,
    reason: ClarificationReason | None = None,
) -> None:
    policy.requires_user_clarification = True
    policy.user_response_policy = "ask_clarification"
    policy.allow_update_workflow = policy.allow_run_blocks = False
    if reason is not None:
        policy.clarification_reason = reason
    if candidates:
        question += "\n\nSafe matches:\n" + "\n".join(f"- {_safe_label(candidate)}" for candidate in candidates)
    policy.clarification_question = question


def _url_parts(url: str) -> tuple[str, str] | None:
    parsed = urlparse(url if "://" in url else f"https://{url}")
    if not parsed.netloc:
        return None
    host = parsed.netloc.lower()
    path = parsed.path.rstrip("/")
    return f"{parsed.scheme.lower()}://{host}{path}", f"{parsed.scheme.lower()}://{host}"


def _match_by_url(credentials: list[Credential], urls: list[str]) -> list[Credential]:
    indexed = [
        (credential, parts)
        for credential in credentials
        if credential.tested_url and (parts := _url_parts(credential.tested_url))
    ]
    requested = [parts for url in urls if (parts := _url_parts(url))]
    for index in range(2):
        matches = [
            credential for credential, parts in indexed if any(parts[index] == target[index] for target in requested)
        ]
        if matches:
            return matches
    return []


_CLARIFICATION_DECISION_PREFIX = "request-policy clarification required:"
_PRIOR_CREDENTIAL_CLARIFICATION_REASONS = frozenset(
    {
        "credential_name_unresolved",
        "credential_invention_requested",
        "workflow_credential_inputs_unbound",
        "raw_secret",
    }
)


def _prior_turn_was_credential_clarification(global_llm_context: str) -> bool:
    # Walk in reverse so a stale credential clarification followed by later non-credential
    # work does not keep routing the generic fallback into credential-help text.
    structured = StructuredContext.from_json_str(global_llm_context)
    for decision in reversed(structured.decisions_made):
        if not decision.startswith(_CLARIFICATION_DECISION_PREFIX):
            continue
        return any(decision.endswith(f"/{reason}") for reason in _PRIOR_CREDENTIAL_CLARIFICATION_REASONS)
    return False


def _clarification_question(policy: RequestPolicy, global_llm_context: str = "") -> str:
    if policy.clarification_reason == "raw_secret":
        return _RAW_SECRET_QUESTION
    if policy.clarification_reason == "credential_name_unresolved":
        if policy.credential_input_kind == "website_stored_credential":
            return _STORED_CREDENTIAL_URL_QUESTION
        return _SAVED_CREDENTIAL_NAME_QUESTION
    if policy.clarification_reason == "credential_invention_requested":
        return (
            "I cannot invent a credential ID. Please provide a valid saved credential ID, "
            f"select an existing credential, or create one in the Credentials UI. {_CREDENTIALS_UI_DIRECTIONS}"
        )
    if policy.clarification_reason == "ambiguous_loop_edit":
        return "Which block or blocks should go inside the loop, and what should the loop iterate over or stop on?"
    if policy.clarification_reason == "invalid_conditional_container":
        return (
            "Conditional blocks route to other blocks; they do not contain loop blocks. "
            "What condition should route into the loop, and should any default branch skip it?"
        )
    if policy.clarification_reason == "missing_conditional_condition":
        return "What condition should trigger this conditional route?"
    if policy.clarification_reason == "missing_target_context":
        if policy.credential_input_kind == "website_stored_credential":
            return _STORED_CREDENTIAL_URL_QUESTION
        return "Which page or URL should the workflow go to?"
    if policy.clarification_reason == "workflow_credential_inputs_unbound":
        return _WORKFLOW_CREDENTIAL_INPUTS_UNBOUND_QUESTION
    if policy.credential_input_kind == "credential_name":
        return _SAVED_CREDENTIAL_NAME_QUESTION
    if policy.credential_input_kind == "website_stored_credential":
        return _STORED_CREDENTIAL_URL_QUESTION
    if _prior_turn_was_credential_clarification(global_llm_context):
        return _SAVED_CREDENTIAL_NAME_QUESTION
    return "I need one more detail before I can build and test this workflow safely."


def _has_resolvable_credential_scope(policy: RequestPolicy) -> bool:
    if policy.credential_input_kind == "credential_id":
        return any(ref.startswith("cred_") for ref in policy.credential_refs)
    if policy.credential_input_kind == "credential_name":
        return bool(policy.credential_refs)
    if policy.credential_input_kind == "website_stored_credential":
        return bool(policy.login_page_urls)
    return False


def _prioritize_credential_clarification(policy: RequestPolicy) -> None:
    if policy.credential_input_kind not in ("credential_id", "credential_name"):
        return
    if not policy.credential_refs:
        return
    if policy.clarification_reason not in _REASONS_OVERRIDDEN_BY_CREDENTIAL_REFS:
        return
    policy.clarification_reason = "credential_name_unresolved"


def _previous_credential_clarification_was_asked(global_llm_context: str) -> bool:
    structured = StructuredContext.from_json_str(global_llm_context)
    return any(
        decision.startswith(_CLARIFICATION_DECISION_PREFIX) and "/credential_name_unresolved" in decision
        for decision in structured.decisions_made
    )


def _discovered_credential_ids_from_context(global_llm_context: str) -> set[str]:
    structured = StructuredContext.from_json_str(global_llm_context)
    return {
        check.credential_id
        for check in structured.credentials_checked
        if check.found and isinstance(check.credential_id, str) and check.credential_id.startswith("cred_")
    }


async def _seed_discovered_credentials(
    policy: RequestPolicy,
    *,
    organization_id: str,
    global_llm_context: str,
) -> None:
    discovered_ids = _discovered_credential_ids_from_context(global_llm_context)
    if not discovered_ids:
        return
    policy.discovered_credentials = await app.DATABASE.credentials.get_credentials_by_ids(
        sorted(discovered_ids),
        organization_id=organization_id,
    )


def _last_assistant_message_was_saved_credential_question(
    chat_history: list[WorkflowCopilotChatHistoryMessage],
) -> bool:
    for message in reversed(chat_history):
        if message.sender == WorkflowCopilotChatSender.AI:
            return (
                _SAVED_CREDENTIAL_NAME_QUESTION_STABLE_PREFIX in message.content
                or _STORED_CREDENTIAL_URL_QUESTION_STABLE_PREFIX in message.content
            )
    return False


def _can_defer_unresolved_credential_name_for_draft(
    policy: RequestPolicy,
    *,
    global_llm_context: str,
) -> bool:
    if policy.clarification_reason != "credential_name_unresolved":
        return False
    if _has_resolvable_credential_scope(policy):
        return True
    if _previous_credential_clarification_was_asked(global_llm_context):
        return True
    return False


def _should_defer_repeated_unresolved_credential_question(
    policy: RequestPolicy,
    *,
    chat_history: list[WorkflowCopilotChatHistoryMessage],
) -> bool:
    if not _last_assistant_message_was_saved_credential_question(chat_history):
        return False
    return (
        policy.credential_input_kind in ("none", "credential_name")
        and policy.clarification_reason == "credential_name_unresolved"
        and not _has_resolvable_credential_scope(policy)
    )


def _defer_unresolved_credential_for_draft(policy: RequestPolicy) -> None:
    # Preserve the reason code for observability after the question stops being user-blocking.
    policy.requires_user_clarification = False
    policy.user_response_policy = "proceed"
    policy.allow_update_workflow = True
    policy.allow_run_blocks = False
    policy.allow_missing_credentials_in_draft = True
    policy.clarification_reason = "credential_name_unresolved"
    policy.clarification_question = None


async def _resolve_credentials(
    policy: RequestPolicy,
    organization_id: str,
    *,
    defer_unresolved_credential_name: bool = False,
) -> None:
    if policy.credential_input_kind == "credential_id":
        ids = _clean_list([ref for ref in policy.credential_refs if ref.startswith("cred_")])
        if not ids:
            return
        existing = await app.DATABASE.credentials.get_credentials_by_ids(ids, organization_id=organization_id)
        found = {credential.credential_id for credential in existing}
        policy.resolved_credentials = existing
        policy.invalid_credential_ids = [credential_id for credential_id in ids if credential_id not in found]
        if policy.invalid_credential_ids and policy.testing_intent != "skip_test":
            formatted = ", ".join(f"`{credential_id}`" for credential_id in policy.invalid_credential_ids)
            _block(
                policy,
                f"The credential ID(s) {formatted} were not found in this organization. Please provide a valid saved credential ID or explicitly ask for an unvalidated draft that will not be run yet.",
                reason="credential_name_unresolved",
            )
        elif policy.invalid_credential_ids:
            policy.allow_run_blocks = False
            policy.allow_missing_credentials_in_draft = True
        return

    if policy.credential_input_kind == "credential_name" and not policy.credential_refs:
        if policy.allow_missing_credentials_in_draft:
            policy.allow_run_blocks = False
            return
        _block(
            policy,
            _SAVED_CREDENTIAL_NAME_QUESTION,
            reason="credential_name_unresolved",
        )
        return
    if policy.credential_input_kind == "website_stored_credential" and not policy.login_page_urls:
        _block(
            policy,
            _STORED_CREDENTIAL_URL_QUESTION,
            reason="missing_target_context",
        )
        return
    if policy.credential_input_kind not in ("credential_name", "website_stored_credential"):
        return

    credentials = await _load_credentials(organization_id)
    if policy.credential_input_kind == "credential_name":
        for ref in policy.credential_refs:
            matches = [credential for credential in credentials if credential.name == ref]
            if len(matches) == 1:
                policy.resolved_credentials.append(matches[0])
            elif matches:
                _block(
                    policy,
                    "I found multiple stored credentials with that exact name. Which one should I use?",
                    matches,
                    reason="credential_name_unresolved",
                )
                return
            elif policy.testing_intent == "skip_test" or defer_unresolved_credential_name:
                policy.allow_run_blocks, policy.allow_missing_credentials_in_draft = False, True
                if defer_unresolved_credential_name:
                    _defer_unresolved_credential_for_draft(policy)
            else:
                _block(
                    policy,
                    f"I could not find a stored credential named `{ref}`. Please choose an existing credential by exact name or a credential ID beginning with cred_.",
                    reason="credential_name_unresolved",
                )
                return
        return

    matches = _match_by_url(credentials, policy.login_page_urls)
    if len(matches) == 1:
        policy.resolved_credentials = matches
    elif matches:
        _block(
            policy,
            "I found multiple stored credentials for that login page. Which one should I use?",
            matches,
            reason="credential_name_unresolved",
        )
    else:
        _block(
            policy,
            "I could not find a stored credential for that login page. Please select a saved credential by exact name or a credential ID beginning with cred_, or create one in the Credentials UI.",
            reason="credential_name_unresolved",
        )


def _is_login_credential_param(param: dict[str, Any]) -> bool:
    raw = param.get("parameter_type")
    if not isinstance(raw, str):
        return False
    try:
        return ParameterType(raw).is_login_credential()
    except (ValueError, TypeError):
        return False


def _iter_login_credential_params(parsed: dict[str, Any]) -> list[tuple[str, dict[str, Any]]]:
    workflow_def = parsed.get("workflow_definition")
    if not isinstance(workflow_def, dict):
        return []
    out: list[tuple[str, dict[str, Any]]] = []
    for param in workflow_def.get("parameters") or []:
        if isinstance(param, dict) and _is_login_credential_param(param):
            out.append(("workflow", param))

    def _walk(block_list: Any) -> None:
        if not isinstance(block_list, list):
            return
        for block in block_list:
            if not isinstance(block, dict):
                continue
            label = str(block.get("label") or "<unlabeled>")
            for param in block.get("parameters") or []:
                if isinstance(param, dict) and _is_login_credential_param(param):
                    out.append((label, param))
            _walk(block.get("loop_blocks"))

    _walk(workflow_def.get("blocks"))
    return out


def _value_resolves_at_init(value: Any) -> bool:
    if value is None:
        return False
    if isinstance(value, str):
        return bool(value.strip())
    return True


def _workflow_credential_inputs_unbound(workflow_yaml: str) -> list[dict[str, str]]:
    if not workflow_yaml:
        return []
    try:
        parsed = safe_load_no_dates(workflow_yaml)
    except Exception:
        return []
    if not isinstance(parsed, dict):
        return []

    workflow_def = parsed.get("workflow_definition")
    if not isinstance(workflow_def, dict):
        return []

    workflow_param_bound: dict[str, bool] = {}
    for param in workflow_def.get("parameters") or []:
        if isinstance(param, dict) and param.get("parameter_type") == "workflow":
            key = param.get("key")
            if isinstance(key, str):
                workflow_param_bound[key] = _value_resolves_at_init(param.get("default_value"))

    findings: list[dict[str, str]] = []
    for location, param in _iter_login_credential_params(parsed):
        for field_name in _LOGIN_CREDENTIAL_REQUIRED_KEY_FIELDS:
            value = param.get(field_name)
            if not isinstance(value, str) or value.strip():
                continue
            findings.append(
                {"location": location, "field": field_name, "missing": "<empty>", "kind": "credential_empty"}
            )
        for field_name, value in param.items():
            if field_name in _CREDENTIAL_PARAM_METADATA_FIELDS:
                continue
            if not isinstance(value, str):
                continue
            for jinja_key in _JINJA_TEMPLATE_VAR_RE.findall(value):
                if jinja_key in workflow_param_bound:
                    if not workflow_param_bound[jinja_key]:
                        findings.append(
                            {
                                "location": location,
                                "field": field_name,
                                "missing": jinja_key,
                                "kind": "credential_template_unbound",
                            }
                        )
                else:
                    findings.append(
                        {
                            "location": location,
                            "field": field_name,
                            "missing": jinja_key,
                            "kind": "credential_template_undefined",
                        }
                    )
    return findings


async def build_request_policy(
    *,
    user_message: str,
    workflow_yaml: str,
    chat_history: list[WorkflowCopilotChatHistoryMessage],
    global_llm_context: str,
    organization_id: str,
    handler: Any,
    active_criteria: list[CompletionCriterion] | None = None,
    config: CopilotConfig | None = None,
) -> RequestPolicy:
    policy = await _classify_request(
        user_message,
        workflow_yaml,
        chat_history,
        global_llm_context,
        handler,
        active_criteria=active_criteria,
        config=config,
    )
    policy.raw_secret_detected = policy.raw_secret_detected or policy.credential_input_kind == "raw_secret"
    policy.existing_workflow_credential_ids = sorted(workflow_credential_ids(workflow_yaml))
    policy.existing_workflow_credential_origins = {
        credential_id: sorted(origins) for credential_id, origins in workflow_credential_origins(workflow_yaml).items()
    }
    try:
        await _apply_exact_credential_name_scope(
            policy,
            user_message=user_message,
            organization_id=organization_id,
        )
    except Exception:
        LOG.warning(
            "request-policy exact credential-name extraction failed",
            organization_id=organization_id,
            exc_info=True,
        )
    # This narrows classifier output only after the classifier has identified
    # credential intent; running it earlier would be overwritten by the model verdict.
    _apply_explicit_code_block_credential_draft_policy(policy, user_message)
    try:
        await _seed_discovered_credentials(
            policy,
            organization_id=organization_id,
            global_llm_context=global_llm_context,
        )
    except Exception:
        LOG.warning(
            "request-policy discovered-credential seeding failed",
            organization_id=organization_id,
            exc_info=True,
        )
    _prioritize_credential_clarification(policy)

    if policy.raw_secret_detected and policy.raw_secret_handling == "redacted_draft":
        policy.testing_intent = "skip_test"
        policy.requires_user_clarification = False
        policy.user_response_policy = "proceed"
        policy.allow_update_workflow = True
        policy.allow_run_blocks = False
        policy.allow_missing_credentials_in_draft = True
        policy.credential_draft_deferred_explicitly = True
        policy.clarification_reason = "none"
        policy.clarification_question = None

    if policy.clarification_reason == "none" and not policy.raw_secret_detected:
        if _workflow_credential_inputs_unbound(workflow_yaml):
            policy.clarification_reason = "workflow_credential_inputs_unbound"
            policy.allow_run_blocks = False
            policy.allow_missing_credentials_in_draft = True
    if policy.testing_intent == "skip_test":
        policy.allow_run_blocks = False
        if (
            policy.credential_input_kind != "raw_secret"
            and policy.clarification_reason not in _PRE_RESOLUTION_CLARIFICATION_REASONS
        ):
            if (
                policy.clarification_reason == "credential_name_unresolved"
                and not _can_defer_unresolved_credential_name_for_draft(
                    policy,
                    global_llm_context=global_llm_context,
                )
            ):
                policy.requires_user_clarification = True
                policy.allow_update_workflow = False
            else:
                policy.requires_user_clarification = False
                policy.allow_missing_credentials_in_draft = True

    if _should_defer_repeated_unresolved_credential_question(
        policy,
        chat_history=chat_history,
    ):
        policy.requires_user_clarification = False
        policy.allow_update_workflow = True
        policy.allow_run_blocks = False
        policy.allow_missing_credentials_in_draft = True

    if policy.raw_secret_detected and policy.raw_secret_handling != "redacted_draft":
        question = _RAW_SECRET_QUESTION
        if policy.clarification_reason in _PRE_RESOLUTION_CLARIFICATION_REASONS:
            question += "\n\n" + _clarification_question(policy, global_llm_context)
        _block(
            policy,
            question,
            reason="raw_secret",
        )
    elif policy.requires_user_clarification and policy.clarification_reason in _PRE_RESOLUTION_CLARIFICATION_REASONS:
        _block(policy, _clarification_question(policy, global_llm_context))
    elif policy.requires_user_clarification and not _has_resolvable_credential_scope(policy):
        _block(policy, _clarification_question(policy, global_llm_context))
    else:
        try:
            # A resolvable credential scope can override the classifier's
            # conservative clarification flag; _resolve_credentials will block
            # again if the lookup is missing or ambiguous.
            policy.requires_user_clarification = False
            await _resolve_credentials(
                policy,
                organization_id,
                defer_unresolved_credential_name=_last_assistant_message_was_saved_credential_question(chat_history),
            )
        except Exception:
            LOG.warning(
                "request-policy credential resolution failed",
                organization_id=organization_id,
                credential_input_kind=policy.credential_input_kind,
                exc_info=True,
            )
            _block(
                policy,
                "I could not verify the requested credential metadata for this organization. Please provide a valid saved credential by exact name or a credential ID beginning with cred_.",
            )

    if policy.authoring_intent == "defer_authoring":
        policy.allow_update_workflow = False
        policy.allow_run_blocks = False
        if not any(is_defer_authoring_durable_fill_criterion(criterion) for criterion in policy.completion_criteria):
            policy.completion_criteria = list(policy.completion_criteria) + [_defer_authoring_durable_fill_criterion()]

    trace_data = policy.to_trace_data()
    if policy.classifier_status == "fallback":
        LOG.warning("request-policy fallback policy used", **trace_data)
    with copilot_span("request_policy", data=trace_data):
        LOG.info("request-policy decision", **trace_data)
    return policy
