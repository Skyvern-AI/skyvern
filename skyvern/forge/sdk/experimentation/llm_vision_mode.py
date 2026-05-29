import os
from typing import Any

import structlog

from skyvern.forge import app
from skyvern.forge.sdk.core.skyvern_context import LLMVisionMode, SkyvernContext

LOG = structlog.get_logger()

LLM_VISION_MODE_FLAG = "llm_vision_mode"


async def resolve_llm_vision_mode_for_context(
    context: SkyvernContext,
    distinct_id: str,
    organization_id: str | None,
    *,
    workflow_permanent_id: str | None = None,
    task_url: str | None = None,
    log_context: dict[str, Any] | None = None,
) -> None:
    if os.getenv("FORCE_DISABLE_LLM_SCREENSHOTS", "").lower() in ("true", "1", "yes"):
        context.set_llm_vision_mode(LLMVisionMode.NO_IMAGES_WITH_A11Y)
        return

    properties: dict[str, str] = {}
    if organization_id:
        properties["organization_id"] = organization_id
    if workflow_permanent_id:
        properties["workflow_permanent_id"] = workflow_permanent_id
    if task_url:
        properties["task_url"] = task_url

    try:
        flag_value = await app.EXPERIMENTATION_PROVIDER.get_value_cached(
            LLM_VISION_MODE_FLAG,
            distinct_id,
            properties=properties,
        )
        context.set_llm_vision_mode(flag_value)
    except Exception:
        LOG.warning(
            "Failed to check llm_vision_mode feature flag",
            exc_info=True,
            distinct_id=distinct_id,
            **(log_context or {}),
        )
        context.set_llm_vision_mode(LLMVisionMode.CONTROL)
