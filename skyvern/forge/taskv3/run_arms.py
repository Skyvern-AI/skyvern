"""Per-run arms for Task V3 experiments that change what one run does (SKY-16501).

Each flag is multivariate so that "randomized to control" and "never randomized" stay apart: a run outside
the rollout, or with no flag at all, gets no variant, and an analyst reading the arm log must not pool it
with control. An env setting per flag can only force the arm on.
"""

from __future__ import annotations

import structlog

from skyvern.forge import app
from skyvern.forge.sdk.core import skyvern_context
from skyvern.forge.sdk.core.skyvern_context import RunArm
from skyvern.forge.sdk.experimentation.providers import NoOpExperimentationProvider

LOG = structlog.get_logger()

OBSERVE_DROP_OFFVIEWPORT_UNNAMED_FLAG = "TASK_V3_OBSERVE_DROP_OFFVIEWPORT_UNNAMED"
TYPE_COORDINATE_CLICK_FLAG = "TASK_V3_TYPE_COORDINATE_CLICK"
UNANSWERABLE_FIELD_REMEDY_FLAG = "TASK_V3_UNANSWERABLE_FIELD_REMEDY"
NO_ACTION_HOLD_FLAG = "TASK_V3_NO_ACTION_HOLD"


def _pinned_arm(context: skyvern_context.SkyvernContext, flag: str, distinct_id: str) -> RunArm | None:
    pin = context.run_arms.get(flag)
    return pin[1] if pin is not None and pin[0] == distinct_id else None


async def resolve_run_arm(
    context: skyvern_context.SkyvernContext,
    flag: str,
    *,
    distinct_id: str,
    organization_id: str | None,
    forced: bool,
) -> RunArm:
    """Resolve ``flag`` once per run and pin it on the context; every failure resolves unrandomized (off)."""
    arm = _pinned_arm(context, flag, distinct_id)
    if arm is not None:
        return arm
    async with context.run_arms_lock:
        arm = _pinned_arm(context, flag, distinct_id)
        if arm is not None:
            return arm
        variant: str | None = None
        try:
            provider = app.EXPERIMENTATION_PROVIDER
            if not isinstance(provider, NoOpExperimentationProvider):
                variant = await provider.get_value_cached(
                    flag,
                    distinct_id,
                    properties={"organization_id": organization_id},
                )
        except Exception:
            LOG.warning("Failed to resolve Task V3 run arm", flag=flag, exc_info=True)
            variant = None
        if variant == "treatment":
            arm = "treatment"
        elif variant == "control":
            arm = "control"
        else:
            # An unknown variant string is a misconfigured flag, not a randomized control run.
            arm = "unrandomized"
        # Rebound rather than mutated, so a shallow copy of the context keeps the pins it was copied with.
        context.run_arms = {**context.run_arms, flag: (distinct_id, arm)}
    LOG.info(
        "Resolved Task V3 run arm",
        flag=flag,
        distinct_id=distinct_id,
        organization_id=organization_id,
        arm=arm,
        variant=variant,
        # The arm is the randomization; this is what the run did, which an env force can override.
        enabled=forced or arm == "treatment",
        forced_by_env=forced,
    )
    return arm


def run_arm_enabled(flag: str, forced: bool) -> bool:
    """Whether the current run is on for ``flag``. Sound only inside ``_execute_task_v3``, after resolution."""
    if forced:
        return True
    context = skyvern_context.current()
    if context is None:
        return False
    pin = context.run_arms.get(flag)
    return pin is not None and pin[1] == "treatment"
