from dataclasses import dataclass, field
from datetime import datetime

from skyvern.config import settings
from skyvern.forge import app
from skyvern.forge.sdk.db.enums import OrganizationAuthTokenType
from skyvern.forge.sdk.schemas.organizations import (
    OnePasswordTokenSource,
    OnePasswordTokenStatusResponse,
)


@dataclass
class OnePasswordTokenResolution:
    token: str | None = field(repr=False)
    source: OnePasswordTokenSource | None
    instance_default_available: bool
    modified_at: datetime | None
    policy_mode: str
    denied_reason: str | None


async def resolve_onepassword_token(organization_id: str) -> OnePasswordTokenResolution:
    organization_token = await app.DATABASE.organizations.get_valid_org_auth_token(
        organization_id=organization_id,
        token_type=OrganizationAuthTokenType.onepassword_service_account.value,
    )
    policy_mode = app.AGENT_FUNCTION.onepassword_instance_default_policy_mode()
    instance_token = settings.OP_SERVICE_ACCOUNT_TOKEN

    instance_default_available = False
    if instance_token:
        instance_default_available = await app.AGENT_FUNCTION.is_onepassword_instance_default_allowed(organization_id)

    if organization_token:
        return OnePasswordTokenResolution(
            token=organization_token.token,
            source="organization",
            instance_default_available=instance_default_available,
            modified_at=organization_token.modified_at,
            policy_mode=policy_mode,
            denied_reason=None,
        )

    if instance_default_available:
        return OnePasswordTokenResolution(
            token=instance_token,
            source="instance_default",
            instance_default_available=True,
            modified_at=None,
            policy_mode=policy_mode,
            denied_reason=None,
        )

    return OnePasswordTokenResolution(
        token=None,
        source=None,
        instance_default_available=False,
        modified_at=None,
        policy_mode=policy_mode,
        denied_reason="no_instance_default" if not instance_token else "not_allowlisted",
    )


def to_status(resolution: OnePasswordTokenResolution) -> OnePasswordTokenStatusResponse:
    return OnePasswordTokenStatusResponse(
        configured=resolution.token is not None,
        source=resolution.source,
        instance_default_available=resolution.instance_default_available,
        modified_at=resolution.modified_at if resolution.source == "organization" else None,
    )
