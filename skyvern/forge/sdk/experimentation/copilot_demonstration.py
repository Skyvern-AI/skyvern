import structlog

from skyvern.config import settings
from skyvern.forge import app
from skyvern.forge.sdk.cache.base import BaseCache

LOG = structlog.get_logger()

COPILOT_DEMONSTRATION_FLAG = "WORKFLOW_COPILOT_DEMONSTRATION"


def shared_copilot_demonstration_cache() -> BaseCache | None:
    cache = app.CACHE
    return cache if cache is not None and cache.is_shared else None


async def copilot_demonstration_enabled(organization_id: str | None) -> bool:
    if shared_copilot_demonstration_cache() is None:
        return False
    if settings.WORKFLOW_COPILOT_DEMONSTRATION_ENABLED or not organization_id:
        return settings.WORKFLOW_COPILOT_DEMONSTRATION_ENABLED
    try:
        return (
            await app.EXPERIMENTATION_PROVIDER.is_feature_enabled_cached(
                COPILOT_DEMONSTRATION_FLAG,
                organization_id,
                properties={"organization_id": organization_id},
            )
            is True
        )
    except Exception:
        LOG.warning("Failed to resolve Copilot demonstration feature flag; failing closed", exc_info=True)
        return False
