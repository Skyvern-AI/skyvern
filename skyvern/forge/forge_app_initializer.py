import structlog

from skyvern.config import settings
from skyvern.forge import app, set_force_app_instance
from skyvern.forge.forge_app import ForgeApp, create_forge_app
from skyvern.forge.sdk.core import security
from skyvern.forge.sdk.db.client import AgentDB
from skyvern.forge.sdk.db.enums import OrganizationAuthTokenType
from skyvern.forge.sdk.schemas.organizations import Organization
from skyvern.forge.sdk.services.local_org_auth_token_service import SKYVERN_LOCAL_DOMAIN, SKYVERN_LOCAL_ORG
from skyvern.forge.sdk.services.org_auth_token_service import API_KEY_LIFETIME

LOG = structlog.get_logger()


def start_forge_app(db: AgentDB | None = None) -> ForgeApp:
    force_app_instance = create_forge_app(db)
    set_force_app_instance(force_app_instance)

    if settings.ADDITIONAL_MODULES:
        for module in settings.ADDITIONAL_MODULES:
            LOG.info("Loading additional module to set up api app", module=module)
            app_module = __import__(module)
            configure_app_fn = getattr(app_module, "configure_app", None)
            if not configure_app_fn:
                raise RuntimeError(f"Missing configure_app function in {module}")

            configure_app_fn(force_app_instance)
        LOG.info(
            "Additional modules loaded to set up api app",
            modules=settings.ADDITIONAL_MODULES,
        )

    return force_app_instance


async def get_or_create_local_organization() -> Organization:
    organization = await app.DATABASE.get_organization_by_domain(SKYVERN_LOCAL_DOMAIN)
    if not organization:
        organization = await app.DATABASE.create_organization(
            organization_name=SKYVERN_LOCAL_ORG,
            domain=SKYVERN_LOCAL_DOMAIN,
            max_steps_per_run=10,
            max_retries_per_step=3,
        )
        api_key = security.create_access_token(
            organization.organization_id,
            expires_delta=API_KEY_LIFETIME,
        )
        # generate OrganizationAutoToken
        await app.DATABASE.create_org_auth_token(
            organization_id=organization.organization_id,
            token=api_key,
            token_type=OrganizationAuthTokenType.api,
        )
    return organization


async def setup_local_organization() -> str:
    organization = await get_or_create_local_organization()
    org_auth_token = await app.DATABASE.get_valid_org_auth_token(
        organization_id=organization.organization_id,
        token_type=OrganizationAuthTokenType.api.value,
    )
    return org_auth_token.token if org_auth_token else ""
