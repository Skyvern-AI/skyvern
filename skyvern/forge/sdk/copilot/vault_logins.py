"""Logins in the organization's connected vaults, offered to Copilot before they are saved credentials."""

from __future__ import annotations

import hashlib
from datetime import UTC, datetime
from typing import TYPE_CHECKING

from skyvern.forge.sdk.schemas.credentials import Credential, CredentialType
from skyvern.forge.sdk.services.credential.credential_vault_service import (
    VaultLogin,
    VaultLoginListing,
    list_vault_logins,
)

if TYPE_CHECKING:
    from skyvern.forge.sdk.copilot.request_policy import RequestPolicy

VAULT_LOGIN_REF_PREFIX = "vlogin_"


def _opaque_ref(login: VaultLogin) -> str:
    key = f"{login.vault_type}:{login.vault_id}:{login.item_id}".encode()
    return f"{VAULT_LOGIN_REF_PREFIX}{hashlib.sha256(key).hexdigest()[:16]}"


def vault_login_ref(login: VaultLogin) -> str:
    """The id a listed vault login goes by: its saved credential once imported, else a stable opaque ref."""
    return login.credential_id or _opaque_ref(login)


def vault_login_credential(organization_id: str, credential_id: str, login: VaultLogin) -> Credential:
    now = datetime.now(UTC)
    return Credential(
        credential_id=credential_id,
        organization_id=organization_id,
        name=login.title,
        vault_type=login.vault_type,
        vault_id=login.vault_id,
        item_id=login.item_id,
        credential_type=CredentialType.PASSWORD,
        username=None,
        card_last4=None,
        card_brand=None,
        tested_url=login.tested_url,
        created_at=now,
        modified_at=now,
    )


async def load_vault_logins(
    organization_id: str, policy: RequestPolicy | None
) -> tuple[dict[str, VaultLogin], VaultLoginListing]:
    """Every listed vault login by id, an imported one also by the opaque ref an earlier turn may have handed out,
    and the listing that read them. Listing can force-sync a vault, so the turn keeps it and lists again only while
    a vault was unreadable; the logins it did read still compete for a page meanwhile."""
    if (
        policy is not None
        and policy.vault_logins is not None
        and policy.vault_listing is not None
        and not policy.vault_listing.unreadable
    ):
        return policy.vault_logins, policy.vault_listing
    listing = await list_vault_logins(organization_id)
    logins = {ref: login for login in listing.logins for ref in {_opaque_ref(login), vault_login_ref(login)}}
    if policy is not None:
        policy.vault_logins, policy.vault_listing = logins, listing
    return logins, listing
