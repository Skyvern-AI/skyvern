"""Per-run resolution of the frame-perception arm.

`TASK_V3_FRAME_PERCEPTION` gates reading, acting in and verifying inside child frames as one
unit. It shipped as an env setting alone, so the only way to enable it for any traffic was to
enable it for all of it. This resolves it per run instead, and every site that used to read the
setting directly reads `frame_perception_enabled()`.
"""

from __future__ import annotations

import structlog

from skyvern.config import settings
from skyvern.forge import app
from skyvern.forge.sdk.core import skyvern_context
from skyvern.forge.sdk.experimentation.providers import NoOpExperimentationProvider

LOG = structlog.get_logger()

FRAME_PERCEPTION_FLAG = "TASK_V3_FRAME_PERCEPTION"


async def resolve_frame_perception(
    context: skyvern_context.SkyvernContext,
    *,
    distinct_id: str,
    organization_id: str | None,
) -> None:
    """Resolve the arm once per run and pin it on the context; off is the outcome of every failure.

    The pin is what makes the observe tool's build-time read agree with every other consumer's
    per-call read, so a cache expiry or mid-run ramp cannot flip the arm under a running loop --
    see `cloud_docs/feature-flags/task-v3-frame-perception.md` for the two terms, the durability
    caveat and why this resolves even when the env setting already forced the arm on.
    """
    if context.frame_perception_resolved_run_id == distinct_id:
        return
    async with context.frame_perception_lock:
        # Re-checked under the lock: parallel branches sharing this context both pass the check
        # above, and the loser must inherit the winner's arm rather than overwrite it.
        if context.frame_perception_resolved_run_id == distinct_id:
            return
        enabled = False
        try:
            provider = app.EXPERIMENTATION_PROVIDER
            # No experimentation configured (OSS default) -> off, and never query the provider.
            if not isinstance(provider, NoOpExperimentationProvider):
                enabled = await provider.is_feature_enabled_cached(
                    FRAME_PERCEPTION_FLAG,
                    distinct_id,
                    properties={"organization_id": organization_id},
                )
        except Exception:
            LOG.warning("Failed to resolve TASK_V3_FRAME_PERCEPTION feature flag", exc_info=True)
            enabled = False
        context.frame_perception_flag = enabled
        context.frame_perception_resolved_run_id = distinct_id
    LOG.info(
        "Resolved frame perception arm",
        distinct_id=distinct_id,
        # The provider's own resolution record carries the FLAG, which reads false for a run the env
        # setting forced on. An operator watching a ramp needs the arm the run actually took.
        frame_perception=settings.TASK_V3_FRAME_PERCEPTION or enabled,
        flag=enabled,
        forced_by_env=settings.TASK_V3_FRAME_PERCEPTION,
    )


def frame_perception_enabled() -> bool:
    """Whether this run reads, acts in and verifies inside child frames.

    The env setting is the force-on term and short-circuits before the pin, so an unresolved run
    reads it alone. Takes no run id, so unlike ``workflow_block_engine_override`` this is sound
    only because every consumer sits inside ``_execute_task_v3`` -- the v1 step engine shares this
    context, so give this an explicit run id before calling it from outside that call tree.
    """
    if settings.TASK_V3_FRAME_PERCEPTION:
        return True
    context = skyvern_context.current()
    if context is None or context.frame_perception_resolved_run_id is None:
        return False
    return context.frame_perception_flag
