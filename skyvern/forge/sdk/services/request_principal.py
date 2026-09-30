import asyncio
from contextvars import ContextVar
from dataclasses import dataclass
from enum import StrEnum

import structlog
from fastapi import HTTPException

from skyvern.forge import app
from skyvern.forge.request_logging import set_request_principal
from skyvern.forge.sdk.schemas.organizations import OrganizationAuthTokenType

LOG = structlog.get_logger()

# ponytail: a failing identity lookup is retried by every request that carries a bearer, each bounded by
# this timeout. Cache the failure briefly if an identity-provider outage ever shows up as request latency.
BEARER_IDENTITY_TIMEOUT_SECONDS = 2.0


class AuthKind(StrEnum):
    api_key = "api_key"
    ui_session = "ui_session"
    bearer = "bearer"
    # The key authorized the request; a valid bearer for the same organization supplied the user.
    api_key_and_bearer = "api_key_and_bearer"
    ui_session_and_bearer = "ui_session_and_bearer"


@dataclass(frozen=True)
class BearerIdentity:
    user_id: str
    # As found in the token, never normalized: the claim shapes spell one role differently.
    org_role: str | None = None
    org_role_claim: str | None = None


@dataclass(frozen=True)
class RequestPrincipal:
    organization_id: str
    auth_kind: AuthKind
    user_id: str | None = None
    org_role: str | None = None
    org_role_claim: str | None = None


_ResolutionKey = tuple[str, OrganizationAuthTokenType | None, str | None]
_request_principal: ContextVar[tuple[_ResolutionKey, RequestPrincipal] | None] = ContextVar(
    "request_principal", default=None
)


def get_request_principal() -> RequestPrincipal | None:
    resolved = _request_principal.get()
    return resolved[1] if resolved else None


async def _resolve_bearer_identity(bearer_token: str, organization_id: str) -> BearerIdentity | None:
    try:
        identity = await asyncio.wait_for(
            app.AGENT_FUNCTION.resolve_bearer_identity(bearer_token, organization_id),
            timeout=BEARER_IDENTITY_TIMEOUT_SECONDS,
        )
    except HTTPException:
        return None
    except Exception:
        LOG.warning("Failed to resolve the bearer identity for the request principal", exc_info=True)
        return None
    return identity if isinstance(identity, BearerIdentity) else None


def _auth_kind(api_key_type: OrganizationAuthTokenType | None, has_user: bool) -> AuthKind:
    if api_key_type is None:
        return AuthKind.bearer
    if api_key_type == OrganizationAuthTokenType.ui_session:
        return AuthKind.ui_session_and_bearer if has_user else AuthKind.ui_session
    return AuthKind.api_key_and_bearer if has_user else AuthKind.api_key


async def resolve_request_principal(
    organization_id: str,
    *,
    api_key_type: OrganizationAuthTokenType | None = None,
    bearer_token: str | None = None,
) -> RequestPrincipal:
    """Record who the authenticated request acts as. Runs after authentication and never rejects.

    ``api_key_type`` is the key that authorized the request, or None when the bearer did. The user
    and role only ever come from a bearer the deployment's identity provider vouches for.
    """
    resolution_key = (organization_id, api_key_type, bearer_token)
    resolved = _request_principal.get()
    if resolved is not None and resolved[0] == resolution_key:
        return resolved[1]

    identity = await _resolve_bearer_identity(bearer_token, organization_id) if bearer_token else None
    principal = RequestPrincipal(
        organization_id=organization_id,
        auth_kind=_auth_kind(api_key_type, identity is not None),
        user_id=identity.user_id if identity else None,
        org_role=identity.org_role if identity else None,
        org_role_claim=identity.org_role_claim if identity else None,
    )
    _request_principal.set((resolution_key, principal))
    set_request_principal(principal)
    return principal
