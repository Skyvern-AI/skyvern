from typing import Annotated, Any

from fastapi import Depends

from skyvern.forge import app
from skyvern.forge.sdk.integrations.catalog import integration_entries, integration_entries_for_org, page_payload
from skyvern.forge.sdk.routes.google_drive import google_drive_router
from skyvern.forge.sdk.routes.routers import base_router, legacy_base_router
from skyvern.forge.sdk.schemas.organizations import Organization
from skyvern.forge.sdk.services import org_auth_service


@base_router.get("/integrations", include_in_schema=False)
@legacy_base_router.get("/integrations", include_in_schema=False)
async def list_integrations(
    current_org: Annotated[Organization, Depends(org_auth_service.get_current_org_for_credential_routes)],
) -> list[dict[str, Any]]:
    # The page reads no org flags, so it always lists the Drive folder actions; the frontend shows them only when the
    # org's DRIVE_FOLDER_ACTIONS flag is on, and a saved one refuses to run while it is off.
    entries = await integration_entries_for_org(
        current_org.organization_id, include_unavailable=True, drive_folder_actions=True
    )
    # Every entry the gates allow, so a hidden or gated app is never offered again as a request.
    known = [*entries, *integration_entries(powerbi_available=True, gmail_available=True)]
    entries += await app.AGENT_FUNCTION.more_integration_entries(known)
    return [page_payload(entry) for entry in entries]


base_router.include_router(google_drive_router, prefix="/integrations/google/drive", include_in_schema=False)
legacy_base_router.include_router(google_drive_router, prefix="/integrations/google/drive", include_in_schema=False)
