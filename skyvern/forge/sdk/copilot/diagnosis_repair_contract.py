from __future__ import annotations

from enum import StrEnum
from typing import TYPE_CHECKING, Any

from pydantic import BaseModel, ConfigDict, Field

from skyvern.forge.sdk.copilot.composition_evidence import interactive_challenge_controls
from skyvern.forge.sdk.copilot.failure_tracking import (
    ACTIVE_RUN_TERMINAL_EVIDENCE_FAILURE_CATEGORY,
    RepairRootCauseIdentity,
    compute_repair_root_cause_signature,
)
from skyvern.forge.sdk.copilot.output_policy import url_origin
from skyvern.forge.sdk.copilot.request_policy import redact_raw_secrets_for_prompt
from skyvern.forge.sdk.copilot.run_outcome import trusted_terminal_challenge_category_name
from skyvern.forge.sdk.copilot.terminal_predicates import outcome_fully_verified
from skyvern.forge.sdk.copilot.workflow_credential_utils import URL_CANDIDATE_RE

if TYPE_CHECKING:
    from skyvern.forge.sdk.copilot.completion_verification import CompletionVerificationResult
    from skyvern.forge.sdk.copilot.context import CopilotContext

_TEXT_MAX = 240
_SUMMARY_MAX = 180
_MAX_ITEMS = 20
_FAILED_STATUSES = {"failed", "terminated", "canceled", "timed_out"}
_CREDENTIAL_INPUT_MISSING_SKIP_REASONS = {"workflow_credential_inputs_unbound", "credential_name_unresolved"}
_PRE_RUN_CREDENTIAL_FAILURE_CATEGORIES = {"CREDENTIAL_ERROR", "PARAMETER_BINDING_ERROR"}
_REPAIRABLE_RUNTIME_CATEGORIES = {"AUTH_FAILURE", "OUTCOME_UNVERIFIED"}
_TERMINAL_ANTI_BOT_TERMS = ("captcha", "challenge", "verification", "anti-bot", "anti bot", "turnstile")


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class DiagnosisFailureType(StrEnum):
    NO_FAILURE = "no_failure"
    FAILED_RUN = "failed_run"
    SUSPICIOUS_SUCCESS = "suspicious_success"
    TERMINAL_CHALLENGE_BLOCKER = "terminal_challenge_blocker"
    MISSING_CREDENTIAL_OR_INIT = "missing_credential_or_init"
    REPAIRABLE_BLOCK_FAILURE = "repairable_block_failure"
    ACTIVE_RUN_TERMINAL_EVIDENCE = "active_run_terminal_evidence"
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


class RepairLoopState(StrictModel):
    streak_token: str | None = None
    consecutive_identical_repair_count: int = 0
    ceiling_reached: bool = False


class DiagnosisRepairContract(StrictModel):
    diagnosis_input: DiagnosisInput
    diagnosis_result: DiagnosisResult
    repair_decision: RepairDecision
    verification_result: VerificationResult
    repair_loop_state: RepairLoopState = Field(default_factory=RepairLoopState)

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
            "consecutive_identical_repair_count": self.repair_loop_state.consecutive_identical_repair_count,
            "ceiling_reached": self.repair_loop_state.ceiling_reached,
            "streak_token": self.repair_loop_state.streak_token,
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
    blocks: list[Any] = raw_blocks[:_MAX_ITEMS] if isinstance(raw_blocks, list) else []

    run_ok = bool(result.get("ok", False))
    suspicious = run_ok and bool(getattr(ctx, "last_test_suspicious_success", False))
    failed_blocks = _failed_block_labels(blocks)
    categories = _failure_categories(data)
    terminal_challenge_categories = _trusted_terminal_challenge_categories(data)
    run_status = _safe_str(data.get("overall_status"))
    workflow_run_id = _safe_str(data.get("workflow_run_id"))
    summary = _failure_summary(result, data, blocks)
    root_cause_identity = compute_repair_root_cause_signature(
        failure_categories=categories,
        failure_reason=_safe_str(data.get("failure_reason")),
        error_texts=[_safe_str(result.get("error"))],
        blocks=[block for block in blocks if isinstance(block, dict)],
        detected_challenge=bool(getattr(ctx, "last_test_anti_bot", None)),
    )
    outcome_verified = outcome_fully_verified(ctx)
    completion_verification = getattr(ctx, "completion_verification_result", None)
    completion_verification_failed = _completion_verification_failed(completion_verification)
    failure_type = _failure_type(
        run_ok,
        suspicious,
        outcome_verified,
        completion_verification_failed,
        failed_blocks,
        categories,
        terminal_challenge_categories,
        result,
        data,
    )
    next_action = _next_action(failure_type, ctx, data)
    frontier = _safe_str(data.get("frontier_start_label"))
    target_blocks = failed_blocks or ([frontier] if frontier else []) if next_action == RepairNextAction.REPAIR else []
    user_goal_satisfied, completion_contract_satisfied = _verification_satisfaction(
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
        RepairNextAction.ASK: "Ask the user for the missing authority or context before changing the workflow.",
        RepairNextAction.STOP: "Stop retrying the current failure and report the blocker.",
        RepairNextAction.ESCALATE: "Escalate because the current evidence is insufficient for an autonomous repair.",
    }.get(next_action, _safe_text(f"Repair the workflow based on: {summary}", _SUMMARY_MAX))
    if next_action == RepairNextAction.REPAIR and failure_type == DiagnosisFailureType.SUSPICIOUS_SUCCESS:
        decision_summary = "Repair the data-producing block so completion is proven by meaningful output."
    elif failure_type == DiagnosisFailureType.TERMINAL_CHALLENGE_BLOCKER:
        decision_summary = (
            "Stop retrying because structured evidence shows the workflow path is blocked by a site challenge."
        )
    elif failure_type == DiagnosisFailureType.ACTIVE_RUN_TERMINAL_EVIDENCE:
        decision_summary = (
            "Stop the current retry loop: the active run reached the requested browser state, "
            "but the reusable workflow is not verified end-to-end."
        )
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
            required_authority=required_authority,
            completion_check=completion_check,
        ),
        verification_result=VerificationResult(
            run_status=run_status,
            user_goal_satisfied=user_goal_satisfied,
            completion_contract_satisfied=completion_contract_satisfied,
            remaining_blocker=remaining_blocker,
        ),
    )


def _dict(value: Any) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}


def _safe_str(value: Any) -> str | None:
    text = "" if value is None else str(value).strip()
    return text or None


def _safe_text(value: str | None, max_chars: int = _TEXT_MAX) -> str:
    text = redact_raw_secrets_for_prompt((value or "").strip())
    text = URL_CANDIDATE_RE.sub(lambda m: url_origin(m.group(0)) or "[URL]", text)
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


def _trusted_terminal_challenge_categories(data: dict[str, Any]) -> list[str]:
    raw = data.get("failure_categories")
    if not isinstance(raw, list):
        return []
    return list(
        dict.fromkeys(
            category
            for entry in raw[:_MAX_ITEMS]
            if isinstance(entry, dict)
            for category in [trusted_terminal_challenge_category_name(entry)]
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
    candidates = [result.get("error"), result.get("message"), data.get("failure_reason")]
    candidates += [block.get("failure_reason") for block in blocks if isinstance(block, dict)]
    for candidate in candidates:
        if isinstance(candidate, str) and candidate.strip():
            return _safe_text(candidate, _SUMMARY_MAX)
    return "Run completed." if bool(result.get("ok", False)) else "No failure reason was provided."


def _failure_type(
    run_ok: bool,
    suspicious: bool,
    outcome_verified: bool,
    completion_verification_failed: bool,
    failed_blocks: list[str],
    categories: list[str],
    terminal_challenge_categories: list[str],
    result: dict[str, Any],
    data: dict[str, Any],
) -> DiagnosisFailureType:
    skip_reason = _safe_str(data.get("skip_reason"))
    if skip_reason in _CREDENTIAL_INPUT_MISSING_SKIP_REASONS:
        return DiagnosisFailureType.MISSING_CREDENTIAL_OR_INIT
    # Trusted challenge categories win over a clean-looking run status because
    # verified challenge evidence means the apparent success is not usable.
    if terminal_challenge_categories:
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
        ACTIVE_RUN_TERMINAL_EVIDENCE_FAILURE_CATEGORY in categories
        or data.get("active_run_terminal_evidence_detected") is True
    ):
        return DiagnosisFailureType.ACTIVE_RUN_TERMINAL_EVIDENCE
    if (
        category_set & _PRE_RUN_CREDENTIAL_FAILURE_CATEGORIES
        or "organization not found" in error_text
        or "workflow not found" in error_text
        or "browser session" in error_text
    ):
        return DiagnosisFailureType.MISSING_CREDENTIAL_OR_INIT
    if outcome_verified:
        return DiagnosisFailureType.NO_FAILURE
    if failed_blocks or category_set & _REPAIRABLE_RUNTIME_CATEGORIES:
        return DiagnosisFailureType.REPAIRABLE_BLOCK_FAILURE
    if completion_verification_failed:
        return DiagnosisFailureType.SUSPICIOUS_SUCCESS
    if run_ok:
        return DiagnosisFailureType.SUSPICIOUS_SUCCESS if suspicious else DiagnosisFailureType.NO_FAILURE
    if "credential" in error_text:
        return DiagnosisFailureType.MISSING_CREDENTIAL_OR_INIT
    return DiagnosisFailureType.FAILED_RUN if result.get("ok") is False else DiagnosisFailureType.UNKNOWN


def _next_action(failure_type: DiagnosisFailureType, ctx: CopilotContext, data: dict[str, Any]) -> RepairNextAction:
    if failure_type == DiagnosisFailureType.NO_FAILURE:
        return RepairNextAction.NO_CHANGE
    if (
        data.get("skip_reason") == "workflow_credential_inputs_unbound"
        or failure_type == DiagnosisFailureType.MISSING_CREDENTIAL_OR_INIT
    ):
        return RepairNextAction.ASK
    if failure_type == DiagnosisFailureType.UNRECOVERABLE_TOOL_ERROR:
        return RepairNextAction.STOP
    if failure_type == DiagnosisFailureType.ACTIVE_RUN_TERMINAL_EVIDENCE:
        return RepairNextAction.STOP
    if failure_type == DiagnosisFailureType.TERMINAL_CHALLENGE_BLOCKER:
        return RepairNextAction.STOP
    if ctx.last_test_non_retriable_nav_error:
        return RepairNextAction.STOP
    if _last_test_anti_bot_is_terminal(ctx, data) and failure_type in {
        DiagnosisFailureType.FAILED_RUN,
        DiagnosisFailureType.REPAIRABLE_BLOCK_FAILURE,
        DiagnosisFailureType.SUSPICIOUS_SUCCESS,
    }:
        return RepairNextAction.STOP
    if outcome_fully_verified(ctx):
        return RepairNextAction.NO_CHANGE
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


def _last_test_anti_bot_is_terminal(ctx: CopilotContext, data: dict[str, Any]) -> bool:
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

    failure_reason = getattr(ctx, "last_test_failure_reason", None)
    if not isinstance(failure_reason, str) or not failure_reason.strip():
        failure_reason = _safe_str(data.get("failure_reason"))
    reason_lower = anti_bot_reason.lower()
    failure_lower = str(failure_reason or "").lower()
    combined = f"{reason_lower} {failure_lower}"
    has_challenge_term = any(term in combined for term in _TERMINAL_ANTI_BOT_TERMS)
    if not has_challenge_term:
        return False
    if "challenge-gated disabled submit/search control" in reason_lower and "disabled" in failure_lower:
        return True
    return ("blocker" in combined or "blocked" in combined) and (
        "verify" in combined or "human" in combined or "captcha" in combined or "challenge" in combined
    )


def _verification_satisfaction(
    run_ok: bool,
    suspicious: bool,
    run_status: str | None,
    completion_verification: CompletionVerificationResult | None = None,
    data: dict[str, Any] | None = None,
    failure_type: DiagnosisFailureType | None = None,
) -> tuple[bool | None, bool | None]:
    if failure_type == DiagnosisFailureType.TERMINAL_CHALLENGE_BLOCKER:
        return False, False
    if isinstance(data, dict) and data.get("active_run_terminal_evidence_detected") is True:
        trace = data.get("active_run_terminal_completion_verification")
        fully_satisfied = isinstance(trace, dict) and trace.get("fully_satisfied") is True
        return fully_satisfied, fully_satisfied
    if failure_type == DiagnosisFailureType.MISSING_CREDENTIAL_OR_INIT:
        return False, False
    if (
        failure_type == DiagnosisFailureType.NO_FAILURE
        and completion_verification is not None
        and completion_verification.is_fully_satisfied()
    ):
        return True, True
    if completion_verification is not None and not completion_verification.is_fully_satisfied():
        return False, False
    user_goal_satisfied = (not suspicious) if run_ok else None if run_status is None else False
    # When the verification judge was required (a non-null result), its verdict
    # is authoritative: an unmet or unavailable verdict cannot claim the outcome.
    # With no result (no criteria, judge skipped) fall back to run trust.
    if completion_verification is not None:
        completion_contract_satisfied: bool | None = completion_verification.is_fully_satisfied()
    else:
        completion_contract_satisfied = user_goal_satisfied
    return user_goal_satisfied, completion_contract_satisfied


def _completion_verification_failed(completion_verification: CompletionVerificationResult | None) -> bool:
    return completion_verification is not None and not completion_verification.is_fully_satisfied()


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
        "probable_site_block_streak_count",
        "per_tool_budget_nudge_count",
        "repeated_action_fingerprint_streak_count",
    )
    return {key: int(getattr(ctx, key, 0) or 0) for key in keys}
