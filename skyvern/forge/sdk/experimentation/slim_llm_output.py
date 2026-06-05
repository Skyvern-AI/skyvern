from __future__ import annotations

import structlog

from skyvern.forge import app
from skyvern.forge.sdk.core import skyvern_context
from skyvern.forge.sdk.experimentation.prompt_families import (
    SLIM_LLM_OUTPUT_PROMPTS_FLAG,
    SLIM_VARIANT_SAFE,
    SLIM_VARIANT_TERSE,
    SLIM_VARIANTS,
    family_for_template,
    slim_variant_for_family,
)

LOG = structlog.get_logger()

# Template contract: slim_output is None (control) or one of these strings — never
# "off", which is truthy in Jinja and would slim the control cohort.
TEMPLATE_VALUE_BY_VARIANT: dict[str, str] = {
    SLIM_VARIANT_SAFE: "safe",
    SLIM_VARIANT_TERSE: "terse",
}


async def get_slim_output_template_value(template_name: str) -> str | None:
    """Resolve the run's assigned slim variant (once per run, memoized on context) and
    map it to the template var for this template's family; None = control schema."""
    context = skyvern_context.current()
    if context is None:
        return None
    if not context.slim_output_variant_resolved:
        # Single-flight: parallel first-use callers (speculative extract-actions +
        # verification) must all observe the same one-shot resolution.
        async with context.slim_output_variant_lock:
            if not context.slim_output_variant_resolved:
                await _resolve_assigned_variant(context)
    variant = slim_variant_for_family(context.slim_output_variant_assigned, family_for_template(template_name))
    if variant is None:
        return None
    return TEMPLATE_VALUE_BY_VARIANT[variant]


async def _resolve_assigned_variant(context: skyvern_context.SkyvernContext) -> None:
    distinct_id = context.workflow_run_id or context.task_id or context.task_v2_id or context.run_id
    if not distinct_id:
        # No run identity to bucket on — pin the context to control instead of
        # re-entering the lock on every subsequent render.
        context.slim_output_variant_resolved = True
        return
    try:
        variant = await app.EXPERIMENTATION_PROVIDER.get_value_cached(
            SLIM_LLM_OUTPUT_PROMPTS_FLAG,
            distinct_id,
            properties={
                "organization_id": context.organization_id,
                "workflow_permanent_id": context.workflow_permanent_id,
            },
        )
    except Exception:
        # Flag-evaluation failure must never alter prompt content; pin the whole run
        # to control so cohorts stay internally consistent.
        LOG.warning("Failed to resolve SLIM_LLM_OUTPUT_PROMPTS; defaulting to control", exc_info=True)
        variant = None
    context.slim_output_variant_assigned = variant if variant in SLIM_VARIANTS else None
    context.slim_output_variant_resolved = True
