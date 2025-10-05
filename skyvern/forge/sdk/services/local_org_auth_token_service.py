from __future__ import annotations

import os
from pathlib import Path

import structlog
from dotenv import set_key

from skyvern.config import settings
from skyvern.forge import app
from skyvern.forge.sdk.core import security
from skyvern.forge.sdk.schemas.organizations import Organization, OrganizationAuthTokenType
from skyvern.forge.sdk.services.org_auth_token_service import API_KEY_LIFETIME

LOG = structlog.get_logger()
PROJECT_ROOT = Path(__file__).resolve().parents[4]
ROOT_ENV_PATH = PROJECT_ROOT / ".env"
FRONTEND_ENV_PATH = PROJECT_ROOT / "skyvern-frontend" / ".env"
SKYVERN_LOCAL_ORG = "Skyvern-local"
SKYVERN_LOCAL_DOMAIN = "skyvern.local"


def fingerprint_token(value: str) -> str:
    return f"{value[:6]}…{value[-4:]}" if len(value) > 12 else value


def _write_env(path: Path, key: str, value: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if not path.exists():
        path.touch()
    set_key(str(path), key, value)
    LOG.info(".env written", path=(str(path)), key=key)


def write_env_files(api_key: str) -> None:
    """Persist the API key into the root and frontend .env files and update runtime state."""
    _write_env(ROOT_ENV_PATH, "SKYVERN_API_KEY", api_key)
    _write_env(FRONTEND_ENV_PATH, "VITE_SKYVERN_API_KEY", api_key)
    settings.SKYVERN_API_KEY = api_key
    os.environ["SKYVERN_API_KEY"] = api_key
    os.environ["VITE_SKYVERN_API_KEY"] = api_key


async def ensure_local_org() -> Organization:
    """Ensure the local development organization exists and return it."""
    organization = await app.DATABASE.get_organization_by_domain(SKYVERN_LOCAL_DOMAIN)
    if organization:
        return organization

    return await app.DATABASE.create_organization(
        organization_name=SKYVERN_LOCAL_ORG,
        domain=SKYVERN_LOCAL_DOMAIN,
        max_steps_per_run=10,
        max_retries_per_step=3,
    )


async def regenerate_local_api_key(
    organization_id: str | None = None,
    *,
    write_env: bool = True,
) -> tuple[str, str]:
    """Create a fresh API key for the local organization and optionally update env files."""
    if organization_id is None:
        organization = await ensure_local_org()
        org_id = organization.organization_id
    else:
        org_id = organization_id

    await app.DATABASE.invalidate_org_auth_tokens(
        organization_id=org_id,
        token_type=OrganizationAuthTokenType.api,
    )

    api_key = security.create_access_token(org_id, expires_delta=API_KEY_LIFETIME)
    await app.DATABASE.create_org_auth_token(
        organization_id=org_id,
        token=api_key,
        token_type=OrganizationAuthTokenType.api,
    )

    if write_env:
        write_env_files(api_key)

    LOG.info(
        "Local API key regenerated",
        organization_id=org_id,
        fingerprint=fingerprint_token(api_key),
    )
    return api_key, org_id
