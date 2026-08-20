"""Leaf predicates for terminal outcome verification.

These read only ``AgentContext`` fields and have no copilot-module imports, so
the diagnosis, enforcement, and agent layers can all key barrier decisions on the
same judge verdict without an import cycle.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from skyvern.forge.sdk.copilot.turn_origin import TurnOrigin

if TYPE_CHECKING:
    from skyvern.forge.sdk.copilot.runtime import AgentContext


def artifact_health_blocked(ctx: AgentContext) -> bool:
    reason = ctx.last_artifact_health_blocker_reason
    return isinstance(reason, str) and bool(reason.strip())


def outcome_criteria_evaluated(ctx: AgentContext) -> bool:
    result = ctx.completion_verification_result
    return result is not None and result.status == "evaluated"


def outcome_fully_verified(ctx: AgentContext) -> bool:
    """Whether the isolated unattended verifier confirmed every criterion."""
    if getattr(ctx, "turn_origin", TurnOrigin.interactive) != TurnOrigin.runtime_self_heal:
        return False
    if artifact_health_blocked(ctx):
        return False
    if not outcome_criteria_evaluated(ctx):
        return False
    result = ctx.completion_verification_result
    return result is not None and result.is_fully_satisfied()
