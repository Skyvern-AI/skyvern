from __future__ import annotations

from typing import Any

from skyvern.forge.sdk.copilot.runtime import AgentContext
from skyvern.forge.sdk.schemas.google_oauth import GoogleOAuthCredentialBase
from skyvern.forge.sdk.schemas.microsoft_oauth import MicrosoftOAuthCredentialBase
from skyvern.forge.sdk.services import google_oauth_service, microsoft_oauth_service


def _serialize(
    credential: GoogleOAuthCredentialBase | MicrosoftOAuthCredentialBase,
    provider: str,
) -> dict[str, Any]:
    # Allowlist rather than a model dump: both source models are token-free today,
    # so a dump would start leaking the day either one gains a token field.
    result: dict[str, Any] = {
        "connection_id": credential.id,
        "provider": provider,
        "name": credential.credential_name,
        "state": credential.state,
        "scopes_granted": list(credential.scopes_granted),
    }
    if credential.email_address:
        result["email_address"] = credential.email_address
    return result


async def _list_integrations(params: dict[str, Any], ctx: AgentContext) -> dict[str, Any]:
    # Each provider is read through whatever its own Integrations page shows, so a connection the
    # user can see is never reported as absent. Google surfaces expired grants as state=error;
    # Microsoft has no such listing, so active is all it can offer.
    google_credentials = await google_oauth_service.get_visible_credentials_for_org(ctx.organization_id)
    microsoft_credentials = await microsoft_oauth_service.get_credentials_for_org(ctx.organization_id)
    integrations = [_serialize(credential, "google") for credential in google_credentials] + [
        _serialize(credential, "microsoft") for credential in microsoft_credentials
    ]
    return {
        "ok": True,
        "data": {
            "integrations": integrations,
            "count": len(integrations),
        },
    }
