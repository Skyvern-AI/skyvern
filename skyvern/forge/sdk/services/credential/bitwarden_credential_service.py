import structlog
from fastapi import HTTPException

from skyvern.forge import app
from skyvern.forge.sdk.schemas.credentials import (
    CreateCredentialRequest,
    Credential,
    CredentialItem,
    CredentialVaultType,
)
from skyvern.forge.sdk.services.bitwarden import BitwardenService
from skyvern.forge.sdk.services.credential.credential_vault_service import CredentialVaultService

LOG = structlog.get_logger()


class BitwardenCredentialVaultService(CredentialVaultService):
    async def create_credential(self, organization_id: str, data: CreateCredentialRequest) -> Credential:
        org_collection = await app.DATABASE.get_organization_bitwarden_collection(organization_id)

        if not org_collection:
            LOG.info(
                "There is no collection for the organization. Creating new collection.",
                organization_id=organization_id,
            )
            collection_id = await BitwardenService.create_collection(
                name=organization_id,
            )
            org_collection = await app.DATABASE.create_organization_bitwarden_collection(
                organization_id,
                collection_id,
            )

        item_id = await BitwardenService.create_credential_item(
            collection_id=org_collection.collection_id,
            name=data.name,
            credential=data.credential,
        )

        credential = await self._create_db_credential(
            organization_id=organization_id,
            data=data,
            item_id=item_id,
            vault_type=CredentialVaultType.BITWARDEN,
        )

        return credential

    async def delete_credential(
        self,
        credential: Credential,
    ) -> None:
        organization_bitwarden_collection = await app.DATABASE.get_organization_bitwarden_collection(
            credential.organization_id
        )
        if not organization_bitwarden_collection:
            raise HTTPException(status_code=404, detail="Credential account not found. It might have been deleted.")

        await app.DATABASE.delete_credential(credential.credential_id, credential.organization_id)
        await BitwardenService.delete_credential_item(credential.item_id)

    async def get_credential_item(self, db_credential: Credential) -> CredentialItem:
        return await BitwardenService.get_credential_item(db_credential.item_id)
