import os
import time
from pathlib import Path

import jwt
import structlog
from dotenv import set_key
from jwt.exceptions import PyJWTError
from pydantic import ValidationError

from skyvern.config import settings
from skyvern.forge import app
from skyvern.forge.sdk.core import security
from skyvern.forge.sdk.db.enums import OrganizationAuthTokenType
from skyvern.forge.sdk.models import TokenPayload
from skyvern.forge.sdk.schemas.organizations import Organization
from skyvern.forge.sdk.services.org_auth_token_service import API_KEY_LIFETIME
from skyvern.utils.env_paths import resolve_backend_env_path, resolve_frontend_env_path

LOG = structlog.get_logger()
SKYVERN_LOCAL_ORG = "Skyvern-local"
SKYVERN_LOCAL_DOMAIN = "skyvern.local"


def _write_env(path: Path, key: str, value: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if not path.exists():
        path.touch()
    set_key(path, key, value)
    LOG.info(".env written", path=str(path), key=key)


def fingerprint_token(value: str) -> str:
    return f"{value[:6]}…{value[-4:]}" if len(value) > 12 else "[redacted -- token too short]"


async def ensure_local_org() -> Organization:
    """Ensure the local development organization exists and return it."""
    organization = await app.DATABASE.organizations.get_organization_by_domain(SKYVERN_LOCAL_DOMAIN)
    if organization:
        return organization

    return await app.DATABASE.organizations.create_organization(
        organization_name=SKYVERN_LOCAL_ORG,
        domain=SKYVERN_LOCAL_DOMAIN,
        max_steps_per_run=10,
        max_retries_per_step=3,
    )


async def ensure_local_org_with_id(organization_id: str) -> Organization:
    """Ensure the local org exists, preferring the caller-provided ID on first creation.

    If a local org already exists for the shared local domain, this returns that
    org unchanged even when its organization_id differs from the requested one.
    """
    organization = await app.DATABASE.organizations.get_organization_by_domain(SKYVERN_LOCAL_DOMAIN)
    if organization:
        return organization

    return await app.DATABASE.organizations.create_organization(
        organization_id=organization_id,
        organization_name=SKYVERN_LOCAL_ORG,
        domain=SKYVERN_LOCAL_DOMAIN,
        max_steps_per_run=10,
        max_retries_per_step=3,
    )


def _decode_local_api_key_payload(api_key: str) -> TokenPayload | None:
    try:
        payload = jwt.decode(
            api_key,
            settings.SECRET_KEY,
            algorithms=[settings.SIGNATURE_ALGORITHM],
            options={"verify_exp": False},
        )
        return TokenPayload(**payload)
    except (PyJWTError, ValidationError):
        return None


async def ensure_local_api_key(api_key: str) -> tuple[str, str] | None:
    """Ensure the provided API key remains usable for the local organization.

    Preserves the externally provided key by syncing it into the database
    instead of rewriting env files, but only when it is still a valid local JWT.
    Returns ``None`` when the key cannot be preserved and the caller should
    regenerate a new local key.
    """
    payload = _decode_local_api_key_payload(api_key)
    if payload is None:
        LOG.warning("Existing local API key is not a valid JWT; regenerating", fingerprint=fingerprint_token(api_key))
        return None
    if payload.exp < time.time():
        LOG.warning("Existing local API key is expired; regenerating", fingerprint=fingerprint_token(api_key))
        return None

    organization = await app.DATABASE.organizations.get_organization_by_domain(SKYVERN_LOCAL_DOMAIN)
    if organization is None:
        organization = await ensure_local_org_with_id(payload.sub)
    elif organization.organization_id != payload.sub:
        LOG.warning(
            "Existing local organization does not match API key subject; regenerating",
            existing_organization_id=organization.organization_id,
            token_organization_id=payload.sub,
            fingerprint=fingerprint_token(api_key),
        )
        return None

    org_id = organization.organization_id
    existing_token = await app.DATABASE.organizations.validate_org_auth_token(
        organization_id=org_id,
        token_type=OrganizationAuthTokenType.api,
        token=api_key,
    )
    if existing_token is None:
        await app.DATABASE.organizations.replace_org_auth_token(
            organization_id=org_id,
            token_type=OrganizationAuthTokenType.api,
            token=api_key,
        )
        LOG.info(
            "Local API key synced",
            organization_id=org_id,
            fingerprint=fingerprint_token(api_key),
        )

    settings.SKYVERN_API_KEY = api_key
    os.environ["SKYVERN_API_KEY"] = api_key
    return api_key, org_id


async def regenerate_local_api_key() -> tuple[str, str, str, str | None]:
    """Create a fresh API key for the local organization and persist it to env files.

    Returns:
        tuple: (api_key, org_id, backend_env_path, frontend_env_path_or_none)
    """
    organization = await ensure_local_org()
    org_id = organization.organization_id

    await app.DATABASE.organizations.invalidate_org_auth_tokens(
        organization_id=org_id,
        token_type=OrganizationAuthTokenType.api,
    )

    api_key = security.create_access_token(org_id, expires_delta=API_KEY_LIFETIME)
    await app.DATABASE.organizations.create_org_auth_token(
        organization_id=org_id,
        token=api_key,
        token_type=OrganizationAuthTokenType.api,
    )

    backend_env_path = resolve_backend_env_path()
    _write_env(backend_env_path, "SKYVERN_API_KEY", api_key)

    frontend_env_path = resolve_frontend_env_path()
    if frontend_env_path:
        _write_env(frontend_env_path, "VITE_SKYVERN_API_KEY", api_key)
    else:
        LOG.warning("Frontend directory not found; skipping VITE_SKYVERN_API_KEY update")

    settings.SKYVERN_API_KEY = api_key
    os.environ["SKYVERN_API_KEY"] = api_key

    LOG.info(
        "Local API key regenerated",
        organization_id=org_id,
        fingerprint=fingerprint_token(api_key),
    )
    return api_key, org_id, str(backend_env_path), str(frontend_env_path) if frontend_env_path else None
