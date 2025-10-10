import structlog
from fastapi import HTTPException

from skyvern.forge import app
from skyvern.forge.sdk.schemas.credentials import (
    CreateCredentialRequest,
    Credential,
    CredentialItem,
    CredentialResponse,
    CredentialType,
    CredentialVaultType,
    CreditCardCredentialResponse,
    PasswordCredentialResponse,
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

    async def get_credential(self, organization_id: str, credential_id: str) -> CredentialResponse:
        organization_bitwarden_collection = await app.DATABASE.get_organization_bitwarden_collection(organization_id)
        if not organization_bitwarden_collection:
            raise HTTPException(status_code=404, detail="Credential account not found. It might have been deleted.")

        credential = await app.DATABASE.get_credential(credential_id=credential_id, organization_id=organization_id)
        if not credential:
            raise HTTPException(status_code=404, detail="Credential not found")

        credential_item = await BitwardenService.get_credential_item(credential.item_id)
        if not credential_item:
            raise HTTPException(status_code=404, detail="Credential not found")

        if credential_item.credential_type == CredentialType.PASSWORD:
            credential_response = PasswordCredentialResponse(
                username=credential_item.credential.username,
                totp_type=credential.totp_type,
            )
            return CredentialResponse(
                credential=credential_response,
                credential_id=credential.credential_id,
                credential_type=credential_item.credential_type,
                name=credential_item.name,
            )
        if credential_item.credential_type == CredentialType.CREDIT_CARD:
            credential_response = CreditCardCredentialResponse(
                last_four=credential_item.credential.card_number[-4:],
                brand=credential_item.credential.card_brand,
            )
            return CredentialResponse(
                credential=credential_response,
                credential_id=credential.credential_id,
                credential_type=credential_item.credential_type,
                name=credential_item.name,
            )
        raise HTTPException(status_code=400, detail="Invalid credential type")

    async def get_credentials(self, organization_id: str, page: int, page_size: int) -> list[CredentialResponse]:
        organization_bitwarden_collection = await app.DATABASE.get_organization_bitwarden_collection(organization_id)
        if not organization_bitwarden_collection:
            return []

        credentials = await app.DATABASE.get_credentials(organization_id, page=page, page_size=page_size)
        items = await BitwardenService.get_collection_items(organization_bitwarden_collection.collection_id)

        response_items = []
        for credential in credentials:
            item = next((item for item in items if item.item_id == credential.item_id), None)
            if not item:
                LOG.warning(
                    "Credential item not found in vault",
                    credential_id=credential.credential_id,
                    item_id=credential.item_id,
                )
                continue
            if item.credential_type == CredentialType.PASSWORD:
                credential_response = PasswordCredentialResponse(
                    username=item.credential.username,
                    totp_type=credential.totp_type,
                )
                response_items.append(
                    CredentialResponse(
                        credential=credential_response,
                        credential_id=credential.credential_id,
                        credential_type=item.credential_type,
                        name=item.name,
                    )
                )
            elif item.credential_type == CredentialType.CREDIT_CARD:
                credential_response = CreditCardCredentialResponse(
                    last_four=item.credential.card_number[-4:],
                    brand=item.credential.card_brand,
                )
                response_items.append(
                    CredentialResponse(
                        credential=credential_response,
                        credential_id=credential.credential_id,
                        credential_type=item.credential_type,
                        name=item.name,
                    )
                )
        return response_items

    async def get_credential_item(self, db_credential: Credential) -> CredentialItem:
        return await BitwardenService.get_credential_item(db_credential.item_id)
