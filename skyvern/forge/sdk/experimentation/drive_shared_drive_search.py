import structlog

from skyvern.forge import app

LOG = structlog.get_logger()

DRIVE_SHARED_DRIVE_SEARCH_FLAG = "DRIVE_SHARED_DRIVE_SEARCH"

# Drive rejects a page token replayed under different corpora, so picker tokens carry the mode they were issued in.
_ALL_DRIVES_TOKEN_PREFIX = "all:"
_USER_DRIVE_TOKEN_PREFIX = "user:"


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


def tag_page_token(page_token: str | None, search_all_drives: bool) -> str | None:
    if not page_token:
        return None
    prefix = _ALL_DRIVES_TOKEN_PREFIX if search_all_drives else _USER_DRIVE_TOKEN_PREFIX
    return f"{prefix}{page_token}"


async def resolve_page_search_mode(organization_id: str, page_token: str | None) -> tuple[str | None, bool]:
    """Return the raw Drive page token and the search mode to use; a tagged token keeps the mode it was issued in."""
    if page_token and page_token.startswith(_ALL_DRIVES_TOKEN_PREFIX):
        return page_token.removeprefix(_ALL_DRIVES_TOKEN_PREFIX), True
    if page_token and page_token.startswith(_USER_DRIVE_TOKEN_PREFIX):
        return page_token.removeprefix(_USER_DRIVE_TOKEN_PREFIX), False
    return page_token or None, await drive_shared_drive_search_enabled(organization_id)
