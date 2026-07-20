"""Leaf predicates for terminal outcome verification.

These read only ``AgentContext`` fields and have no copilot-module imports, so
the diagnosis, enforcement, and agent layers can all key barrier decisions on the
same judge verdict without an import cycle.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from skyvern.forge.sdk.copilot.runtime import AgentContext


def artifact_health_blocked(ctx: AgentContext) -> bool:
    reason = ctx.last_artifact_health_blocker_reason
    return isinstance(reason, str) and bool(reason.strip())


def outcome_criteria_evaluated(ctx: AgentContext) -> bool:
    result = ctx.completion_verification_result
    return result is not None and result.status == "evaluated"


def _has_same_run_committed_demonstrated_outcome(ctx: AgentContext) -> bool:
    outcome = getattr(ctx, "last_run_outcome", None)
    run_id = getattr(ctx, "last_run_blocks_workflow_run_id", None)
    return (
        getattr(outcome, "verdict", None) == "demonstrated"
        and isinstance(run_id, str)
        and bool(run_id)
        and getattr(outcome, "workflow_run_id", None) == run_id
    )


def outcome_fully_verified(ctx: AgentContext) -> bool:
    """Whether the judge confirmed every outcome criterion from this run's evidence.

    The verdict is authoritative over run status: a run that reached the goal is
    recognized even when it was canceled or only partially completed.
    """
    if artifact_health_blocked(ctx):
        return False
    if _has_same_run_committed_demonstrated_outcome(ctx):
        return True
    if not outcome_criteria_evaluated(ctx):
        return False
    result = ctx.completion_verification_result
    return result is not None and result.is_fully_satisfied()
