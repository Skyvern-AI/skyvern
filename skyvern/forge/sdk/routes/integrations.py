from typing import Annotated, Any

from fastapi import Depends

from skyvern.forge.sdk.integrations.catalog import integration_entries_for_org, page_payload
from skyvern.forge.sdk.routes.routers import base_router, legacy_base_router
from skyvern.forge.sdk.schemas.organizations import Organization
from skyvern.forge.sdk.services import org_auth_service


@base_router.get("/integrations", include_in_schema=False)
@legacy_base_router.get("/integrations", include_in_schema=False)
async def list_integrations(
    current_org: Annotated[Organization, Depends(org_auth_service.get_current_org_for_credential_routes)],
) -> list[dict[str, Any]]:
    entries = await integration_entries_for_org(current_org.organization_id)
    return [page_payload(entry) for entry in entries]
