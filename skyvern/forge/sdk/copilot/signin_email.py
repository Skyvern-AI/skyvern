"""The org's connected Gmail address, as a default sign-in identity."""

from __future__ import annotations

import asyncio
import re

import httpx
import structlog

from skyvern.forge.sdk.services import google_oauth_service
from skyvern.services.email.gmail_client import GMAIL_API_BASE, get_json

LOG = structlog.get_logger()

_GMAIL_PROFILE_TIMEOUT_SECONDS = 10.0
_EMAIL_ADDRESS_RE = re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9-]+(?:\.[A-Za-z0-9-]+)+")
_MAX_EMAIL_ADDRESS_LENGTH = 254
# This runs inside the per-turn policy build, so the whole lookup is bounded rather than
# left to the retrying HTTP client multiplied by however many connections an org has.
_LOOKUP_DEADLINE_SECONDS = 15.0
_MAX_CREDENTIALS_TRIED = 3


def is_email_address(value: str) -> bool:
    """Shape check for a value that reaches an LLM prompt and a workflow parameter default."""
    return len(value) <= _MAX_EMAIL_ADDRESS_LENGTH and bool(_EMAIL_ADDRESS_RE.fullmatch(value))


async def connected_gmail_address(organization_id: str) -> str | None:
    try:
        async with asyncio.timeout(_LOOKUP_DEADLINE_SECONDS):
            return await _connected_gmail_address(organization_id)
    except Exception:
        LOG.warning("copilot signin email: lookup did not resolve an address", exc_info=True)
        return None


async def _connected_gmail_address(organization_id: str) -> str | None:
    gmail_scopes = google_oauth_service.scopes_for_profile("gmail")
    try:
        # Active-only: the visible listing also returns ERROR-state credentials, which
        # load_credential_secrets cannot load, so newer broken ones would eat the cap.
        credentials = await google_oauth_service.get_credentials_for_org(organization_id)
    except Exception:
        LOG.warning("copilot signin email: google credential listing failed", exc_info=True)
        return None

    # Cap the profile lookups, not the listing: capping first would let a few Sheets-only
    # connections hide a Gmail one further down the list.
    gmail_credentials = [
        credential
        for credential in credentials
        if google_oauth_service.has_required_scopes(credential.scopes_granted, gmail_scopes)
    ]
    for credential in gmail_credentials[:_MAX_CREDENTIALS_TRIED]:
        try:
            secrets = await google_oauth_service.load_credential_secrets(organization_id, credential.id)
            access_token = await google_oauth_service.access_token_from_secrets(secrets, organization_id)
            async with httpx.AsyncClient(timeout=_GMAIL_PROFILE_TIMEOUT_SECONDS) as client:
                profile = await get_json(
                    client,
                    f"{GMAIL_API_BASE}/users/me/profile",
                    access_token=access_token,
                )
        except Exception:
            LOG.warning(
                "copilot signin email: gmail profile lookup failed",
                credential_id=credential.id,
                exc_info=True,
            )
            continue
        address = profile.get("emailAddress")
        if isinstance(address, str) and is_email_address(address.strip()):
            return address.strip()
    return None
