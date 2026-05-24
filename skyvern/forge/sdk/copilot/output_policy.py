from __future__ import annotations

import re
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any, cast

from skyvern.forge.sdk.copilot.context import COPILOT_RESPONSE_TYPES, ResponseType
from skyvern.forge.sdk.copilot.output_utils import (
    looks_like_workflow_delivery_claim,
    looks_like_workflow_yaml_in_chat,
)
from skyvern.forge.sdk.copilot.request_policy import RAW_SECRET_PATTERNS, RequestPolicy, contains_email_password_pair
from skyvern.forge.sdk.copilot.workflow_credential_utils import (
    block_credential_ids,
    credential_params,
    parse_workflow_yaml,
    url_origin,
    workflow_blocks,
    workflow_credential_origins_from_parsed,
)

WORKFLOW_PRESENT_SENTINEL = object()
_CREDENTIAL_ID_RE = re.compile(r"\bcred_[A-Za-z0-9][A-Za-z0-9_-]*\b")
_PLACEHOLDER_MARKERS = ("{{", "{%", "[REDACTED_SECRET]")
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
    WORKFLOW_YAML_IN_REPLY = "workflow_yaml_in_reply"


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
    if _contains_internal_block_taxonomy_leak(user_response, output_kind):
        verdict.add(OutputPolicyReason.INTERNAL_BLOCK_TAXONOMY_LEAK)
    if response_type in ("REPLY", "ASK_QUESTION") and looks_like_workflow_yaml_in_chat(user_response):
        verdict.add(OutputPolicyReason.WORKFLOW_YAML_IN_REPLY)

    if isinstance(request_policy, RequestPolicy):
        if request_policy.user_response_policy == "ask_clarification" and response_type != "ASK_QUESTION":
            verdict.add(OutputPolicyReason.REQUEST_POLICY_CLARIFICATION_BYPASS)
        _apply_credential_policy(verdict, request_policy, values, workflow_yaml)

    if output_kind == CopilotOutputKind.WORKFLOW_UPDATE_PROPOSAL and not workflow_was_persisted:
        verdict.add(OutputPolicyReason.PERSISTENCE_STATE_MISMATCH)
    elif output_kind == CopilotOutputKind.WORKFLOW_DRAFT_PROPOSAL and workflow_was_persisted:
        verdict.add(OutputPolicyReason.PERSISTENCE_STATE_MISMATCH)

    return verdict


def format_output_policy_tool_error(verdict: OutputPolicyVerdict) -> str:
    reasons = ", ".join(reason.value for reason in verdict.reason_codes) or "unknown"
    return f"Output policy blocked this Copilot output before persistence. Reason codes: {reasons}."


def _contains_raw_secret(value: Any) -> bool:
    for text in _policy_text_values(value):
        if contains_email_password_pair(text):
            return True
        for pattern in RAW_SECRET_PATTERNS:
            for match in pattern.finditer(text):
                if not any(marker in match.group(0) for marker in _PLACEHOLDER_MARKERS):
                    return True
    return False


def _contains_internal_tool_instruction(user_response: str | None) -> bool:
    if not isinstance(user_response, str):
        return False
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
) -> bool:
    if not user_response:
        return False
    if _contains_deprecated_block_identifier(user_response):
        return True
    if output_kind != CopilotOutputKind.INFORMATIONAL_ANSWER:
        return False
    taxonomy_terms = _internal_block_taxonomy_terms(user_response)
    return len(taxonomy_terms) >= _INFORMATIONAL_TAXONOMY_TERM_THRESHOLD


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
    allowed_ids = approved_ids | allowed_unresolved_ids | _existing_workflow_credential_ids(request_policy)
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
    for credential in request_policy.resolved_credentials:
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
