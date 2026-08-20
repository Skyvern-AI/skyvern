import structlog
from fastapi import HTTPException

from skyvern.exceptions import HttpException
from skyvern.forge import app
from skyvern.forge.sdk.schemas.credentials import (
    CreateCredentialRequest,
    Credential,
    CredentialItem,
    CredentialType,
    CredentialVaultType,
    CreditCardCredential,
    PasswordCredential,
)
from skyvern.forge.sdk.services.bitwarden import BitwardenService
from skyvern.forge.sdk.services.credential.credential_vault_service import CredentialVaultService

LOG = structlog.get_logger()


class BitwardenCredentialVaultService(CredentialVaultService):
    async def create_credential(self, organization_id: str, data: CreateCredentialRequest) -> Credential:
        org_collection = await app.DATABASE.credentials.get_organization_bitwarden_collection(organization_id)

        if not org_collection:
            LOG.info(
                "There is no collection for the organization. Creating new collection.",
                organization_id=organization_id,
            )
            collection_id = await BitwardenService.create_collection(
                name=organization_id,
            )
            org_collection = await app.DATABASE.credentials.create_organization_bitwarden_collection(
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
        org_collection = await app.DATABASE.credentials.get_organization_bitwarden_collection(
            credential.organization_id
        )

        if not org_collection:
            raise HTTPException(status_code=404, detail="Credential account not found. It might have been deleted.")

        credential_data = data.credential
        if data.credential_type == CredentialType.PASSWORD and isinstance(credential_data, PasswordCredential):
            credential_data = await self._preserve_omitted_password_metadata(
                credential=credential,
                updated_credential=credential_data,
            )
        elif data.credential_type == CredentialType.CREDIT_CARD and isinstance(credential_data, CreditCardCredential):
            credential_data = await self._preserve_omitted_credit_card_fields(
                credential=credential,
                updated_credential=credential_data,
            )

        # Create new vault item with the updated data
        new_item_id = await BitwardenService.create_credential_item(
            collection_id=org_collection.collection_id,
            name=data.name,
            credential=credential_data,
        )

        # Update DB record to point to the new vault item
        try:
            updated_credential = await self._update_db_credential(
                credential=credential,
                data=data,
                item_id=new_item_id,
            )
        except BaseException:
            LOG.warning(
                "DB update failed; reclaiming the new Bitwarden vault item",
                organization_id=credential.organization_id,
                new_item_id=new_item_id,
            )
            await self._reclaim_orphaned_vault_item(
                delete=lambda: BitwardenService.delete_credential_item(new_item_id),
                organization_id=credential.organization_id,
                item_id=new_item_id,
                vault_type=CredentialVaultType.BITWARDEN,
            )
            raise

        return updated_credential

    async def delete_credential(
        self,
        credential: Credential,
    ) -> None:
        organization_bitwarden_collection = await app.DATABASE.credentials.get_organization_bitwarden_collection(
            credential.organization_id
        )
        if not organization_bitwarden_collection:
            raise HTTPException(status_code=404, detail="Credential account not found. It might have been deleted.")

        await app.DATABASE.credentials.delete_credential(credential.credential_id, credential.organization_id)
        await self._run_task_to_completion(
            self._delete_credential_item_or_enqueue_cleanup(credential),
            cancellation_failure_message="Bitwarden vault-item cleanup failed while the delete was being cancelled",
            log_context={
                "credential_id": credential.credential_id,
                "item_id": credential.item_id,
                "organization_id": credential.organization_id,
            },
        )

    async def _delete_credential_item_or_enqueue_cleanup(self, credential: Credential) -> None:
        try:
            await BitwardenService.delete_credential_item(credential.item_id)
        except BaseException as exc:
            if isinstance(exc, HttpException) and exc.status_code == 404:
                return
            try:
                cleanup_enqueued = await self._enqueue_orphaned_vault_item_cleanup(
                    organization_id=credential.organization_id,
                    item_id=credential.item_id,
                    vault_type=CredentialVaultType.BITWARDEN,
                )
            except Exception:
                if isinstance(exc, Exception):
                    raise
                # Preserve provider cancellation even when its fallback enqueue also fails.
            else:
                log = LOG.warning if cleanup_enqueued else LOG.error
                message = (
                    "Bitwarden vault-item delete failed after DB row deletion; enqueued durable cleanup"
                    if cleanup_enqueued
                    else "Bitwarden vault-item delete failed after DB row deletion; durable cleanup unavailable"
                )
                log(
                    message,
                    organization_id=credential.organization_id,
                    item_id=credential.item_id,
                    error_type=type(exc).__name__,
                )
            if not isinstance(exc, Exception):
                raise

    async def post_delete_credential_item(self, item_id: str, organization_id: str | None = None) -> bool:
        try:
            await BitwardenService.delete_credential_item(item_id)
            LOG.info(
                "Successfully deleted credential item from Bitwarden in background",
                item_id=item_id,
            )
            return True
        except Exception as exc:
            LOG.warning(
                "Failed to delete credential item from Bitwarden in background",
                item_id=item_id,
                error_type=type(exc).__name__,
            )
            return False

    async def get_credential_item(self, db_credential: Credential) -> CredentialItem:
        return await BitwardenService.get_credential_item(db_credential.item_id)
