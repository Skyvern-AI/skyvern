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
    member_api_key = "member_api_key"
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
    # Last, so callers that build a principal positionally as (organization, kind, user, role) keep their meaning.
    api_key_token_id: str | None = None


def synthetic_user_id(organization_id: str) -> str:
    return f"{organization_id}_user"


def requesting_user_id(organization_id: str) -> str | None:
    """The actor behind the live request, recorded like a Trigger owner; None for a background job."""
    return trigger_owner_id(get_request_principal(), organization_id)


_ResolutionKey = tuple[str, str | None, str | None, bytes | None]
_request_principal: ContextVar[tuple[_ResolutionKey, RequestPrincipal, BearerIdentityStatus] | None] = ContextVar(
    "request_principal", default=None
)


# Set by the route authorization dependency, which FastAPI resolves before the route's auth dependency.
pending_route_action: ContextVar[str | None] = ContextVar("pending_route_action", default=None)


def get_request_principal() -> RequestPrincipal | None:
    request_state = get_request_principal_state()
    if request_state is not None:
        return request_state.principal
    resolved = _request_principal.get()
    return resolved[1] if resolved else None


def human_user_id(principal: RequestPrincipal | None, organization_id: str) -> str | None:
    """The person behind the request (a bearer user, or the member a member-bound key acts as); None for organization
    API keys, which act for the whole organization."""
    if (
        principal is None
        or principal.organization_id != organization_id
        or principal.auth_kind
        not in {AuthKind.bearer, AuthKind.ui_session, AuthKind.ui_session_and_bearer, AuthKind.member_api_key}
        or principal.user_id is None
        or principal.user_id == synthetic_user_id(organization_id)
    ):
        return None
    return principal.user_id


def reject_non_admin_api_key_secret_access(principal: RequestPrincipal | None) -> None:
    if not isinstance(principal, RequestPrincipal):
        return
    if principal.auth_kind == AuthKind.member_api_key:
        raise HTTPException(status_code=403, detail="Organization API-key secrets require an Admin session")


async def resolve_api_key_profile_identity(organization_id: str, token_id: str) -> tuple[str, str | None, bool]:
    try:
        profile = await app.AGENT_FUNCTION.resolve_api_key_profile(organization_id, token_id)
    except Exception:
        LOG.warning("Failed to resolve API key profile", exc_info=True)
        raise HTTPException(status_code=401, detail="Invalid credentials") from None
    if (
        profile is None
        or not profile[2]
        or profile[0] not in {"full_org", "member"}
        or (profile[0] == "member" and not profile[1])
    ):
        raise HTTPException(status_code=401, detail="Invalid credentials")
    return profile


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


def _build_request_principal(
    organization_id: str,
    api_key_type: OrganizationAuthTokenType | None,
    api_key_token_id: str | None,
    identity: BearerIdentity | None,
    member_user_id: str | None,
) -> RequestPrincipal:
    member_key = member_user_id is not None
    return RequestPrincipal(
        organization_id=organization_id,
        auth_kind=AuthKind.member_api_key if member_key else _auth_kind(api_key_type, identity is not None),
        api_key_token_id=api_key_token_id,
        user_id=member_user_id or (identity.user_id if identity else None),
        org_role="org:member" if member_user_id is not None else (identity.org_role if identity else None),
        org_role_claim=identity.org_role_claim if identity else None,
        token_has_organization_claim=(
            True if member_key else (identity.token_has_organization_claim if identity else None)
        ),
    )


async def resolve_request_principal(
    organization_id: str,
    *,
    api_key_type: OrganizationAuthTokenType | None = None,
    api_key_token_id: str | None = None,
    api_key_profile: tuple[str, str | None, bool] | None = None,
    api_key_profile_resolved: bool = False,
    bearer_token: str | None = None,
) -> RequestPrincipal:
    principal = await _resolve_request_principal(
        organization_id,
        api_key_type=api_key_type,
        api_key_token_id=api_key_token_id,
        api_key_profile=api_key_profile,
        api_key_profile_resolved=api_key_profile_resolved,
        bearer_token=bearer_token,
    )
    await app.AGENT_FUNCTION.enforce_request_role(principal, pending_route_action.get(), _bearer_identity_status())
    return principal


def _bearer_identity_status() -> BearerIdentityStatus | None:
    request_state = get_request_principal_state()
    if request_state is not None and request_state.bearer_identity_status is not None:
        return BearerIdentityStatus(request_state.bearer_identity_status)
    resolved = _request_principal.get()
    return resolved[2] if resolved else None


async def _resolve_request_principal(
    organization_id: str,
    *,
    api_key_type: OrganizationAuthTokenType | None = None,
    api_key_token_id: str | None = None,
    api_key_profile: tuple[str, str | None, bool] | None = None,
    api_key_profile_resolved: bool = False,
    bearer_token: str | None = None,
) -> RequestPrincipal:
    """Resolve the caller's identity and fail closed when an API-key profile is invalid."""
    member_user_id: str | None = None
    if api_key_type == OrganizationAuthTokenType.api and api_key_token_id:
        if not api_key_profile_resolved:
            api_key_profile = await resolve_api_key_profile_identity(organization_id, api_key_token_id)
        elif api_key_profile is None:
            raise HTTPException(status_code=401, detail="Invalid credentials")
        if api_key_profile[0] == "member":
            member_user_id = api_key_profile[1]

    bearer_hash = hashlib.sha256(bearer_token.encode()).digest() if bearer_token else None
    resolution_key = (
        organization_id,
        api_key_type.value if api_key_type is not None else None,
        api_key_token_id,
        bearer_hash,
    )
    identity_key = (organization_id, bearer_hash)
    request_state = get_request_principal_state()
    if request_state is not None:
        async with request_state.principal_resolution_lock:
            if request_state.principal is not None and member_user_id is None:
                set_request_principal(
                    request_state.principal,
                    identity_key,
                    request_state.bearer_identity_status or BearerIdentityStatus.no_bearer.value,
                )
                return request_state.principal

            identity_result = (
                await _resolve_bearer_identity(bearer_token, organization_id)
                if bearer_token and member_user_id is None
                else BearerIdentityResolution(None, BearerIdentityStatus.no_bearer)
            )
            principal = _build_request_principal(
                organization_id,
                api_key_type,
                api_key_token_id,
                identity_result.identity,
                member_user_id,
            )
            set_request_principal(principal, identity_key, identity_result.status.value)
            return principal

    resolved = _request_principal.get()
    if resolved is not None and resolved[0] == resolution_key:
        return resolved[1]

    identity_result = (
        await _resolve_bearer_identity(bearer_token, organization_id)
        if bearer_token and member_user_id is None
        else BearerIdentityResolution(None, BearerIdentityStatus.no_bearer)
    )
    principal = _build_request_principal(
        organization_id,
        api_key_type,
        api_key_token_id,
        identity_result.identity,
        member_user_id,
    )
    _request_principal.set((resolution_key, principal, identity_result.status))
    return principal


def api_key_actor_id(organization_id: str) -> str | None:
    """The actor a key authorized the live request as: the org's admin-equivalent synthetic user for an organization
    key, even with a bearer that names a narrower member, or the member a member-bound key acts as; None otherwise."""
    principal = get_request_principal()
    if principal is None or principal.organization_id != organization_id:
        return None
    if principal.auth_kind == AuthKind.member_api_key:
        return human_user_id(principal, organization_id)
    return (
        synthetic_user_id(organization_id)
        if principal.auth_kind in {AuthKind.api_key, AuthKind.api_key_and_bearer}
        else None
    )


def trigger_owner_id(principal: RequestPrincipal | None, organization_id: str) -> str | None:
    """Owner recorded on schedules and run groups; an API key records the org's admin-equivalent synthetic user."""
    if principal is None or principal.organization_id != organization_id:
        return None
    if principal.auth_kind in {AuthKind.api_key, AuthKind.api_key_and_bearer}:
        return synthetic_user_id(organization_id)
    # An unidentified UI session stays ownerless, so its runs fail closed in restricted folders.
    return human_user_id(principal, organization_id)
