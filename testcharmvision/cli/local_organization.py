"""Local organization setup for CLI init (create org and API key)."""

from testcharmvision.forge import app
from testcharmvision.forge.sdk.core import security
from testcharmvision.forge.sdk.db.enums import OrganizationAuthTokenType
from testcharmvision.forge.sdk.schemas.organizations import Organization
from testcharmvision.forge.sdk.services.local_org_auth_token_service import (
    TESTCHARMVISION_LOCAL_DOMAIN,
    TESTCHARMVISION_LOCAL_ORG,
)
from testcharmvision.forge.sdk.services.org_auth_token_service import API_KEY_LIFETIME


async def get_or_create_local_organization() -> Organization:
    organization = await app.DATABASE.get_organization_by_domain(TESTCHARMVISION_LOCAL_DOMAIN)
    if not organization:
        organization = await app.DATABASE.create_organization(
            organization_name=TESTCHARMVISION_LOCAL_ORG,
            domain=TESTCHARMVISION_LOCAL_DOMAIN,
            max_steps_per_run=10,
            max_retries_per_step=3,
        )
        api_key = security.create_access_token(
            organization.organization_id,
            expires_delta=API_KEY_LIFETIME,
        )
        await app.DATABASE.create_org_auth_token(
            organization_id=organization.organization_id,
            token=api_key,
            token_type=OrganizationAuthTokenType.api,
        )
    return organization


async def setup_local_organization() -> str:
    organization = await get_or_create_local_organization()
    org_auth_token = await app.DATABASE.get_valid_org_auth_token(
        organization_id=organization.organization_id,
        token_type=OrganizationAuthTokenType.api.value,
    )
    return org_auth_token.token if org_auth_token else ""
