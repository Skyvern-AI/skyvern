import uuid
from typing import Annotated, Any, ClassVar, Literal, Union

import structlog
from pydantic import BaseModel, Field, TypeAdapter

from skyvern.exceptions import CredentialItemNotFoundError
from skyvern.forge import app
from skyvern.forge.sdk.api.aws import AsyncAWSClient
from skyvern.forge.sdk.schemas.credentials import (
    CreateCredentialRequest,
    Credential,
    CredentialItem,
    CredentialType,
    CredentialVaultType,
    CreditCardBillingAddress,
    CreditCardCredential,
    PasswordCredential,
    SecretCredential,
)
from skyvern.forge.sdk.services.credential.credential_vault_service import CredentialVaultService

LOG = structlog.get_logger()


class AwsSecretsManagerCredentialVaultService(CredentialVaultService):
    class _PasswordCredentialDataImage(BaseModel):
        type: Literal["password"]
        password: str
        username: str
        totp: str | None = None
        metadata: dict[str, str] | None = None

    class _CreditCardCredentialDataImage(BaseModel):
        type: Literal["credit_card"]
        card_number: str
        card_cvv: str
        card_exp_month: str
        card_exp_year: str
        card_brand: str
        card_holder_name: str
        billing_address: CreditCardBillingAddress | None = None
        billing_email: str | None = None
        billing_phone: str | None = None
        metadata: dict[str, str] | None = None

    class _SecretCredentialDataImage(BaseModel):
        type: Literal["secret"]
        secret_value: str
        secret_label: str | None = None

    _CredentialDataImage = Annotated[
        Union[_PasswordCredentialDataImage, _CreditCardCredentialDataImage, _SecretCredentialDataImage],
        Field(discriminator="type"),
    ]

    _CREDENTIAL_DATA_ADAPTER: ClassVar[TypeAdapter[Any]] = TypeAdapter(_CredentialDataImage)

    def __init__(self, client: AsyncAWSClient, name_prefix: str, kms_key_id: str | None = None) -> None:
        self._client = client
        self._name_prefix = name_prefix
        self._kms_key_id = kms_key_id

    async def create_credential(
        self,
        organization_id: str,
        data: CreateCredentialRequest,
        created_by: str | None = None,
        is_passkey_setup_pending: bool = False,
    ) -> Credential:
        # The ID is fixed before CreateSecret so a create whose response is lost can still be reclaimed.
        item_id = self._new_item_id(organization_id)
        try:
            await self._create_secret(item_id, data.credential)
            return await self._create_db_credential(
                organization_id=organization_id,
                data=data,
                item_id=item_id,
                vault_type=CredentialVaultType.AWS_SECRETS_MANAGER,
                created_by=created_by,
                is_passkey_setup_pending=is_passkey_setup_pending,
            )
        except BaseException:
            await self._reclaim_orphaned_vault_item(
                delete=lambda: self._client.delete_secret_permanently(item_id),
                organization_id=organization_id,
                item_id=item_id,
                vault_type=CredentialVaultType.AWS_SECRETS_MANAGER,
            )
            raise

    async def update_credential(self, credential: Credential, data: CreateCredentialRequest) -> Credential:
        credential_data = data.credential
        if data.credential_type == CredentialType.PASSWORD and isinstance(credential_data, PasswordCredential):
            credential_data = await self._preserve_omitted_password_fields(
                credential=credential,
                updated_credential=credential_data,
            )
        elif data.credential_type == CredentialType.CREDIT_CARD and isinstance(credential_data, CreditCardCredential):
            credential_data = await self._preserve_omitted_credit_card_fields(
                credential=credential,
                updated_credential=credential_data,
            )

        # A new secret per update, as Bitwarden gets a new item: the route's concurrent-write check compares item IDs, and
        # the route deletes the replaced secret, so no earlier value stays readable.
        item_id = self._new_item_id(credential.organization_id)
        try:
            await self._create_secret(item_id, credential_data)
            return await self._update_db_credential(credential=credential, data=data, item_id=item_id)
        except BaseException:
            await self._reclaim_orphaned_vault_item(
                delete=lambda: self._client.delete_secret_permanently(item_id),
                organization_id=credential.organization_id,
                item_id=item_id,
                vault_type=CredentialVaultType.AWS_SECRETS_MANAGER,
            )
            raise

    async def delete_credential(self, credential: Credential) -> None:
        # Delete the secret before the row, so a transient Secrets Manager error leaves a retryable state.
        await self._client.delete_secret_permanently(credential.item_id)
        await app.DATABASE.credentials.delete_credential(credential.credential_id, credential.organization_id)

    async def post_delete_credential_item(self, item_id: str, organization_id: str | None = None) -> bool:
        # The reaper drops its pending record only on True, so the secret must really be gone.
        try:
            await self._client.delete_secret_permanently(item_id)
            return True
        except Exception:
            LOG.warning(
                "Failed to delete AWS Secrets Manager credential item in background cleanup",
                item_id=item_id,
                exc_info=True,
            )
            return False

    async def get_credential_item(self, db_credential: Credential) -> CredentialItem:
        secret_json_str = await self._client.get_secret_if_exists(db_credential.item_id)
        if secret_json_str is None:
            raise CredentialItemNotFoundError(f"AWS Secrets Manager secret not found for {db_credential.item_id}")

        data = AwsSecretsManagerCredentialVaultService._CREDENTIAL_DATA_ADAPTER.validate_json(secret_json_str)
        if isinstance(data, AwsSecretsManagerCredentialVaultService._PasswordCredentialDataImage):
            return CredentialItem(
                item_id=db_credential.item_id,
                credential=PasswordCredential(
                    username=data.username,
                    password=data.password,
                    totp=data.totp,
                    totp_type=db_credential.totp_type,
                    metadata=data.metadata,
                ),
                name=db_credential.name,
                credential_type=CredentialType.PASSWORD,
            )
        elif isinstance(data, AwsSecretsManagerCredentialVaultService._CreditCardCredentialDataImage):
            return CredentialItem(
                item_id=db_credential.item_id,
                credential=CreditCardCredential(
                    card_holder_name=data.card_holder_name,
                    card_number=data.card_number,
                    card_exp_month=data.card_exp_month,
                    card_exp_year=data.card_exp_year,
                    card_cvv=data.card_cvv,
                    card_brand=data.card_brand,
                    billing_address=data.billing_address,
                    billing_email=data.billing_email,
                    billing_phone=data.billing_phone,
                    metadata=data.metadata,
                ),
                name=db_credential.name,
                credential_type=CredentialType.CREDIT_CARD,
            )
        elif isinstance(data, AwsSecretsManagerCredentialVaultService._SecretCredentialDataImage):
            return CredentialItem(
                item_id=db_credential.item_id,
                credential=SecretCredential(secret_value=data.secret_value, secret_label=data.secret_label),
                name=db_credential.name,
                credential_type=CredentialType.SECRET,
            )
        else:
            raise TypeError(f"Invalid credential type: {type(data)}")

    def _new_item_id(self, organization_id: str) -> str:
        return f"{self._name_prefix}{organization_id}/{uuid.uuid4()}"

    async def _create_secret(
        self, item_id: str, credential: PasswordCredential | CreditCardCredential | SecretCredential
    ) -> None:
        await self._client.create_secret(
            item_id,
            self._build_data_image(credential).model_dump_json(exclude_none=True),
            kms_key_id=self._kms_key_id,
        )

    def _build_data_image(self, credential: PasswordCredential | CreditCardCredential | SecretCredential) -> BaseModel:
        if isinstance(credential, PasswordCredential):
            return AwsSecretsManagerCredentialVaultService._PasswordCredentialDataImage(
                type="password",
                username=credential.username,
                password=credential.password,
                totp=credential.totp,
                metadata=credential.metadata,
            )
        elif isinstance(credential, CreditCardCredential):
            return AwsSecretsManagerCredentialVaultService._CreditCardCredentialDataImage(
                type="credit_card",
                card_number=credential.card_number,
                card_cvv=credential.card_cvv,
                card_exp_month=credential.card_exp_month,
                card_exp_year=credential.card_exp_year,
                card_brand=credential.card_brand,
                card_holder_name=credential.card_holder_name,
                billing_address=credential.billing_address,
                billing_email=credential.billing_email,
                billing_phone=credential.billing_phone,
                metadata=credential.metadata,
            )
        elif isinstance(credential, SecretCredential):
            return AwsSecretsManagerCredentialVaultService._SecretCredentialDataImage(
                type="secret",
                secret_value=credential.secret_value,
                secret_label=credential.secret_label,
            )
        else:
            raise TypeError(f"Invalid credential type: {type(credential)}")
