"""Leaf predicates for terminal outcome verification.

These read only ``CopilotContext`` fields and have no copilot-module imports, so
the diagnosis, enforcement, and agent layers can all key barrier decisions on the
same judge verdict without an import cycle.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from skyvern.forge.sdk.copilot.context import CopilotContext


def artifact_health_blocked(ctx: CopilotContext) -> bool:
    reason = ctx.last_artifact_health_blocker_reason
    return isinstance(reason, str) and bool(reason.strip())


def outcome_criteria_evaluated(ctx: CopilotContext) -> bool:
    result = ctx.completion_verification_result
    return result is not None and result.status == "evaluated"


def outcome_fully_verified(ctx: CopilotContext) -> bool:
    """Whether the judge confirmed every outcome criterion from this run's evidence.

    The verdict is authoritative over run status: a run that reached the goal is
    recognized even when it was canceled or only partially completed.
    """
    if artifact_health_blocked(ctx):
        return False
    if not outcome_criteria_evaluated(ctx):
        return False
    result = ctx.completion_verification_result
    return result is not None and result.is_fully_satisfied()
