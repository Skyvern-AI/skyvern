from datetime import timedelta

import structlog

from skyvern.forge.app import DATABASE
from skyvern.forge.sdk.core import security
from skyvern.forge.sdk.schemas.organizations import OrganizationAuthToken, OrganizationAuthTokenType

LOG = structlog.get_logger()
API_KEY_LIFETIME = timedelta(weeks=5200)


async def create_org_api_token(org_id: str) -> OrganizationAuthToken:
    """Creates an API token for the specified org_id.

    Args:
        org_id: The org_id for which to create an API token.

    Returns:
        The API token created for the specified org_id.
    """
    # get the organization
    organization = await DATABASE.get_organization(org_id)
    if not organization:
        raise Exception(f"Organization id {org_id} not found")

    # [START create_org_api_token]
    api_key = security.create_access_token(
        org_id,
        expires_delta=API_KEY_LIFETIME,
    )
    # generate OrganizationAutoToken
    org_auth_token = await DATABASE.create_org_auth_token(
        organization_id=org_id,
        token=api_key,
        token_type=OrganizationAuthTokenType.api,
    )
    LOG.info("Created API token for organization", organization_id=org_id)
    return org_auth_token
