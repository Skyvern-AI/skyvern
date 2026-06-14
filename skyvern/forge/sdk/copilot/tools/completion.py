import time
from typing import Any

import structlog

from skyvern.config import settings
from skyvern.forge.sdk.copilot.completion_criteria_store import note_adjudication_on_turn_state
from skyvern.forge.sdk.copilot.completion_verification import (
    CompletionVerificationResult,
    CriterionVerdict,
    RunEvidenceSnapshot,
    combine_verification_results,
    evaluate_completion_criteria,
    grade_definition_criteria,
    resolve_unknown,
    summarize_unsatisfied_outcomes,
)
from skyvern.forge.sdk.copilot.enforcement import _goal_likely_needs_more_blocks
from skyvern.forge.sdk.copilot.llm_config import resolve_main_copilot_handler
from skyvern.forge.sdk.copilot.outcome_verification_trace import record_completion_verification
from skyvern.forge.sdk.copilot.request_policy import CompletionCriterion
from skyvern.forge.sdk.copilot.tracing_setup import copilot_span

from ._shared import (
    RUN_BLOCKS_SAFETY_CEILING_SECONDS,
    _copilot_seconds_remaining,
    _current_workflow_block_labels,
    _current_workflow_has_evidence_block,
    _is_meaningful_extracted_data,
    _valid_runtime_anchor_url,
)
from .blockers import (
    _active_run_terminal_evidence_detected,
    _analyze_run_blocks,
    _run_blocks_structured_blocker_message,
)

LOG = structlog.get_logger()


def _completion_verification_criteria(copilot_ctx: Any) -> list[Any]:
    policy = getattr(copilot_ctx, "request_policy", None)
    # A method-mandated criterion asserts HOW the goal was reached; the outcome
    # judge sees only end-state evidence.
    return [c for c in (policy.completion_criteria if policy is not None else []) if not c.method_mandated]


def _split_criteria_by_plane(criteria: list[Any]) -> tuple[list[CompletionCriterion], list[CompletionCriterion]]:
    if not settings.COPILOT_PERSISTED_COMPLETION_CRITERIA_ENABLED:
        return list(criteria), []
    run_criteria = [c for c in criteria if getattr(c, "level", "run") != "definition"]
    definition_criteria = [c for c in criteria if getattr(c, "level", "run") == "definition"]
    return run_criteria, definition_criteria


def _definition_plane_workflow_yaml(copilot_ctx: Any) -> str | None:
    last_yaml = getattr(copilot_ctx, "last_workflow_yaml", None)
    if isinstance(last_yaml, str) and last_yaml.strip():
        return last_yaml
    initial_yaml = getattr(copilot_ctx, "workflow_yaml", None)
    return initial_yaml if isinstance(initial_yaml, str) else None


def _record_adjudication_on_turn_state(copilot_ctx: Any, verification: CompletionVerificationResult | None) -> None:
    if verification is None or verification.status != "evaluated":
        return
    note_adjudication_on_turn_state(
        getattr(copilot_ctx, "completion_criteria_turn_state", None),
        verification,
        fully_satisfied_workflow_yaml=_definition_plane_workflow_yaml(copilot_ctx)
        if verification.is_fully_satisfied()
        else None,
    )


def _verified_context_block_labels_for_snapshot(
    copilot_ctx: Any, current_labels: list[str], executed_labels: list[str]
) -> list[str]:
    current_label_set = set(current_labels)
    executed_label_set = set(executed_labels)
    candidates: set[str] = set()
    for raw_values in (
        getattr(copilot_ctx, "verified_prefix_labels", None),
        getattr(getattr(copilot_ctx, "workflow_verification_evidence", None), "block_verified", None),
    ):
        if not isinstance(raw_values, list):
            continue
        candidates.update(str(label) for label in raw_values if isinstance(label, str) and label in current_label_set)
    return [label for label in current_labels if label in candidates and label not in executed_label_set]


def _build_page_observation_evidence_snapshot(
    copilot_ctx: Any,
    *,
    url: str,
    title: str = "",
    observed_data: object | None = None,
) -> RunEvidenceSnapshot:
    run_id = getattr(copilot_ctx, "last_run_blocks_workflow_run_id", None)
    block_outputs: dict[str, Any] = {}
    if isinstance(observed_data, dict) and observed_data:
        block_outputs["current_page_observation"] = observed_data
    elif observed_data is not None:
        block_outputs["current_page_observation"] = str(observed_data)
    return RunEvidenceSnapshot(
        workflow_run_id=run_id if isinstance(run_id, str) else None,
        block_outputs=block_outputs,
        current_url=_valid_runtime_anchor_url(url),
        page_title=title if isinstance(title, str) and title.strip() else None,
    )


async def _maybe_run_completion_verification_from_page_observation(
    copilot_ctx: Any,
    *,
    url: str,
    title: str = "",
    observed_data: object | None = None,
) -> CompletionVerificationResult | None:
    """Verify completion only for post-run page observations after failed tests."""

    if not settings.COPILOT_OUTCOME_VERIFICATION_ENABLED:
        return None
    existing = getattr(copilot_ctx, "completion_verification_result", None)
    if isinstance(existing, CompletionVerificationResult) and existing.is_fully_satisfied():
        return existing
    if getattr(copilot_ctx, "post_run_page_observation_after_failed_test", False) is not True:
        return None
    criteria = _completion_verification_criteria(copilot_ctx)
    if not criteria:
        return None
    run_criteria, definition_criteria = _split_criteria_by_plane(criteria)
    criterion_ids = [criterion.id for criterion in criteria]
    definition_verdicts = (
        grade_definition_criteria(definition_criteria, _definition_plane_workflow_yaml(copilot_ctx))
        if definition_criteria
        else []
    )
    if not run_criteria:
        verification = combine_verification_results(criterion_ids, None, definition_verdicts)
    else:
        handler = await _completion_verification_handler(copilot_ctx)
        if handler is None:
            return None
        remaining = _copilot_seconds_remaining(copilot_ctx)
        if (
            remaining is not None
            and remaining
            <= settings.COPILOT_COMPLETION_JUDGE_TIMEOUT_SECONDS + _COMPLETION_VERIFICATION_BUDGET_MARGIN_SECONDS
        ):
            verification = CompletionVerificationResult(status="unavailable")
        else:
            snapshot = _build_page_observation_evidence_snapshot(
                copilot_ctx,
                url=url,
                title=title,
                observed_data=observed_data,
            )
            if not snapshot.has_evidence():
                run_result = CompletionVerificationResult(
                    status="evaluated",
                    criterion_ids=[criterion.id for criterion in run_criteria],
                    verdicts=[
                        CriterionVerdict(criterion_id=criterion.id, state="unsatisfied", reason_code="no_evidence")
                        for criterion in run_criteria
                    ],
                )
            else:
                run_result = await evaluate_completion_criteria(run_criteria, snapshot, handler)
            verification = combine_verification_results(criterion_ids, run_result, definition_verdicts)

    if (
        isinstance(existing, CompletionVerificationResult)
        and not verification.is_fully_satisfied()
        and not (existing.status == "unavailable" and verification.status == "evaluated")
    ):
        return existing

    copilot_ctx.completion_verification_result = verification
    record_completion_verification(copilot_ctx, verification)
    _record_adjudication_on_turn_state(copilot_ctx, verification)
    if verification.status == "evaluated":
        _emit_completion_verification_trace(copilot_ctx, verification)
    return verification


_COMPLETION_VERIFICATION_BUDGET_MARGIN_SECONDS = 5.0


async def _completion_verification_handler(copilot_ctx: Any) -> Any | None:
    return await resolve_main_copilot_handler(
        getattr(copilot_ctx, "workflow_permanent_id", None),
        getattr(copilot_ctx, "organization_id", None),
    )


def _is_outcome_evidence_candidate(copilot_ctx: Any, result: dict[str, Any]) -> bool:
    """A clean ok=True run worth judging on its whole-workflow outcome.

    Recognition is governed by the outcome evidence the user can observe, not by
    whether every block was verified as an end-to-end prefix (SKY-10576). The judge
    requires positive evidence for every criterion, and ``empty_data_blocks`` rejects
    a run whose outcome block produced nothing, so an incomplete run never passes --
    this only lets an already-reached goal be recognized without a redundant
    full-prefix re-run.
    """
    if not bool(result.get("ok", False)):
        return False
    if _run_blocks_structured_blocker_message(result, copilot_ctx):
        return False
    _anti_bot, empty_data_blocks, _categories = _analyze_run_blocks(result, copilot_ctx)
    return not empty_data_blocks


def _is_unfinished_run_verification_candidate(copilot_ctx: Any, result: dict[str, Any]) -> bool:
    """A canceled/partial run (ok=False) still worth judging because it left runtime
    evidence behind. The judge confirms a criterion only on positive evidence, so a
    broken run never spuriously passes; this only lets a reached goal be recognized
    even though the run did not finish cleanly — recognition must not key on run status.
    """
    if bool(result.get("ok", False)):
        return False
    if _active_run_terminal_evidence_detected(result):
        return False
    if _run_blocks_structured_blocker_message(result, copilot_ctx):
        return False
    data = result.get("data")
    if not isinstance(data, dict):
        return False
    return _valid_runtime_anchor_url(data.get("current_url")) is not None


def _build_run_evidence_snapshot(copilot_ctx: Any, result: dict[str, Any]) -> RunEvidenceSnapshot:
    data = result.get("data")
    data = data if isinstance(data, dict) else {}
    current_label_order = _current_workflow_block_labels(copilot_ctx)
    current_labels = set(current_label_order)
    # Evidence must be what THIS run produced. ``verified_block_outputs`` accumulates
    # across incremental runs, so sourcing from it would let an output from a prior
    # run satisfy a criterion the current run never re-produced.
    blocks = data.get("blocks")
    block_outputs: dict[str, Any] = {}
    if isinstance(blocks, list):
        for block in blocks:
            if not isinstance(block, dict):
                continue
            label = block.get("label")
            output = block.get("extracted_data")
            if isinstance(label, str) and label in current_labels and _is_meaningful_extracted_data(output):
                block_outputs[label] = output
    executed = data.get("executed_block_labels")
    executed_block_labels = [str(label) for label in executed] if isinstance(executed, list) else []
    page_title = data.get("page_title")
    run_id = data.get("workflow_run_id")
    return RunEvidenceSnapshot(
        workflow_run_id=run_id if isinstance(run_id, str) else None,
        block_outputs=block_outputs,
        current_url=_valid_runtime_anchor_url(data.get("current_url")),
        page_title=page_title if isinstance(page_title, str) and page_title.strip() else None,
        executed_block_labels=executed_block_labels,
        verified_context_block_labels=_verified_context_block_labels_for_snapshot(
            copilot_ctx,
            current_label_order,
            executed_block_labels,
        ),
    )


async def _maybe_run_completion_verification(
    copilot_ctx: Any, result: dict[str, Any], handler_start: float
) -> CompletionVerificationResult | None:
    if not settings.COPILOT_OUTCOME_VERIFICATION_ENABLED:
        return None
    if getattr(copilot_ctx, "copilot_total_timeout_exceeded", False):
        return None
    criteria = _completion_verification_criteria(copilot_ctx)
    if not criteria:
        return None
    if not (
        _is_outcome_evidence_candidate(copilot_ctx, result)
        or _is_unfinished_run_verification_candidate(copilot_ctx, result)
    ):
        return None
    run_criteria, definition_criteria = _split_criteria_by_plane(criteria)
    criterion_ids = [criterion.id for criterion in criteria]
    definition_verdicts = (
        grade_definition_criteria(definition_criteria, _definition_plane_workflow_yaml(copilot_ctx))
        if definition_criteria
        else []
    )
    if not run_criteria:
        return combine_verification_results(criterion_ids, None, definition_verdicts)
    # A missing judge handler is an infra/config state, not a transient failure:
    # fall back to the prior gate rather than fail closed on every run.
    handler = await _completion_verification_handler(copilot_ctx)
    if handler is None:
        return None
    # Too little budget to verify a candidate run: fail closed (unavailable) rather
    # than let the run-status proxy claim an unverified outcome as success.
    remaining = RUN_BLOCKS_SAFETY_CEILING_SECONDS - (time.monotonic() - handler_start)
    if remaining <= settings.COPILOT_COMPLETION_JUDGE_TIMEOUT_SECONDS + _COMPLETION_VERIFICATION_BUDGET_MARGIN_SECONDS:
        return CompletionVerificationResult(status="unavailable")
    snapshot = _build_run_evidence_snapshot(copilot_ctx, result)
    if not snapshot.has_evidence():
        run_result = CompletionVerificationResult(
            status="evaluated",
            criterion_ids=[criterion.id for criterion in run_criteria],
            verdicts=[
                CriterionVerdict(criterion_id=criterion.id, state="unsatisfied", reason_code="no_evidence")
                for criterion in run_criteria
            ],
        )
    else:
        run_result = await evaluate_completion_criteria(run_criteria, snapshot, handler)
    return combine_verification_results(criterion_ids, run_result, definition_verdicts)


def _outcome_unverified_reason(
    copilot_ctx: Any, completion_verification: CompletionVerificationResult | None
) -> str | None:
    if completion_verification is None:
        return None
    if completion_verification.status == "evaluated":
        if completion_verification.is_fully_satisfied():
            return None
        policy = getattr(copilot_ctx, "request_policy", None)
        criteria = list(policy.completion_criteria) if policy is not None else []
        unmet = summarize_unsatisfied_outcomes(completion_verification, criteria)
        detail = f": {unmet}" if unmet else ""
        turn_state = getattr(copilot_ctx, "completion_criteria_turn_state", None)
        known_good = (
            " A previously tested revision of this workflow satisfied every criterion; if repairs regress "
            "working blocks, prefer restoring that revision over rewriting them."
            if turn_state is not None and getattr(turn_state, "known_good_yaml_available", False)
            else ""
        )
        return (
            f"The run completed but did not demonstrate the goal outcome(s){detail}. "
            "Add an end-state confirmation (an extraction or validation block) that observes the outcome, "
            f"then re-run.{known_good}"
        )
    # An 'unavailable' result reaches here only when verification was required;
    # fail closed and ask for a re-run rather than claim an unverified success.
    return (
        "The run completed but the goal outcome could not be verified (verification was unavailable). "
        "Re-run to verify the outcome before reporting success."
    )


def _outcome_failure_warrants_repair(
    copilot_ctx: Any, completion_verification: CompletionVerificationResult | None
) -> bool:
    """Whether an unmet outcome should route to suspicious-success/repair rather
    than continue-building.

    Contradicting evidence is always a real failure. Absence of evidence is a
    failure only once the workflow has an outcome-producing block; while the agent
    is still adding blocks toward the goal, an unmet criterion is expected, and the
    run should keep building. Terminal success stays withheld in both cases via the
    verification result, so this only governs the repair directive, not the gate.
    """
    if completion_verification is None:
        return False
    if any(verdict.reason_code == "evidence_contradicts" for verdict in completion_verification.verdicts):
        return True
    # Repair needs at least one affirmatively unsatisfied criterion; unknown alone
    # (absent judge signal, unmappable definition checks) never warrants repair.
    if settings.COPILOT_PERSISTED_COMPLETION_CRITERIA_ENABLED and not any(
        resolve_unknown(verdict.state) == "unsatisfied" for verdict in completion_verification.verdicts
    ):
        return False
    return _current_workflow_has_evidence_block(copilot_ctx)


def _tool_visible_result_after_completion_verification(
    copilot_ctx: Any,
    result: dict[str, Any],
    completion_verification: CompletionVerificationResult | None,
) -> dict[str, Any]:
    outcome_unverified_reason = _outcome_unverified_reason(copilot_ctx, completion_verification)
    if outcome_unverified_reason is None:
        return result
    if not _outcome_failure_warrants_repair(copilot_ctx, completion_verification):
        return result

    data = result.get("data")
    copied_data = dict(data) if isinstance(data, dict) else {}
    copied_data["failure_reason"] = outcome_unverified_reason
    copied_data["completion_verification"] = (
        completion_verification.to_trace_data() if completion_verification is not None else None
    )
    categories = copied_data.get("failure_categories")
    copied_categories = list(categories) if isinstance(categories, list) else []
    copied_categories.insert(
        0,
        {
            "category": "OUTCOME_UNVERIFIED",
            "confidence_float": 1.0,
            "reasoning": outcome_unverified_reason,
        },
    )
    copied_data["failure_categories"] = copied_categories
    return {
        **result,
        "ok": False,
        "error": outcome_unverified_reason,
        "data": copied_data,
    }


def _emit_completion_verification_trace(
    copilot_ctx: Any, completion_verification: CompletionVerificationResult
) -> None:
    block_count = getattr(copilot_ctx, "last_update_block_count", None)
    policy = getattr(copilot_ctx, "request_policy", None)
    contract = policy.completion_contract if policy is not None else None
    heuristic_would_block = isinstance(block_count, int) and _goal_likely_needs_more_blocks(
        getattr(copilot_ctx, "user_message", ""), block_count, contract
    )
    trace_data = {
        **completion_verification.to_trace_data(),
        "heuristic_would_block": heuristic_would_block,
        "evidence_block_present": _current_workflow_has_evidence_block(copilot_ctx),
        "warrants_repair": _outcome_failure_warrants_repair(copilot_ctx, completion_verification),
    }
    LOG.info(
        "copilot completion verification",
        **{f"completion_verification_{key}": value for key, value in trace_data.items()},
    )
    with copilot_span("completion_verification", data=trace_data):
        pass
