import math
from datetime import UTC, datetime, timedelta

import jwt
import structlog
from fastapi import HTTPException, status
from jwt.exceptions import PyJWTError

from skyvern.config import settings
from skyvern.forge import app
from skyvern.forge.sdk.core import security
from skyvern.forge.sdk.schemas.organizations import OrganizationAuthToken, OrganizationAuthTokenType

LOG = structlog.get_logger()
API_KEY_LIFETIME = timedelta(weeks=5200)


def _ui_session_expiration(token: OrganizationAuthToken, org_id: str) -> int | None:
    try:
        payload = jwt.decode(
            token.token,
            settings.SECRET_KEY,
            algorithms=[settings.SIGNATURE_ALGORITHM],
            options={"verify_exp": False},
        )
    except PyJWTError:
        return None
    expires_at = payload.get("exp")
    if (
        payload.get("sub") != org_id
        or payload.get("token_type") != OrganizationAuthTokenType.ui_session.value
        or isinstance(expires_at, bool)
        or not isinstance(expires_at, (int, float))
        or (isinstance(expires_at, float) and not math.isfinite(expires_at))
    ):
        return None
    return int(expires_at)


async def create_org_api_token(org_id: str) -> OrganizationAuthToken:
    """Creates an API token for the specified org_id.

    Args:
        org_id: The org_id for which to create an API token.

    Returns:
        The API token created for the specified org_id.
    """
    # get the organization
    organization = await app.DATABASE.organizations.get_organization(org_id)
    if not organization:
        raise Exception(f"Organization id {org_id} not found")

    # [START create_org_api_token]
    api_key = security.create_access_token(
        org_id,
        expires_delta=API_KEY_LIFETIME,
    )
    # generate OrganizationAutoToken
    org_auth_token = await app.DATABASE.organizations.create_org_auth_token(
        organization_id=org_id,
        token=api_key,
        token_type=OrganizationAuthTokenType.api,
    )
    LOG.info("Created API token for organization", organization_id=org_id)
    return org_auth_token


async def create_org_ui_session_token(org_id: str) -> tuple[OrganizationAuthToken, int]:
    organization = await app.DATABASE.organizations.get_organization(org_id)
    if not organization:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Organization not found")

    expires_delta = timedelta(minutes=settings.UI_SESSION_TOKEN_TTL_MINUTES)
    now = datetime.now(UTC)
    now_timestamp = now.timestamp()
    reusable_token: tuple[OrganizationAuthToken, int] | None = None
    expired_token_ids: list[str] = []
    for existing_token in await app.DATABASE.organizations.get_valid_org_auth_tokens(
        organization_id=org_id,
        token_type=OrganizationAuthTokenType.ui_session,
    ):
        existing_expiration = _ui_session_expiration(existing_token, org_id)
        if existing_expiration is None or existing_expiration <= now_timestamp:
            expired_token_ids.append(existing_token.id)
        elif reusable_token is None and existing_expiration - now_timestamp > expires_delta.total_seconds() / 2:
            reusable_token = existing_token, existing_expiration

    await app.DATABASE.organizations.delete_org_auth_tokens(
        organization_id=org_id,
        token_type=OrganizationAuthTokenType.ui_session,
        token_ids=expired_token_ids,
    )
    if reusable_token is not None:
        return reusable_token

    expires_at = int((now + expires_delta).timestamp())
    token = security.create_access_token(
        org_id,
        expires_delta=expires_delta,
        token_type=OrganizationAuthTokenType.ui_session.value,
    )
    org_auth_token = await app.DATABASE.organizations.create_org_auth_token(
        organization_id=org_id,
        token=token,
        token_type=OrganizationAuthTokenType.ui_session,
    )
    LOG.info("Created UI session token")
    return org_auth_token, expires_at
