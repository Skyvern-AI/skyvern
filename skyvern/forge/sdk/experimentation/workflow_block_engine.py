from __future__ import annotations

from typing import TYPE_CHECKING

import structlog

from skyvern.forge import app
from skyvern.forge.sdk.core import skyvern_context
from skyvern.forge.sdk.experimentation.providers import NoOpExperimentationProvider
from skyvern.schemas.run_enums import RunEngine

if TYPE_CHECKING:
    # Runtime import would cycle: block.py imports workflow_block_engine_override from this module.
    from skyvern.forge.sdk.workflow.models.block import V3AbIneligibleReason

LOG = structlog.get_logger()

WORKFLOW_TASK_V3_AB_FLAG = "WORKFLOW_TASK_V3_AB"
DISABLE_TASK_V3_FLAG = "DISABLE_TASK_V3"


async def task_v3_disabled(distinct_id: str, organization_id: str | None) -> bool:
    """The Task V3 kill switch, evaluated identically for the dispatch gate and the A/B resolver.

    The provider caches on (flag, distinct_id, properties), so both callers must build the same
    key or the kill switch can answer differently for the same run.
    """
    return await app.EXPERIMENTATION_PROVIDER.is_feature_enabled_cached(
        DISABLE_TASK_V3_FLAG, distinct_id, properties={"organization_id": organization_id}
    )


async def resolve_workflow_block_engine_arm(
    context: skyvern_context.SkyvernContext,
    *,
    workflow_run_id: str,
    organization_id: str | None,
    workflow_permanent_id: str | None,
    ineligibility_reason: V3AbIneligibleReason | None,
) -> None:
    """Resolve the workflow-block engine A/B once at execution start and pin the arm on the context.

    Bucketed per workflow run so every block of one run shares an arm. Idempotent per run: once
    resolved, a TTL expiry or a mid-run flag ramp cannot flip it. The pin records the run it was
    resolved for, so a nested execution sharing this context -- an inline child workflow run has its
    own workflow_run_id and its own definition -- re-resolves instead of inheriting an arm that was
    never checked against its blocks. Control (no override) is the safe default and the outcome of
    every failure.
    """
    if context.workflow_block_engine_resolved_run_id == workflow_run_id:
        return
    provider = app.EXPERIMENTATION_PROVIDER
    if isinstance(provider, NoOpExperimentationProvider):
        # No experimentation configured (OSS default) -> control, and never query the provider.
        context.workflow_block_engine_override = None
        context.workflow_block_engine_resolved_run_id = workflow_run_id
        return
    async with context.workflow_block_engine_lock:
        if context.workflow_block_engine_resolved_run_id == workflow_run_id:
            return
        override: RunEngine | None = None
        run_is_eligible = ineligibility_reason is None
        try:
            if run_is_eligible:
                # The kill switch, shared with the dispatch gate via task_v3_disabled so both
                # evaluations use the same cache key, wins over the experiment. A kill flipped
                # mid-run still takes effect at dispatch while these rows already read v3:
                # stopping the run wins over its attribution.
                disabled = await task_v3_disabled(workflow_run_id, organization_id)
                if not disabled and await provider.is_feature_enabled_cached(
                    WORKFLOW_TASK_V3_AB_FLAG,
                    workflow_run_id,
                    properties={
                        "organization_id": organization_id,
                        "workflow_permanent_id": workflow_permanent_id or "not_workflow",
                    },
                ):
                    override = RunEngine.skyvern_v3
        except Exception:
            LOG.warning(
                "Failed to resolve the workflow-block engine arm; using control",
                workflow_run_id=workflow_run_id,
                exc_info=True,
            )
            override = None
        context.workflow_block_engine_override = override
        context.workflow_block_engine_resolved_run_id = workflow_run_id
        LOG.info(
            "Resolved workflow-block engine arm",
            workflow_run_id=workflow_run_id,
            workflow_permanent_id=workflow_permanent_id,
            arm="treatment" if override else "control",
            run_is_eligible=run_is_eligible,
            ineligibility_reason=ineligibility_reason,
        )


def workflow_block_engine_override(workflow_run_id: str | None) -> RunEngine | None:
    """The engine the A/B pinned for this run, or None when the run is control or unresolved.

    Returns None unless the pin was resolved for this exact run, so an execution path that never
    reaches the resolver (task_v2, the cached-script block helpers) keeps its declared engine.
    """
    if not workflow_run_id:
        return None
    context = skyvern_context.current()
    if context is None or context.workflow_block_engine_resolved_run_id != workflow_run_id:
        return None
    return context.workflow_block_engine_override
