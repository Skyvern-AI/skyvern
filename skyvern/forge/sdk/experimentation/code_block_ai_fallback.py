import structlog

from skyvern.forge import app

LOG = structlog.get_logger()

CODE_BLOCK_AI_FALLBACK_FLAG = "ENABLE_CODE_BLOCK_AI_FALLBACK"


async def code_block_ai_fallback_flag_enabled(organization_id: str | None) -> bool:
    # Org-scoped on purpose: every run in an org must resolve the same way, so distinct_id is the org.
    if not organization_id:
        return False
    try:
        return bool(
            await app.EXPERIMENTATION_PROVIDER.is_feature_enabled_cached(
                CODE_BLOCK_AI_FALLBACK_FLAG,
                organization_id,
                properties={"organization_id": organization_id},
            )
        )
    except Exception:
        LOG.warning("Failed to resolve code block AI fallback feature flag; failing closed", exc_info=True)
        return False
