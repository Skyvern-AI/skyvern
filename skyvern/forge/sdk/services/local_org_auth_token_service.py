import os
from pathlib import Path

import structlog
from dotenv import set_key

from skyvern.config import settings
from skyvern.forge import app
from skyvern.forge.sdk.core import security
from skyvern.forge.sdk.schemas.organizations import Organization, OrganizationAuthTokenType
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
    organization = await app.DATABASE.get_organization_by_domain(SKYVERN_LOCAL_DOMAIN)
    if organization:
        return organization

    return await app.DATABASE.create_organization(
        organization_name=SKYVERN_LOCAL_ORG,
        domain=SKYVERN_LOCAL_DOMAIN,
        max_steps_per_run=10,
        max_retries_per_step=3,
    )


async def regenerate_local_api_key() -> tuple[str, str, str, str | None]:
    """Create a fresh API key for the local organization and persist it to env files.

    Returns:
        tuple: (api_key, org_id, backend_env_path, frontend_env_path_or_none)
    """
    organization = await ensure_local_org()
    org_id = organization.organization_id

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
