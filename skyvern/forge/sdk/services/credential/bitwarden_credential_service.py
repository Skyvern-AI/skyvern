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

    async def update_credential(self, credential: Credential, data: CreateCredentialRequest) -> Credential:
        org_collection = await app.DATABASE.get_organization_bitwarden_collection(credential.organization_id)

        if not org_collection:
            raise HTTPException(status_code=404, detail="Credential account not found. It might have been deleted.")

        # Create new vault item with the updated data
        new_item_id = await BitwardenService.create_credential_item(
            collection_id=org_collection.collection_id,
            name=data.name,
            credential=data.credential,
        )

        # Update DB record to point to the new vault item
        try:
            updated_credential = await self._update_db_credential(
                credential=credential,
                data=data,
                item_id=new_item_id,
            )
        except Exception:
            LOG.warning(
                "DB update failed, attempting to clean up new Bitwarden vault item",
                organization_id=credential.organization_id,
                new_item_id=new_item_id,
            )
            try:
                await BitwardenService.delete_credential_item(new_item_id)
            except Exception as cleanup_error:
                LOG.error(
                    "Failed to clean up orphaned Bitwarden vault item",
                    organization_id=credential.organization_id,
                    new_item_id=new_item_id,
                    error=str(cleanup_error),
                )
            raise

        return updated_credential

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

    async def post_delete_credential_item(self, item_id: str, organization_id: str | None = None) -> None:
        try:
            await BitwardenService.delete_credential_item(item_id)
            LOG.info(
                "Successfully deleted credential item from Bitwarden in background",
                item_id=item_id,
            )
        except Exception as e:
            LOG.warning(
                "Failed to delete credential item from Bitwarden in background",
                item_id=item_id,
                error=str(e),
                exc_info=True,
            )

    async def get_credential_item(self, db_credential: Credential) -> CredentialItem:
        return await BitwardenService.get_credential_item(db_credential.item_id)
