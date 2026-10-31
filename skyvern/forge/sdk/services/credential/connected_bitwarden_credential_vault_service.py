from fastapi import HTTPException

from skyvern.exceptions import CredentialSourceNotConfiguredError
from skyvern.forge import app
from skyvern.forge.sdk.db.enums import OrganizationAuthTokenType
from skyvern.forge.sdk.schemas.credentials import (
    CreateCredentialRequest,
    Credential,
    CredentialItem,
    CredentialType,
    CredentialVaultType,
    PasswordCredential,
    TotpType,
)
from skyvern.forge.sdk.schemas.organizations import BitwardenOrganizationAuthToken, Organization
from skyvern.forge.sdk.services.bitwarden import BitwardenConstants, BitwardenLoginSecret, BitwardenService
from skyvern.forge.sdk.services.credential.credential_vault_service import CredentialVaultService, VaultLogin

CONNECTED_BITWARDEN_TIMEOUT_SECONDS = 30
# The organization connects one Bitwarden account, so every imported item shares this vault id. The credentials
# unique index has no vault type, and no 1Password vault id takes this form.
CONNECTED_BITWARDEN_VAULT_ID = "bitwarden"


async def _org_vault_access(organization_id: str) -> tuple[BitwardenOrganizationAuthToken, Organization] | None:
    # Org-scoped only, like the editor's item listing: never the instance's global Bitwarden credentials.
    token = await app.DATABASE.organizations.get_valid_org_auth_token(
        organization_id, OrganizationAuthTokenType.bitwarden_credential.value
    )
    if token is None:
        return None
    organization = await app.DATABASE.organizations.get_organization(organization_id=organization_id)
    return (token, organization) if organization is not None else None


class ConnectedBitwardenCredentialVaultService(CredentialVaultService):
    """Logins imported from the organization's connected Bitwarden account. The row holds metadata only."""

    async def create_credential(
        self,
        organization_id: str,
        data: CreateCredentialRequest,
        created_by: str | None = None,
        is_passkey_setup_pending: bool = False,
    ) -> Credential:
        raise HTTPException(status_code=405, detail="Creating Bitwarden-imported credentials is not supported")

    async def update_credential(self, credential: Credential, data: CreateCredentialRequest) -> Credential:
        raise HTTPException(status_code=405, detail="Updating Bitwarden-imported credentials is not supported")

    async def delete_credential(self, credential: Credential) -> None:
        await app.DATABASE.credentials.delete_credential(credential.credential_id, credential.organization_id)

    async def list_logins(self, organization_id: str) -> list[VaultLogin] | None:
        access = await _org_vault_access(organization_id)
        if access is None:
            return None
        token, organization = access
        overviews = await BitwardenService.list_item_overviews(
            client_id=None,
            client_secret=None,
            master_password=token.credential.master_password,
            bw_organization_id=organization.bw_organization_id,
            bw_collection_ids=organization.bw_collection_ids,
            email=str(token.credential.email),
            timeout=CONNECTED_BITWARDEN_TIMEOUT_SECONDS,
        )
        # No tested_url: the run-time guard and Copilot read a tested_url as the whole site, which would widen a
        # Host-match URI.
        return [
            VaultLogin(
                vault_type=CredentialVaultType.BITWARDEN_CONNECTED,
                vault_id=CONNECTED_BITWARDEN_VAULT_ID,
                item_id=overview.item_id,
                title=overview.title,
                urls=tuple(overview.urls),
                sites=tuple(overview.sites),
            )
            for overview in overviews
            if overview.credential_type == CredentialType.PASSWORD
        ]

    async def get_credential_item(self, db_credential: Credential) -> CredentialItem:
        access = await _org_vault_access(db_credential.organization_id)
        if access is None or not db_credential.item_id:
            raise CredentialSourceNotConfiguredError("The organization's Bitwarden connection is not configured")
        token, organization = access
        collection_ids = organization.bw_collection_ids
        secret = await BitwardenService.get_secret_value_from_url(
            None,
            None,
            token.credential.master_password,
            organization.bw_organization_id,
            collection_ids,
            # Only satisfies the pre-read check; the item's own collections are checked against the scope.
            collection_id=collection_ids[0] if collection_ids else None,
            item_id=db_credential.item_id,
            max_retries=1,
            timeout=CONNECTED_BITWARDEN_TIMEOUT_SECONDS,
            email=str(token.credential.email),
        )
        totp = secret.get(BitwardenConstants.TOTP) or None
        return CredentialItem(
            item_id=db_credential.item_id,
            name=db_credential.name,
            credential_type=CredentialType.PASSWORD,
            credential=PasswordCredential(
                username=secret.get(BitwardenConstants.USERNAME) or "",
                password=secret.get(BitwardenConstants.PASSWORD) or "",
                totp=totp,
                totp_type=TotpType.AUTHENTICATOR if totp else TotpType.NONE,
            ),
            # A read that lost its site list names no checkable site, so a release refuses the login.
            login_sites=secret.login_sites if isinstance(secret, BitwardenLoginSecret) else [],
        )
