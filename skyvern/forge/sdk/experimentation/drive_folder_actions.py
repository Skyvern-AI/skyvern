import structlog

from skyvern.forge import app

LOG = structlog.get_logger()

DRIVE_FOLDER_ACTIONS_FLAG = "DRIVE_FOLDER_ACTIONS"


async def drive_folder_actions_enabled(organization_id: str | None) -> bool:
    # Org-scoped on purpose: an org's catalog and its saved Drive actions must resolve the same way, so distinct_id
    # is the org.
    if not organization_id:
        return False
    try:
        return (
            await app.EXPERIMENTATION_PROVIDER.is_feature_enabled_cached(
                DRIVE_FOLDER_ACTIONS_FLAG,
                organization_id,
                properties={"organization_id": organization_id},
            )
            is True
        )
    except Exception:
        LOG.warning("Failed to resolve Drive folder actions feature flag; failing closed", exc_info=True)
        return False
