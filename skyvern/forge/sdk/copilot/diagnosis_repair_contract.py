from __future__ import annotations

import re
from enum import StrEnum
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from skyvern.forge.sdk.copilot.output_policy import url_origin
from skyvern.forge.sdk.copilot.request_policy import redact_raw_secrets_for_prompt

_TEXT_MAX = 240
_SUMMARY_MAX = 180
_MAX_ITEMS = 20
_FAILED_STATUSES = {"failed", "terminated", "canceled", "timed_out"}
_URL_CANDIDATE_RE = re.compile(r"https?://[^\s)>,]+")


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class DiagnosisFailureType(StrEnum):
    NO_FAILURE = "no_failure"
    FAILED_RUN = "failed_run"
    SUSPICIOUS_SUCCESS = "suspicious_success"
    MISSING_CREDENTIAL_OR_INIT = "missing_credential_or_init"
    REPAIRABLE_BLOCK_FAILURE = "repairable_block_failure"
    UNKNOWN = "unknown"


class RepairNextAction(StrEnum):
    REPAIR = "repair"
    ASK = "ask"
    STOP = "stop"
    ESCALATE = "escalate"
    NO_CHANGE = "no_change"


class DiagnosisInput(StrictModel):
    user_goal: str = ""
    turn_intent: dict[str, Any] = Field(default_factory=dict)
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
    prior_repair_attempts: dict[str, int] = Field(default_factory=dict)


class DiagnosisResult(StrictModel):
    suspected_failure_type: DiagnosisFailureType = DiagnosisFailureType.UNKNOWN
    root_cause_summary: str = ""
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

    def to_trace_data(self) -> dict[str, Any]:
        return {
            "failure_type": self.diagnosis_result.suspected_failure_type.value,
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
        }


def build_diagnosis_repair_contract(
    *,
    source_tool: str,
    result: dict[str, Any],
    ctx: Any,
    workflow_updated: bool = False,
) -> DiagnosisRepairContract:
    data = _dict(result.get("data")) if isinstance(result, dict) else {}
    raw_blocks = data.get("blocks")
    blocks: list[Any] = raw_blocks[:_MAX_ITEMS] if isinstance(raw_blocks, list) else []

    run_ok = bool(result.get("ok", False))
    suspicious = run_ok and bool(getattr(ctx, "last_test_suspicious_success", False))
    failed_blocks = _failed_block_labels(blocks)
    categories = _failure_categories(data)
    run_status = _safe_str(data.get("overall_status"))
    workflow_run_id = _safe_str(data.get("workflow_run_id"))
    summary = _failure_summary(result, data, blocks)
    failure_type = _failure_type(run_ok, suspicious, failed_blocks, categories, result, data)
    next_action = _next_action(failure_type, ctx, data)
    frontier = _safe_str(data.get("frontier_start_label"))
    target_blocks = failed_blocks or ([frontier] if frontier else []) if next_action == RepairNextAction.REPAIR else []
    user_goal_satisfied, completion_contract_satisfied = _verification_satisfaction(run_ok, suspicious, run_status)
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
        RepairNextAction.ASK: "Ask the user for the missing authority or context before changing the workflow.",
        RepairNextAction.STOP: "Stop retrying the current failure and report the blocker.",
        RepairNextAction.ESCALATE: "Escalate because the current evidence is insufficient for an autonomous repair.",
    }.get(next_action, _safe_text(f"Repair the workflow based on: {summary}", _SUMMARY_MAX))
    if next_action == RepairNextAction.REPAIR and failure_type == DiagnosisFailureType.SUSPICIOUS_SUCCESS:
        decision_summary = "Repair the data-producing block so completion is proven by meaningful output."
    completion_check = {
        RepairNextAction.NO_CHANGE: "Current run already satisfies the goal.",
        RepairNextAction.ASK: "Resume diagnosis after the user supplies the missing context.",
        RepairNextAction.STOP: "Do not rerun unchanged; user-visible blocker must be resolved first.",
    }.get(
        next_action,
        f"Run repaired block labels and confirm success: {', '.join(target_blocks)}"
        if target_blocks
        else "Run the repaired workflow path and confirm the requested goal is satisfied.",
    )
    required_authority: list[str] = []
    if next_action == RepairNextAction.ASK:
        required_authority = ["may_answer_without_mutation"]
    elif next_action == RepairNextAction.REPAIR:
        authority = getattr(getattr(ctx, "turn_intent", None), "authority", None)
        required_authority = ["may_update_workflow"] + (
            ["may_run_blocks"] if getattr(authority, "may_run_blocks", True) else []
        )

    return DiagnosisRepairContract(
        diagnosis_input=DiagnosisInput(
            user_goal=_safe_text(_ctx_user_goal(ctx)),
            turn_intent=_turn_intent_summary(ctx),
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
            prior_repair_attempts=_prior_repair_attempts(ctx),
        ),
        diagnosis_result=DiagnosisResult(
            suspected_failure_type=failure_type,
            root_cause_summary=summary,
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
            required_authority=required_authority,
            completion_check=completion_check,
        ),
        verification_result=VerificationResult(
            run_status=run_status,
            user_goal_satisfied=user_goal_satisfied,
            completion_contract_satisfied=completion_contract_satisfied,
            remaining_blocker=None if run_ok and not suspicious else summary or "Run did not pass.",
        ),
    )


def _dict(value: Any) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}


def _safe_str(value: Any) -> str | None:
    text = "" if value is None else str(value).strip()
    return text or None


def _safe_text(value: str | None, max_chars: int = _TEXT_MAX) -> str:
    text = redact_raw_secrets_for_prompt((value or "").strip())
    text = _URL_CANDIDATE_RE.sub(lambda m: url_origin(m.group(0)) or "[URL]", text)
    return text if len(text) <= max_chars else text[: max_chars - 3].rstrip() + "..."


def _str_list(value: Any) -> list[str]:
    return (
        [_safe_text(str(item), 80) for item in value[:_MAX_ITEMS] if str(item).strip()]
        if isinstance(value, list)
        else []
    )


def _ctx_user_goal(ctx: Any) -> str:
    intent = getattr(ctx, "turn_intent", None)
    goal = getattr(intent, "user_goal", None)
    if isinstance(goal, str) and goal.strip():
        return goal
    user_message = getattr(ctx, "user_message", None)
    return user_message if isinstance(user_message, str) else ""


def _turn_intent_summary(ctx: Any) -> dict[str, Any]:
    to_trace_data = getattr(getattr(ctx, "turn_intent", None), "to_trace_data", None)
    if not callable(to_trace_data):
        return {}
    try:
        return dict(to_trace_data())
    except Exception:
        return {}


def _failure_categories(data: dict[str, Any]) -> list[str]:
    raw = data.get("failure_categories")
    if not isinstance(raw, list):
        return []
    return list(
        dict.fromkeys(
            category
            for entry in raw[:_MAX_ITEMS]
            if isinstance(entry, dict)
            for category in [_safe_str(entry.get("category"))]
            if category
        )
    )


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
    candidates = [result.get("error"), data.get("failure_reason")]
    candidates += [block.get("failure_reason") for block in blocks if isinstance(block, dict)]
    for candidate in candidates:
        if isinstance(candidate, str) and candidate.strip():
            return _safe_text(candidate, _SUMMARY_MAX)
    return "Run completed." if bool(result.get("ok", False)) else "No failure reason was provided."


def _failure_type(
    run_ok: bool,
    suspicious: bool,
    failed_blocks: list[str],
    categories: list[str],
    result: dict[str, Any],
    data: dict[str, Any],
) -> DiagnosisFailureType:
    if run_ok:
        return DiagnosisFailureType.SUSPICIOUS_SUCCESS if suspicious else DiagnosisFailureType.NO_FAILURE
    error_text = " ".join(
        str(value).lower()
        for value in (result.get("error"), data.get("failure_reason"), data.get("skip_reason"))
        if value
    )
    if (
        data.get("skip_reason") == "workflow_credential_inputs_unbound"
        or "credential" in error_text
        or "organization not found" in error_text
        or "workflow not found" in error_text
        or "browser session" in error_text
        or "PARAMETER_BINDING_ERROR" in categories
    ):
        return DiagnosisFailureType.MISSING_CREDENTIAL_OR_INIT
    if failed_blocks:
        return DiagnosisFailureType.REPAIRABLE_BLOCK_FAILURE
    return DiagnosisFailureType.FAILED_RUN if result.get("ok") is False else DiagnosisFailureType.UNKNOWN


def _next_action(failure_type: DiagnosisFailureType, ctx: Any, data: dict[str, Any]) -> RepairNextAction:
    if failure_type == DiagnosisFailureType.NO_FAILURE:
        return RepairNextAction.NO_CHANGE
    if (
        data.get("skip_reason") == "workflow_credential_inputs_unbound"
        or failure_type == DiagnosisFailureType.MISSING_CREDENTIAL_OR_INIT
    ):
        return RepairNextAction.ASK
    if getattr(ctx, "last_test_non_retriable_nav_error", None):
        return RepairNextAction.STOP
    authority = getattr(getattr(ctx, "turn_intent", None), "authority", None)
    if getattr(authority, "requires_user_input", False) or getattr(authority, "may_update_workflow", True) is False:
        return RepairNextAction.ASK
    if failure_type in {
        DiagnosisFailureType.REPAIRABLE_BLOCK_FAILURE,
        DiagnosisFailureType.SUSPICIOUS_SUCCESS,
        DiagnosisFailureType.FAILED_RUN,
    }:
        return RepairNextAction.REPAIR
    return RepairNextAction.ESCALATE


def _verification_satisfaction(
    run_ok: bool,
    suspicious: bool,
    run_status: str | None,
) -> tuple[bool | None, bool | None]:
    user_goal_satisfied = (not suspicious) if run_ok else None if run_status is None else False
    # Current tool evidence has one completion signal: whether the tested run
    # can be trusted. Keep these fields separate for future richer contracts,
    # but mirror them until an explicit completion-contract signal exists.
    completion_contract_satisfied = user_goal_satisfied
    return user_goal_satisfied, completion_contract_satisfied


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


def _prior_repair_attempts(ctx: Any) -> dict[str, int]:
    keys = (
        "repeated_failure_streak_count",
        "failed_test_nudge_count",
        "null_data_streak_count",
        "probable_site_block_streak_count",
        "per_tool_budget_nudge_count",
        "repeated_action_fingerprint_streak_count",
    )
    return {key: int(getattr(ctx, key, 0) or 0) for key in keys}
