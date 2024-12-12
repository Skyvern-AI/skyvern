import time
from typing import Annotated

from asyncache import cached
from cachetools import TTLCache
from fastapi import Header, HTTPException, status
from jose import jwt
from jose.exceptions import JWTError
from pydantic import ValidationError

from skyvern.config import settings
from skyvern.forge import app
from skyvern.forge.sdk.core import skyvern_context
from skyvern.forge.sdk.db.client import AgentDB
from skyvern.forge.sdk.models import TokenPayload
from skyvern.forge.sdk.schemas.organizations import Organization, OrganizationAuthTokenType

AUTHENTICATION_TTL = 60 * 60  # one hour
CACHE_SIZE = 128
ALGORITHM = "HS256"


async def get_current_org(
    x_api_key: Annotated[str | None, Header()] = None,
    authorization: Annotated[str | None, Header()] = None,
) -> Organization:
    if not x_api_key and not authorization:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Invalid credentials",
        )
    if x_api_key:
        return await _get_current_org_cached(x_api_key, app.DATABASE)
    elif authorization:
        return await _authenticate_helper(authorization)

    raise HTTPException(
        status_code=status.HTTP_403_FORBIDDEN,
        detail="Invalid credentials",
    )


async def get_current_org_with_api_key(
    x_api_key: Annotated[str | None, Header()] = None,
) -> Organization:
    if not x_api_key:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Invalid credentials",
        )
    return await _get_current_org_cached(x_api_key, app.DATABASE)


async def get_current_org_with_authentication(
    authorization: Annotated[str | None, Header()] = None,
) -> Organization:
    if not authorization:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Invalid credentials",
        )
    return await _authenticate_helper(authorization)


async def _authenticate_helper(authorization: str) -> Organization:
    token = authorization.split(" ")[1]
    if not app.authentication_function:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Invalid authentication method",
        )
    organization = await app.authentication_function(token)
    if not organization:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Invalid credentials",
        )
    return organization


@cached(cache=TTLCache(maxsize=CACHE_SIZE, ttl=AUTHENTICATION_TTL))
async def _get_current_org_cached(x_api_key: str, db: AgentDB) -> Organization:
    """
    Authentication is cached for one hour
    """
    try:
        payload = jwt.decode(
            x_api_key,
            settings.SECRET_KEY,
            algorithms=[ALGORITHM],
        )
        api_key_data = TokenPayload(**payload)
    except (JWTError, ValidationError):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Could not validate credentials",
        )
    if api_key_data.exp < time.time():
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Auth token is expired",
        )

    organization = await db.get_organization(organization_id=api_key_data.sub)
    if not organization:
        raise HTTPException(status_code=404, detail="Organization not found")

    # check if the token exists in the database
    api_key_db_obj = await db.validate_org_auth_token(
        organization_id=organization.organization_id,
        token_type=OrganizationAuthTokenType.api,
        token=x_api_key,
        valid=None,
    )
    if not api_key_db_obj:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Invalid credentials",
        )

    if api_key_db_obj.valid is False:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Your API key has expired. Please retrieve the latest one from https://app.skyvern.com/settings",
        )

    # set organization_id in skyvern context and log context
    context = skyvern_context.current()
    if context:
        context.organization_id = organization.organization_id
    return organization
