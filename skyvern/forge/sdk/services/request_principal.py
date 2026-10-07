import asyncio
import hashlib
from contextvars import ContextVar
from dataclasses import dataclass
from enum import StrEnum

import structlog
from fastapi import HTTPException

from skyvern.forge import app
from skyvern.forge.request_logging import get_request_principal_state, set_request_principal
from skyvern.forge.sdk.schemas.organizations import OrganizationAuthTokenType

LOG = structlog.get_logger()

# ponytail: a failed identity lookup can hold the request for up to this bound.
BEARER_IDENTITY_TIMEOUT_SECONDS = 2.0


class AuthKind(StrEnum):
    api_key = "api_key"
    ui_session = "ui_session"
    bearer = "bearer"
    # The key authorized the request; a valid bearer for the same organization supplied the user.
    api_key_and_bearer = "api_key_and_bearer"
    ui_session_and_bearer = "ui_session_and_bearer"


class BearerIdentityStatus(StrEnum):
    verified = "verified"
    no_bearer = "no_bearer"
    invalid_bearer = "invalid_bearer"
    organization_mismatch = "organization_mismatch"
    identity_provider_unconfigured = "identity_provider_unconfigured"
    lookup_failed = "lookup_failed"
    lookup_timed_out = "lookup_timed_out"


@dataclass(frozen=True)
class BearerIdentity:
    user_id: str
    # As found in the token, never normalized: the claim shapes spell one role differently.
    org_role: str | None = None
    org_role_claim: str | None = None
    token_has_organization_claim: bool | None = None


@dataclass(frozen=True)
class BearerIdentityResolution:
    identity: BearerIdentity | None
    status: BearerIdentityStatus


@dataclass(frozen=True)
class RequestPrincipal:
    organization_id: str
    auth_kind: AuthKind
    user_id: str | None = None
    org_role: str | None = None
    org_role_claim: str | None = None
    token_has_organization_claim: bool | None = None


_ResolutionKey = tuple[str, str | None, bytes | None]
_request_principal: ContextVar[tuple[_ResolutionKey, RequestPrincipal, BearerIdentityStatus] | None] = ContextVar(
    "request_principal", default=None
)


def get_request_principal() -> RequestPrincipal | None:
    request_state = get_request_principal_state()
    if request_state is not None:
        return request_state.principal
    resolved = _request_principal.get()
    return resolved[1] if resolved else None


async def _resolve_bearer_identity(bearer_token: str, organization_id: str) -> BearerIdentityResolution:
    try:
        resolution = await asyncio.wait_for(
            app.AGENT_FUNCTION.resolve_bearer_identity(bearer_token, organization_id),
            timeout=BEARER_IDENTITY_TIMEOUT_SECONDS,
        )
    except TimeoutError:
        return BearerIdentityResolution(None, BearerIdentityStatus.lookup_timed_out)
    except HTTPException:
        return BearerIdentityResolution(None, BearerIdentityStatus.invalid_bearer)
    except Exception:  # noqa: BLE001 - identity attribution must never reject an authenticated request.
        LOG.warning("Failed to resolve the bearer identity for the request principal", exc_info=True)
        return BearerIdentityResolution(None, BearerIdentityStatus.lookup_failed)
    return (
        resolution
        if isinstance(resolution, BearerIdentityResolution)
        else BearerIdentityResolution(None, BearerIdentityStatus.lookup_failed)
    )


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
    """Record the caller's identity after auth without changing the request outcome."""
    bearer_hash = hashlib.sha256(bearer_token.encode()).digest() if bearer_token else None
    resolution_key = (organization_id, api_key_type.value if api_key_type is not None else None, bearer_hash)
    identity_key = (organization_id, bearer_hash)
    request_state = get_request_principal_state()
    if request_state is not None:
        async with request_state.principal_resolution_lock:
            if request_state.principal is not None:
                set_request_principal(
                    request_state.principal,
                    identity_key,
                    request_state.bearer_identity_status or BearerIdentityStatus.no_bearer.value,
                )
                return request_state.principal

            identity_result = (
                await _resolve_bearer_identity(bearer_token, organization_id)
                if bearer_token
                else BearerIdentityResolution(None, BearerIdentityStatus.no_bearer)
            )
            identity = identity_result.identity
            principal = RequestPrincipal(
                organization_id=organization_id,
                auth_kind=_auth_kind(api_key_type, identity is not None),
                user_id=identity.user_id if identity else None,
                org_role=identity.org_role if identity else None,
                org_role_claim=identity.org_role_claim if identity else None,
                token_has_organization_claim=identity.token_has_organization_claim if identity else None,
            )
            set_request_principal(principal, identity_key, identity_result.status.value)
            return principal

    resolved = _request_principal.get()
    if resolved is not None and resolved[0] == resolution_key:
        return resolved[1]

    identity_result = (
        await _resolve_bearer_identity(bearer_token, organization_id)
        if bearer_token
        else BearerIdentityResolution(None, BearerIdentityStatus.no_bearer)
    )
    identity = identity_result.identity
    principal = RequestPrincipal(
        organization_id=organization_id,
        auth_kind=_auth_kind(api_key_type, identity is not None),
        user_id=identity.user_id if identity else None,
        org_role=identity.org_role if identity else None,
        org_role_claim=identity.org_role_claim if identity else None,
        token_has_organization_claim=identity.token_has_organization_claim if identity else None,
    )
    _request_principal.set((resolution_key, principal, identity_result.status))
    return principal
