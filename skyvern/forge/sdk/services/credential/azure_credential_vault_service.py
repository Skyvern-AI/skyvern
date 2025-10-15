import uuid
from typing import Annotated, Literal, Union

import structlog
from azure.identity.aio import ClientSecretCredential
from fastapi import HTTPException
from pydantic import BaseModel, Field, TypeAdapter

from skyvern.forge import app
from skyvern.forge.sdk.api.azure import AsyncAzureVaultClient
from skyvern.forge.sdk.schemas.credentials import (
    CreateCredentialRequest,
    Credential,
    CredentialItem,
    CredentialResponse,
    CredentialType,
    CredentialVaultType,
    CreditCardCredential,
    CreditCardCredentialResponse,
    PasswordCredential,
    PasswordCredentialResponse,
)
from skyvern.forge.sdk.services.credential.credential_vault_service import CredentialVaultService

LOG = structlog.get_logger()


class AzureCredentialVaultService(CredentialVaultService):
    class _PasswordCredentialDataImage(BaseModel):
        type: Literal["password"]
        password: str
        username: str
        totp: str | None = None

    class _CreditCardCredentialDataImage(BaseModel):
        type: Literal["credit_card"]
        card_number: str
        card_cvv: str
        card_exp_month: str
        card_exp_year: str
        card_brand: str
        card_holder_name: str

    _CredentialDataImage = Annotated[
        Union[_PasswordCredentialDataImage, _CreditCardCredentialDataImage], Field(discriminator="type")
    ]

    def __init__(self, tenant_id: str, client_id: str, client_secret: str, vault_name: str):
        self._client = AsyncAzureVaultClient(
            ClientSecretCredential(
                tenant_id=tenant_id,
                client_id=client_id,
                client_secret=client_secret,
            )
        )
        self._vault_name = vault_name

    async def create_credential(self, organization_id: str, data: CreateCredentialRequest) -> Credential:
        item_id = await self._create_azure_secret_item(
            organization_id=organization_id,
            credential=data.credential,
        )

        credential = await self._create_db_credential(
            organization_id=organization_id,
            data=data,
            item_id=item_id,
            vault_type=CredentialVaultType.AZURE_VAULT,
        )

        return credential

    async def delete_credential(
        self,
        credential: Credential,
    ) -> None:
        await app.DATABASE.delete_credential(credential.credential_id, credential.organization_id)
        # Deleting takes several seconds, so we empty the value and delete async so customers do not have to wait
        await self._client.create_or_update_secret(
            vault_name=self._vault_name,
            secret_name=credential.item_id,
            secret_value="",
        )

    async def post_delete_credential_item(self, item_id: str) -> None:
        """
        Background task to delete the credential item from Azure Key Vault.
        This allows the API to respond quickly while the deletion happens asynchronously.
        """
        try:
            LOG.info(
                "Deleting credential item from Azure Key Vault in background",
                item_id=item_id,
                vault_name=self._vault_name,
            )
            await self._client.delete_secret(secret_name=item_id, vault_name=self._vault_name)
            LOG.info(
                "Successfully deleted credential item from Azure Key Vault",
                item_id=item_id,
                vault_name=self._vault_name,
            )
        except Exception as e:
            LOG.exception(
                "Failed to delete credential item from Azure Key Vault in background",
                item_id=item_id,
                vault_name=self._vault_name,
                error=str(e),
            )

    async def get_credential(self, organization_id: str, credential_id: str) -> CredentialResponse:
        credential = await app.DATABASE.get_credential(credential_id=credential_id, organization_id=organization_id)
        if not credential:
            raise HTTPException(status_code=404, detail="Credential not found")

        return _convert_to_response(credential)

    async def get_credentials(self, organization_id: str, page: int, page_size: int) -> list[CredentialResponse]:
        credentials = await app.DATABASE.get_credentials(organization_id, page=page, page_size=page_size)
        return [_convert_to_response(credential) for credential in credentials]

    async def get_credential_item(self, db_credential: Credential) -> CredentialItem:
        secret_json_str = await self._client.get_secret(secret_name=db_credential.item_id, vault_name=self._vault_name)
        if secret_json_str is None:
            raise ValueError(f"Azure Credential Vault secret not found for {db_credential.item_id}")

        data = TypeAdapter(AzureCredentialVaultService._CredentialDataImage).validate_json(secret_json_str)
        if isinstance(data, AzureCredentialVaultService._PasswordCredentialDataImage):
            return CredentialItem(
                item_id=db_credential.item_id,
                credential=PasswordCredential(
                    username=data.username,
                    password=data.password,
                    totp=data.totp,
                    totp_type=db_credential.totp_type,
                ),
                name=db_credential.name,
                credential_type=CredentialType.PASSWORD,
            )
        elif isinstance(data, AzureCredentialVaultService._CreditCardCredentialDataImage):
            return CredentialItem(
                item_id=db_credential.item_id,
                credential=CreditCardCredential(
                    card_holder_name=data.card_holder_name,
                    card_number=data.card_number,
                    card_exp_month=data.card_exp_month,
                    card_exp_year=data.card_exp_year,
                    card_cvv=data.card_cvv,
                    card_brand=data.card_brand,
                ),
                name=db_credential.name,
                credential_type=CredentialType.CREDIT_CARD,
            )
        else:
            raise TypeError(f"Invalid credential type: {type(data)}")

    async def _create_azure_secret_item(
        self,
        organization_id: str,
        credential: PasswordCredential | CreditCardCredential,
    ) -> str:
        if isinstance(credential, PasswordCredential):
            data = AzureCredentialVaultService._PasswordCredentialDataImage(
                type="password",
                username=credential.username,
                password=credential.password,
                totp=credential.totp,
            )
        elif isinstance(credential, CreditCardCredential):
            data = AzureCredentialVaultService._CreditCardCredentialDataImage(
                type="credit_card",
                card_number=credential.card_number,
                card_cvv=credential.card_cvv,
                card_exp_month=credential.card_exp_month,
                card_exp_year=credential.card_exp_year,
                card_brand=credential.card_brand,
                card_holder_name=credential.card_holder_name,
            )
        else:
            raise TypeError(f"Invalid credential type: {type(credential)}")

        secret_name = f"{organization_id}-{uuid.uuid4()}".replace("_", "")
        secret_value = data.model_dump_json(exclude_none=True)

        return await self._client.create_or_update_secret(
            vault_name=self._vault_name,
            secret_name=secret_name,
            secret_value=secret_value,
        )


def _convert_to_response(credential: Credential) -> CredentialResponse:
    if credential.credential_type == CredentialType.PASSWORD:
        credential_response = PasswordCredentialResponse(
            username=credential.username or credential.credential_id,
            totp_type=credential.totp_type,
        )
        return CredentialResponse(
            credential=credential_response,
            credential_id=credential.credential_id,
            credential_type=credential.credential_type,
            name=credential.name,
        )
    elif credential.credential_type == CredentialType.CREDIT_CARD:
        credential_response = CreditCardCredentialResponse(
            last_four=credential.card_last4 or "****",
            brand=credential.card_brand or "Card Brand",
        )
        return CredentialResponse(
            credential=credential_response,
            credential_id=credential.credential_id,
            credential_type=credential.credential_type,
            name=credential.name,
        )
    else:
        raise HTTPException(status_code=400, detail="Credential type not supported")
