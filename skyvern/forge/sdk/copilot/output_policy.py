from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any
from urllib.parse import urlparse

from skyvern.forge.sdk.copilot.output_utils import looks_like_workflow_delivery_claim
from skyvern.forge.sdk.copilot.request_policy import RAW_SECRET_PATTERNS, RequestPolicy
from skyvern.utils.yaml_loader import safe_load_no_dates

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
    OUTPUT_POLICY_CONTEXT_MISSING = "output_policy_context_missing"


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
    if value is None:
        return False
    text = _policy_text(value)
    for pattern in RAW_SECRET_PATTERNS:
        for match in pattern.finditer(text):
            if not any(marker in match.group(0) for marker in _PLACEHOLDER_MARKERS):
                return True
    return False


def _policy_text(value: Any) -> str:
    if isinstance(value, str):
        return value
    try:
        return json.dumps(value, default=str, sort_keys=True)
    except TypeError:
        return str(value)


def _has_unvalidated_affordance(user_response: str | None) -> bool:
    if not user_response:
        return False
    lower = user_response.lower()
    has_disclosure = any(phrase in lower for phrase in UNVALIDATED_DISCLOSURE_PHRASES)
    return bool(_UNVALIDATED_PROPOSAL_AFFORDANCE_RE.search(user_response) and has_disclosure)


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
    allowed_ids = approved_ids | allowed_unresolved_ids
    if any(credential_id not in allowed_ids for credential_id in found_ids):
        verdict.add(OutputPolicyReason.UNAPPROVED_CREDENTIAL_REFERENCE)

    if workflow_yaml and _workflow_broadens_credential_scope(workflow_yaml, request_policy):
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


def _credential_ids(value: Any) -> set[str]:
    if value is None:
        return set()
    return set(_CREDENTIAL_ID_RE.findall(_policy_text(value)))


def _workflow_broadens_credential_scope(workflow_yaml: str, request_policy: RequestPolicy) -> bool:
    parsed = _parse_workflow_yaml(workflow_yaml)
    if not isinstance(parsed, dict):
        return False

    approved_origins = _approved_origins_by_id(request_policy)
    if not approved_origins:
        # No tested_url metadata means there is no deterministic origin scope
        # to compare against. The request policy still controls whether the
        # credential itself is approved; do not infer URL broadening from
        # missing credential metadata.
        return False

    workflow_definition = parsed.get("workflow_definition")
    if not isinstance(workflow_definition, dict):
        return False

    credential_params = _credential_params(workflow_definition.get("parameters"))
    if not credential_params:
        return False

    return any(
        _block_broadens_credential_scope(block, credential_params, approved_origins) for block in _blocks(parsed)
    )


def _parse_workflow_yaml(workflow_yaml: str) -> Any:
    try:
        return safe_load_no_dates(workflow_yaml)
    except Exception:
        return None


def _approved_origins_by_id(request_policy: RequestPolicy) -> dict[str, set[str]]:
    origins: dict[str, set[str]] = {}
    for credential in request_policy.resolved_credentials:
        credential_id = getattr(credential, "credential_id", None)
        tested_url = getattr(credential, "tested_url", None)
        if isinstance(credential_id, str) and isinstance(tested_url, str):
            origin = _origin(tested_url)
            if origin:
                origins.setdefault(credential_id, set()).add(origin)
    return origins


def _origin(url: str) -> str | None:
    parsed = urlparse(url if "://" in url else f"https://{url}")
    if not parsed.netloc:
        return None
    # Keep scheme in the origin. http:// and https:// are different security
    # contexts, so crossing between them is treated as scope broadening.
    return f"{parsed.scheme.lower()}://{parsed.netloc.lower()}"


def _credential_params(parameters: Any) -> dict[str, str]:
    if not isinstance(parameters, list):
        return {}
    credential_params: dict[str, str] = {}
    for parameter in parameters:
        if not isinstance(parameter, dict):
            continue
        key = parameter.get("key")
        if not isinstance(key, str):
            continue
        parameter_type = str(parameter.get("parameter_type") or "").lower()
        workflow_parameter_type = str(parameter.get("workflow_parameter_type") or "").lower()
        if parameter_type == "credential" and isinstance(parameter.get("credential_id"), str):
            credential_params[key] = parameter["credential_id"]
        elif (
            parameter_type == "workflow"
            and workflow_parameter_type == "credential_id"
            and isinstance(parameter.get("default_value"), str)
        ):
            credential_params[key] = parameter["default_value"]
    return credential_params


def _blocks(parsed: dict[str, Any]) -> list[dict[str, Any]]:
    workflow_definition = parsed.get("workflow_definition")
    if not isinstance(workflow_definition, dict):
        return []

    collected: list[dict[str, Any]] = []

    def visit(blocks: Any) -> None:
        if not isinstance(blocks, list):
            return
        for block in blocks:
            if not isinstance(block, dict):
                continue
            collected.append(block)
            visit(block.get("loop_blocks"))

    visit(workflow_definition.get("blocks"))
    return collected


def _block_broadens_credential_scope(
    block: dict[str, Any],
    credential_params: dict[str, str],
    approved_origins: dict[str, set[str]],
) -> bool:
    credential_ids = _block_credential_ids(block, credential_params)
    if not credential_ids:
        return False

    block_url = block.get("url")
    if not isinstance(block_url, str) or not block_url.strip():
        return False
    origin = _origin(block_url)
    if not origin:
        return True

    for credential_id in credential_ids:
        allowed_origins = approved_origins.get(credential_id)
        if allowed_origins and origin not in allowed_origins:
            return True
    return False


def _block_credential_ids(block: dict[str, Any], credential_params: dict[str, str]) -> set[str]:
    credential_ids: set[str] = set()
    parameter_keys = block.get("parameter_keys")
    if isinstance(parameter_keys, list):
        for key in parameter_keys:
            if isinstance(key, str) and key in credential_params:
                credential_ids.add(credential_params[key])
    direct_credential_id = block.get("credential_id")
    if isinstance(direct_credential_id, str):
        credential_ids.add(direct_credential_id)
    return credential_ids
