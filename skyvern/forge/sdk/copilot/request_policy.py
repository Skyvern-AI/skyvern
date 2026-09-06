from __future__ import annotations

import asyncio
import copy
import hashlib
import json
import re
import time
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass, field, replace
from typing import Any, Literal, cast, get_args

import structlog
from pydantic import BaseModel, ConfigDict, ValidationError

from skyvern.config import settings
from skyvern.forge import app
from skyvern.forge.log_redaction import redact_sensitive_fields
from skyvern.forge.prompts import prompt_engine
from skyvern.forge.sdk.api.llm.api_handler import LLMAPIHandler
from skyvern.forge.sdk.copilot.config import CopilotConfig
from skyvern.forge.sdk.copilot.context import ProposedCredential, StructuredContext
from skyvern.forge.sdk.copilot.credential_resolution import (
    CredentialResolution,
    CredentialResolutionTier,
)
from skyvern.forge.sdk.copilot.credential_resolution import deduplicate_credentials as _deduplicate_credentials
from skyvern.forge.sdk.copilot.credential_resolution import (
    is_resolved_page_url,
)
from skyvern.forge.sdk.copilot.credential_resolution import load_credentials as _load_credentials
from skyvern.forge.sdk.copilot.credential_resolution import (
    loggable_origin,
    resolve_by_url,
    unresolved_page_url_for_log,
)
from skyvern.forge.sdk.copilot.credential_resolution import url_parts as _url_parts
from skyvern.forge.sdk.copilot.reached_download_target import REGISTERED_DOWNLOAD_REQUESTED_OUTPUT_PATHS
from skyvern.forge.sdk.copilot.request_slots import (
    CanonicalRequestSlotV1,
    RequestSlotContractV1,
    RequestSlotProducerInputV1,
    request_slot_request_digest,
    request_slot_source_text,
    request_slot_sources,
)
from skyvern.forge.sdk.copilot.secret_redaction import (
    RAW_SECRET_PATTERNS,
    contains_email_password_pair,
    raw_secret_spans_for_prompt,
    redact_raw_secrets_for_prompt,
)
from skyvern.forge.sdk.copilot.tracing_setup import copilot_span
from skyvern.forge.sdk.copilot.workflow_credential_utils import (
    URL_CANDIDATE_RE,
    workflow_credential_ids,
    workflow_credential_origins,
)
from skyvern.forge.sdk.schemas.credentials import Credential, TotpType
from skyvern.forge.sdk.schemas.google_oauth import STATE_ACTIVE
from skyvern.forge.sdk.schemas.workflow_copilot import (
    TURN_OPENER_SENDERS,
    WorkflowCopilotChatHistoryMessage,
    WorkflowCopilotChatSender,
)
from skyvern.forge.sdk.services import google_oauth_service
from skyvern.forge.sdk.workflow.models.parameter import ParameterType
from skyvern.utils.strings import escape_code_fences
from skyvern.utils.yaml_loader import safe_load_no_dates

LOG = structlog.get_logger()


def _parse_final_response(raw: str) -> dict[str, Any]:
    from skyvern.forge.sdk.copilot.output_utils import parse_final_response

    return parse_final_response(raw)


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
_TERMINAL_ACTION_RECONCILIATION_RESPONSE_FIELDS = frozenset({"version", "criterion_id", "terminal_action_family"})
_TERMINAL_ACTION_RECONCILABLE_CREDENTIAL_KINDS = frozenset(
    {"credential_id", "credential_name", "website_stored_credential"}
)
_LONE_REGISTERED_DOWNLOAD_OUTPUT_PATH = "output.downloaded_files"
REGISTERED_DOWNLOAD_COMPLETION_CRITERION_ID = "__copilot_registered_download__downloaded_files_non_empty"
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
    "login_credentials_unresolved",
    "safety_screen_unavailable",
]
RawSecretHandling = Literal["none", "block", "redacted_draft"]
RawSecretSafetyStatus = Literal["not_run", "clean", "detected", "blocked"]
RawSecretSafetyFailureKind = Literal[
    "none",
    "missing_handler",
    "timeout",
    "provider_error",
    "malformed_output",
    "contradictory_verdict",
    "invalid_citation",
]
_RAW_SECRET_SAFETY_PROMPT_NAME = "workflow-copilot-raw-secret-safety"
_RAW_SECRET_SAFETY_UNAVAILABLE_TURN = "[INPUT_UNAVAILABLE_SAFETY_SCREEN_INCOMPLETE]"
_REDACTED_SECRET_PLACEHOLDER = "[REDACTED_SECRET]"


class RawSecretSafetyVerdict(BaseModel):
    # Tolerates unknown keys: a cosmetic schema drift in a model-generated blob must not
    # cost the user their turn, and the security-relevant fields are validated regardless.
    model_config = ConfigDict(extra="ignore")

    version: Literal["1"]
    state: Literal["clean", "detected"]
    citations: list[str]


@dataclass(frozen=True)
class _RawSecretSafetyScreen:
    status: RawSecretSafetyStatus
    canonical_user_message: str
    handling: RawSecretHandling = "none"
    citation_count: int = 0
    exonerated_citation_count: int = 0
    failure_kind: RawSecretSafetyFailureKind = "none"
    latency_ms: float = 0.0


def _raw_secret_safety_verdict(raw: object) -> RawSecretSafetyVerdict | None:
    if isinstance(raw, str):
        raw = _parse_final_response(raw)
    try:
        return RawSecretSafetyVerdict.model_validate(raw)
    except ValidationError:
        return None


def _validate_raw_secret_safety_verdict(
    verdict: RawSecretSafetyVerdict,
    *,
    original_user_message: str,
    screened_user_message: str,
) -> RawSecretSafetyFailureKind:
    if verdict.state == "clean":
        if verdict.citations:
            return "contradictory_verdict"
        return "none"
    if not verdict.citations:
        return "contradictory_verdict"
    if len(set(verdict.citations)) != len(verdict.citations) or any(not citation for citation in verdict.citations):
        return "contradictory_verdict"
    if any(
        citation == _REDACTED_SECRET_PLACEHOLDER
        or not _verify_raw_secret_evidence(citation, original_user_message)
        or citation not in screened_user_message
        for citation in verdict.citations
    ):
        return "invalid_citation"
    return "none"


def _screen_unavailable(
    failure_kind: RawSecretSafetyFailureKind,
    latency_ms: float = 0.0,
) -> _RawSecretSafetyScreen:
    return _RawSecretSafetyScreen(
        status="blocked",
        canonical_user_message=_RAW_SECRET_SAFETY_UNAVAILABLE_TURN,
        handling="block",
        failure_kind=failure_kind,
        latency_ms=latency_ms,
    )


async def _saved_credential_ids(citations: list[str], organization_id: str) -> set[str]:
    credential_ids = [citation for citation in citations if citation.startswith("cred_")]
    connection_ids = {citation for citation in citations if citation.startswith("goac_")}
    verified_ids: set[str] = set()
    if credential_ids:
        credentials = await app.DATABASE.credentials.get_credentials_by_ids(
            credential_ids,
            organization_id=organization_id,
        )
        verified_ids.update(credential.credential_id for credential in credentials)
    if connection_ids:
        connections = await google_oauth_service.get_visible_credentials_for_org(organization_id)
        verified_ids.update(connection.id for connection in connections if connection.id in connection_ids)
    return verified_ids


async def _exonerate_saved_credential_citations(citations: list[str], organization_id: str) -> list[str]:
    """Drop citations that are verified non-secret IDs visible to this org.

    Credential names are user-controlled and can equal real secret material, so equality with a
    saved name cannot make a cited span safe. Generated credential and Google connection IDs are
    non-secret identifiers; the database lookup verifies that a cited ID belongs to this organization
    before exonerating it.
    """
    try:
        credential_ids = await _saved_credential_ids(citations, organization_id)
    except Exception:
        LOG.warning(
            "raw-secret safety saved-credential exoneration lookup failed",
            organization_id=organization_id,
            exc_info=True,
        )
        return citations
    return [citation for citation in citations if citation not in credential_ids]


_MIN_RAW_SECRET_EVIDENCE_CHARS = 4
_RAW_SECRET_EVIDENCE_BEFORE_URI_VALUE_BOUNDARIES = "=?&#"
_RAW_SECRET_EVIDENCE_AFTER_URI_VALUE_BOUNDARIES = "&#"
_RAW_SECRET_EVIDENCE_SENTENCE_PUNCTUATION = ".,;:!?"
_RAW_SECRET_EVIDENCE_CLOSING_BOUNDARIES = ")]\"}'"


def _redact_raw_secrets_outside_url_candidates(message: str) -> str:
    """Keep independently occurring URL tokens intact for the model-backed disclosure decision."""
    deterministic_secret_spans = raw_secret_spans_for_prompt(message)
    pieces: list[str] = []
    cursor = 0
    for candidate in URL_CANDIDATE_RE.finditer(message):
        if any(
            start < candidate.end()
            and candidate.start() < end
            and not (candidate.start() <= start and end <= candidate.end())
            for start, end in deterministic_secret_spans
        ):
            continue
        pieces.append(redact_raw_secrets_for_prompt(message[cursor : candidate.start()]))
        pieces.append(candidate.group(0))
        cursor = candidate.end()
    pieces.append(redact_raw_secrets_for_prompt(message[cursor:]))
    return "".join(pieces)


def _raw_secret_evidence_uri_candidate_end(
    message: str,
    index: int,
    end: int,
    url_candidate_spans: tuple[tuple[int, int], ...],
) -> int | None:
    for candidate_start, candidate_end in url_candidate_spans:
        if candidate_start >= index or end > candidate_end:
            continue
        before = message[index - 1]
        if before in _RAW_SECRET_EVIDENCE_BEFORE_URI_VALUE_BOUNDARIES:
            return candidate_end
    return None


def _raw_secret_evidence_is_url_userinfo_password(
    message: str,
    index: int,
    end: int,
    url_candidate_spans: tuple[tuple[int, int], ...],
) -> bool:
    for candidate_start, candidate_end in url_candidate_spans:
        if index < candidate_start or end > candidate_end:
            continue
        candidate = message[candidate_start:candidate_end]
        scheme_end = candidate.find("://")
        if scheme_end < 0:
            continue
        authority_start = scheme_end + 3
        authority_end = min(
            (position for delimiter in "/?#" if (position := candidate.find(delimiter, authority_start)) >= 0),
            default=len(candidate),
        )
        userinfo_end = candidate.rfind("@", authority_start, authority_end)
        if userinfo_end < 0:
            continue
        password_start = candidate.find(":", authority_start, userinfo_end)
        if password_start < 0:
            continue
        if index == candidate_start + password_start + 1 and end == candidate_start + userinfo_end:
            return True
    return False


def _raw_secret_evidence_after_is_boundary(evidence: str, message: str, end: int) -> bool:
    if end >= len(message):
        return True
    after = message[end]
    if after.isspace() or after in _RAW_SECRET_EVIDENCE_CLOSING_BOUNDARIES:
        return True
    if after in _RAW_SECRET_EVIDENCE_SENTENCE_PUNCTUATION:
        following = message[end + 1] if end + 1 < len(message) else ""
        if not following or following.isspace() or following in _RAW_SECRET_EVIDENCE_CLOSING_BOUNDARIES:
            return True
    return not evidence[-1].isalnum() and after in ".,;:?"


def _raw_secret_evidence_spans(evidence: str | None, user_message: str) -> list[tuple[int, int]]:
    if not evidence or len(evidence) < _MIN_RAW_SECRET_EVIDENCE_CHARS:
        return []
    message = user_message or ""
    url_candidate_spans = tuple(
        (candidate.start(), candidate.end()) for candidate in URL_CANDIDATE_RE.finditer(message)
    )
    spans: list[tuple[int, int]] = []
    start = 0
    while True:
        index = message.find(evidence, start)
        if index < 0:
            return spans
        end = index + len(evidence)
        before = message[index - 1] if index else ""
        uri_candidate_end = _raw_secret_evidence_uri_candidate_end(message, index, end, url_candidate_spans)
        is_userinfo_password = _raw_secret_evidence_is_url_userinfo_password(
            message,
            index,
            end,
            url_candidate_spans,
        )
        before_is_boundary = (
            not before
            or before.isspace()
            or before in "([{\"'"
            or uri_candidate_end is not None
            or is_userinfo_password
        )
        after_is_uri_boundary = is_userinfo_password or (
            uri_candidate_end is not None
            and (end == uri_candidate_end or message[end] in _RAW_SECRET_EVIDENCE_AFTER_URI_VALUE_BOUNDARIES)
        )
        after_is_boundary = after_is_uri_boundary or _raw_secret_evidence_after_is_boundary(evidence, message, end)
        if before_is_boundary and after_is_boundary:
            spans.append((index, end))
        start = index + 1


def _redact_cited_secrets(message: str, citations: list[str]) -> str:
    spans = sorted(
        (span for citation in citations for span in _raw_secret_evidence_spans(citation, message)),
        key=lambda span: (span[0], span[1]),
    )
    merged: list[tuple[int, int]] = []
    for start, end in spans:
        if merged and start <= merged[-1][1]:
            merged[-1] = (merged[-1][0], max(merged[-1][1], end))
        else:
            merged.append((start, end))

    pieces: list[str] = []
    cursor = 0
    for start, end in merged:
        pieces.extend((message[cursor:start], _REDACTED_SECRET_PLACEHOLDER))
        cursor = end
    pieces.append(message[cursor:])
    return "".join(pieces)


async def _screen_raw_secret_safety(
    user_message: str,
    handler: LLMAPIHandler | None,
    *,
    organization_id: str,
) -> _RawSecretSafetyScreen:
    deterministic_safe_message = _redact_raw_secrets_outside_url_candidates(user_message)
    if handler is None:
        return _screen_unavailable("missing_handler")
    try:
        prompt = prompt_engine.load_prompt(
            template=_RAW_SECRET_SAFETY_PROMPT_NAME,
            user_message=escape_code_fences(deterministic_safe_message),
        )
    except Exception:
        return _screen_unavailable("malformed_output")

    started_at = time.monotonic()
    try:
        raw = await asyncio.wait_for(
            handler(prompt=prompt, prompt_name=_RAW_SECRET_SAFETY_PROMPT_NAME),
            timeout=settings.COPILOT_RAW_SECRET_SAFETY_TIMEOUT_SECONDS,
        )
    except TimeoutError:
        return _screen_unavailable("timeout", (time.monotonic() - started_at) * 1000)
    except Exception as exc:
        LOG.warning("raw-secret safety provider failed", exception_type=type(exc).__name__)
        return _screen_unavailable("provider_error", (time.monotonic() - started_at) * 1000)

    latency_ms = (time.monotonic() - started_at) * 1000
    verdict = _raw_secret_safety_verdict(raw)
    if verdict is None:
        return _screen_unavailable("malformed_output", latency_ms)
    failure_kind = _validate_raw_secret_safety_verdict(
        verdict,
        original_user_message=user_message,
        screened_user_message=deterministic_safe_message,
    )
    if failure_kind != "none":
        return _screen_unavailable(failure_kind, latency_ms)

    if verdict.state == "detected":
        cited = await _exonerate_saved_credential_citations(verdict.citations, organization_id)
        exonerated = len(verdict.citations) - len(cited)
        if cited:
            return _RawSecretSafetyScreen(
                status="detected",
                canonical_user_message=_redact_cited_secrets(deterministic_safe_message, cited),
                handling="redacted_draft",
                citation_count=len(cited),
                exonerated_citation_count=exonerated,
                latency_ms=latency_ms,
            )
        return _RawSecretSafetyScreen(
            status="clean",
            canonical_user_message=deterministic_safe_message,
            handling="none",
            exonerated_citation_count=exonerated,
            latency_ms=latency_ms,
        )

    return _RawSecretSafetyScreen(
        status="clean",
        canonical_user_message=deterministic_safe_message,
        handling="none",
        citation_count=len(verdict.citations),
        latency_ms=latency_ms,
    )


_VALID_CLARIFICATION_REASONS: frozenset[ClarificationReason] = frozenset(get_args(ClarificationReason))
# The server owns these in model-authored JSON. safety_screen_unavailable is minted
# deterministically; login_credentials_unresolved names a pause frame, and a model-emitted one
# would still stamp a credential CTA on the terminal reply.
_DETERMINISTIC_ONLY_CLARIFICATION_REASONS: frozenset[ClarificationReason] = frozenset(
    {"login_credentials_unresolved", "safety_screen_unavailable"}
)
# Concrete credential states for which the combined update-and-run tool persists
# the draft but cannot start a browser run.
CREDENTIAL_DEFERRED_DRAFT_REASONS: frozenset[ClarificationReason] = frozenset(
    {"workflow_credential_inputs_unbound", "credential_name_unresolved"}
)
# Broader: any reason credential_prompt_reason() should surface an add-credential CTA for.
CREDENTIAL_PROMPT_CLARIFICATION_REASONS: frozenset[ClarificationReason] = frozenset(
    {
        "raw_secret",
        "credential_name_unresolved",
        "credential_invention_requested",
        "workflow_credential_inputs_unbound",
        "login_credentials_unresolved",
    }
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
RAW_SECRET_QUESTION = (
    "Please do not paste raw login credentials or secrets in chat because they can enter model telemetry and execution traces. "
    "Store the credential in the Skyvern Credentials UI and reply with its exact saved credential name or a credential ID beginning with cred_. "
    f"{_CREDENTIALS_UI_DIRECTIONS} "
    f"{RAW_SECRET_REFUSAL_SENTINEL}."
)
SAFETY_SCREEN_UNAVAILABLE_QUESTION = (
    "I couldn't finish safety screening on that message, so I never read it. "
    "That's a problem on my side, not something wrong with what you sent — please send it again."
)
_AMBIGUOUS_URL_CREDENTIAL_QUESTION = "I found multiple stored credentials for that login page. Which one should I use?"
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
_CREDENTIAL_REPLACEMENT_TARGET_RES = (
    re.compile(
        r"\bswitch(?:ing)?\s+to\b"
        r"(?=\s+(?:(?:my|the|a)\s+)?(?:(?:saved|stored)\s+)?"
        r"(?:credential\b|cred_|[A-Za-z0-9_.@:-]{2,100}\s+credential\b))",
        re.I,
    ),
    re.compile(
        r"\breplace\s+(?:(?:it|this|that)\b|cred_[A-Za-z0-9_-]+|"
        r"(?:(?:my|the|a)\s+)?(?:(?:saved|stored)\s+)?credential"
        r"(?:\s+(?:(?:id|named|called)\s+)?[A-Za-z0-9_.@:-]{2,100})?)"
        r"\s+with\b"
        r"(?=\s+(?:(?:my|the|a)\s+)?(?:(?:saved|stored)\s+)?"
        r"(?:credential\b|cred_|[A-Za-z0-9_.@:-]{2,100}\s+credential\b))",
        re.I,
    ),
)
_REPLACEMENT_CONTEXT_RE = re.compile(
    r"\breplace\b(?P<object>[^.!?\n,;]{1,80}?)\bwith\s+"
    r"(?:(?:my|the|a)\s+)?(?:(?:saved|stored)\s+)?"
    r"(?:credential(?:\s+(?:id|named|called))?\s*)?$",
    re.I,
)
_CREDENTIAL_REPLACEMENT_OBJECT_RE = re.compile(
    r"\s*(?:(?:it|this|that)|cred_[A-Za-z0-9_-]+|"
    r"(?:(?:my|the|a)\s+)?(?:(?:saved|stored)\s+)?credential"
    r"(?:\s+(?:(?:id|named|called)\s+)?[A-Za-z0-9_.@:-]{2,100})?)\s*",
    re.I,
)
_NEGATED_CREDENTIAL_CONTEXT_RE = re.compile(
    r"(?:"
    r"\b(?:do\s+not|don't|never|without|avoid(?:ing)?|exclud(?:e|ed|es|ing))\b"
    r"(?:(?!\b(?:but|instead)\b)[^.!?\n,;])*"
    r"|\b(?:instead\s+of|rather\s+than|not|except)\s+"
    r"(?:(?:the|a)\s+)?(?:(?:saved\s+)?credential(?:\s+id)?\s*)?"
    r")$",
    re.I,
)
_EXPLICIT_LOGIN_ACTION_RE = re.compile(
    r"(?<![/\w-])(?:log[\s-]?in|login|sign[\s-]?in)\b"
    r"(?!\s+(?:form|page|path|route|screen|url)\b)",
    re.I,
)
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
_REQUEST_SLOT_ID_RE = re.compile(r"^[0-9a-f]{64}$")
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
TerminalActionVerificationMode = Literal["family_record_v1", "semantic_outcome_v1"]
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
    "value_present",
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
AntecedentFamily = Literal["unconditional", "blocker", "undecidable"]
_JUDGMENT_PREDICATES: frozenset[str] = frozenset(get_args(JudgmentPredicate))
MintDegrade = Literal[
    "turn_unsatisfiable_fallback",
    "contingent_missing_antecedent",
    "undecidable_judgment",
]
MINT_DEGRADE_VALUES: frozenset[str] = frozenset(get_args(MintDegrade))
ANTECEDENT_FAMILY_VALUES: frozenset[str] = frozenset(get_args(AntecedentFamily))
_CRITERION_KINDS: frozenset[str] = frozenset({"outcome", "terminal_action", "validation_classification"})
_TERMINAL_ACTION_FAMILIES: frozenset[str] = frozenset({"request", "application", "form", "order"})
_EXPECTED_OUTPUT_SHAPES: frozenset[str] = frozenset(get_args(ExpectedOutputShape))
_REQUESTED_OUTPUT_EVIDENCE_SOURCES: frozenset[str] = frozenset(get_args(RequestedOutputEvidenceSource))
RequestedOutputPathMintSource = Literal["classifier_default", "classifier_declared"]
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
# When one of these is followed by "of <noun phrase>" the field is the noun phrase, not the
# quantifier; the head-at-end window below cannot reach a subject that trails the field word.
_OUTPUT_QUANTIFIER_HEAD_WORDS = frozenset(
    "number numbers count counts total totals amount amounts sum sums quantity quantities "
    "average averages percentage percentages proportion proportions share shares".split()
)
# Words that end a quantifier's noun phrase: a scope/time qualifier ("... in the past 7 days") or another
# preposition begins here, so the field name stops before it.
_OUTPUT_PHRASE_BOUNDARY_WORDS = frozenset("in on over during within for per by from since between as at with".split())
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
    antecedent_family: AntecedentFamily | None = None
    deliverable_kind: Literal["registered_download"] | None = None
    # Author-time seam signal only: unlike ``deliverable_kind`` it survives canonicalization onto
    # non-canonical output paths, so it is never rendered to the completion verifier.
    declared_deliverable_kind: Literal["registered_download"] | None = None
    # Classifier-authored license for the plain-outcome abstention: non-null only when this
    # criterion's separate observation may abstain behind the confirmed canonical download.
    deliverable_confirmation_criterion_id: str | None = None
    implicit: bool = False
    method_mandated: bool = False
    # "definition": a property of the workflow definition itself, graded against the
    # YAML; "run": an end state only a run can evidence. Invalid input coerces to "run".
    level: CriterionLevel = "run"
    output_path: str | None = None
    # Short page-facing noun for ``output_path`` (e.g. "visitors"), carried typed so the
    # extraction binder never has to re-tokenize it back out of ``outcome`` prose.
    requested_output_label: str | None = None
    expected_output_value: ExpectedOutputValue | None = None
    expected_output_shape: ExpectedOutputShape | None = None
    requested_output_evidence_source: RequestedOutputEvidenceSource = "runtime_output"
    requested_output_path_mint_source: RequestedOutputPathMintSource | None = None
    kind: CriterionKind = "outcome"
    terminal_action_family: TerminalActionFamily | None = None
    # Native terminal actions retain the historical structured-family grader. A criterion
    # promoted from a generic outcome must opt into semantic verification instead.
    terminal_action_verification_mode: TerminalActionVerificationMode = "family_record_v1"
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


@dataclass(frozen=True)
class TerminalActionReconciliationV1:
    version: Literal["1"]
    criterion_id: str | None
    terminal_action_family: TerminalActionFamily | None


@dataclass(frozen=True)
class RequestSlotFailureSurfaceV1:
    version: Literal["1"]
    reason: Literal["request_slot_failure"]
    failure_kind: Literal["request_slot_failure"]
    retryable: Literal[False]
    request_slot_failure_kind: str


RequestSlotDatumField = Literal["output_path", "classification_output_key"]


@dataclass(frozen=True)
class RequestSlotDatumBindingV1:
    version: Literal["1"]
    criterion_index: int
    datum_field: RequestSlotDatumField
    datum_value: str
    source_id: str
    source_quote: str


@dataclass(frozen=True)
class RequestSlotDatumBindingTargetV1:
    criterion_index: int
    datum_field: RequestSlotDatumField
    datum_value: str
    criterion_outcome_sha256: str


@dataclass(frozen=True)
class TrustedRequestSlotDatumBindingV1:
    version: Literal["1"]
    criterion_index: int
    datum_field: RequestSlotDatumField
    datum_value: str
    criterion_outcome_sha256: str
    source_id: str
    source_quote: str
    slot_id: str


DatumBindingApplicationPredicate = Literal[
    "accepted",
    "invalid_criteria_container",
    "invalid_contract",
    "anchor_incoherent",
    "anchor_not_unique",
]


@dataclass(frozen=True)
class RequestSlotDatumBindingApplicationDecisionV1:
    version: Literal["1"]
    predicate: DatumBindingApplicationPredicate
    criterion_index: int | None = None
    accepted_payload: dict[str, Any] | None = None
    trusted_bindings: tuple[TrustedRequestSlotDatumBindingV1, ...] = ()


AnchorCorrectionPredicate = Literal[
    "accepted",
    "invalid_criteria_container",
    "criterion_count_changed",
    "payload_semantics_changed",
    "criteria_semantics_changed",
    "criterion_not_object",
    "criterion_semantics_changed",
    "missing_corrected_anchor",
    "original_quote_not_admissible",
    "missing_request_slot_fields",
    "corrected_anchor_not_admissible",
    "unexpected_anchor_change",
]


@dataclass(frozen=True)
class RequestSlotAnchorCorrectionDecisionV1:
    version: Literal["1"]
    predicate: AnchorCorrectionPredicate
    criterion_index: int | None = None
    accepted_payload: dict[str, Any] | None = None


LiveCredentialSeam = Literal["fill", "page_observation"]
LivePageResolutionVerdict = Literal["resolved", "ambiguous", "no_match", "declined", "abstain"]


@dataclass(frozen=True)
class LivePageResolutionRecord:
    verdict: LivePageResolutionVerdict
    tier: CredentialResolutionTier | None = None
    candidates: tuple[Credential, ...] = ()
    # The page the verdict is about. Without it a later observation's record is rendered as an answer
    # about whichever page the reply happens to mention.
    page_url: str = ""


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
    # Credentials bound this turn without a user ask (deterministic URL/urlless resolution + live-page
    # sole-match); read-only telemetry for the auto-bind receipt, never a run-authority source.
    auto_bound_credentials: list[Credential] = field(default_factory=list)
    # credential_id -> the login page URL that granted it, so each later fill can confirm the
    # browser has not left that site and a redirect cannot inherit the grant.
    live_page_admitted_urls: dict[str, str] = field(default_factory=dict)
    live_page_resolution: LivePageResolutionRecord | None = None
    live_page_logged_urls: set[str] = field(default_factory=set)
    # Ids that reached auto_bound_credentials from a carried proposal rather than from a page this
    # turn admitted. Without it the recorder cannot tell its own hydration from fresh evidence and
    # re-mints the carry every turn, so it never expires.
    seeded_proposal_credential_ids: set[str] = field(default_factory=set)
    # Ids whose citation passed on the carry this turn. Whether that stands as the user's answer is
    # settled at turn end. Kept apart from current_turn_named_credential_ids, whose claim is that
    # the user wrote the identifier out themselves.
    carry_cited_credential_ids: set[str] = field(default_factory=set)
    # Approves persisting a bound credential, not running it: run authority stays
    # scoped to resolved_credentials (ADR 0002).
    discovered_credentials: list[Credential] = field(default_factory=list)
    invalid_credential_ids: list[str] = field(default_factory=list)
    clarification_question: str | None = None
    raw_secret_detected: bool = False
    raw_secret_evidence: str | None = None
    raw_secret_handling: RawSecretHandling = "none"
    raw_secret_safety_status: RawSecretSafetyStatus = "not_run"
    raw_secret_safety_failure_kind: RawSecretSafetyFailureKind = "none"
    raw_secret_safety_citation_count: int = 0
    raw_secret_safety_exonerated_citation_count: int = 0
    raw_secret_safety_latency_ms: float = 0.0
    clarification_reason: ClarificationReason = "none"
    # The login page URLs a tool ask was formed against. A model-supplied URL reaches this list only
    # after it matched a site in ``user_provided_site_urls``; the connected resume falls back to the
    # classifier-authored ``login_page_urls`` when it is empty, which carries no such grounding.
    credential_ask_login_page_urls: list[str] = field(default_factory=list)
    # Credentials this turn resolved from an explicit user reference — an exact saved name or a
    # cred_ id — as distinct from approvals carried in from earlier turns. Naming a credential
    # answers which one to use, which is what lets the login page answer where it may be typed.
    current_turn_named_credential_ids: set[str] = field(default_factory=set)
    # Sites the user themselves provided anywhere in this chat, one URL per origin. A credential may
    # only be released onto one of these (or a vault/tested match); a site only a model produced is
    # never eligible.
    user_provided_site_urls: list[str] = field(default_factory=list)
    # Which user message (1-based) each of those URLs came from, so a release records its provenance.
    user_site_url_sources: dict[str, int] = field(default_factory=dict)
    existing_workflow_credential_ids: list[str] = field(default_factory=list)
    # Read from the saved workflow row, never from the submitted YAML. The submission is the live
    # canvas, which carries a copilot proposal the user has not accepted, so it cannot grant a run.
    persisted_workflow_credential_ids: list[str] = field(default_factory=list)
    # Active Google OAuth connections admitted only for workflow execution. These never enter
    # resolved_credentials, which remains the password-fill authority plane from ADR 0002.
    run_approved_google_connection_ids: list[str] = field(default_factory=list)
    # The connection the user picked from the account card on this turn, if any. Recorded as
    # durable approval at turn end. A draft binding alone is never a substitute; the dispatch
    # seam may separately admit a selected native Sheets citation for that dispatch only when it
    # is backed by this turn's server-owned list_integrations result.
    selected_connected_account_id: str | None = None
    # Sorted at the trace/JSON boundary; YAML traversal uses sets.
    existing_workflow_credential_origins: dict[str, list[str]] = field(default_factory=dict)
    classifier_status: str = "not_run"
    classifier_failure_kind: str = "none"
    classifier_retry_count: int = 0
    classifier_non_runtime_requested_output_evidence_sources: list[str] = field(default_factory=list)
    completion_contract_status: str = "absent"
    request_slot_failure_kind: str | None = None
    # Ephemeral, server-owned provenance for exact credential references. This is deliberately
    # excluded from traces and comparisons: it must describe only the latest literal user turn,
    # even when the agent input later expands terse replies with earlier context.
    canonical_user_message: str = field(default="", repr=False, compare=False)
    _authoring_pending: bool = field(default=False, repr=False, compare=False)

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
            "run_approved_google_connection_count": len(self.run_approved_google_connection_ids),
            "has_completion_contract": bool(self.completion_contract),
            "completion_criteria_count": len(self.graded_completion_criteria()),
            "completion_criteria_implicit_count": sum(
                1 for criterion in self.completion_criteria if criterion.implicit
            ),
            "completion_criteria_method_mandated_count": sum(
                1 for criterion in self.completion_criteria if criterion.method_mandated
            ),
            "completion_criteria_terminal_action_count": sum(
                1
                for criterion in self.completion_criteria
                if criterion.kind == "terminal_action" and not criterion.method_mandated
            ),
            "raw_secret_detected": self.raw_secret_detected,
            "has_raw_secret_evidence": self.raw_secret_evidence is not None,
            "raw_secret_handling": self.raw_secret_handling,
            "raw_secret_safety_status": self.raw_secret_safety_status,
            "raw_secret_safety_failure_kind": self.raw_secret_safety_failure_kind,
            "raw_secret_safety_citation_count": self.raw_secret_safety_citation_count,
            "raw_secret_safety_exonerated_citation_count": self.raw_secret_safety_exonerated_citation_count,
            "raw_secret_safety_latency_ms": round(self.raw_secret_safety_latency_ms, 3),
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
        family_criteria = [
            criterion for criterion in self.graded_completion_criteria() if criterion.antecedent_family is not None
        ]
        data["antecedent_family_criterion_count"] = len(family_criteria)
        for index, criterion in enumerate(family_criteria[:_MAX_TRACE_COMPLETION_CRITERIA]):
            prefix = f"antecedent_family_criterion_{index}"
            data[f"{prefix}_id"] = criterion.id
            data[f"{prefix}_antecedent_family"] = criterion.antecedent_family
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
        neutral_reported_booleans = [
            criterion for criterion in self.completion_criteria if is_neutral_reported_boolean_criterion(criterion)
        ]
        data["neutral_reported_boolean_criterion_count"] = len(neutral_reported_booleans)
        for index, criterion in enumerate(neutral_reported_booleans[:_MAX_TRACE_COMPLETION_CRITERIA]):
            prefix = f"neutral_reported_boolean_criterion_{index}"
            data[f"{prefix}_id"] = criterion.id
            data[f"{prefix}_kind"] = criterion.kind
            data[f"{prefix}_classification_output_key"] = criterion.classification_output_key
            data[f"{prefix}_expected_output_shape"] = criterion.expected_output_shape
            data[f"{prefix}_expected_output_value"] = criterion.expected_output_value
            data[f"{prefix}_expected_classification"] = criterion.expected_classification
            data[f"{prefix}_evidence_source"] = criterion.requested_output_evidence_source
            data[f"{prefix}_request_slot_id"] = criterion.request_slot_id
            data[f"{prefix}_pinability"] = criterion.pinability
            data[f"{prefix}_mint_disposition"] = criterion.mint_disposition
            data[f"{prefix}_requested_output_floor_rekeyed"] = criterion.requested_output_floor_rekeyed
            data[f"{prefix}_floor_rekeyed_from_path"] = criterion.floor_rekeyed_from_path
        return data

    def prompt_summary(self) -> str:
        lines = [
            f"credential_input_kind: {self.credential_input_kind}",
            f"raw_secret_handling: {self.raw_secret_handling}",
        ]
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
                *[f"- {credential_candidate_label(credential)}" for credential in self.resolved_credentials],
            ]
        if self.invalid_credential_ids:
            lines.append("invalid_credential_ids: " + ", ".join(f"`{cid}`" for cid in self.invalid_credential_ids))
        return "\n".join(lines)

    def trust_floor_summary(self) -> str:
        return "\n".join(
            [
                f"credential_input_kind: {self.credential_input_kind}",
                f"raw_secret_handling: {self.raw_secret_handling}",
            ]
        )


def floor_rekeyed_requested_output_paths(
    criteria: tuple[CompletionCriterion, ...] | list[CompletionCriterion],
) -> set[str]:
    """Identity of runtime-output criteria whose slot rekey cleared ``output_path``; the rekey
    preserves it in ``floor_rekeyed_from_path``, and without it they vanish from the requested set."""
    return {
        criterion.floor_rekeyed_from_path
        for criterion in criteria
        if criterion.requested_output_floor_rekeyed
        and criterion.floor_rekeyed_from_path
        and criterion.kind == "outcome"
        and criterion.level != "definition"
        and not criterion.method_mandated
        and criterion.requested_output_evidence_source == "runtime_output"
    }


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
REDACTED_REFUSED_SECRET_TURN = "[raw credentials redacted — this turn was refused]"
_REDACTED_REFUSED_SECRET_TURN = REDACTED_REFUSED_SECRET_TURN


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


def redact_refused_secret_turns(
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
        and filtered[-1].sender in TURN_OPENER_SENDERS
        and (filtered[-1].content or "").strip() == current_stripped
        and current_stripped
    ):
        filtered = filtered[:-1]

    filtered = redact_refused_secret_turns(filtered)

    user_indices = [i for i, m in enumerate(filtered) if m.sender in TURN_OPENER_SENDERS]
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
        role = "user" if message.sender in TURN_OPENER_SENDERS else "assistant"
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


def _verify_raw_secret_evidence(evidence: str | None, user_message: str) -> bool:
    return bool(_raw_secret_evidence_spans(evidence, user_message))


def _coerce_clarification_reason(value: Any) -> ClarificationReason:
    if value in _VALID_CLARIFICATION_REASONS and value not in _DETERMINISTIC_ONLY_CLARIFICATION_REASONS:
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
        raw = _parse_final_response(raw)
    if not isinstance(raw, dict):
        return None
    if not any(field in raw for field in _CLASSIFICATION_RESPONSE_FIELDS):
        return None
    return raw


def _parse_terminal_action_reconciliation(raw: Any) -> TerminalActionReconciliationV1 | None:
    if isinstance(raw, str):
        raw = _parse_final_response(raw)
    if not isinstance(raw, dict) or set(raw) != _TERMINAL_ACTION_RECONCILIATION_RESPONSE_FIELDS:
        return None
    if raw.get("version") != "1":
        return None
    criterion_id = raw.get("criterion_id")
    family = raw.get("terminal_action_family")
    if criterion_id is None and family is None:
        return TerminalActionReconciliationV1(version="1", criterion_id=None, terminal_action_family=None)
    if not isinstance(criterion_id, str) or not criterion_id.strip():
        return None
    if not isinstance(family, str) or family not in _TERMINAL_ACTION_FAMILIES:
        return None
    return TerminalActionReconciliationV1(
        version="1",
        criterion_id=criterion_id,
        terminal_action_family=cast(TerminalActionFamily, family),
    )


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


def _request_slot_datum_binding(
    item: dict[str, Any],
    *,
    criterion_index: int,
) -> RequestSlotDatumBindingV1 | None:
    raw = item.get("request_slot_datum_binding")
    if not isinstance(raw, dict) or set(raw) != {
        "version",
        "criterion_index",
        "datum_field",
        "datum_value",
        "source_id",
        "source_quote",
    }:
        return None
    version = raw.get("version")
    raw_index = raw.get("criterion_index")
    datum_field = raw.get("datum_field")
    datum_value = raw.get("datum_value")
    source_id = raw.get("source_id")
    source_quote = raw.get("source_quote")
    if (
        version != "1"
        or type(raw_index) is not int
        or raw_index != criterion_index
        or datum_field not in {"output_path", "classification_output_key"}
        or not isinstance(datum_value, str)
        or not datum_value
        or not isinstance(source_id, str)
        or not isinstance(source_quote, str)
        or not source_quote
    ):
        return None
    return RequestSlotDatumBindingV1(
        version="1",
        criterion_index=raw_index,
        datum_field=cast(RequestSlotDatumField, datum_field),
        datum_value=datum_value,
        source_id=source_id,
        source_quote=source_quote,
    )


def _request_slot_datum_binding_is_valid(
    item: dict[str, Any],
    *,
    criterion_index: int,
    request_slot_request: RequestSlotProducerInputV1,
) -> bool:
    binding = _request_slot_datum_binding(item, criterion_index=criterion_index)
    if binding is None:
        return False
    anchor = _request_slot_anchor(item)
    kind = _coerce_criterion_kind(item.get("kind"))
    if kind == "validation_classification":
        expected_datum_field: RequestSlotDatumField | None = "classification_output_key"
        active_datum_value = _coerce_classification_output_key(item.get("classification_output_key"))
    elif isinstance(item.get("output_path"), str) and item["output_path"].strip():
        expected_datum_field = "output_path"
        active_datum_value = item[expected_datum_field].strip()
    else:
        expected_datum_field = None
        active_datum_value = None
    return (
        binding.datum_field == expected_datum_field
        and active_datum_value is not None
        and item.get(binding.datum_field) == active_datum_value == binding.datum_value
        and anchor == (binding.source_id, binding.source_quote)
        and _request_slot_anchor_is_valid(item, request_slot_request=request_slot_request)
    )


def _trusted_request_slot_datum_binding_is_valid(
    item: dict[str, Any],
    *,
    binding: TrustedRequestSlotDatumBindingV1,
    criterion_index: int,
    request_slot_request: RequestSlotProducerInputV1,
) -> bool:
    raw_binding = item.get("request_slot_datum_binding")
    outcome = item.get("outcome")
    return _type_strict_json_equal(
        raw_binding,
        {
            "version": binding.version,
            "criterion_index": binding.criterion_index,
            "datum_field": binding.datum_field,
            "datum_value": binding.datum_value,
            "criterion_outcome_sha256": binding.criterion_outcome_sha256,
            "source_id": binding.source_id,
            "source_quote": binding.source_quote,
        },
    ) and (
        binding.criterion_index == criterion_index
        and item.get(binding.datum_field) == binding.datum_value
        and isinstance(outcome, str)
        and hashlib.sha256(outcome.encode()).hexdigest() == binding.criterion_outcome_sha256
        and _request_slot_anchor(item) == (binding.source_id, binding.source_quote)
        and _request_slot_anchor_is_valid(item, request_slot_request=request_slot_request)
    )


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
    criterion_index: int | None = None,
    trusted_binding: TrustedRequestSlotDatumBindingV1 | None = None,
    allow_embedded_binding: bool = False,
) -> bool:
    if (
        criterion_index is not None
        and trusted_binding is not None
        and _trusted_request_slot_datum_binding_is_valid(
            item,
            binding=trusted_binding,
            criterion_index=criterion_index,
            request_slot_request=request_slot_request,
        )
    ):
        return True
    if (
        criterion_index is not None
        and allow_embedded_binding
        and _request_slot_datum_binding_is_valid(
            item,
            criterion_index=criterion_index,
            request_slot_request=request_slot_request,
        )
    ):
        return True
    return _request_slot_anchor_is_valid(item, request_slot_request=request_slot_request) and (
        _request_slot_anchor_matches_criterion_datum(item)
    )


def _type_strict_json_equal(left: Any, right: Any) -> bool:
    if type(left) is not type(right):
        return False
    if isinstance(left, dict):
        return left.keys() == right.keys() and all(_type_strict_json_equal(left[key], right[key]) for key in left)
    if isinstance(left, list):
        return len(left) == len(right) and all(
            _type_strict_json_equal(left_item, right_item) for left_item, right_item in zip(left, right, strict=True)
        )
    return bool(left == right)


def _request_slot_datum_target(
    item: dict[str, Any],
    *,
    criterion_index: int,
) -> RequestSlotDatumBindingTargetV1 | None:
    outcome = item.get("outcome")
    if not isinstance(outcome, str) or not outcome:
        return None
    kind = _coerce_criterion_kind(item.get("kind"))
    if kind == "validation_classification":
        datum_value = _coerce_classification_output_key(item.get("classification_output_key"))
        datum_field: RequestSlotDatumField = "classification_output_key"
    elif kind == "terminal_action":
        return None
    else:
        raw_output_path = item.get("output_path")
        datum_value = raw_output_path.strip() if isinstance(raw_output_path, str) and raw_output_path.strip() else None
        datum_field = "output_path"
    if datum_value is None:
        return None
    return RequestSlotDatumBindingTargetV1(
        criterion_index=criterion_index,
        datum_field=datum_field,
        datum_value=datum_value,
        criterion_outcome_sha256=hashlib.sha256(outcome.encode()).hexdigest(),
    )


def _request_slot_datum_binding_targets(
    raw_criteria: Any,
    *,
    request_slot_request: RequestSlotProducerInputV1,
) -> tuple[RequestSlotDatumBindingTargetV1, ...]:
    if not isinstance(raw_criteria, list):
        return ()
    targets: list[RequestSlotDatumBindingTargetV1] = []
    for criterion_index, item in enumerate(raw_criteria):
        if not isinstance(item, dict):
            continue
        target = _request_slot_datum_target(item, criterion_index=criterion_index)
        if target is None:
            continue
        # First-pass model assertions are not provenance. Only the unchanged legacy
        # lexical check or a server-joined producer contract may license the datum.
        if _request_slot_anchor_is_valid(item, request_slot_request=request_slot_request) and (
            _request_slot_anchor_matches_criterion_datum(item)
        ):
            continue
        targets.append(target)
    return tuple(targets)


def _interrogative_slot_classification_output_key(
    source_quote: str,
    *,
    preserve_path_subject: bool = False,
) -> str | None:
    tokens = _word_tokens(source_quote)
    if not tokens or tokens[0] != "whether":
        return None
    tokens = tokens[1:]
    while tokens and tokens[0] in {"a", "an", "the"}:
        tokens = tokens[1:]
    if tokens[:2] == ["path", "is"]:
        tokens = [tokens[0], *tokens[2:]] if preserve_path_subject else tokens[2:]
    if not tokens:
        return None
    output_key = "_".join(tokens)
    return output_key if _CLASSIFICATION_OUTPUT_KEY_RE.fullmatch(output_key) is not None else None


def _omitted_request_slot_datum_binding_targets(
    raw_criteria: Any,
    *,
    request_slot_request: RequestSlotProducerInputV1,
    request_slot_contract: RequestSlotContractV1,
    existing_targets: tuple[RequestSlotDatumBindingTargetV1, ...],
) -> tuple[RequestSlotDatumBindingTargetV1, ...]:
    represented_slot_ids = {binding.slot_id for binding in request_slot_contract.datum_bindings}
    if isinstance(raw_criteria, list):
        for item in raw_criteria:
            if not isinstance(item, dict) or not _request_slot_anchor_is_valid(
                item,
                request_slot_request=request_slot_request,
            ):
                continue
            slot = _request_slot_for_anchor(
                _request_slot_anchor(item),
                request_slot_request=request_slot_request,
                request_slot_contract=request_slot_contract,
            )
            if slot is not None:
                represented_slot_ids.add(slot.slot_id)
    represented_output_keys = {target.datum_value for target in existing_targets}
    next_criterion_index = len(raw_criteria) if isinstance(raw_criteria, list) else 0
    targets: list[RequestSlotDatumBindingTargetV1] = []
    for slot in request_slot_contract.slots:
        if (
            slot.slot_id in represented_slot_ids
            or slot.plane.value != "run"
            or slot.pinability.value != "shapeless_valid"
        ):
            continue
        source_quote = request_slot_source_text(request_slot_request, slot)
        output_key = _interrogative_slot_classification_output_key(source_quote)
        if output_key is None or output_key in represented_output_keys:
            continue
        outcome = f"The run reports whether {output_key.replace('_', ' ')}."
        targets.append(
            RequestSlotDatumBindingTargetV1(
                criterion_index=next_criterion_index,
                datum_field="classification_output_key",
                datum_value=output_key,
                criterion_outcome_sha256=hashlib.sha256(outcome.encode()).hexdigest(),
            )
        )
        represented_output_keys.add(output_key)
        next_criterion_index += 1
    return tuple(targets)


def _trusted_omitted_request_slot_datum_bindings(
    *,
    request_slot_request: RequestSlotProducerInputV1,
    request_slot_contract: RequestSlotContractV1,
    omitted_targets: tuple[RequestSlotDatumBindingTargetV1, ...],
) -> tuple[TrustedRequestSlotDatumBindingV1, ...]:
    target_by_key = {(target.criterion_index, target.datum_field): target for target in omitted_targets}
    trusted: list[TrustedRequestSlotDatumBindingV1] = []
    for binding in request_slot_contract.datum_bindings:
        target = target_by_key.get((binding.criterion_index, binding.datum_field))
        if (
            target is None
            or binding.datum_value != target.datum_value
            or binding.criterion_outcome_sha256 != target.criterion_outcome_sha256
        ):
            continue
        resolved_slot = _request_slot_for_anchor(
            (binding.source_id, binding.source_quote),
            request_slot_request=request_slot_request,
            request_slot_contract=request_slot_contract,
        )
        if resolved_slot is None or binding.slot_id != resolved_slot.slot_id:
            continue
        trusted.append(
            TrustedRequestSlotDatumBindingV1(
                version="1",
                criterion_index=target.criterion_index,
                datum_field=target.datum_field,
                datum_value=target.datum_value,
                criterion_outcome_sha256=target.criterion_outcome_sha256,
                source_id=binding.source_id,
                source_quote=binding.source_quote,
                slot_id=binding.slot_id,
            )
        )
    return tuple(trusted)


def _apply_request_slot_datum_bindings(
    original: dict[str, Any],
    *,
    request_slot_request: RequestSlotProducerInputV1,
    request_slot_contract: RequestSlotContractV1,
    datum_binding_targets: tuple[RequestSlotDatumBindingTargetV1, ...] | None = None,
) -> RequestSlotDatumBindingApplicationDecisionV1:
    criteria = original.get("completion_criteria")
    if not isinstance(criteria, list):
        return RequestSlotDatumBindingApplicationDecisionV1(version="1", predicate="invalid_criteria_container")
    if request_slot_contract.request_digest != request_slot_request_digest(request_slot_request):
        return RequestSlotDatumBindingApplicationDecisionV1(version="1", predicate="invalid_contract")

    targets = datum_binding_targets or _request_slot_datum_binding_targets(
        criteria,
        request_slot_request=request_slot_request,
    )
    target_by_key = {(target.criterion_index, target.datum_field): target for target in targets}
    all_binding_by_key = {
        (binding.criterion_index, binding.datum_field): binding for binding in request_slot_contract.datum_bindings
    }
    all_decline_by_key = {
        (decline.criterion_index, decline.datum_field): decline for decline in request_slot_contract.datum_declines
    }
    request_target_by_key = {
        (target.criterion_index, target.datum_field): target for target in request_slot_request.datum_targets
    }
    binding_by_key = {key: binding for key, binding in all_binding_by_key.items() if key in target_by_key}
    decline_by_key = {key: decline for key, decline in all_decline_by_key.items() if key in target_by_key}
    if (
        len(all_binding_by_key) != len(request_slot_contract.datum_bindings)
        or len(all_decline_by_key) != len(request_slot_contract.datum_declines)
        or set(all_binding_by_key) & set(all_decline_by_key)
        or set(all_binding_by_key) | set(all_decline_by_key) != set(request_target_by_key)
        or set(binding_by_key) | set(decline_by_key) != set(target_by_key)
        or any(
            (request_target := request_target_by_key.get(key)) is None
            or request_target.criterion_index != target.criterion_index
            or request_target.datum_field != target.datum_field
            or request_target.datum_value != target.datum_value
            or request_target.criterion_outcome_sha256 != target.criterion_outcome_sha256
            for key, target in target_by_key.items()
        )
    ):
        return RequestSlotDatumBindingApplicationDecisionV1(version="1", predicate="invalid_contract")

    for key, decline in decline_by_key.items():
        target = target_by_key[key]
        if (
            decline.datum_value != target.datum_value
            or decline.criterion_outcome_sha256 != target.criterion_outcome_sha256
        ):
            return RequestSlotDatumBindingApplicationDecisionV1(
                version="1", predicate="invalid_contract", criterion_index=target.criterion_index
            )

    trusted_bindings: list[TrustedRequestSlotDatumBindingV1] = []
    for key, binding in binding_by_key.items():
        target = target_by_key[key]
        criterion = criteria[target.criterion_index]
        if not isinstance(criterion, dict):
            return RequestSlotDatumBindingApplicationDecisionV1(
                version="1", predicate="invalid_contract", criterion_index=target.criterion_index
            )
        outcome = criterion.get("outcome")
        if (
            binding.datum_value != target.datum_value
            or binding.criterion_outcome_sha256 != target.criterion_outcome_sha256
        ):
            return RequestSlotDatumBindingApplicationDecisionV1(
                version="1", predicate="invalid_contract", criterion_index=target.criterion_index
            )
        if (
            not isinstance(outcome, str)
            or hashlib.sha256(outcome.encode()).hexdigest() != target.criterion_outcome_sha256
        ):
            return RequestSlotDatumBindingApplicationDecisionV1(
                version="1", predicate="anchor_incoherent", criterion_index=target.criterion_index
            )
        candidate = {
            **criterion,
            "request_slot_source_id": binding.source_id,
            "request_slot_source_quote": binding.source_quote,
        }
        if not _request_slot_anchor_is_valid(candidate, request_slot_request=request_slot_request):
            return RequestSlotDatumBindingApplicationDecisionV1(
                version="1", predicate="anchor_not_unique", criterion_index=target.criterion_index
            )
        resolved_slot = _request_slot_for_anchor(
            (binding.source_id, binding.source_quote),
            request_slot_request=request_slot_request,
            request_slot_contract=request_slot_contract,
        )
        if resolved_slot is None or binding.slot_id != resolved_slot.slot_id:
            return RequestSlotDatumBindingApplicationDecisionV1(
                version="1", predicate="invalid_contract", criterion_index=target.criterion_index
            )
        trusted_bindings.append(
            TrustedRequestSlotDatumBindingV1(
                version="1",
                criterion_index=target.criterion_index,
                datum_field=target.datum_field,
                datum_value=target.datum_value,
                criterion_outcome_sha256=target.criterion_outcome_sha256,
                source_id=binding.source_id,
                source_quote=binding.source_quote,
                slot_id=binding.slot_id,
            )
        )

    accepted_payload = copy.deepcopy(original)
    accepted_criteria = accepted_payload["completion_criteria"]
    for trusted_binding in trusted_bindings:
        accepted_item = accepted_criteria[trusted_binding.criterion_index]
        accepted_item["request_slot_source_id"] = trusted_binding.source_id
        accepted_item["request_slot_source_quote"] = trusted_binding.source_quote
        accepted_item["request_slot_datum_binding"] = {
            "version": trusted_binding.version,
            "criterion_index": trusted_binding.criterion_index,
            "datum_field": trusted_binding.datum_field,
            "datum_value": trusted_binding.datum_value,
            "criterion_outcome_sha256": trusted_binding.criterion_outcome_sha256,
            "source_id": trusted_binding.source_id,
            "source_quote": trusted_binding.source_quote,
        }
    return RequestSlotDatumBindingApplicationDecisionV1(
        version="1",
        predicate="accepted",
        accepted_payload=accepted_payload,
        trusted_bindings=tuple(trusted_bindings),
    )


def _request_slot_claims_need_anchor_correction(
    raw_criteria: Any,
    *,
    request_slot_request: RequestSlotProducerInputV1,
) -> bool:
    return isinstance(raw_criteria, list) and any(
        isinstance(item, dict)
        and _item_claims_request_slot(item)
        and not _request_slot_anchor_is_admissible(
            item,
            request_slot_request=request_slot_request,
            criterion_index=criterion_index,
        )
        for criterion_index, item in enumerate(raw_criteria)
    )


def _accept_request_slot_anchor_correction(
    original: dict[str, Any],
    corrected: dict[str, Any],
    *,
    request_slot_request: RequestSlotProducerInputV1,
) -> RequestSlotAnchorCorrectionDecisionV1:
    original_criteria = original.get("completion_criteria")
    corrected_criteria = corrected.get("completion_criteria")
    if not isinstance(original_criteria, list) or not isinstance(corrected_criteria, list):
        return RequestSlotAnchorCorrectionDecisionV1(version="1", predicate="invalid_criteria_container")
    if len(original_criteria) != len(corrected_criteria):
        return RequestSlotAnchorCorrectionDecisionV1(version="1", predicate="criterion_count_changed")

    anchor_fields = {"request_slot_source_id", "request_slot_source_quote"}
    if _classifier_payload_semantics(original) != _classifier_payload_semantics(corrected):
        return RequestSlotAnchorCorrectionDecisionV1(version="1", predicate="payload_semantics_changed")
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
        return RequestSlotAnchorCorrectionDecisionV1(version="1", predicate="criteria_semantics_changed")

    accepted_criteria: list[dict[str, Any]] = []
    binding_absent = object()
    for criterion_index, (original_item, corrected_item) in enumerate(
        zip(original_criteria, corrected_criteria, strict=True)
    ):
        if not isinstance(original_item, dict) or not isinstance(corrected_item, dict):
            return RequestSlotAnchorCorrectionDecisionV1(
                version="1", predicate="criterion_not_object", criterion_index=criterion_index
            )
        if not _type_strict_json_equal(
            original_item.get("request_slot_datum_binding", binding_absent),
            corrected_item.get("request_slot_datum_binding", binding_absent),
        ):
            return RequestSlotAnchorCorrectionDecisionV1(
                version="1", predicate="criterion_semantics_changed", criterion_index=criterion_index
            )
        original_without_anchors = {key: value for key, value in original_item.items() if key not in anchor_fields}
        corrected_without_anchors = {key: value for key, value in corrected_item.items() if key not in anchor_fields}
        if _classifier_criterion_semantics(original_without_anchors) != _classifier_criterion_semantics(
            corrected_without_anchors
        ):
            return RequestSlotAnchorCorrectionDecisionV1(
                version="1", predicate="criterion_semantics_changed", criterion_index=criterion_index
            )
        accepted_item = dict(original_item)
        if _item_claims_request_slot(original_item):
            original_quote = original_item.get("request_slot_source_quote")
            corrected_anchor = _request_slot_anchor(corrected_item)
            if corrected_anchor is None:
                return RequestSlotAnchorCorrectionDecisionV1(
                    version="1", predicate="missing_corrected_anchor", criterion_index=criterion_index
                )
            corrected_source_id, corrected_quote = corrected_anchor
            if isinstance(original_quote, str) and original_quote:
                corrected_source_for_original_quote = {
                    **original_item,
                    "request_slot_source_id": corrected_source_id,
                }
                if not _request_slot_anchor_is_admissible(
                    corrected_source_for_original_quote,
                    request_slot_request=request_slot_request,
                    criterion_index=criterion_index,
                    allow_embedded_binding=True,
                ):
                    return RequestSlotAnchorCorrectionDecisionV1(
                        version="1", predicate="original_quote_not_admissible", criterion_index=criterion_index
                    )
                accepted_item["request_slot_source_id"] = corrected_source_id
            else:
                if not _item_claims_request_slot_fields(original_item):
                    return RequestSlotAnchorCorrectionDecisionV1(
                        version="1", predicate="missing_request_slot_fields", criterion_index=criterion_index
                    )
                corrected_anchor_for_original_datum = {
                    **original_item,
                    "request_slot_source_id": corrected_source_id,
                    "request_slot_source_quote": corrected_quote,
                }
                if not _request_slot_anchor_is_admissible(
                    corrected_anchor_for_original_datum,
                    request_slot_request=request_slot_request,
                    criterion_index=criterion_index,
                    allow_embedded_binding=True,
                ):
                    return RequestSlotAnchorCorrectionDecisionV1(
                        version="1", predicate="corrected_anchor_not_admissible", criterion_index=criterion_index
                    )
                accepted_item["request_slot_source_id"] = corrected_source_id
                accepted_item["request_slot_source_quote"] = corrected_quote
        elif any(corrected_item.get(field) != original_item.get(field) for field in anchor_fields):
            return RequestSlotAnchorCorrectionDecisionV1(
                version="1", predicate="unexpected_anchor_change", criterion_index=criterion_index
            )
        accepted_criteria.append(accepted_item)
    return RequestSlotAnchorCorrectionDecisionV1(
        version="1",
        predicate="accepted",
        accepted_payload={**original, "completion_criteria": accepted_criteria},
    )


def _redact_anchor_correction_capture_value(value: Any) -> Any:
    if isinstance(value, str):
        return redact_raw_secrets_for_prompt(value)
    if isinstance(value, dict):
        redacted: dict[Any, Any] = {}
        for key, item in value.items():
            if key == "raw_secret_evidence":
                redacted[key] = None if item is None else "[REDACTED_SECRET]"
            else:
                redacted[key] = _redact_anchor_correction_capture_value(item)
        return redacted
    if isinstance(value, list):
        return [_redact_anchor_correction_capture_value(item) for item in value]
    return value


def _anchor_correction_rejection_capture(
    original: dict[str, Any],
    corrected: dict[str, Any],
) -> dict[str, str] | None:
    try:
        original_redacted = _redact_anchor_correction_capture_value(redact_sensitive_fields(original))
        corrected_redacted = _redact_anchor_correction_capture_value(redact_sensitive_fields(corrected))
        original_json = json.dumps(original_redacted, ensure_ascii=True, separators=(",", ":"), sort_keys=True)
        corrected_json = json.dumps(corrected_redacted, ensure_ascii=True, separators=(",", ":"), sort_keys=True)
        pair_json = json.dumps(
            {"corrected": corrected_redacted, "original": original_redacted},
            ensure_ascii=True,
            separators=(",", ":"),
            sort_keys=True,
        )
    except (TypeError, ValueError, RecursionError):
        return None
    return {
        "original_payload_json": original_json,
        "corrected_payload_json": corrected_json,
        "original_sha256": hashlib.sha256(original_json.encode()).hexdigest(),
        "corrected_sha256": hashlib.sha256(corrected_json.encode()).hexdigest(),
        "pair_sha256": hashlib.sha256(pair_json.encode()).hexdigest(),
    }


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


def _top_level_output_key(output_path: str | None) -> str | None:
    if output_path is None or not output_path.startswith("output."):
        return None
    key = output_path.removeprefix("output.")
    return key if _CLASSIFICATION_OUTPUT_KEY_RE.fullmatch(key) else None


def _boolean_output_signal_count(criterion: CompletionCriterion) -> int:
    return sum(
        (
            isinstance(criterion.expected_output_value, bool),
            criterion.expected_output_shape == "goal_judgment_boolean",
            isinstance(criterion.expected_classification, bool),
        )
    )


def _has_boolean_output_signal(criterion: CompletionCriterion) -> bool:
    return _boolean_output_signal_count(criterion) > 0


def _shapeless_boolean_output_binding_key(criterion: CompletionCriterion) -> str | None:
    if _boolean_output_signal_count(criterion) != 1:
        return None
    bindings: list[str] = []
    if criterion.output_path is not None:
        output_key = _top_level_output_key(criterion.output_path)
        if output_key is None:
            return None
        bindings.append(output_key)
    if criterion.classification_output_key is not None:
        if _CLASSIFICATION_OUTPUT_KEY_RE.fullmatch(criterion.classification_output_key) is None:
            return None
        bindings.append(criterion.classification_output_key)
    return bindings[0] if len(bindings) == 1 else None


def is_neutral_reported_boolean_criterion(criterion: CompletionCriterion) -> bool:
    output_key = criterion.classification_output_key
    return (
        criterion.kind == "outcome"
        and criterion.output_path is None
        and criterion.expected_output_value is None
        and criterion.expected_classification is None
        and criterion.expected_output_shape == "goal_judgment_boolean"
        and criterion.requested_output_evidence_source == "independent_run_evidence"
        and output_key is not None
        and criterion.requested_output_floor_rekeyed is False
        and criterion.floor_rekeyed_from_path is None
        and criterion.request_slot_id is not None
        and _REQUEST_SLOT_ID_RE.fullmatch(criterion.request_slot_id) is not None
        and criterion.id == criterion.request_slot_id
        and _CLASSIFICATION_OUTPUT_KEY_RE.fullmatch(output_key) is not None
        and criterion.pinability == "shapeless_valid"
        and criterion.mint_disposition == "decidable"
        and criterion.mint_degrade is None
        and criterion.judgment_truth_condition is None
        and criterion.antecedent_family in {"unconditional", "blocker"}
    )


def _is_legacy_floor_marked_neutral_reported_boolean_criterion(criterion: CompletionCriterion) -> bool:
    if not (
        criterion.requested_output_floor_rekeyed
        and criterion.classification_output_key is not None
        and criterion.floor_rekeyed_from_path == f"output.{criterion.classification_output_key}"
    ):
        return False
    return is_neutral_reported_boolean_criterion(
        replace(
            criterion,
            requested_output_floor_rekeyed=False,
            floor_rekeyed_from_path=None,
        )
    )


def normalize_neutral_reported_boolean_criterion(criterion: CompletionCriterion) -> CompletionCriterion:
    claims_neutral_reported_boolean = criterion.kind == "outcome" and (
        criterion.classification_output_key is not None
        or (criterion.pinability == "shapeless_valid" and _has_boolean_output_signal(criterion))
    )
    if not claims_neutral_reported_boolean:
        return criterion
    if is_neutral_reported_boolean_criterion(criterion):
        return criterion
    if _is_legacy_floor_marked_neutral_reported_boolean_criterion(criterion):
        return replace(
            criterion,
            requested_output_floor_rekeyed=False,
            floor_rekeyed_from_path=None,
        )
    return replace(
        criterion,
        output_path=None,
        expected_output_value=None,
        expected_output_shape=None,
        requested_output_evidence_source="runtime_output",
        classification_output_key=None,
        expected_classification=None,
        judgment_truth_condition=None,
        antecedent_family="undecidable",
        requested_output_floor_rekeyed=False,
        floor_rekeyed_from_path=None,
        mint_disposition="degraded",
        mint_degrade="undecidable_judgment",
    )


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
        antecedent_family="undecidable",
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


def _omitted_request_slot_criterion(
    *,
    slot: CanonicalRequestSlotV1,
    source_quote: str,
    trusted_datum_bindings: tuple[TrustedRequestSlotDatumBindingV1, ...],
) -> CompletionCriterion:
    criterion = CompletionCriterion(id="c0", outcome=source_quote)
    if len(trusted_datum_bindings) != 1:
        return criterion
    binding = trusted_datum_bindings[0]
    classification_output_key = _coerce_classification_output_key(binding.datum_value)
    if not (
        binding.slot_id == slot.slot_id
        and binding.source_id == slot.source_id
        and binding.source_quote == source_quote
        and binding.datum_field == "classification_output_key"
        and classification_output_key == binding.datum_value
    ):
        return criterion
    return replace(
        criterion,
        kind="validation_classification",
        expected_output_shape="goal_judgment_boolean",
        requested_output_evidence_source="independent_run_evidence",
        classification_output_key=classification_output_key,
        mint_disposition="pending",
    )


def _canonical_mint_path(path: str) -> str:
    return path.strip().removeprefix("output.")


def _minted_path_has_typed_child(path: str, sibling_output_paths: frozenset[str]) -> bool:
    """True when another requested-output path is nested under ``path`` (``path.x`` or
    ``path[].x``), i.e. ``path`` is a collection parent whose contract is carried by its
    typed children -- so it should floor-rekey, not mint a loose value_present criterion.
    Paths are compared canonically so a child written without the ``output.`` prefix still
    suppresses the mint."""
    parent = _canonical_mint_path(path)
    return any(
        (child := _canonical_mint_path(sibling)) != parent
        and (child.startswith(f"{parent}.") or child.startswith(f"{parent}["))
        for sibling in sibling_output_paths
    )


def _bind_criterion_to_request_slot(
    criterion: CompletionCriterion,
    *,
    slot: CanonicalRequestSlotV1,
    source_quote: str,
    allow_canonical_path_fallback: bool = True,
    sibling_output_paths: frozenset[str] = frozenset(),
) -> CompletionCriterion:
    pinability = cast(Pinability, slot.pinability.value)
    lexical_interrogative_key = _interrogative_slot_classification_output_key(
        source_quote,
        preserve_path_subject=True,
    )
    if (
        pinability == "shapeless_valid"
        and slot.plane.value == "run"
        and lexical_interrogative_key is not None
        and criterion.classification_output_key is not None
        and lexical_interrogative_key == f"path_{criterion.classification_output_key}"
    ):
        criterion = replace(criterion, classification_output_key=lexical_interrogative_key)
    has_exact_requirement = _request_slot_has_exact_requirement(criterion)
    antecedent_family = cast(AntecedentFamily, slot.antecedent_family.value)
    degraded = (
        pinability == "unpinnable"
        or (pinability == "pinned" and not has_exact_requirement)
        or antecedent_family == "undecidable"
    )
    neutral_boolean_candidate = (
        slot.plane.value == "run"
        and criterion.level == "run"
        and pinability == "shapeless_valid"
        and lexical_interrogative_key is not None
        and not degraded
    )
    neutral_boolean_key = _shapeless_boolean_output_binding_key(criterion) if neutral_boolean_candidate else None
    malformed_shapeless_boolean = (
        neutral_boolean_candidate and _has_boolean_output_signal(criterion) and neutral_boolean_key is None
    )
    assertive_shapeless_boolean = (
        slot.plane.value == "run"
        and criterion.level == "run"
        and pinability == "shapeless_valid"
        and lexical_interrogative_key is None
        and _has_boolean_output_signal(criterion)
    )
    degraded = degraded or malformed_shapeless_boolean or assertive_shapeless_boolean

    minted_output_path = criterion.output_path or (
        f"output.{criterion.classification_output_key}" if criterion.classification_output_key is not None else None
    )
    # A single named non-boolean run-plane value keeps its computed output_path with a
    # presence-only shape so the grounding gate can check it, without claiming an exact value.
    minted_requested_output = (
        pinability == "shapeless_valid"
        and slot.plane.value == "run"
        and minted_output_path is not None
        and not _minted_path_has_typed_child(minted_output_path, sibling_output_paths)
        and not _has_boolean_output_signal(criterion)
        and not neutral_boolean_candidate
        and not malformed_shapeless_boolean
        and not assertive_shapeless_boolean
        and antecedent_family in {"unconditional", "blocker"}
    )

    rekeyed = degraded or (pinability == "shapeless_valid" and not minted_requested_output)

    expected_output_value = criterion.expected_output_value
    expected_output_shape = criterion.expected_output_shape
    requested_output_evidence_source = criterion.requested_output_evidence_source
    expected_classification = criterion.expected_classification
    kind = criterion.kind
    classification_output_key = criterion.classification_output_key
    judgment_truth_condition = criterion.judgment_truth_condition
    original_output_path = (
        criterion.output_path
        or (f"output.{classification_output_key}" if classification_output_key is not None else None)
        or (slot.canonical_path if allow_canonical_path_fallback else None)
    )
    if rekeyed and neutral_boolean_key is None and original_output_path is None:
        degraded = True
        antecedent_family = "undecidable"
    if pinability != "pinned" or degraded:
        expected_output_value = None
        expected_output_shape = None
        expected_classification = None
        judgment_truth_condition = None
    if minted_requested_output:
        expected_output_shape = "value_present"
    if malformed_shapeless_boolean:
        requested_output_evidence_source = "runtime_output"
        classification_output_key = None
        antecedent_family = "undecidable"
    if neutral_boolean_key is not None:
        kind = "outcome"
        outcome = f"The run reports whether {neutral_boolean_key.replace('_', ' ')}."
        expected_output_shape = "goal_judgment_boolean"
        requested_output_evidence_source = "independent_run_evidence"
        classification_output_key = neutral_boolean_key
    else:
        outcome = criterion.outcome or source_quote
    if rekeyed and kind == "validation_classification":
        kind = "outcome"
        classification_output_key = None
    output_path = None if rekeyed else criterion.output_path
    if minted_requested_output:
        kind = "outcome"
        classification_output_key = None
        if output_path is None:
            output_path = minted_output_path
    if neutral_boolean_key is not None:
        original_output_path = f"output.{neutral_boolean_key}"
    requested_output_floor_rekeyed = rekeyed and neutral_boolean_key is None and original_output_path is not None

    pending = pinability == "pinned" and (
        isinstance(expected_output_value, bool)
        or expected_output_shape == "goal_judgment_boolean"
        or isinstance(expected_classification, bool)
        or judgment_truth_condition is not None
    )
    if rekeyed:
        LOG.info(
            "copilot_request_slot_criterion_rekeyed",
            slot_id=slot.slot_id,
            canonical_path=slot.canonical_path,
            pinability=pinability,
            antecedent_family=antecedent_family,
            has_exact_requirement=has_exact_requirement,
            degraded=degraded,
            rekeyed_reason="degraded" if degraded else "shapeless_valid",
            kind=kind,
            original_output_path=original_output_path,
            requested_output_evidence_source=criterion.requested_output_evidence_source,
            outcome=(criterion.outcome or source_quote or "")[:80],
        )
    if minted_requested_output:
        LOG.info(
            "copilot_request_slot_criterion_minted_requested_output",
            slot_id=slot.slot_id,
            output_path=output_path,
        )
    return replace(
        criterion,
        id=slot.slot_id,
        outcome=outcome,
        level=cast(CriterionLevel, slot.plane.value),
        output_path=output_path,
        expected_output_value=expected_output_value,
        expected_output_shape=expected_output_shape,
        requested_output_evidence_source=requested_output_evidence_source,
        kind=kind,
        classification_output_key=classification_output_key,
        expected_classification=expected_classification,
        judgment_truth_condition=judgment_truth_condition,
        antecedent_family=antecedent_family,
        request_slot_id=slot.slot_id,
        pinability=pinability,
        mint_disposition="degraded" if degraded else ("pending" if pending else "decidable"),
        mint_degrade="undecidable_judgment" if degraded else None,
        requested_output_floor_rekeyed=requested_output_floor_rekeyed,
        floor_rekeyed_from_path=original_output_path if requested_output_floor_rekeyed else None,
    )


def _parse_fresh_request_slot_criteria(
    raw: Any,
    *,
    request_slot_request: RequestSlotProducerInputV1,
    request_slot_contract: RequestSlotContractV1,
    trusted_datum_bindings: tuple[TrustedRequestSlotDatumBindingV1, ...] = (),
) -> list[CompletionCriterion]:
    if request_slot_contract.version != "1" or not isinstance(raw, list):
        return []
    criterion_index_by_item_id = {id(item): index for index, item in enumerate(raw) if isinstance(item, dict)}
    trusted_bindings_by_index: dict[int, list[TrustedRequestSlotDatumBindingV1]] = {}
    trusted_bindings_by_slot_id: dict[str, list[TrustedRequestSlotDatumBindingV1]] = {}
    for binding in trusted_datum_bindings:
        trusted_bindings_by_index.setdefault(binding.criterion_index, []).append(binding)
        trusted_bindings_by_slot_id.setdefault(binding.slot_id, []).append(binding)
    bound_by_slot_id: dict[str, CompletionCriterion] = {}
    non_slot_criteria: list[CompletionCriterion] = []
    entries = list(_parse_completion_criterion_entries(raw))
    sibling_output_paths = frozenset(criterion.output_path for _item, criterion in entries if criterion.output_path)
    for item, criterion in entries:
        criterion_index = criterion_index_by_item_id[id(item)]
        anchor = _request_slot_anchor(item)
        slot = (
            _request_slot_for_anchor(
                anchor,
                request_slot_request=request_slot_request,
                request_slot_contract=request_slot_contract,
            )
            if _request_slot_anchor_is_admissible(
                item,
                request_slot_request=request_slot_request,
                criterion_index=criterion_index,
                trusted_binding=(
                    trusted_bindings_by_index[criterion_index][0]
                    if len(trusted_bindings_by_index.get(criterion_index, ())) == 1
                    else None
                ),
                allow_embedded_binding=False,
            )
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
            sibling_output_paths=sibling_output_paths,
        )

    for slot in request_slot_contract.slots:
        if slot.slot_id in bound_by_slot_id:
            continue
        source_quote = request_slot_source_text(request_slot_request, slot)
        slot_bindings = tuple(trusted_bindings_by_slot_id.get(slot.slot_id, ()))
        bound_by_slot_id[slot.slot_id] = _bind_criterion_to_request_slot(
            _omitted_request_slot_criterion(
                slot=slot,
                source_quote=source_quote,
                trusted_datum_bindings=slot_bindings,
            ),
            slot=slot,
            source_quote=source_quote,
            allow_canonical_path_fallback=False,
            sibling_output_paths=sibling_output_paths,
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
    trusted_datum_bindings: tuple[TrustedRequestSlotDatumBindingV1, ...] = (),
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
            trusted_datum_bindings=trusted_datum_bindings,
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
        if expected_output_shape == "value_present":
            # value_present is minted by the policy only (see _bind_criterion_to_request_slot); a
            # classifier-supplied one on a pinned slot would be credited by presence and bypass the
            # exact-value bar. Drop it here so only the mint can set it.
            expected_output_shape = None
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
        elif (
            output_path is not None
            and expected_output_value is None
            and deliverable_kind == "registered_download"
            and kind != "validation_classification"
            and output_path in REGISTERED_DOWNLOAD_REQUESTED_OUTPUT_PATHS
        ):
            requested_output_path_mint_source = "classifier_declared"
            if emit_mint_events:
                LOG.info(
                    "copilot_registered_download_requested_output_minted",
                    output_path=output_path,
                    requested_output_path_mint_source=requested_output_path_mint_source,
                    criterion_id=f"c{len(entries)}",
                )
        method_mandated = bool(item.get("method_mandated"))
        level_raw = item.get("level")
        level = (
            cast(CriterionLevel, level_raw) if isinstance(level_raw, str) and level_raw in _CRITERION_LEVELS else "run"
        )
        deliverable_confirmation_criterion_id = _normalize_deliverable_confirmation_criterion_id(
            item.get("deliverable_confirmation_criterion_id")
        )
        if (
            level != "run"
            or kind != "outcome"
            or output_path is not None
            or deliverable_kind is not None
            or method_mandated
        ):
            deliverable_confirmation_criterion_id = None
        key = (
            contingent_on or "",
            contingent_antecedent_output_path or "",
            output_path or classification_output_key or normalized_criterion_outcome_key(outcome),
            deliverable_kind or "",
            deliverable_confirmation_criterion_id or "",
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
        entries.append(
            (
                item,
                CompletionCriterion(
                    id=f"c{len(entries)}",
                    outcome=outcome,
                    contingent_on=contingent_on,
                    contingent_antecedent_output_path=contingent_antecedent_output_path,
                    deliverable_kind=deliverable_kind,
                    deliverable_confirmation_criterion_id=deliverable_confirmation_criterion_id,
                    declared_deliverable_kind=deliverable_kind,
                    implicit=bool(item.get("implicit")),
                    method_mandated=method_mandated,
                    level=level,
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


def _normalize_deliverable_confirmation_criterion_id(raw: object) -> str | None:
    return REGISTERED_DOWNLOAD_COMPLETION_CRITERION_ID if raw == REGISTERED_DOWNLOAD_COMPLETION_CRITERION_ID else None


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
    quantifier_index = next(
        (
            index
            for index, word in enumerate(normalized_words)
            if word in _OUTPUT_QUANTIFIER_HEAD_WORDS
            and index + 1 < len(normalized_words)
            and normalized_words[index + 1] == "of"
        ),
        None,
    )
    if quantifier_index is not None:
        subject_words: list[str] = []
        for word, normalized_word in zip(words[quantifier_index + 2 :], normalized_words[quantifier_index + 2 :]):
            if normalized_word in _OUTPUT_PHRASE_BOUNDARY_WORDS:
                break
            if normalized_word in _OUTPUT_GENERIC_WORDS:
                continue
            subject_words.append(word)
        if subject_words:
            return " ".join(subject_words)
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
    if expected_shape == "value_present":
        # A minted single-value extraction is decidable by presence, not an undecidable judgment.
        return "decidable", None
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
    if criterion.deliverable_confirmation_criterion_id is not None:
        return 0
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
                requested_output_label=field_label,
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


def _detected_output_candidates(user_message: str, aliases: dict[str, str] | None = None) -> tuple[str, ...]:
    """Requested-output fields the deterministic scan finds, for the slot producer's bind-or-refute
    obligation. Verbatim message substrings only — the detector composes phrases across spans, and
    an obligation the producer cannot quote could only ever be refuted."""
    message_fold = user_message.casefold()
    seen: set[str] = set()
    candidates: list[str] = []
    for field_name in _requested_output_fields(user_message, _normalize_requested_output_aliases(aliases)):
        cleaned = field_name.strip()
        fold = cleaned.casefold()
        if not cleaned or len(cleaned) > 64 or fold in seen or fold not in message_fold:
            continue
        seen.add(fold)
        candidates.append(cleaned)
        if len(candidates) == 8:
            break
    return tuple(candidates)


def _apply_requested_output_labels(
    policy: RequestPolicy, user_message: str, aliases: dict[str, str] | None = None
) -> None:
    """Stamp the short requested-output noun onto classifier-authored criteria by ``output_path``,
    which the canonical mint already carries but classifier-authored criteria only name in prose."""

    schema_aliases = _normalize_requested_output_aliases(
        schema_output_path_aliases_from_criteria(policy.completion_criteria)
    )
    config_aliases = _normalize_requested_output_aliases(aliases)
    label_by_output_path: dict[str, str] = {}
    label_by_field_name: dict[str, str] = {}
    for field_name in _requested_output_fields(user_message, {**config_aliases, **schema_aliases}):
        output_path = _requested_output_path_for_detected_field(field_name, schema_aliases, config_aliases)
        label = _requested_output_field_label(field_name, output_path)
        label_by_output_path.setdefault(output_path, label)
        label_by_field_name.setdefault(field_name, label)
    if not label_by_output_path:
        return
    policy.completion_criteria = [
        replace(criterion, requested_output_label=label_by_output_path[criterion.output_path])
        if criterion.requested_output_label is None
        and criterion.output_path is not None
        and criterion.output_path in label_by_output_path
        else criterion
        for criterion in policy.completion_criteria
    ]
    # The path join misses whenever the classifier names the field differently than the detector
    # derives it ("visitor_count" vs "visitors"); the criterion's own outcome text still names the
    # field, so a uniquely covering criterion takes the label rather than falling back to prose.
    for field_name, label in label_by_field_name.items():
        covering = [
            index
            for index, criterion in enumerate(policy.completion_criteria)
            if criterion.requested_output_label is None
            and criterion.output_path is not None
            and _criterion_text_covers_requested_output(criterion, field_name)
        ]
        if len(covering) != 1:
            continue
        index = covering[0]
        policy.completion_criteria = [
            replace(criterion, requested_output_label=label) if position == index else criterion
            for position, criterion in enumerate(policy.completion_criteria)
        ]


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
        if criterion.deliverable_confirmation_criterion_id:
            item["deliverable_confirmation_criterion_id"] = criterion.deliverable_confirmation_criterion_id
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
    _apply_requested_output_labels(policy, user_message, requested_output_path_aliases)
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


def _account_state_atom(value: str) -> str:
    """Org-controlled text goes into a delimiter-structured prompt block, so a quote or a backtick
    left intact would forge a fact boundary or a credential id, and any of the characters str.split
    treats as whitespace — U+2028, U+0085 and friends, not just CR and LF — would forge a line."""
    return " ".join(value.replace('"', " ").replace("`", " ").split())


def credential_candidate_label(credential: Credential) -> str:
    label = f'"{_account_state_atom(credential.name)}" (`{credential.credential_id}`)'
    facts: list[str] = []
    if credential.tested_url:
        facts.append(f'tested_url: "{_account_state_atom(credential.tested_url)}"')
    if credential.totp_type != TotpType.NONE:
        facts.append(f"totp_type: {credential.totp_type}")
    if not facts:
        return label
    return f"{label} - {'; '.join(facts)}"


def _ground_user_provided_sites(
    policy: RequestPolicy,
    user_message: str,
    full_chat_history: Sequence[WorkflowCopilotChatHistoryMessage],
) -> None:
    """Record every site the user themselves gave this chat, so the fill seam can release a credential
    onto one of them and never onto a site only a model produced.

    Linking a later "log into pathfold" back to a URL from an earlier turn is the agent's job, not
    this function's: it reads the whole conversation. What is recorded here is only the deterministic
    part — the origins the user actually wrote.
    """
    # USER only, never TURN_OPENER_SENDERS: this set releases credentials, so a site must have
    # been written by the person, not by a row the product authored on their behalf.
    user_texts = [
        message.content
        for message in full_chat_history
        if message.sender == WorkflowCopilotChatSender.USER and message.content
    ]
    user_texts.append(user_message or "")
    seen_origins: set[str] = set()
    urls: list[str] = []
    sources: dict[str, int] = {}
    for index, text in enumerate(user_texts, start=1):
        for candidate in URL_CANDIDATE_RE.findall(text):
            cleaned = candidate.rstrip(".,;:!?")
            parts = _url_parts(cleaned)
            if parts is None or parts[2] in seen_origins:
                continue
            seen_origins.add(parts[2])
            urls.append(cleaned)
            sources[cleaned] = index
    policy.user_provided_site_urls = urls
    policy.user_site_url_sources = sources


def _prior_approved_connection_ids(global_llm_context: str) -> set[str]:
    structured = StructuredContext.from_json_str(global_llm_context)
    return {
        record.credential_id for record in structured.approved_connections if record.credential_id.startswith("goac_")
    }


def _prior_approved_credential_ids(global_llm_context: str) -> set[str]:
    structured = StructuredContext.from_json_str(global_llm_context)
    return {
        record.credential_id for record in structured.approved_credentials if record.credential_id.startswith("cred_")
    }


async def _seed_prior_approved_credentials(
    policy: RequestPolicy,
    *,
    organization_id: str,
    global_llm_context: str,
) -> None:
    approved_ids = _prior_approved_credential_ids(global_llm_context)
    # An approval minted from a carry recorded the page that vouched for the credential; restoring it
    # keeps the fill pinned there instead of falling through to any site named in the conversation.
    for record in StructuredContext.from_json_str(global_llm_context).approved_credentials:
        if record.credential_id in approved_ids and record.admitted_url:
            policy.live_page_admitted_urls.setdefault(record.credential_id, record.admitted_url)
            policy.seeded_proposal_credential_ids.add(record.credential_id)
    missing_ids = sorted(approved_ids - {credential.credential_id for credential in policy.resolved_credentials})
    if not missing_ids:
        return
    credentials = await app.DATABASE.credentials.get_credentials_by_ids(
        missing_ids,
        organization_id=organization_id,
    )
    policy.resolved_credentials = _deduplicate_credentials(
        [
            *policy.resolved_credentials,
            *(credential for credential in credentials if credential.credential_id in approved_ids),
        ]
    )


def _proposed_credential_seed(global_llm_context: str) -> ProposedCredential | None:
    seed = StructuredContext.from_json_str(global_llm_context).proposed_credential
    if seed is None or not seed.credential_id.startswith("cred_") or not seed.admitted_url:
        return None
    return seed


async def _seed_proposed_credential(
    policy: RequestPolicy,
    *,
    organization_id: str,
    global_llm_context: str,
) -> None:
    seed = _proposed_credential_seed(global_llm_context)
    if seed is None:
        return
    credentials = await app.DATABASE.credentials.get_credentials_by_ids(
        [seed.credential_id],
        organization_id=organization_id,
    )
    credential = next((item for item in credentials if item.credential_id == seed.credential_id), None)
    if credential is None:
        return
    policy.resolved_credentials = _deduplicate_credentials([*policy.resolved_credentials, credential])
    # Restoring the recorded origin keeps the hydrated id page-vouched, so the turn-end recorder
    # still skips it rather than promoting a one-turn carry to durable approval.
    policy.live_page_admitted_urls.setdefault(seed.credential_id, seed.admitted_url)
    policy.seeded_proposal_credential_ids.add(seed.credential_id)
    # The server bound this and the user did not name it, so it enters through the auto-bound record:
    # a write to current_turn_named_credential_ids would refuse the user's own later answer.
    if seed.credential_id not in {item.credential_id for item in policy.auto_bound_credentials}:
        policy.auto_bound_credentials.append(credential)
    LOG.info(
        "copilot_credential_proposal_hydrated",
        organization_id=organization_id,
        credential_id=seed.credential_id,
        origin_arm=seed.origin_arm,
        admitted_url=loggable_origin(seed.admitted_url),
    )


@dataclass(frozen=True)
class LiveCredentialAdmission:
    admitted: bool
    steer: str | None = None
    page_url: str | None = None


def _live_page_log_allowed(policy: RequestPolicy, page_url: str, seam: LiveCredentialSeam, outcome: str = "") -> bool:
    """Whether this seam may emit the admission fingerprint for this page.

    The observation seam runs on every page the scout reaches, so without a per-URL guard a
    single turn's no-match spam would drown the admission signal it shares a name with. The key
    carries the outcome so a declining observation cannot eat the token before the grant that
    follows it on the same URL — the grant is the signal the guard exists to protect.
    """
    if seam != "page_observation":
        return True
    key = f"{page_url}::{outcome}"
    if key in policy.live_page_logged_urls:
        return False
    policy.live_page_logged_urls.add(key)
    return True


def live_page_credentials_admissible(policy: RequestPolicy) -> bool:
    """Whether this turn may bind a saved credential from a live page at all.

    Callers that must pay for evidence — a browser read, a credential load — check this first so a
    turn that could never admit does not buy the evidence to decide it.
    """
    return not policy.raw_secret_detected


def _live_page_url_abstains(
    policy: RequestPolicy,
    *,
    organization_id: str,
    page_url: str,
    seam: LiveCredentialSeam,
    credential_id: str | None = None,
) -> bool:
    """Whether the seam was handed something that is not a page, and must make no claim about it.

    `no_match` is a factual statement about the org's saved credentials. From an unresolvable input
    the seam has looked at no page at all, so it is not entitled to make one.
    """
    if not page_url or is_resolved_page_url(page_url):
        return False
    if _live_page_log_allowed(policy, page_url, seam, "abstain"):
        LOG.info(
            "copilot credential live-page admission",
            outcome="abstain",
            seam=seam,
            organization_id=organization_id,
            credential_id=credential_id,
            page_url=unresolved_page_url_for_log(page_url),
            tier=None,
            candidate_credential_ids=[],
        )
    return True


async def _resolve_live_page_credentials(
    policy: RequestPolicy,
    *,
    organization_id: str,
    page_url: str,
    load_org_credentials: Callable[[], Awaitable[list[Credential]]] | None,
    seam: LiveCredentialSeam,
    credential_id: str | None = None,
) -> CredentialResolution | None:
    """Ask the live page which saved credentials it vouches for, or None when a precondition declines."""
    if not live_page_credentials_admissible(policy) or not page_url:
        return None
    return resolve_by_url(
        await (load_org_credentials() if load_org_credentials else _load_credentials(organization_id)),
        [page_url],
        # The scout reached this page, so its own path is the evidence; a bare host match would let
        # any other page on that host — a profile, an upload, a user-content route — claim the login.
        tiers=("url_exact", "url_path"),
        password_only=True,
    )


def _record_live_page_admission(policy: RequestPolicy, candidates: Sequence[Credential], page_url: str) -> None:
    # Only a credential this page is what granted gets the page-scoped stamp. One the turn already
    # resolved (the user named it) keeps its unstamped standing: record_approved_credentials_in_global_llm_context
    # skips stamped ids, so stamping it here would strip its durable cross-turn approval.
    already_resolved = {credential.credential_id for credential in policy.resolved_credentials}
    policy.resolved_credentials = _deduplicate_credentials(list(policy.resolved_credentials) + list(candidates))
    for candidate in candidates:
        if candidate.credential_id not in already_resolved:
            policy.live_page_admitted_urls[candidate.credential_id] = page_url
            policy.auto_bound_credentials.append(candidate)


async def resolve_credential_for_live_page(
    policy: RequestPolicy,
    *,
    organization_id: str,
    page_url: str,
    load_org_credentials: Callable[[], Awaitable[list[Credential]]] | None = None,
) -> LivePageResolutionRecord:
    """Grant run authority for the sole saved credential the observed page vouches for.

    Same discriminator and same records as the fill seam; the difference is only that no
    credential was requested, so a sole match is the answer rather than something to check against.
    """
    if _live_page_url_abstains(policy, organization_id=organization_id, page_url=page_url, seam="page_observation"):
        return LivePageResolutionRecord(verdict="abstain", page_url=page_url)
    resolution = await _resolve_live_page_credentials(
        policy,
        organization_id=organization_id,
        page_url=page_url,
        load_org_credentials=load_org_credentials,
        seam="page_observation",
    )
    if resolution is None:
        record = LivePageResolutionRecord(verdict="declined", page_url=page_url)
    else:
        record = LivePageResolutionRecord(
            verdict="no_match" if resolution.verdict == "unresolved" else resolution.verdict,
            tier=resolution.tier,
            candidates=tuple(resolution.candidates),
            page_url=page_url,
        )
    if record.verdict == "resolved":
        _record_live_page_admission(policy, record.candidates, page_url)
    if record.verdict == "ambiguous":
        policy.discovered_credentials = _deduplicate_credentials(
            list(policy.discovered_credentials) + list(record.candidates)
        )
    # A later page that matches nothing must not erase the login page's answer, which the
    # blocked-output renderer still needs at the end of the turn. An ambiguous verdict also outranks a
    # later resolved one from a different page: the ambiguity is the thing the user has to settle, and
    # a same-page verdict is a genuine update rather than an unrelated overwrite.
    stored = policy.live_page_resolution
    if record.verdict in {"resolved", "ambiguous"} and (
        stored is None
        or stored.verdict != "ambiguous"
        or stored.page_url == record.page_url
        or record.verdict == "ambiguous"
    ):
        policy.live_page_resolution = record
    observation_outcome = "admitted" if record.verdict == "resolved" else record.verdict
    if _live_page_log_allowed(policy, page_url, "page_observation", observation_outcome):
        LOG.info(
            "copilot credential live-page admission",
            outcome=observation_outcome,
            seam="page_observation",
            organization_id=organization_id,
            page_url=loggable_origin(page_url),
            tier=record.tier,
            candidate_credential_ids=[candidate.credential_id for candidate in record.candidates],
        )
    return record


async def admit_credential_for_live_page(
    policy: RequestPolicy,
    *,
    organization_id: str,
    credential_id: str,
    page_url: str,
    load_org_credentials: Callable[[], Awaitable[list[Credential]]] | None = None,
) -> LiveCredentialAdmission:
    """Grant run authority for the credential whose saved login URL matches the page the scout reached.

    The discriminator is that server-computed match, never the model's claim: a credential the page
    does not vouch for is refused, and so is one saved without a login URL, which is evidence about
    no page at all.
    """
    if not credential_id:
        return LiveCredentialAdmission(False)
    if _live_page_url_abstains(
        policy,
        organization_id=organization_id,
        page_url=page_url,
        seam="fill",
        credential_id=credential_id,
    ):
        return LiveCredentialAdmission(False)
    resolution = await _resolve_live_page_credentials(
        policy,
        organization_id=organization_id,
        page_url=page_url,
        load_org_credentials=load_org_credentials,
        seam="fill",
        credential_id=credential_id,
    )
    if resolution is None:
        return LiveCredentialAdmission(False)
    # Being *one of* several page matches is not authority; only a sole match the page
    # vouches for is, so an ambiguous set still asks even when it contains the requested id.
    admitted = resolution.verdict == "resolved" and resolution.contains(credential_id)
    LOG.info(
        "copilot credential live-page admission",
        outcome="admitted"
        if admitted
        else ("wrong_credential" if resolution.verdict == "resolved" else resolution.verdict),
        seam="fill",
        organization_id=organization_id,
        credential_id=credential_id,
        page_url=loggable_origin(page_url),
        tier=resolution.tier,
        candidate_credential_ids=[candidate.credential_id for candidate in resolution.candidates],
    )
    if admitted:
        _record_live_page_admission(policy, resolution.candidates, page_url)
        return LiveCredentialAdmission(True, page_url=page_url)

    if resolution.verdict == "ambiguous":
        policy.discovered_credentials = _deduplicate_credentials(
            list(policy.discovered_credentials) + list(resolution.candidates)
        )
        return LiveCredentialAdmission(
            False,
            steer=f"{_AMBIGUOUS_URL_CREDENTIAL_QUESTION} Ask the user which one to use, then fill it.",
        )
    if resolution.verdict == "resolved":
        return LiveCredentialAdmission(
            False,
            steer=(
                f"`{credential_id}` is not the saved credential for this login page. Ask the user which "
                "saved credential to use here."
            ),
        )
    return LiveCredentialAdmission(False)


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


async def _build_request_policy_bootstrap(
    *,
    user_message: str,
    workflow_yaml: str,
    chat_history: list[WorkflowCopilotChatHistoryMessage],
    global_llm_context: str,
    organization_id: str,
    prior_user_messages: Sequence[WorkflowCopilotChatHistoryMessage] = (),
    persisted_workflow_yaml: str | None = None,
    selected_connected_account_id: str | None = None,
    _preclassified_policy: RequestPolicy | None = None,
) -> RequestPolicy:
    """Build the deterministic request safety record without model-backed policy inference."""
    credential_ids = _credential_ids(user_message)
    raw_secret_present = _raw_secret_detected(user_message)
    policy = _preclassified_policy or RequestPolicy(
        authoring_intent="author_now",
        credential_input_kind=("raw_secret" if raw_secret_present else "credential_id" if credential_ids else "none"),
        credential_refs=credential_ids,
        raw_secret_detected=raw_secret_present,
        raw_secret_handling="redacted_draft" if raw_secret_present else "none",
        classifier_status="not_run",
        completion_contract_status="absent",
        canonical_user_message=(redact_raw_secrets_for_prompt(user_message) if raw_secret_present else user_message),
    )
    policy.persisted_workflow_credential_ids = sorted(workflow_credential_ids(persisted_workflow_yaml or ""))
    policy.existing_workflow_credential_ids = sorted(workflow_credential_ids(workflow_yaml))
    policy.existing_workflow_credential_origins = {
        credential_id: sorted(origins) for credential_id, origins in workflow_credential_origins(workflow_yaml).items()
    }
    google_connection_candidates = {
        credential_id for credential_id in policy.persisted_workflow_credential_ids if credential_id.startswith("goac_")
    }
    google_connection_candidates |= _prior_approved_connection_ids(global_llm_context)
    policy.selected_connected_account_id = None
    if google_connection_candidates or selected_connected_account_id is not None:
        try:
            active_connections = await google_oauth_service.get_credentials_for_org(organization_id)
        except Exception:
            LOG.warning(
                "request-policy Google connection authority lookup failed",
                organization_id=organization_id,
                exc_info=True,
            )
        else:
            active_ids = {connection.id for connection in active_connections}
            # Picker metadata can refresh after OAuth, independently of the previous turn.
            # Validate the explicit selection before it becomes durable run authority.
            selected = next(
                (
                    connection
                    for connection in active_connections
                    if connection.id == selected_connected_account_id
                    and connection.organization_id == organization_id
                    and connection.state == STATE_ACTIVE
                    and google_oauth_service.GOOGLE_SHEETS_DATA_SCOPE in connection.scopes_granted
                ),
                None,
            )
            if selected is not None:
                policy.selected_connected_account_id = selected.id
                google_connection_candidates.add(selected.id)
            policy.run_approved_google_connection_ids = sorted(google_connection_candidates & active_ids)
    _ground_user_provided_sites(policy, user_message, prior_user_messages or chat_history)
    try:
        await _seed_prior_approved_credentials(
            policy,
            organization_id=organization_id,
            global_llm_context=global_llm_context,
        )
    except Exception:
        LOG.warning(
            "request-policy prior approved credential seeding failed",
            organization_id=organization_id,
            exc_info=True,
        )
    try:
        await _seed_proposed_credential(
            policy,
            organization_id=organization_id,
            global_llm_context=global_llm_context,
        )
    except Exception:
        LOG.warning(
            "request-policy proposed credential seeding failed",
            organization_id=organization_id,
            exc_info=True,
        )

    # The literal is already absent from server-owned provenance. RequestPolicy keeps
    # the safety-approved redacted draft update-only and prevents a browser run.
    if policy.raw_secret_detected:
        policy.allow_run_blocks = False
        redacted_draft_candidate = policy.raw_secret_handling == "redacted_draft"
        policy.allow_missing_credentials_in_draft = redacted_draft_candidate
        policy.credential_draft_deferred_explicitly = redacted_draft_candidate

    if _workflow_credential_inputs_unbound(workflow_yaml):
        policy.clarification_reason = "workflow_credential_inputs_unbound"
        policy.allow_run_blocks = False
        policy.allow_missing_credentials_in_draft = True

    trace_data = policy.to_trace_data()
    with copilot_span("request_policy", data=trace_data):
        LOG.info("request-policy deterministic bootstrap", **trace_data)
    return policy


async def build_request_policy(
    *,
    user_message: str,
    workflow_yaml: str,
    chat_history: list[WorkflowCopilotChatHistoryMessage],
    global_llm_context: str,
    organization_id: str,
    handler: LLMAPIHandler | None,
    active_criteria: list[CompletionCriterion] | None = None,
    config: CopilotConfig | None = None,
    prior_user_messages: Sequence[WorkflowCopilotChatHistoryMessage] = (),
) -> RequestPolicy:
    """Compatibility wrapper for offline probes that still call the retired builder shape.

    Interactive production uses ``build_request_policy_trust_floor``. Keep this wrapper only until
    the remaining recorded-policy probes migrate; the deterministic bootstrap has no model/config
    dependency and cannot revive the retired classifier.
    """
    del handler, active_criteria, config
    return await _build_request_policy_bootstrap(
        user_message=user_message,
        workflow_yaml=workflow_yaml,
        chat_history=chat_history,
        global_llm_context=global_llm_context,
        organization_id=organization_id,
        prior_user_messages=prior_user_messages,
    )


async def build_request_policy_trust_floor(
    *,
    user_message: str,
    workflow_yaml: str,
    chat_history: list[WorkflowCopilotChatHistoryMessage],
    global_llm_context: str,
    organization_id: str,
    handler: LLMAPIHandler | None,
    config: CopilotConfig | None = None,
    prior_user_messages: Sequence[WorkflowCopilotChatHistoryMessage] = (),
    persisted_workflow_yaml: str | None = None,
    selected_connected_account_id: str | None = None,
) -> RequestPolicy:
    del config
    safety = await _screen_raw_secret_safety(user_message, handler, organization_id=organization_id)
    credential_ids = _credential_ids(user_message)
    raw_secret_present = safety.status == "detected"
    # Only an unavailable screen withholds the turn. A screen that ran and cited redacts what it
    # cited and continues update-only, so a detection never costs the user the rest of their turn.
    safety_blocked = safety.status == "blocked"
    policy = RequestPolicy(
        authoring_intent="author_now",
        credential_input_kind=("raw_secret" if raw_secret_present else "credential_id" if credential_ids else "none"),
        credential_refs=credential_ids,
        raw_secret_detected=raw_secret_present,
        # The safety screen owns the redacted-draft transition.
        raw_secret_handling=safety.handling,
        raw_secret_safety_status=safety.status,
        raw_secret_safety_failure_kind=safety.failure_kind,
        raw_secret_safety_citation_count=safety.citation_count,
        raw_secret_safety_exonerated_citation_count=safety.exonerated_citation_count,
        raw_secret_safety_latency_ms=safety.latency_ms,
        requires_user_clarification=safety_blocked,
        allow_update_workflow=not safety_blocked,
        allow_run_blocks=not safety_blocked,
        user_response_policy="ask_clarification" if safety_blocked else "proceed",
        clarification_reason="safety_screen_unavailable" if safety_blocked else "none",
        clarification_question=SAFETY_SCREEN_UNAVAILABLE_QUESTION if safety_blocked else None,
        classifier_status="not_run",
        completion_contract_status="absent",
        canonical_user_message=safety.canonical_user_message,
    )
    policy = await _build_request_policy_bootstrap(
        user_message=user_message,
        workflow_yaml=workflow_yaml,
        chat_history=chat_history,
        global_llm_context=global_llm_context,
        organization_id=organization_id,
        prior_user_messages=prior_user_messages,
        persisted_workflow_yaml=persisted_workflow_yaml,
        selected_connected_account_id=selected_connected_account_id,
        _preclassified_policy=policy,
    )
    LOG.info(
        "raw-secret safety screen",
        status=policy.raw_secret_safety_status,
        failure_kind=policy.raw_secret_safety_failure_kind,
        citation_count=policy.raw_secret_safety_citation_count,
        exonerated_citation_count=policy.raw_secret_safety_exonerated_citation_count,
        handling=policy.raw_secret_handling,
        latency_ms=round(policy.raw_secret_safety_latency_ms, 3),
    )
    return policy
