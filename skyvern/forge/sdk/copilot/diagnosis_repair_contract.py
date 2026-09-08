from __future__ import annotations

import hashlib
import json
import re
from enum import StrEnum
from typing import TYPE_CHECKING, Any

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from skyvern.forge.sdk.copilot.build_test_outcome import SOLVER_ATTEMPT_KEY, ChallengeEffects, Lever
from skyvern.forge.sdk.copilot.challenge_evidence import (
    ANTI_BOT_CHALLENGE_ALIAS_CATEGORIES,
    carrier_backed_anti_bot_categories,
    typed_challenge_kind,
)
from skyvern.forge.sdk.copilot.completion_verification import CompletionVerificationResult
from skyvern.forge.sdk.copilot.composition_evidence import interactive_challenge_controls
from skyvern.forge.sdk.copilot.context import CodeAuthoringRepairContext
from skyvern.forge.sdk.copilot.failure_tracking import (
    RepairRootCauseIdentity,
    compute_repair_root_cause_signature,
)
from skyvern.forge.sdk.copilot.output_policy import url_origin
from skyvern.forge.sdk.copilot.request_policy import redact_raw_secrets_for_prompt
from skyvern.forge.sdk.copilot.run_outcome import trusted_terminal_challenge_category_name
from skyvern.forge.sdk.copilot.runtime_authoring_repair import (
    run_challenge_is_runtime_clearable,
    run_id_from_result_data,
)
from skyvern.forge.sdk.copilot.workflow_credential_utils import URL_CANDIDATE_RE
from skyvern.webeye.actions.action_types import ActionType

if TYPE_CHECKING:
    from skyvern.forge.sdk.copilot.context import CopilotContext

_TEXT_MAX = 240
_SUMMARY_MAX = 180
_MAX_ITEMS = 20
# Slots inside _MAX_ITEMS held for failures that precede the newest rows, so a long run that failed
# early still names it without the row list growing with the failure count.
_EARLY_FAILURE_MAX_ITEMS = 5
_FAILED_STATUSES = {"failed", "terminated", "canceled", "timed_out"}
_CREDENTIAL_INPUT_MISSING_SKIP_REASONS = {"workflow_credential_inputs_unbound", "credential_name_unresolved"}
_PRE_RUN_CREDENTIAL_FAILURE_CATEGORIES = {"CREDENTIAL_ERROR", "PARAMETER_BINDING_ERROR"}
_REPAIRABLE_RUNTIME_CATEGORIES = {"AUTH_FAILURE"}
_AUTHORING_REPAIR_SIGNATURE_VERSION = "authoring_repair_context:v1"
_AUTHORING_REPAIR_CATEGORY = "CODE_AUTHORING_REPAIR"
_SOLVER_FAILURE_MAX = 200


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class DiagnosisFailureType(StrEnum):
    NO_FAILURE = "no_failure"
    FAILED_RUN = "failed_run"
    SUSPICIOUS_SUCCESS = "suspicious_success"
    TERMINAL_CHALLENGE_BLOCKER = "terminal_challenge_blocker"
    MISSING_CREDENTIAL_OR_INIT = "missing_credential_or_init"
    REPAIRABLE_BLOCK_FAILURE = "repairable_block_failure"
    UNRECOVERABLE_TOOL_ERROR = "unrecoverable_tool_error"
    UNKNOWN = "unknown"


class RepairNextAction(StrEnum):
    REPAIR = "repair"
    ASK = "ask"
    STOP = "stop"
    ESCALATE = "escalate"
    NO_CHANGE = "no_change"


class DiagnosisInput(StrictModel):
    user_goal: str = ""
    source_tool: str
    workflow_updated: bool = False
    workflow_run_id: str | None = None
    run_status: str | None = None
    requested_block_labels: list[str] = Field(default_factory=list)
    executed_block_labels: list[str] = Field(default_factory=list)
    frontier_start_label: str | None = None
    failed_block_labels: list[str] = Field(default_factory=list)
    failure_categories: list[str] = Field(default_factory=list)
    browser_page_state: dict[str, Any] = Field(default_factory=dict)


class DiagnosisResult(StrictModel):
    suspected_failure_type: DiagnosisFailureType = DiagnosisFailureType.UNKNOWN
    root_cause_summary: str = ""
    root_cause_identity: RepairRootCauseIdentity = Field(default_factory=RepairRootCauseIdentity)
    confidence: float = Field(default=0.0, ge=0.0, le=1.0)
    evidence_references: list[str] = Field(default_factory=list)
    missing_context: list[str] = Field(default_factory=list)


class RepairDecision(StrictModel):
    next_action: RepairNextAction = RepairNextAction.NO_CHANGE
    target_blocks: list[str] = Field(default_factory=list)
    proposed_change_summary: str = ""
    required_authority: list[str] = Field(default_factory=list)
    completion_check: str = ""


class VerificationResult(StrictModel):
    run_status: str | None = None
    user_goal_satisfied: bool | None = None
    completion_contract_satisfied: bool | None = None
    remaining_blocker: str | None = None


class DiagnosisRepairContract(StrictModel):
    diagnosis_input: DiagnosisInput
    diagnosis_result: DiagnosisResult
    repair_decision: RepairDecision
    verification_result: VerificationResult
    challenge: ChallengeEffects | None = None
    levers: list[Lever] = Field(default_factory=list)

    def to_trace_data(self) -> dict[str, Any]:
        identity = self.diagnosis_result.root_cause_identity
        return {
            "failure_type": self.diagnosis_result.suspected_failure_type.value,
            "root_cause_signature": identity.root_cause_signature,
            "root_cause_primary_category": identity.primary_category,
            "root_cause_categories": list(identity.failure_categories),
            "root_cause_error_class": identity.error_class,
            "root_cause_selector_kind": identity.selector_kind,
            "root_cause_selector": identity.selector,
            "next_action": self.repair_decision.next_action.value,
            "confidence": self.diagnosis_result.confidence,
            "source_tool": self.diagnosis_input.source_tool,
            "workflow_updated": self.diagnosis_input.workflow_updated,
            "run_status": self.verification_result.run_status,
            "failed_block_count": len(self.diagnosis_input.failed_block_labels),
            "failure_categories": list(self.diagnosis_input.failure_categories),
            "target_block_count": len(self.repair_decision.target_blocks),
            "missing_context": list(self.diagnosis_result.missing_context),
            "user_goal_satisfied": self.verification_result.user_goal_satisfied,
            "completion_contract_satisfied": self.verification_result.completion_contract_satisfied,
            "challenge_solver_result": self.challenge.solver_result if self.challenge else None,
            "levers": [lever.mechanism for lever in self.levers],
        }


def build_diagnosis_repair_contract(
    *,
    source_tool: str,
    result: dict[str, Any],
    ctx: CopilotContext,
    workflow_updated: bool = False,
) -> DiagnosisRepairContract:
    data = _dict(result.get("data")) if isinstance(result, dict) else {}
    raw_blocks = data.get("blocks")
    # Rows arrive chronologically, so the cap keeps the tail: the failing block the run stopped on
    # has to survive it.
    blocks: list[Any] = _capped_blocks(raw_blocks) if isinstance(raw_blocks, list) else []

    run_ok = bool(result.get("ok", False))
    suspicious = run_ok and bool(getattr(ctx, "last_test_suspicious_success", False))
    failed_blocks = _failed_block_labels(blocks)
    carrier_categories = carrier_backed_anti_bot_categories(data.get("failure_categories"))
    categories = _failure_categories(carrier_categories)
    terminal_challenge_categories = _trusted_terminal_challenge_categories(carrier_categories)
    run_status = _safe_str(data.get("overall_status"))
    workflow_run_id = _safe_str(data.get("workflow_run_id"))
    summary = _failure_summary(result, data, blocks)
    repair_context = _current_code_authoring_repair_context(data)
    root_cause_identity = _repair_context_root_cause_identity(repair_context) or compute_repair_root_cause_signature(
        failure_categories=categories,
        failure_reason=_safe_str(data.get("failure_reason")),
        error_texts=[_safe_str(result.get("error"))],
        blocks=[block for block in blocks if isinstance(block, dict)],
        detected_challenge=bool(getattr(ctx, "last_test_anti_bot", None)),
    )
    # Interactive authoring runs are classified from their run result. Completion
    # verification is retained only for unattended self-heal and cannot rewrite
    # the diagnosis or repair decision for this tool boundary.
    completion_verification = None
    completion_verification_failed = False
    failure_type = _failure_type(
        run_ok,
        suspicious,
        completion_verification_failed,
        failed_blocks,
        categories,
        terminal_challenge_categories,
        result,
        data,
        repair_context,
        challenge_runtime_clearable=run_challenge_is_runtime_clearable(ctx, run_id_from_result_data(data)),
    )
    next_action = _next_action(failure_type, ctx, data, repair_context)
    challenge = (
        _challenge_effects(ctx, blocks, categories, data)
        if categories or getattr(ctx, "last_test_anti_bot", None)
        else None
    )
    frontier = _safe_str(data.get("frontier_start_label"))
    target_blocks = failed_blocks or ([frontier] if frontier else []) if next_action == RepairNextAction.REPAIR else []
    if next_action == RepairNextAction.REPAIR and not target_blocks and repair_context is not None:
        target_blocks = [repair_context.block_label]
    user_goal_satisfied, completion_contract_satisfied = _verification_satisfaction(
        ctx,
        run_ok,
        suspicious,
        run_status,
        completion_verification,
        data,
        failure_type,
    )
    remaining_blocker = (
        None
        if (
            next_action == RepairNextAction.NO_CHANGE
            and user_goal_satisfied is True
            and completion_contract_satisfied is True
        )
        else summary or "Run did not pass."
    )
    confidence = (
        0.9
        if failure_type == DiagnosisFailureType.NO_FAILURE
        else 0.85
        if categories
        else 0.75
        if failed_blocks
        else 0.65
        if run_status
        else 0.2
        if failure_type == DiagnosisFailureType.UNKNOWN
        else 0.55
    )
    decision_summary = {
        RepairNextAction.NO_CHANGE: "No repair needed.",
        RepairNextAction.ASK: "Ask the user for the missing context before changing the workflow.",
        RepairNextAction.STOP: "Stop retrying the current failure and report the blocker.",
        RepairNextAction.ESCALATE: "Escalate because the current evidence is insufficient for an autonomous repair.",
    }.get(next_action, _safe_text(f"Repair the workflow based on: {summary}", _SUMMARY_MAX))
    if next_action == RepairNextAction.REPAIR and failure_type == DiagnosisFailureType.SUSPICIOUS_SUCCESS:
        decision_summary = "Repair the data-producing block so completion is proven by meaningful output."
    elif failure_type == DiagnosisFailureType.TERMINAL_CHALLENGE_BLOCKER:
        decision_summary = (
            "Stop retrying because structured evidence shows the workflow path is blocked by a site challenge."
        )
    if (
        next_action == RepairNextAction.NO_CHANGE
        and user_goal_satisfied is True
        and completion_contract_satisfied is True
    ):
        completion_check = "Current run already satisfies the goal."
    else:
        completion_check = {
            RepairNextAction.NO_CHANGE: "No repair selected; completion remains unverified.",
            RepairNextAction.ASK: "Resume diagnosis after the user supplies the missing context.",
            RepairNextAction.STOP: "Do not rerun unchanged; user-visible blocker must be resolved first.",
        }.get(
            next_action,
            f"Run repaired block labels and confirm success: {', '.join(target_blocks)}"
            if target_blocks
            else "Run the repaired workflow path and confirm the requested goal is satisfied.",
        )
    return DiagnosisRepairContract(
        diagnosis_input=DiagnosisInput(
            user_goal=_safe_text(_ctx_user_goal(ctx)),
            source_tool=source_tool,
            workflow_updated=workflow_updated,
            workflow_run_id=workflow_run_id,
            run_status=run_status,
            requested_block_labels=_str_list(data.get("requested_block_labels")),
            executed_block_labels=_str_list(data.get("executed_block_labels")),
            frontier_start_label=_safe_str(data.get("frontier_start_label")),
            failed_block_labels=failed_blocks,
            failure_categories=categories,
            browser_page_state=_browser_page_state(data),
        ),
        diagnosis_result=DiagnosisResult(
            suspected_failure_type=failure_type,
            root_cause_summary=summary,
            root_cause_identity=root_cause_identity,
            confidence=confidence,
            evidence_references=(
                ([f"workflow_run:{workflow_run_id}"] if workflow_run_id else [])
                + [f"failed_block:{label}" for label in failed_blocks[:_MAX_ITEMS]]
                + [f"failure_category:{category}" for category in categories[:_MAX_ITEMS]]
            ),
            missing_context=_missing_context(result, data, failure_type),
        ),
        repair_decision=RepairDecision(
            next_action=next_action,
            target_blocks=target_blocks,
            proposed_change_summary=decision_summary,
            required_authority=[],
            completion_check=completion_check,
        ),
        verification_result=VerificationResult(
            run_status=run_status,
            user_goal_satisfied=user_goal_satisfied,
            completion_contract_satisfied=completion_contract_satisfied,
            remaining_blocker=remaining_blocker,
        ),
        challenge=challenge,
        levers=_levers(ctx, challenge, failure_type, data, categories),
    )


def _solver_facts(blocks: list[Any], data: dict[str, Any]) -> tuple[str, str | None]:
    """Returns (result, failure_text) over failed / attempted / not_attempted / unresolved.

    A completed row means the solve step ran, not that the challenge cleared: the runtime's
    no-solver fallback also reports success. An absent action history means unresolved, not a
    non-attempt: the optional lookup swallows its own failures, and a later tool result in the
    same turn carries no run history at all."""
    """Prefer the record captured before the traces were stripped; fall back to any trace still present."""
    carried = data.get(SOLVER_ATTEMPT_KEY)
    if isinstance(carried, dict):
        result = str(carried.get("result") or "unresolved")
        if result not in {"failed", "attempted", "not_attempted", "unresolved"}:
            result = "unresolved"
        return result, _safe_text(_safe_str(carried.get("failure")), _SOLVER_FAILURE_MAX)
    attempted = False
    failed = False
    saw_history = False
    failure: str | None = None
    for block in blocks:
        trace = block.get("action_trace") if isinstance(block, dict) else None
        if not isinstance(trace, list):
            continue
        saw_history = True
        for entry in trace:
            if not isinstance(entry, dict) or entry.get("action") != ActionType.SOLVE_CAPTCHA.value:
                continue
            attempted = True
            if entry.get("status") == "failed":
                failed = True
                if failure is None:
                    failure = _safe_text(_safe_str(entry.get("response")), _SOLVER_FAILURE_MAX)
    if failed:
        return "failed", failure
    if attempted:
        return "attempted", failure
    return ("not_attempted" if saw_history else "unresolved"), failure


def _challenge_effects(
    ctx: CopilotContext, blocks: list[Any], categories: list[str], data: dict[str, Any]
) -> ChallengeEffects | None:
    """Observed solver facts for a run that met a wall; None when nothing on the run says challenge."""
    if not any(category in ANTI_BOT_CHALLENGE_ALIAS_CATEGORIES for category in categories) and not getattr(
        ctx, "last_test_anti_bot", None
    ):
        return None
    result, failure = _solver_facts(blocks, data)
    kind = typed_challenge_kind(getattr(ctx, "composition_page_evidence", None))
    return ChallengeEffects(
        kind=kind.value if kind is not None else None,
        solver_available=_solver_available_for_current_page(ctx, data),
        solver_attempted=None if result == "unresolved" else result in {"failed", "attempted"},
        solver_result=result,
        solver_failure=failure,
    )


def _levers(
    ctx: CopilotContext,
    challenge: ChallengeEffects | None,
    failure_type: DiagnosisFailureType,
    data: dict[str, Any],
    categories: list[str],
) -> list[Lever]:
    """Every product lever that exists for this wall, unordered, with availability read from existing state."""
    credential_shaped = failure_type == DiagnosisFailureType.MISSING_CREDENTIAL_OR_INIT and (
        _safe_str(data.get("skip_reason")) in _CREDENTIAL_INPUT_MISSING_SKIP_REASONS or "CREDENTIAL_ERROR" in categories
    )
    if challenge is None and not credential_shaped:
        return []
    policy = ctx.request_policy
    approved = bool(policy and (policy.resolved_credentials or policy.selected_connected_account_id))
    credential_lever = Lever(
        mechanism="credential_totp_or_inbox",
        knowledge_topic="login_block",
        availability="credential approved this chat" if approved else "no credential approved this chat",
    )
    human_lever = Lever(mechanism="human_interaction", knowledge_topic="human_interaction_block")
    if challenge is None:
        return [credential_lever, human_lever]
    solver_available = challenge.solver_available
    return [
        Lever(
            mechanism="captcha_solver",
            knowledge_topic="captcha_solver",
            availability=(
                "unresolved" if solver_available is None else ("available" if solver_available else "unavailable")
            ),
        ),
        Lever(mechanism="proxy_location", knowledge_topic="proxy_location", availability=_proxy_label(ctx)),
        Lever(mechanism="browser_profile", knowledge_topic="proxy_location"),
        credential_lever,
        human_lever,
    ]


def author_time_levers(ctx: CopilotContext) -> list[Lever]:
    """The same lever inventory for a challenge seen at author time, before any run exists."""
    challenge = ChallengeEffects(
        kind=(kind.value if (kind := typed_challenge_kind(getattr(ctx, "composition_page_evidence", None))) else None),
        solver_available=_solver_available_for_current_page(ctx),
    )
    return _levers(ctx, challenge, DiagnosisFailureType.UNKNOWN, {}, [])


def _solver_available_for_current_page(ctx: CopilotContext, data: dict[str, Any] | None = None) -> bool | None:
    """Availability is resolved per page, so a value resolved for another URL says nothing here.

    Post-run composition evidence is only stored under the code-only browser policy, so the run
    result's own URL stands in for it; without either URL the answer stays unresolved."""
    available = getattr(ctx, "captcha_solver_available", None)
    if available is None:
        return None
    resolved_for = getattr(ctx, "captcha_solver_available_for_url", None)
    evidence = getattr(ctx, "composition_page_evidence", None)
    current = (evidence.get("current_url") or evidence.get("inspected_url")) if isinstance(evidence, dict) else None
    if not current and isinstance(data, dict):
        current = _safe_str(data.get("current_url"))
    if not (resolved_for and current and resolved_for == current):
        return None
    # The remote browser vendor refuses a solver extension on a session with no proxy, so a
    # no-proxy run's browser may have had none regardless of the org-level answer.
    return None if available and _declares_no_proxy(ctx) else available


_NO_PROXY_VALUES = {"NONE", "NULL", "NO_PROXY"}


def _raw_proxy_location(ctx: CopilotContext) -> Any:
    raw = getattr(ctx, "effective_workflow_proxy_location", None)
    if raw is None:
        raw = getattr(getattr(ctx, "last_workflow", None), "proxy_location", None)
    return raw


def _declares_no_proxy(ctx: CopilotContext) -> bool:
    """True only when the run named a no-proxy location, never when it named nothing at all."""
    raw = _raw_proxy_location(ctx)
    if raw is None or isinstance(raw, dict):
        return False
    value = _safe_str(getattr(raw, "value", raw))
    return value is not None and value.upper() in _NO_PROXY_VALUES


def _proxy_label(ctx: CopilotContext) -> str | None:
    """A non-secret label for the run's proxy. A custom proxy is a dict whose URL can embed
    credentials, so it is named by shape and never serialized."""
    raw = _raw_proxy_location(ctx)
    if raw is None:
        return None
    if isinstance(raw, dict):
        return "custom proxy"
    country = getattr(raw, "country", None)
    if country is not None:
        parts = [str(country)] + [
            str(part) for part in (getattr(raw, "subdivision", None), getattr(raw, "city", None)) if part
        ]
        return "-".join(parts)
    value = _safe_str(getattr(raw, "value", raw))
    return None if value is None or value.upper() in _NO_PROXY_VALUES else value


def _dict(value: Any) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}


def _safe_str(value: Any) -> str | None:
    text = "" if value is None else str(value).strip()
    return text or None


def _safe_text(value: str | None, max_chars: int = _TEXT_MAX) -> str:
    text = redact_raw_secrets_for_prompt((value or "").strip())
    text = URL_CANDIDATE_RE.sub(lambda m: url_origin(m.group(0)) or "[URL]", text)
    return text if len(text) <= max_chars else text[: max_chars - 3].rstrip() + "..."


def _safe_identity_list(values: list[str]) -> list[str]:
    return sorted(dict.fromkeys(item for value in values for item in [_safe_text(str(value), 80)] if item))


def _identity_token(value: str) -> str:
    return re.sub(r"[^a-zA-Z0-9]+", "_", value.strip().lower()).strip("_") or "unknown"


def _repair_context_root_cause_identity(
    repair_context: CodeAuthoringRepairContext | None,
) -> RepairRootCauseIdentity | None:
    if repair_context is None:
        return None
    reason_code = _safe_text(repair_context.reason_code, 80)
    if not reason_code:
        return None
    block_label = _safe_text(repair_context.block_label, 80)
    selector = ""
    selector_kind = ""
    payload: dict[str, Any] = {
        "version": _AUTHORING_REPAIR_SIGNATURE_VERSION,
        "reason_code": reason_code,
        "block_label": block_label,
    }
    if reason_code == "ambiguous_bare_selector":
        selector = _safe_text(repair_context.selector, 120)
        refiner_selector = _safe_text(repair_context.refiner_selector, 120)
        payload["selector"] = selector
        payload["refiner_selector"] = refiner_selector
        selector_kind = "selector" if selector else ""
    elif reason_code == "runtime_block_failure":
        runtime_failure_class = _safe_text(repair_context.runtime_failure_class, 80)
        payload["runtime_failure_class"] = runtime_failure_class
        payload["failed_block_status"] = _safe_text(repair_context.failed_block_status, 80)
        payload["current_origin"] = _safe_text(repair_context.current_origin, 120)
        payload["page_evidence_source"] = _safe_text(repair_context.page_evidence_source, 80)
        payload["observed_after_workflow_run"] = repair_context.observed_after_workflow_run
        payload["page_form_summaries"] = _safe_identity_list(repair_context.page_form_summaries)
        payload["page_result_summaries"] = _safe_identity_list(repair_context.page_result_summaries)
        payload["page_action_summaries"] = _safe_identity_list(repair_context.page_action_summaries)
        payload["page_challenge_summaries"] = _safe_identity_list(repair_context.page_challenge_summaries)
        payload["page_obstruction_summaries"] = _safe_identity_list(repair_context.page_obstruction_summaries)
    elif reason_code == "runtime_missing_output_dependency":
        payload["missing_output_key"] = _safe_text(repair_context.missing_output_key, 120)
        payload["available_output_keys"] = _safe_identity_list(repair_context.available_output_keys)
        payload["current_block_parameter_keys"] = _safe_identity_list(repair_context.current_block_parameter_keys)
        payload["output_dependency_failure_class"] = _safe_text(repair_context.output_dependency_failure_class, 80)
    elif repair_context.unresolved_names:
        payload["unresolved_names"] = _safe_identity_list(repair_context.unresolved_names)

    error_class_suffix = _identity_token(reason_code)
    if reason_code == "runtime_block_failure" and repair_context.runtime_failure_class:
        error_class_suffix = f"{error_class_suffix}_{_identity_token(repair_context.runtime_failure_class)}"
    serialized = json.dumps(payload, sort_keys=True, separators=(",", ":"))
    return RepairRootCauseIdentity(
        root_cause_signature=hashlib.sha256(serialized.encode("utf-8")).hexdigest(),
        primary_category=_AUTHORING_REPAIR_CATEGORY,
        failure_categories=(_AUTHORING_REPAIR_CATEGORY,),
        error_class=f"code_authoring_{error_class_suffix}",
        selector_kind=selector_kind,
        selector=selector,
    )


def _str_list(value: Any) -> list[str]:
    return (
        [_safe_text(str(item), 80) for item in value[:_MAX_ITEMS] if str(item).strip()]
        if isinstance(value, list)
        else []
    )


def _ctx_user_goal(ctx: Any) -> str:
    user_message = getattr(ctx, "user_message", None)
    return user_message if isinstance(user_message, str) else ""


def _failure_categories(raw: list[Any]) -> list[str]:
    return list(
        dict.fromkeys(
            category
            for entry in raw[:_MAX_ITEMS]
            if isinstance(entry, dict)
            for category in [_safe_str(entry.get("category"))]
            if category
        )
    )


def _trusted_terminal_challenge_categories(raw: list[Any]) -> list[str]:
    return list(
        dict.fromkeys(
            category
            for entry in raw[:_MAX_ITEMS]
            if isinstance(entry, dict)
            for category in [trusted_terminal_challenge_category_name(entry)]
            if category
        )
    )


def _capped_blocks(raw_blocks: list[Any]) -> list[Any]:
    """At most ``_MAX_ITEMS`` rows: the newest failures preceding the tail, then the tail itself.
    Rows arrive chronologically, so a plain tail keeps the block the run stopped on but loses an
    earlier failure the contract exists to name; a plain head does the reverse. A run that fails
    many blocks must not grow this list — it feeds a model prompt."""
    if len(raw_blocks) <= _MAX_ITEMS:
        return list(raw_blocks)
    early = [
        block
        for block in raw_blocks[:-_MAX_ITEMS]
        if isinstance(block, dict) and str(block.get("status") or "").lower() in _FAILED_STATUSES
    ]
    reserved = early[-_EARLY_FAILURE_MAX_ITEMS:]
    return reserved + raw_blocks[len(reserved) - _MAX_ITEMS :]


def _failed_block_labels(blocks: list[Any]) -> list[str]:
    return list(
        dict.fromkeys(
            label
            for block in blocks
            if isinstance(block, dict) and str(block.get("status") or "").lower() in _FAILED_STATUSES
            for label in [_safe_str(block.get("label"))]
            if label
        )
    )


def _failure_summary(result: dict[str, Any], data: dict[str, Any], blocks: list[Any]) -> str:
    candidates = [result.get("error"), result.get("message"), data.get("failure_reason")]
    candidates += [block.get("failure_reason") for block in blocks if isinstance(block, dict)]
    for candidate in candidates:
        if isinstance(candidate, str) and candidate.strip():
            return _safe_text(candidate, _SUMMARY_MAX)
    return "Run completed." if bool(result.get("ok", False)) else "No failure reason was provided."


def _failure_type(
    run_ok: bool,
    suspicious: bool,
    completion_verification_failed: bool,
    failed_blocks: list[str],
    categories: list[str],
    terminal_challenge_categories: list[str],
    result: dict[str, Any],
    data: dict[str, Any],
    repair_context: CodeAuthoringRepairContext | None,
    challenge_runtime_clearable: bool = False,
) -> DiagnosisFailureType:
    # A paused run is waiting on a person, not failing: classifying it as a failure would tell the
    # model to repair a workflow whose only problem is that nobody has approved it yet.
    if (data.get("control_signal") or {}).get("kind") == "watchdog_paused":
        return DiagnosisFailureType.NO_FAILURE
    skip_reason = _safe_str(data.get("skip_reason"))
    if skip_reason in _CREDENTIAL_INPUT_MISSING_SKIP_REASONS:
        return DiagnosisFailureType.MISSING_CREDENTIAL_OR_INIT
    # Trusted challenge categories win over a clean-looking run status because
    # verified challenge evidence means the apparent success is not usable.
    if terminal_challenge_categories and not challenge_runtime_clearable:
        return DiagnosisFailureType.TERMINAL_CHALLENGE_BLOCKER
    category_set = set(categories)
    error_text = " ".join(
        str(value).lower()
        for value in (result.get("error"), data.get("failure_reason"), data.get("skip_reason"))
        if value
    )
    if (
        "UNRECOVERABLE_TOOL_ERROR" in categories
        or "browser session not found" in error_text
        or "no browser context" in error_text
        or ("session not found" in error_text and "browser" in error_text)
        or ("404" in error_text and "browser session" in error_text)
    ):
        return DiagnosisFailureType.UNRECOVERABLE_TOOL_ERROR
    if (
        category_set & _PRE_RUN_CREDENTIAL_FAILURE_CATEGORIES
        or "organization not found" in error_text
        or "workflow not found" in error_text
        or "browser session" in error_text
    ):
        return DiagnosisFailureType.MISSING_CREDENTIAL_OR_INIT
    if _safe_str(data.get("failure_type")) == "missing_credential_or_init":
        return DiagnosisFailureType.MISSING_CREDENTIAL_OR_INIT
    if repair_context is not None:
        return DiagnosisFailureType.REPAIRABLE_BLOCK_FAILURE
    if failed_blocks:
        return DiagnosisFailureType.REPAIRABLE_BLOCK_FAILURE
    if category_set & _REPAIRABLE_RUNTIME_CATEGORIES:
        return DiagnosisFailureType.REPAIRABLE_BLOCK_FAILURE
    if completion_verification_failed:
        return DiagnosisFailureType.SUSPICIOUS_SUCCESS
    if run_ok:
        return DiagnosisFailureType.SUSPICIOUS_SUCCESS if suspicious else DiagnosisFailureType.NO_FAILURE
    if "credential" in error_text:
        return DiagnosisFailureType.MISSING_CREDENTIAL_OR_INIT
    return DiagnosisFailureType.FAILED_RUN if result.get("ok") is False else DiagnosisFailureType.UNKNOWN


def _next_action(
    failure_type: DiagnosisFailureType,
    ctx: CopilotContext,
    data: dict[str, Any],
    repair_context: CodeAuthoringRepairContext | None,
) -> RepairNextAction:
    if failure_type == DiagnosisFailureType.NO_FAILURE:
        return RepairNextAction.NO_CHANGE
    if failure_type == DiagnosisFailureType.UNRECOVERABLE_TOOL_ERROR:
        return RepairNextAction.STOP
    if failure_type == DiagnosisFailureType.TERMINAL_CHALLENGE_BLOCKER:
        return RepairNextAction.STOP
    if ctx.last_test_non_retriable_nav_error:
        return RepairNextAction.STOP
    if (
        _last_test_anti_bot_is_terminal(ctx)
        and not run_challenge_is_runtime_clearable(ctx, run_id_from_result_data(data))
        and failure_type
        in {
            DiagnosisFailureType.FAILED_RUN,
            DiagnosisFailureType.REPAIRABLE_BLOCK_FAILURE,
            DiagnosisFailureType.SUSPICIOUS_SUCCESS,
        }
    ):
        return RepairNextAction.STOP
    if repair_context is not None:
        return RepairNextAction.REPAIR
    if (
        data.get("skip_reason") == "workflow_credential_inputs_unbound"
        or failure_type == DiagnosisFailureType.MISSING_CREDENTIAL_OR_INIT
    ):
        return RepairNextAction.ASK
    if failure_type in {
        DiagnosisFailureType.REPAIRABLE_BLOCK_FAILURE,
        DiagnosisFailureType.SUSPICIOUS_SUCCESS,
        DiagnosisFailureType.FAILED_RUN,
    }:
        return RepairNextAction.REPAIR
    return RepairNextAction.ESCALATE


def _current_code_authoring_repair_context(data: dict[str, Any]) -> CodeAuthoringRepairContext | None:
    raw_context = data.get("authoring_repair_context")
    if not isinstance(raw_context, dict):
        return None
    try:
        return CodeAuthoringRepairContext.model_validate(raw_context)
    except ValidationError:
        return None


def _last_test_anti_bot_is_terminal(ctx: CopilotContext) -> bool:
    anti_bot_reason = getattr(ctx, "last_test_anti_bot", None)
    if not isinstance(anti_bot_reason, str) or not anti_bot_reason.strip():
        return False

    evidence = getattr(ctx, "composition_page_evidence", None)
    if isinstance(evidence, dict) and evidence.get("observed_after_workflow_run") is True:
        challenge_state = evidence.get("challenge_state")
        if isinstance(challenge_state, dict) and (
            challenge_state.get("requires_human_verification") is True
            or challenge_state.get("gates_submit_controls") is True
        ):
            return True
        controls = evidence.get("challenge_controls")
        if isinstance(controls, list) and interactive_challenge_controls(controls):
            return True

    return False


def _verification_satisfaction(
    ctx: CopilotContext,
    run_ok: bool,
    suspicious: bool,
    run_status: str | None,
    completion_verification: CompletionVerificationResult | None = None,
    data: dict[str, Any] | None = None,
    failure_type: DiagnosisFailureType | None = None,
) -> tuple[bool | None, bool | None]:
    if failure_type == DiagnosisFailureType.TERMINAL_CHALLENGE_BLOCKER:
        return False, False
    if failure_type == DiagnosisFailureType.MISSING_CREDENTIAL_OR_INIT:
        return False, False
    if failure_type in {
        DiagnosisFailureType.REPAIRABLE_BLOCK_FAILURE,
        DiagnosisFailureType.FAILED_RUN,
        DiagnosisFailureType.UNRECOVERABLE_TOOL_ERROR,
    }:
        return False, False
    user_goal_satisfied = (not suspicious) if run_ok else None if run_status is None else False
    return user_goal_satisfied, user_goal_satisfied


def _missing_context(result: dict[str, Any], data: dict[str, Any], failure_type: DiagnosisFailureType) -> list[str]:
    missing: list[str] = []
    if data.get("workflow_run_id") is None and failure_type not in {
        DiagnosisFailureType.NO_FAILURE,
        DiagnosisFailureType.MISSING_CREDENTIAL_OR_INIT,
    }:
        missing.append("workflow_run_id")
    if not data.get("blocks") and failure_type == DiagnosisFailureType.FAILED_RUN:
        missing.append("block_results")
    if not result.get("error") and not data.get("failure_reason") and failure_type != DiagnosisFailureType.NO_FAILURE:
        missing.append("failure_reason")
    return missing


def _browser_page_state(data: dict[str, Any]) -> dict[str, Any]:
    raw_url = _safe_str(data.get("current_url"))
    return {
        "current_origin": url_origin(raw_url) if raw_url else None,
        "has_current_url": bool(raw_url),
        "has_page_title": bool(_safe_str(data.get("page_title"))),
    }
