import time
from dataclasses import dataclass
from typing import Annotated, Sequence

import jwt
import structlog
from asyncache import cached
from cachetools import TTLCache
from fastapi import Header, HTTPException, status
from jwt.exceptions import PyJWTError
from opentelemetry import trace
from pydantic import ValidationError

from skyvern.config import settings
from skyvern.forge import app
from skyvern.forge.sdk.core import skyvern_context
from skyvern.forge.sdk.db.agent_db import AgentDB
from skyvern.forge.sdk.models import TokenPayload
from skyvern.forge.sdk.schemas.organizations import Organization, OrganizationAuthToken, OrganizationAuthTokenType

LOG = structlog.get_logger()

AUTHENTICATION_TTL = 60 * 60  # one hour
CACHE_SIZE = 128
ALGORITHM = "HS256"
_SAFE_JWT_ERROR_REASONS = {
    "Not enough segments",
    "Invalid payload padding",
    "Invalid header padding",
    "Invalid crypto padding",
    "Signature verification failed",
    "Invalid header string: must be a json object",
}


@dataclass
class ApiKeyValidationResult:
    organization: Organization
    payload: TokenPayload
    token: OrganizationAuthToken


def _normalize_api_key_with_flags(raw_api_key: str) -> tuple[str, dict[str, bool]]:
    normalized = raw_api_key
    flags = {
        "api_key_had_whitespace_padding": False,
        "api_key_had_bearer_prefix": False,
        "api_key_had_outer_quotes": False,
    }

    def _strip_and_track(value: str) -> str:
        stripped = value.strip()
        if stripped != value:
            flags["api_key_had_whitespace_padding"] = True
        return stripped

    # At most three wrapper layers matter here: whitespace, Bearer prefix, outer quotes.
    for _ in range(3):
        updated = normalized

        updated = _strip_and_track(updated)

        if updated[:7].lower() == "bearer ":
            flags["api_key_had_bearer_prefix"] = True
            updated = _strip_and_track(updated[7:])

        if len(updated) >= 2 and updated[0] == updated[-1] and updated[0] in {"'", '"'}:
            flags["api_key_had_outer_quotes"] = True
            updated = _strip_and_track(updated[1:-1])

        if updated == normalized:
            break
        normalized = updated

    return normalized, flags


def _get_api_key_debug_fields(
    raw_api_key: str | None, normalized_api_key: str | None, flags: dict[str, bool] | None
) -> dict[str, bool | int | str | None]:
    if raw_api_key is None or normalized_api_key is None or flags is None:
        return {
            "api_key_original_length": None,
            "api_key_normalized_length": None,
            "api_key_raw_segment_count": None,
            "api_key_normalized_segment_count": None,
            "api_key_had_whitespace_padding": None,
            "api_key_had_bearer_prefix": None,
            "api_key_had_outer_quotes": None,
            "api_key_was_normalized": None,
            "normalized_api_key_decodes": None,
            "normalized_api_key_would_be_expired": None,
            "normalized_api_key_error_type": None,
            "normalized_api_key_error_reason": None,
        }

    debug_fields: dict[str, bool | int | str | None] = {
        "api_key_original_length": len(raw_api_key),
        "api_key_normalized_length": len(normalized_api_key),
        "api_key_raw_segment_count": raw_api_key.count(".") + 1 if raw_api_key else 0,
        "api_key_normalized_segment_count": normalized_api_key.count(".") + 1 if normalized_api_key else 0,
        "api_key_had_whitespace_padding": flags["api_key_had_whitespace_padding"],
        "api_key_had_bearer_prefix": flags["api_key_had_bearer_prefix"],
        "api_key_had_outer_quotes": flags["api_key_had_outer_quotes"],
        "api_key_was_normalized": normalized_api_key != raw_api_key,
        "normalized_api_key_decodes": None,
        "normalized_api_key_would_be_expired": None,
        "normalized_api_key_error_type": None,
        "normalized_api_key_error_reason": None,
    }
    if not normalized_api_key or normalized_api_key == raw_api_key:
        return debug_fields

    try:
        payload = jwt.decode(
            normalized_api_key,
            settings.SECRET_KEY,
            algorithms=[ALGORITHM],
            # Diagnostic only: determine whether the token shape is valid regardless of expiry.
            options={"verify_exp": False},
        )
        api_key_data = TokenPayload(**payload)
        debug_fields["normalized_api_key_decodes"] = True
        debug_fields["normalized_api_key_would_be_expired"] = api_key_data.exp < time.time()
    # Diagnostic code should never change the main 403 path.
    except Exception as exc:
        debug_fields["normalized_api_key_decodes"] = False
        debug_fields["normalized_api_key_error_type"] = type(exc).__name__
        debug_fields["normalized_api_key_error_reason"] = _get_safe_auth_error_reason(exc)

    return debug_fields


def _get_safe_auth_error_reason(exc: Exception) -> str:
    if isinstance(exc, ValidationError):
        locations = [tuple(error["loc"]) for error in exc.errors()]
        return f"{exc.error_count()} validation error(s): {locations}"

    if isinstance(exc, PyJWTError):
        message = str(exc)
        if message in _SAFE_JWT_ERROR_REASONS:
            return message

    return type(exc).__name__


async def get_current_org(
    x_api_key: Annotated[
        str | None,
        Header(
            description="Skyvern API key for authentication. API key can be found at https://app.skyvern.com/settings."
        ),
    ] = None,
    authorization: Annotated[str | None, Header(include_in_schema=False)] = None,
) -> Organization:
    if not x_api_key and not authorization:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Invalid credentials",
        )
    organization = None
    if x_api_key:
        organization = await _get_current_org_cached(x_api_key, app.DATABASE)
    elif authorization:
        organization = await _authenticate_helper(authorization)

    if organization:
        try:
            # Set in context
            curr_ctx = skyvern_context.current()
            if curr_ctx:
                curr_ctx.organization_id = organization.organization_id
                curr_ctx.organization_name = organization.organization_name

            # Set organization info on OTEL span for tracing
            if settings.OTEL_ENABLED:
                try:
                    span = trace.get_current_span()
                    if span:
                        span.set_attribute("organization_id", organization.organization_id)
                        if organization.organization_name:
                            span.set_attribute("organization_name", organization.organization_name)
                except Exception:
                    pass  # Silently ignore OTEL errors
        except Exception:
            pass

        return organization
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
    parts = authorization.split(" ", 1)
    if len(parts) < 2 or not parts[1]:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Invalid credentials",
        )
    token = parts[1]
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

    # set organization_id in skyvern context and log context
    context = skyvern_context.current()
    if context:
        context.organization_id = organization.organization_id
        context.organization_name = organization.organization_name

    return organization


async def get_current_user_id(
    authorization: Annotated[str | None, Header(include_in_schema=False)] = None,
    x_api_key: Annotated[str | None, Header(include_in_schema=False)] = None,
    x_user_agent: Annotated[str | None, Header(include_in_schema=False)] = None,
) -> str:
    # Try authorization header first, but only if the authentication function is configured
    if authorization and app.authenticate_user_function:
        return await _authenticate_user_helper(authorization)

    # Fall back to API key + skyvern-ui user agent
    if x_api_key and x_user_agent == "skyvern-ui":
        organization = await _get_current_org_cached(x_api_key, app.DATABASE)
        if organization:
            return f"{organization.organization_id}_user"

    raise HTTPException(
        status_code=status.HTTP_403_FORBIDDEN,
        detail="Invalid credentials",
    )


async def get_current_user_id_with_authentication(
    authorization: Annotated[str | None, Header()] = None,
) -> str:
    if not authorization:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Invalid credentials",
        )
    return await _authenticate_user_helper(authorization)


async def _authenticate_user_helper(authorization: str) -> str:
    parts = authorization.split(" ", 1)
    if len(parts) < 2 or not parts[1]:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Invalid credentials",
        )
    token = parts[1]
    if not app.authenticate_user_function:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Invalid user authentication method",
        )
    user_id = await app.authenticate_user_function(token)
    if not user_id:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Invalid credentials",
        )
    return user_id


async def resolve_org_from_api_key(
    x_api_key: str,
    db: AgentDB,
    token_types: Sequence[OrganizationAuthTokenType] = (OrganizationAuthTokenType.api,),
) -> ApiKeyValidationResult:
    """Decode and validate the API key against the database."""
    try:
        payload = jwt.decode(
            x_api_key,
            settings.SECRET_KEY,
            algorithms=[ALGORITHM],
            options={"verify_exp": False},
        )
        api_key_data = TokenPayload(**payload)
    except (PyJWTError, ValidationError) as exc:
        try:
            if x_api_key is None:
                normalized_api_key = None
                normalization_flags = None
            else:
                normalized_api_key, normalization_flags = _normalize_api_key_with_flags(x_api_key)
            api_key_debug_fields = _get_api_key_debug_fields(x_api_key, normalized_api_key, normalization_flags)
            LOG.error(
                "Error decoding JWT",
                error_type=type(exc).__name__,
                error_reason=_get_safe_auth_error_reason(exc),
                **api_key_debug_fields,
            )
        except Exception as diagnostic_exc:
            LOG.warning(
                "Diagnostic helper failed during JWT error logging",
                diagnostic_error_type=type(diagnostic_exc).__name__,
            )
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Could not validate credentials",
        )
    if api_key_data.exp < time.time():
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Auth token is expired",
        )

    organization = await db.organizations.get_organization(organization_id=api_key_data.sub)
    if not organization:
        LOG.warning("Organization not found", organization_id=api_key_data.sub, **payload)
        raise HTTPException(status_code=404, detail="Organization not found")

    api_key_db_obj: OrganizationAuthToken | None = None
    # Try token types in priority order and stop at the first valid match.
    for token_type in token_types:
        api_key_db_obj = await db.organizations.validate_org_auth_token(
            organization_id=organization.organization_id,
            token_type=token_type,
            token=x_api_key,
            valid=None,
        )
        if api_key_db_obj:
            break

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

    return ApiKeyValidationResult(
        organization=organization,
        payload=api_key_data,
        token=api_key_db_obj,
    )


@cached(cache=TTLCache(maxsize=CACHE_SIZE, ttl=AUTHENTICATION_TTL))
async def _get_current_org_cached(x_api_key: str, db: AgentDB) -> Organization:
    """Authentication is cached for one hour."""
    validation = await resolve_org_from_api_key(x_api_key, db)

    # set organization_id in skyvern context and log context
    context = skyvern_context.current()
    if context:
        context.organization_id = validation.organization.organization_id
        context.organization_name = validation.organization.organization_name
    return validation.organization
