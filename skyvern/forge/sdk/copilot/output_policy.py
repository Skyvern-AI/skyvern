from __future__ import annotations

import re
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any, cast

from skyvern.forge.sdk.copilot.blocker_signal import contains_internal_machinery_leak
from skyvern.forge.sdk.copilot.context import COPILOT_RESPONSE_TYPES, ResponseType
from skyvern.forge.sdk.copilot.output_utils import (
    looks_like_workflow_delivery_claim,
    looks_like_workflow_yaml_in_chat,
)
from skyvern.forge.sdk.copilot.request_policy import (
    RequestPolicy,
    contains_email_password_pair,
    request_policy_has_present_completion_contract,
)
from skyvern.forge.sdk.copilot.secret_redaction import (
    RAW_SECRET_PATTERNS,
    SECRET_KEYWORD_ASSIGNMENT_PATTERN,
)
from skyvern.forge.sdk.copilot.workflow_credential_utils import (
    block_credential_ids,
    credential_params,
    parse_workflow_yaml,
    url_origin,
    workflow_blocks,
    workflow_credential_ids_from_parsed,
    workflow_credential_origins_from_parsed,
)

WORKFLOW_PRESENT_SENTINEL = object()
_CREDENTIAL_ID_RE = re.compile(r"\bcred_[A-Za-z0-9][A-Za-z0-9_-]*\b")
_PLACEHOLDER_MARKERS = ("{{", "{%", "[REDACTED_SECRET]")
# RHS of a secret-keyword assignment that references a bound value instead of carrying one:
# a `parameters`-rooted lookup (quoted-key subscript / .get / attribute), or an attribute
# chain ending in a credential field (`cred.password` / `await cred.otp()`), optionally wrapped in str(...).
# `totp` remains allowed for backward compatibility with old synthesized code;
# new Code-block OTP flows should use `await cred.otp()`.
# Fully anchored — only closing punctuation may follow, so a literal appended to a
# reference (`cred.password+"hunter2"`) or a dotted literal (a JWT) never passes.
_SANCTIONED_SECRET_REFERENCE_RE = re.compile(
    r"^(?:str\()?"
    r"(?:parameters(?:\[(?:'[^']*'|\"[^\"]*\")\]|\.get\((?:'[^']*'|\"[^\"]*\")\)|(?:\.[A-Za-z_][A-Za-z0-9_]*)+)"
    r"|[A-Za-z_][A-Za-z0-9_]*(?:\.[A-Za-z_][A-Za-z0-9_]*)*\.(?:username|password|totp)"
    r"|(?:await\s+)?[A-Za-z_][A-Za-z0-9_]*(?:\.[A-Za-z_][A-Za-z0-9_]*)*\.otp\(\))"
    r"[)\]\},;.'\"]*$"
)
# The RHS can be a multi-token expression such as `await login_credentials.otp()`.
# Callers pass the single line containing the match, so this is not expected to
# consume across embedded newlines.
_SECRET_ASSIGNMENT_RHS_RE = re.compile(r"[:=]\s*(.+)\s*$")
_UNVALIDATED_PROPOSAL_AFFORDANCE_RE = re.compile(
    r"\baccept\b(?=[\s\S]{0,120}\bsav(?:e|ed|ing)\b)(?=[\s\S]{0,160}\b(?:reject|discard)\b)",
    re.IGNORECASE,
)
UNVALIDATED_DISCLOSURE_PHRASES = (
    "not tested",
    "not been tested",
    "not verified",
    "not been verified",
    "hasn't been tested",
    "hasn't been verified",
    "unvalidated",
)
_INTERNAL_TOOL_INSTRUCTION_MARKERS = (
    "call get_run_results",
    "call update_and_run_blocks",
)
_INTERNAL_TOOL_RETRY_PHRASES = ("do not retry", "do not re-invoke")
_INTERNAL_TOOL_RETRY_CONTEXT_MARKERS = (
    "block running tool",
    "block running tools",
    "block-running tool",
    "block-running tools",
    "get_run_results",
    "update_and_run_blocks",
    "tool call",
    "this tool",
    "the tool",
    "workflow_run_id",
)
_INTERNAL_TOOL_INSTRUCTION_TRANSLATION = str.maketrans({char: " " for char in "`'\"()[]{}.,:;"})
_INTERNAL_BLOCK_TYPE_TERMS = frozenset(
    {
        "navigation",
        "extraction",
        "validation",
        "login",
        "goto_url",
        "file_download",
        "file_upload",
        "text_prompt",
        "for_loop",
        "conditional",
        "action",
        "wait",
    }
)
_INTERNAL_BLOCK_TYPE_CONTEXT_MARKERS = (
    "block type",
    "block types",
    "internal block",
    "internal blocks",
    "workflow block",
    "workflow blocks",
    "supported block",
    "supported blocks",
)
# Three distinct internal names in an informational reply implies taxonomy
# enumeration, while one or two may be incidental product-language prose.
_INFORMATIONAL_TAXONOMY_TERM_THRESHOLD = 3
_IDENTIFIER_TOKEN_RE = re.compile(r"[A-Za-z][A-Za-z0-9_]*")
_IDENTIFIER_DELIMITERS = frozenset({"`", '"', "'"})

_INTERNAL_CLASSIFIER_VOCAB_PHRASES = ("turnintent classified this turn as",)
_INTERNAL_CLASSIFIER_REASON_CODE_LABEL_RE = re.compile(r"\bsafe_reason_code\s*[:=]")
_INTERNAL_CLASSIFIER_REASON_CODE_PREFIXES = (
    "turn_intent_",
    "request_policy_",
    "build_phase_",
)
_INTERNAL_CLASSIFIER_DELIMITED_NAMES = frozenset({"turnintent", "requestpolicy"})

_INTERNAL_TOOL_PARAPHRASE_PHRASES = ("nudge turn", "nudge-turn")
_INTERNAL_COPILOT_SENTINEL_PREFIX = "[copilot:"
_LOOP_DETECTED_MARKER = "loop detected"

_SELF_PRESCRIPTIVE_FIXED_PHRASE = "send me a normal instruction like"
_SELF_PRESCRIPTIVE_IMPERATIVE_VERBS = frozenset({"send", "reply", "type", "respond"})
_SELF_PRESCRIPTIVE_POLITE_PREFIXES = frozenset({"please", "kindly", "just", "now"})
# Apostrophes inside contractions (`you'd`, `I'll`, `won't`) are not quote openers;
# treat the single-quote characters as exemplar openers only at word boundaries.
_SELF_PRESCRIPTIVE_UNCONDITIONAL_QUOTES = frozenset('"“”')
_SELF_PRESCRIPTIVE_BOUNDARY_QUOTES = frozenset("'‘’")
_SELF_PRESCRIPTIVE_QUOTED_WINDOW = 60
_SELF_PRESCRIPTIVE_CONTINUATION_WINDOW = 120
_SELF_PRESCRIPTIVE_POSITION_TERMINATORS = frozenset({"\n", ";", ".", "!", "?"})
_SELF_PRESCRIPTIVE_POSITION_COORDINATORS = (", ", "and ", "or ")
# Continuation cues distinguish chat-direction prescription ("type X to continue", "send X
# next and I'll keep going") from imperative docs prose ("type X in the field", "send X
# as JSON"). The fixed phrase above still fires without a cue; this guard only applies
# to the looser verb+quoted-exemplar heuristic.
_SELF_PRESCRIPTIVE_CONTINUATION_RE = re.compile(
    r"\b(?:"
    r"next|then|instead|keep going"
    r"|to (?:continue|proceed|stop|abort|cancel|resume|retry)"
    r"|and (?:i|we|you)['‘’]ll"
    r")\b",
    re.IGNORECASE,
)
_OUTPUT_FIELD_CONFIRMATION_RE = re.compile(
    r"(?:"
    r"\b(?:confirm|verify|approve|check|tell me|let me know)\b"
    r"(?=[\s\S]{0,180}\b(?:output|record|schema|field|fields)\b)"
    r"(?=[\s\S]{0,220}\b(?:field|fields|schema|record)\b)"
    r"|\b(?:which|what)\s+fields\s+should\b"
    r")",
    re.IGNORECASE,
)

# Response types whose `user_response` is rendered verbatim as the agent's final
# message — REPLY and REPLACE_WORKFLOW. ASK_QUESTION is excluded because legitimate
# clarifications ("Reply 'yes' to proceed") would false-positive the residual detectors.
_USER_VISIBLE_REPLY_TYPES: frozenset[str] = frozenset({"REPLY", "REPLACE_WORKFLOW"})


@dataclass(frozen=True)
class ResponseScaffoldingNormalization:
    response_type: ResponseType
    user_response: str | None
    changed: bool = False


class CopilotOutputKind(StrEnum):
    INFORMATIONAL_ANSWER = "informational_answer"
    CLARIFICATION_REQUEST = "clarification_request"
    REFUSAL = "refusal"
    WORKFLOW_DRAFT_PROPOSAL = "workflow_draft_proposal"
    WORKFLOW_UPDATE_PROPOSAL = "workflow_update_proposal"
    WORKFLOW_RUN_RESULT = "workflow_run_result"


class OutputPolicyReason(StrEnum):
    RAW_SECRET_LEAK = "raw_secret_leak"
    REQUEST_POLICY_CLARIFICATION_BYPASS = "request_policy_clarification_bypass"
    UNAPPROVED_CREDENTIAL_REFERENCE = "unapproved_credential_reference"
    CREDENTIAL_SCOPE_BROADENED = "credential_scope_broadened"
    UNBACKED_WORKFLOW_DELIVERY_CLAIM = "unbacked_workflow_delivery_claim"
    MISSING_UNVALIDATED_PROPOSAL_AFFORDANCE = "missing_unvalidated_proposal_affordance"
    MISSING_PROPOSAL_STATE = "missing_proposal_state"
    PERSISTENCE_STATE_MISMATCH = "persistence_state_mismatch"
    INTERNAL_TOOL_INSTRUCTION_LEAK = "internal_tool_instruction_leak"
    OUTPUT_POLICY_CONTEXT_MISSING = "output_policy_context_missing"
    INTERNAL_BLOCK_TAXONOMY_LEAK = "internal_block_taxonomy_leak"
    INTERNAL_CLASSIFIER_VOCAB_LEAK = "internal_classifier_vocab_leak"
    SELF_PRESCRIPTIVE_PHRASE_LEAK = "self_prescriptive_phrase_leak"
    WORKFLOW_YAML_IN_REPLY = "workflow_yaml_in_reply"
    AVOIDABLE_OUTPUT_FIELD_CONFIRMATION = "avoidable_output_field_confirmation"


@dataclass
class OutputPolicyVerdict:
    allowed: bool = True
    output_kind: CopilotOutputKind = CopilotOutputKind.INFORMATIONAL_ANSWER
    reason_codes: list[OutputPolicyReason] = field(default_factory=list)

    def __post_init__(self) -> None:
        if self.reason_codes:
            self.allowed = False

    def add(self, reason: OutputPolicyReason) -> None:
        if reason not in self.reason_codes:
            self.reason_codes.append(reason)
        self.allowed = False

    def remove(self, reason: OutputPolicyReason) -> None:
        if reason in self.reason_codes:
            self.reason_codes.remove(reason)
        self.allowed = not self.reason_codes


_FINAL_OUTPUT_HARD_BLOCK_REASONS: frozenset[OutputPolicyReason] = frozenset(
    {
        OutputPolicyReason.RAW_SECRET_LEAK,
        OutputPolicyReason.REQUEST_POLICY_CLARIFICATION_BYPASS,
        OutputPolicyReason.UNAPPROVED_CREDENTIAL_REFERENCE,
        OutputPolicyReason.CREDENTIAL_SCOPE_BROADENED,
        OutputPolicyReason.PERSISTENCE_STATE_MISMATCH,
        OutputPolicyReason.INTERNAL_TOOL_INSTRUCTION_LEAK,
        OutputPolicyReason.OUTPUT_POLICY_CONTEXT_MISSING,
        OutputPolicyReason.AVOIDABLE_OUTPUT_FIELD_CONFIRMATION,
    }
)


def hard_block_output_policy_verdict(verdict: OutputPolicyVerdict) -> OutputPolicyVerdict:
    hard_reasons = [reason for reason in verdict.reason_codes if reason in _FINAL_OUTPUT_HARD_BLOCK_REASONS]
    return OutputPolicyVerdict(
        allowed=not hard_reasons,
        output_kind=verdict.output_kind,
        reason_codes=hard_reasons,
    )


def derive_output_kind(
    *,
    response_type: str,
    request_policy: RequestPolicy | None,
    updated_workflow: Any | None,
    workflow_was_persisted: bool,
    workflow_attempted: bool,
    unvalidated: bool,
) -> CopilotOutputKind:
    # Policy and explicit response type win before workflow state: a required
    # clarification must never be reclassified as a workflow proposal.
    if isinstance(request_policy, RequestPolicy) and request_policy.user_response_policy == "ask_clarification":
        return CopilotOutputKind.CLARIFICATION_REQUEST
    if response_type == "ASK_QUESTION":
        return CopilotOutputKind.CLARIFICATION_REQUEST
    if updated_workflow is not None and workflow_attempted and not unvalidated:
        return CopilotOutputKind.WORKFLOW_RUN_RESULT
    if updated_workflow is not None and workflow_was_persisted:
        return CopilotOutputKind.WORKFLOW_UPDATE_PROPOSAL
    if updated_workflow is not None:
        return CopilotOutputKind.WORKFLOW_DRAFT_PROPOSAL
    if workflow_attempted:
        return CopilotOutputKind.WORKFLOW_RUN_RESULT
    return CopilotOutputKind.INFORMATIONAL_ANSWER


def normalize_response_scaffolding(response_type: str, user_response: str | None) -> ResponseScaffoldingNormalization:
    typed_response_type: ResponseType = (
        cast(ResponseType, response_type) if response_type in COPILOT_RESPONSE_TYPES else "REPLY"
    )
    label, stripped = _split_leading_response_label(user_response)
    if label is None:
        return ResponseScaffoldingNormalization(response_type=typed_response_type, user_response=user_response)
    normalized_type: ResponseType
    if label == "REPLACE_WORKFLOW":
        normalized_type = "REPLACE_WORKFLOW" if typed_response_type == "REPLACE_WORKFLOW" else "REPLY"
    else:
        normalized_type = label
    return ResponseScaffoldingNormalization(response_type=normalized_type, user_response=stripped, changed=True)


def _split_leading_response_label(text: str | None) -> tuple[ResponseType | None, str | None]:
    if not isinstance(text, str):
        return None, text
    candidate = text.lstrip()
    candidate_upper = candidate.upper()
    for response_type in sorted(COPILOT_RESPONSE_TYPES, key=len, reverse=True):
        if not candidate_upper.startswith(response_type):
            continue
        remainder = candidate[len(response_type) :]
        if not remainder:
            continue
        stripped = remainder.lstrip()
        if not stripped:
            return response_type, ""
        if stripped[0] in {":", ","}:
            return response_type, stripped[1:].lstrip()
        protocol_like_label = "_" in response_type or candidate[: len(response_type)].isupper()
        leading_whitespace = remainder[: len(remainder) - len(stripped)]
        if "\n" in leading_whitespace and not stripped.startswith(("{", "```")):
            return response_type, stripped
        if remainder[0].isspace() and protocol_like_label and not stripped.startswith(("{", "```")):
            return response_type, stripped
    return None, text


def output_policy_verdict_to_trace_data(
    verdict: OutputPolicyVerdict,
    *,
    surface: str,
    response_type: str | None = None,
    tool_name: str | None = None,
) -> dict[str, Any]:
    data: dict[str, Any] = {
        "surface": surface,
        "allowed": verdict.allowed,
        "output_kind": verdict.output_kind.value,
        "reason_codes": [reason.value for reason in verdict.reason_codes],
    }
    if response_type is not None:
        data["response_type"] = response_type
    if tool_name is not None:
        data["tool_name"] = tool_name
    return data


def build_output_policy_diagnostics(
    *,
    raw_verdict: OutputPolicyVerdict,
    final_verdict: OutputPolicyVerdict,
    final_output_kind: CopilotOutputKind,
    hard_block_reason_codes: list[OutputPolicyReason],
    soft_rewrite_reason_codes: list[OutputPolicyReason],
) -> dict[str, Any]:
    raw_would_have_failed = bool(raw_verdict.reason_codes)
    contained_failure = bool(hard_block_reason_codes or soft_rewrite_reason_codes)
    return {
        "raw_output_kind": raw_verdict.output_kind.value,
        "final_output_kind": final_output_kind.value,
        "raw_reason_codes": [reason.value for reason in raw_verdict.reason_codes],
        "hard_block_reason_codes": [reason.value for reason in hard_block_reason_codes],
        "soft_rewrite_reason_codes": [reason.value for reason in soft_rewrite_reason_codes],
        "raw_would_have_failed": raw_would_have_failed,
        "contained_failure": raw_would_have_failed and contained_failure,
        "final_output_policy_allowed": final_verdict.allowed,
    }


def output_policy_verdict_from_trace_data(data: Any) -> OutputPolicyVerdict:
    if not isinstance(data, dict):
        return OutputPolicyVerdict(
            allowed=False,
            reason_codes=[OutputPolicyReason.OUTPUT_POLICY_CONTEXT_MISSING],
        )
    reason_codes: list[OutputPolicyReason] = []
    for raw_reason in data.get("reason_codes") or []:
        try:
            reason_codes.append(OutputPolicyReason(str(raw_reason)))
        except ValueError:
            continue
    try:
        output_kind = CopilotOutputKind(str(data.get("output_kind")))
    except ValueError:
        output_kind = CopilotOutputKind.INFORMATIONAL_ANSWER
    return OutputPolicyVerdict(
        allowed=bool(data.get("allowed")) and not reason_codes,
        output_kind=output_kind,
        reason_codes=reason_codes,
    )


def evaluate_output_policy(
    *,
    request_policy: RequestPolicy | None,
    response_type: str = "REPLY",
    user_response: str | None = None,
    global_llm_context: str | None = None,
    workflow_yaml: str | None = None,
    tool_arguments: Any | None = None,
    has_workflow_proposal: bool = False,
    workflow_was_persisted: bool = False,
    workflow_attempted: bool = False,
    unvalidated: bool = False,
    output_kind: CopilotOutputKind | None = None,
) -> OutputPolicyVerdict:
    if output_kind is None:
        output_kind = derive_output_kind(
            response_type=response_type,
            request_policy=request_policy,
            updated_workflow=WORKFLOW_PRESENT_SENTINEL if has_workflow_proposal else None,
            workflow_was_persisted=workflow_was_persisted,
            workflow_attempted=workflow_attempted,
            unvalidated=unvalidated,
        )
    verdict = OutputPolicyVerdict(output_kind=output_kind)
    # Scan only proposed output/tool surfaces for hard leaks. The rolling
    # global context can include prior turn state, so re-scanning it here can
    # repeatedly block otherwise safe follow-up responses.
    values = [user_response, workflow_yaml, tool_arguments]
    if any(_contains_raw_secret(value) for value in values):
        verdict.add(OutputPolicyReason.RAW_SECRET_LEAK)
    if _contains_internal_tool_instruction(user_response):
        verdict.add(OutputPolicyReason.INTERNAL_TOOL_INSTRUCTION_LEAK)
    if (
        response_type == "REPLY"
        and not has_workflow_proposal
        and not workflow_attempted
        and looks_like_workflow_delivery_claim(user_response)
    ):
        verdict.add(OutputPolicyReason.UNBACKED_WORKFLOW_DELIVERY_CLAIM)
        verdict.add(OutputPolicyReason.MISSING_PROPOSAL_STATE)
    if (
        response_type == "REPLY"
        and has_workflow_proposal
        and unvalidated
        and not _has_unvalidated_affordance(user_response)
    ):
        verdict.add(OutputPolicyReason.MISSING_UNVALIDATED_PROPOSAL_AFFORDANCE)
    if _contains_internal_block_taxonomy_leak(user_response, output_kind, response_type):
        verdict.add(OutputPolicyReason.INTERNAL_BLOCK_TAXONOMY_LEAK)
    if response_type in _USER_VISIBLE_REPLY_TYPES and _contains_internal_classifier_vocab_leak(user_response):
        verdict.add(OutputPolicyReason.INTERNAL_CLASSIFIER_VOCAB_LEAK)
    if response_type in _USER_VISIBLE_REPLY_TYPES and _contains_self_prescriptive_phrase(user_response):
        verdict.add(OutputPolicyReason.SELF_PRESCRIPTIVE_PHRASE_LEAK)
    if response_type in ("REPLY", "ASK_QUESTION") and looks_like_workflow_yaml_in_chat(user_response):
        verdict.add(OutputPolicyReason.WORKFLOW_YAML_IN_REPLY)

    if isinstance(request_policy, RequestPolicy):
        if request_policy.user_response_policy == "ask_clarification" and response_type != "ASK_QUESTION":
            verdict.add(OutputPolicyReason.REQUEST_POLICY_CLARIFICATION_BYPASS)
        if (
            response_type == "ASK_QUESTION"
            and request_policy.user_response_policy != "ask_clarification"
            and request_policy_has_present_completion_contract(request_policy)
            and not has_workflow_proposal
            and not workflow_attempted
            and _asks_to_confirm_output_fields(user_response)
        ):
            verdict.add(OutputPolicyReason.AVOIDABLE_OUTPUT_FIELD_CONFIRMATION)
        _apply_credential_policy(verdict, request_policy, values, workflow_yaml)

    if output_kind == CopilotOutputKind.WORKFLOW_UPDATE_PROPOSAL and not workflow_was_persisted:
        verdict.add(OutputPolicyReason.PERSISTENCE_STATE_MISMATCH)
    elif output_kind == CopilotOutputKind.WORKFLOW_DRAFT_PROPOSAL and workflow_was_persisted:
        verdict.add(OutputPolicyReason.PERSISTENCE_STATE_MISMATCH)

    return verdict


def _asks_to_confirm_output_fields(user_response: str | None) -> bool:
    if not isinstance(user_response, str) or not user_response.strip():
        return False
    return bool(_OUTPUT_FIELD_CONFIRMATION_RE.search(user_response[:500]))


def format_output_policy_tool_error(verdict: OutputPolicyVerdict) -> str:
    reasons = ", ".join(reason.value for reason in verdict.reason_codes) or "unknown"
    message = f"Output policy blocked this Copilot output before persistence. Reason codes: {reasons}."
    if OutputPolicyReason.RAW_SECRET_LEAK in verdict.reason_codes:
        message += (
            " For saved credentials, bind a credential_id workflow parameter and reference fields as "
            "`<key>.username`, `<key>.password`, or `await <key>.otp()` for one-time codes; do not split, "
            "concatenate, or obfuscate literal secrets in workflow code or YAML."
        )
    return message


def _contains_raw_secret(value: Any) -> bool:
    for text in _policy_text_values(value):
        if contains_email_password_pair(text):
            return True
        for pattern in RAW_SECRET_PATTERNS:
            for match in pattern.finditer(text):
                matched = match.group(0)
                if any(marker in matched for marker in _PLACEHOLDER_MARKERS):
                    continue
                if pattern is SECRET_KEYWORD_ASSIGNMENT_PATTERN and (
                    _is_sanctioned_secret_reference(matched)
                    or _is_sanctioned_secret_reference(_line_containing_match(text, match))
                ):
                    continue
                return True
    return False


def _line_containing_match(text: str, match: re.Match[str]) -> str:
    line_start = text.rfind("\n", 0, match.start()) + 1
    line_end = text.find("\n", match.end())
    if line_end == -1:
        line_end = len(text)
    return text[line_start:line_end]


def _is_sanctioned_secret_reference(matched: str) -> bool:
    rhs_match = _SECRET_ASSIGNMENT_RHS_RE.search(matched)
    if rhs_match is None:
        return False
    return bool(_SANCTIONED_SECRET_REFERENCE_RE.match(rhs_match.group(1)))


def _contains_internal_tool_instruction(user_response: str | None) -> bool:
    if not isinstance(user_response, str):
        return False
    if contains_internal_machinery_leak(user_response):
        return True
    normalized = " ".join(user_response.lower().translate(_INTERNAL_TOOL_INSTRUCTION_TRANSLATION).split())
    if any(marker in normalized for marker in _INTERNAL_TOOL_INSTRUCTION_MARKERS):
        return True
    return any(phrase in normalized for phrase in _INTERNAL_TOOL_RETRY_PHRASES) and any(
        marker in normalized for marker in _INTERNAL_TOOL_RETRY_CONTEXT_MARKERS
    )


def _policy_text_values(value: Any) -> list[str]:
    if value is None:
        return []
    if isinstance(value, str):
        return [value]
    if isinstance(value, dict):
        values: list[str] = []
        for key, item in value.items():
            values.extend(_policy_text_values(key))
            values.extend(_policy_text_values(item))
        return values
    if isinstance(value, (list, tuple, set)):
        values = []
        for item in value:
            values.extend(_policy_text_values(item))
        return values
    return [str(value)]


def _has_unvalidated_affordance(user_response: str | None) -> bool:
    if not user_response:
        return False
    lower = user_response.lower()
    has_disclosure = any(phrase in lower for phrase in UNVALIDATED_DISCLOSURE_PHRASES)
    return bool(_UNVALIDATED_PROPOSAL_AFFORDANCE_RE.search(user_response) and has_disclosure)


def _contains_internal_block_taxonomy_leak(
    user_response: str | None,
    output_kind: CopilotOutputKind,
    response_type: str,
) -> bool:
    if not user_response:
        return False
    if _contains_deprecated_block_identifier(user_response):
        return True
    if response_type in _USER_VISIBLE_REPLY_TYPES and _contains_internal_tool_vocab_leak(user_response):
        return True
    if output_kind != CopilotOutputKind.INFORMATIONAL_ANSWER:
        return False
    taxonomy_terms = _internal_block_taxonomy_terms(user_response)
    return len(taxonomy_terms) >= _INFORMATIONAL_TAXONOMY_TERM_THRESHOLD


def _contains_internal_tool_vocab_leak(user_response: str) -> bool:
    lower = user_response.lower()
    if _LOOP_DETECTED_MARKER in lower:
        return True
    if _INTERNAL_COPILOT_SENTINEL_PREFIX in lower:
        return True
    return any(phrase in lower for phrase in _INTERNAL_TOOL_PARAPHRASE_PHRASES)


def _contains_deprecated_block_identifier(text: str) -> bool:
    tokens = [_compact_identifier_token(match.group(0)) for match in _IDENTIFIER_TOKEN_RE.finditer(text)]
    if "taskv2" in tokens:
        return True
    return any(left == "task" and right == "v2" for left, right in zip(tokens, tokens[1:]))


def _internal_block_taxonomy_terms(text: str) -> set[str]:
    lower = text.lower()
    has_taxonomy_context = any(marker in lower for marker in _INTERNAL_BLOCK_TYPE_CONTEXT_MARKERS)
    matches = list(_IDENTIFIER_TOKEN_RE.finditer(text))
    terms: set[str] = set()
    for index, match in enumerate(matches):
        term = _normalized_internal_block_term(match.group(0))
        if term is None:
            continue
        if has_taxonomy_context or _is_delimited_identifier(text, match.start(), match.end()):
            terms.add(term)
            continue
        next_token = _compact_identifier_token(matches[index + 1].group(0)) if index + 1 < len(matches) else None
        if next_token in {"block", "blocks", "for"}:
            terms.add(term)
    return terms


def _normalized_internal_block_term(raw: str) -> str | None:
    term = raw.lower()
    if term in _INTERNAL_BLOCK_TYPE_TERMS:
        return term
    return None


def _compact_identifier_token(raw: str) -> str:
    return raw.lower().replace("_", "")


def _is_delimited_identifier(text: str, start: int, end: int) -> bool:
    if start == 0 or end >= len(text):
        return False
    left = text[start - 1]
    return left == text[end] and left in _IDENTIFIER_DELIMITERS


def _contains_internal_classifier_vocab_leak(user_response: str | None) -> bool:
    if not isinstance(user_response, str) or not user_response:
        return False
    lower = user_response.lower()
    if any(phrase in lower for phrase in _INTERNAL_CLASSIFIER_VOCAB_PHRASES):
        return True
    if _INTERNAL_CLASSIFIER_REASON_CODE_LABEL_RE.search(lower):
        return True
    for match in _IDENTIFIER_TOKEN_RE.finditer(user_response):
        token = match.group(0)
        lowered = token.lower()
        if "_" in token and any(lowered.startswith(prefix) for prefix in _INTERNAL_CLASSIFIER_REASON_CODE_PREFIXES):
            return True
        # CamelCase classifier names are internal vocabulary even without backticks;
        # the lowercase forms (`turn intent`, `request policy`) remain natural prose.
        if token in {"TurnIntent", "RequestPolicy"}:
            return True
        if lowered in _INTERNAL_CLASSIFIER_DELIMITED_NAMES and _is_delimited_identifier(
            user_response, match.start(), match.end()
        ):
            return True
    return False


def _contains_self_prescriptive_phrase(user_response: str | None) -> bool:
    if not isinstance(user_response, str) or not user_response:
        return False
    lower = user_response.lower()
    if _SELF_PRESCRIPTIVE_FIXED_PHRASE in lower:
        return True
    for match in _IDENTIFIER_TOKEN_RE.finditer(user_response):
        verb = match.group(0).lower()
        if verb not in _SELF_PRESCRIPTIVE_IMPERATIVE_VERBS:
            continue
        if not _is_imperative_position(user_response, match.start()):
            continue
        window_end = min(len(user_response), match.end() + _SELF_PRESCRIPTIVE_QUOTED_WINDOW)
        if not _contains_quoted_exemplar(user_response, match.end(), window_end):
            continue
        cue_end = min(len(user_response), match.end() + _SELF_PRESCRIPTIVE_CONTINUATION_WINDOW)
        if _SELF_PRESCRIPTIVE_CONTINUATION_RE.search(user_response, match.end(), cue_end):
            return True
    return False


def _contains_quoted_exemplar(text: str, window_start: int, window_end: int) -> bool:
    for index in range(window_start, window_end):
        char = text[index]
        if char in _SELF_PRESCRIPTIVE_UNCONDITIONAL_QUOTES:
            return True
        if char in _SELF_PRESCRIPTIVE_BOUNDARY_QUOTES and not _is_embedded_apostrophe(text, index):
            return True
    return False


def _is_embedded_apostrophe(text: str, index: int) -> bool:
    if index == 0:
        return False
    return text[index - 1].isalnum()


def _is_imperative_position(text: str, verb_start: int) -> bool:
    return _is_position_at_sentence_start(text, verb_start) or _is_position_after_polite_prefix(text, verb_start)


def _is_position_at_sentence_start(text: str, index: int) -> bool:
    if index == 0:
        return True
    # Walk past intra-clause whitespace (spaces, tabs, leading newlines) so terminators
    # followed by whitespace still count as sentence boundaries.
    cursor = index - 1
    while cursor >= 0 and text[cursor] in (" ", "\t"):
        cursor -= 1
    if cursor < 0:
        return True
    char = text[cursor]
    if char in _SELF_PRESCRIPTIVE_POSITION_TERMINATORS:
        return True
    lower = text.lower()
    for coordinator in _SELF_PRESCRIPTIVE_POSITION_COORDINATORS:
        start = index - len(coordinator)
        if start >= 0 and lower[start:index] == coordinator:
            return True
    return False


def _is_position_after_polite_prefix(text: str, verb_start: int) -> bool:
    if verb_start == 0:
        return False
    cursor = verb_start - 1
    while cursor > 0 and text[cursor] == " ":
        cursor -= 1
    word_end = cursor + 1
    word_start = word_end
    while word_start > 0 and text[word_start - 1].isalpha():
        word_start -= 1
    if word_start == word_end:
        return False
    prefix = text[word_start:word_end].lower()
    if prefix not in _SELF_PRESCRIPTIVE_POLITE_PREFIXES:
        return False
    return _is_position_at_sentence_start(text, word_start)


def _apply_credential_policy(
    verdict: OutputPolicyVerdict,
    request_policy: RequestPolicy,
    values: list[Any],
    workflow_yaml: str | None,
) -> None:
    found_ids: set[str] = set()
    for value in values:
        found_ids.update(_credential_ids(value))
    if not found_ids:
        return

    approved_ids = _approved_credential_ids(request_policy)
    allowed_unresolved_ids = _allowed_unresolved_credential_ids(request_policy)
    bound_approved_ids = _bound_approved_credential_ids(request_policy, workflow_yaml)
    allowed_ids = (
        approved_ids | allowed_unresolved_ids | bound_approved_ids | _existing_workflow_credential_ids(request_policy)
    )
    if any(credential_id not in allowed_ids for credential_id in found_ids):
        verdict.add(OutputPolicyReason.UNAPPROVED_CREDENTIAL_REFERENCE)

    if workflow_yaml:
        parsed_workflow = parse_workflow_yaml(workflow_yaml)
        if isinstance(parsed_workflow, dict):
            proposed_origins = workflow_credential_origins_from_parsed(parsed_workflow)
            if _workflow_broadens_credential_scope(parsed_workflow, request_policy) or (
                _existing_workflow_broadens_credential_scope(proposed_origins, request_policy)
            ):
                verdict.add(OutputPolicyReason.CREDENTIAL_SCOPE_BROADENED)


def _approved_credential_ids(request_policy: RequestPolicy) -> set[str]:
    return {
        credential.credential_id
        for credential in request_policy.resolved_credentials
        if isinstance(getattr(credential, "credential_id", None), str)
    }


def _bound_approved_credential_ids(request_policy: RequestPolicy, workflow_yaml: str | None) -> set[str]:
    # Requiring binding keeps a discovered ID leaked into a non-credential field
    # caught by misbinding rather than approved here.
    discovered = {credential.credential_id for credential in request_policy.discovered_credentials}
    discovered = {cid for cid in discovered if cid.startswith("cred_")}
    if not discovered or not workflow_yaml:
        return set()
    parsed = parse_workflow_yaml(workflow_yaml)
    if not isinstance(parsed, dict):
        return set()
    return discovered & workflow_credential_ids_from_parsed(parsed)


def _allowed_unresolved_credential_ids(request_policy: RequestPolicy) -> set[str]:
    if not request_policy.allow_missing_credentials_in_draft:
        return set()
    ids = set(request_policy.invalid_credential_ids)
    ids.update(ref for ref in request_policy.credential_refs if isinstance(ref, str) and ref.startswith("cred_"))
    return ids


def _existing_workflow_credential_ids(request_policy: RequestPolicy) -> set[str]:
    # Saved credential IDs are issued with the cred_ prefix; skip anything else defensively.
    return {
        credential_id
        for credential_id in request_policy.existing_workflow_credential_ids
        if isinstance(credential_id, str) and credential_id.startswith("cred_")
    }


def _existing_workflow_broadens_credential_scope(
    proposed_origins: dict[str, set[str]],
    request_policy: RequestPolicy,
) -> bool:
    existing_ids = _existing_workflow_credential_ids(request_policy)
    if not existing_ids:
        return False

    prior_origins = {
        credential_id: {
            origin for origin in origins if isinstance(origin, str) and origin.startswith(("http://", "https://"))
        }
        for credential_id, origins in request_policy.existing_workflow_credential_origins.items()
        if isinstance(credential_id, str)
    }
    for credential_id in existing_ids:
        new_origins = proposed_origins.get(credential_id, set())
        if not new_origins:
            continue
        allowed_origins = prior_origins.get(credential_id, set())
        if not allowed_origins:
            # Existing workflow credentials without a known prior origin cannot
            # safely authorize a newly introduced URL.
            return True
        if any(origin not in allowed_origins for origin in new_origins):
            return True
    return False


def _credential_ids(value: Any) -> set[str]:
    if value is None:
        return set()
    found: set[str] = set()
    for text in _policy_text_values(value):
        found.update(_CREDENTIAL_ID_RE.findall(text))
    return found


def _workflow_broadens_credential_scope(parsed_workflow: dict[str, Any], request_policy: RequestPolicy) -> bool:
    approved_origins = _approved_origins_by_id(request_policy)
    if not approved_origins:
        # No tested_url metadata means there is no deterministic origin scope
        # to compare against. The request policy still controls whether the
        # credential itself is approved; do not infer URL broadening from
        # missing credential metadata.
        return False

    workflow_definition = parsed_workflow.get("workflow_definition")
    if not isinstance(workflow_definition, dict):
        return False

    credential_params_by_key = credential_params(workflow_definition.get("parameters"))
    if not credential_params_by_key:
        return False

    return any(
        _block_broadens_credential_scope(block, credential_params_by_key, approved_origins)
        for block in workflow_blocks(parsed_workflow)
    )


def _approved_origins_by_id(request_policy: RequestPolicy) -> dict[str, set[str]]:
    origins: dict[str, set[str]] = {}
    for credential in [*request_policy.resolved_credentials, *request_policy.discovered_credentials]:
        credential_id = getattr(credential, "credential_id", None)
        tested_url = getattr(credential, "tested_url", None)
        if isinstance(credential_id, str) and isinstance(tested_url, str):
            origin = url_origin(tested_url)
            if origin:
                origins.setdefault(credential_id, set()).add(origin)
    return origins


def _block_broadens_credential_scope(
    block: dict[str, Any],
    credential_params_by_key: dict[str, str],
    approved_origins: dict[str, set[str]],
) -> bool:
    credential_ids = block_credential_ids(block, credential_params_by_key)
    if not credential_ids:
        return False

    block_url = block.get("url")
    if not isinstance(block_url, str) or not block_url.strip():
        return False
    origin = url_origin(block_url)
    if not origin:
        return True

    for credential_id in credential_ids:
        allowed_origins = approved_origins.get(credential_id)
        if allowed_origins and origin not in allowed_origins:
            return True
    return False
