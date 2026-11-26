import structlog

from skyvern.forge import app

LOG = structlog.get_logger()

DRIVE_SHARED_DRIVE_SEARCH_FLAG = "DRIVE_SHARED_DRIVE_SEARCH"


async def drive_shared_drive_search_enabled(organization_id: str | None) -> bool:
    # Org-scoped on purpose: every Drive call in an org must resolve the same way, so distinct_id is the org.
    if not organization_id:
        return False
    try:
        return (
            await app.EXPERIMENTATION_PROVIDER.is_feature_enabled_cached(
                DRIVE_SHARED_DRIVE_SEARCH_FLAG,
                organization_id,
                properties={"organization_id": organization_id},
            )
            is True
        )
    except Exception:
        LOG.warning("Failed to resolve shared drive search feature flag; failing closed", exc_info=True)
        return False
