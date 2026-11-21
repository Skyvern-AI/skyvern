import structlog

from skyvern.forge import app

LOG = structlog.get_logger()

SHEETS_SAVE_AS_SHEET_FLAG = "SHEETS_SAVE_AS_SHEET"


async def sheets_save_as_sheet_enabled(organization_id: str | None) -> bool:
    # Org-scoped on purpose: every Drive upload in an org must convert the same way, so distinct_id is the org.
    if not organization_id:
        return False
    try:
        return (
            await app.EXPERIMENTATION_PROVIDER.is_feature_enabled_cached(
                SHEETS_SAVE_AS_SHEET_FLAG,
                organization_id,
                properties={"organization_id": organization_id},
            )
            is True
        )
    except Exception:
        LOG.warning("Failed to resolve save-as-sheet feature flag; failing closed", exc_info=True)
        return False
