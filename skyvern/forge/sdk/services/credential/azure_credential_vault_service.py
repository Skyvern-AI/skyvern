import uuid
from typing import Annotated, Literal, Union

import structlog
from pydantic import BaseModel, Field, TypeAdapter

from skyvern.forge import app
from skyvern.forge.sdk.api.azure import AsyncAzureVaultClient
from skyvern.forge.sdk.schemas.credentials import (
    CreateCredentialRequest,
    Credential,
    CredentialItem,
    CredentialType,
    CredentialVaultType,
    CreditCardCredential,
    PasswordCredential,
    SecretCredential,
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

    class _SecretCredentialDataImage(BaseModel):
        type: Literal["secret"]
        secret_value: str
        secret_label: str | None = None

    _CredentialDataImage = Annotated[
        Union[_PasswordCredentialDataImage, _CreditCardCredentialDataImage, _SecretCredentialDataImage],
        Field(discriminator="type"),
    ]

    def __init__(self, client: AsyncAzureVaultClient, vault_name: str):
        self._client = client
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
        elif isinstance(data, AzureCredentialVaultService._SecretCredentialDataImage):
            return CredentialItem(
                item_id=db_credential.item_id,
                credential=SecretCredential(secret_value=data.secret_value, secret_label=data.secret_label),
                name=db_credential.name,
                credential_type=CredentialType.SECRET,
            )
        else:
            raise TypeError(f"Invalid credential type: {type(data)}")

    async def _create_azure_secret_item(
        self,
        organization_id: str,
        credential: PasswordCredential | CreditCardCredential | SecretCredential,
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
        elif isinstance(credential, SecretCredential):
            data = AzureCredentialVaultService._SecretCredentialDataImage(
                type="secret",
                secret_value=credential.secret_value,
                secret_label=credential.secret_label,
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
