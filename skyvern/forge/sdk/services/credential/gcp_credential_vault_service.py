import re
import uuid
from typing import Annotated, Any, ClassVar, Literal, Union

import structlog
from pydantic import BaseModel, Field, TypeAdapter

from skyvern.config import settings
from skyvern.forge import app
from skyvern.forge.sdk.api.gcp import AsyncGcpSecretManagerClient
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


class GcpCredentialVaultService(CredentialVaultService):
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

    # Built once: TypeAdapter rebuilds the discriminated-union validator on every
    # construction, so hoisting it off the per-read path matters on the credential
    # hot path.
    _CREDENTIAL_DATA_ADAPTER: ClassVar[TypeAdapter[Any]] = TypeAdapter(_CredentialDataImage)

    # GCP Secret Manager secret IDs accept only [A-Za-z0-9_-] (max 255 chars).
    _SECRET_ID_PATTERN: ClassVar[re.Pattern[str]] = re.compile(r"^[A-Za-z0-9_-]{1,255}$")

    def __init__(self, client: AsyncGcpSecretManagerClient, project_id: str):
        self._client = client
        self._project_id = project_id

    async def create_credential(self, organization_id: str, data: CreateCredentialRequest) -> Credential:
        item_id = await self._create_gcp_secret_item(
            organization_id=organization_id,
            credential=data.credential,
        )

        credential = await self._create_db_credential(
            organization_id=organization_id,
            data=data,
            item_id=item_id,
            vault_type=CredentialVaultType.GCP,
        )

        return credential

    async def update_credential(self, credential: Credential, data: CreateCredentialRequest) -> Credential:
        credential_data = data.credential
        if data.credential_type == CredentialType.CREDIT_CARD and isinstance(credential_data, CreditCardCredential):
            credential_data = await self._preserve_omitted_credit_card_fields(
                credential=credential,
                updated_credential=credential_data,
            )

        # Updating a Secret Manager secret adds a new version under the same
        # secret id, so we reuse item_id. NOTE: if the DB update below fails the
        # vault holds the new value while DB metadata (name, type, username) is
        # stale; the credential data itself is correct and a retry reconciles
        # the metadata.
        await self._update_gcp_secret_item(
            item_id=credential.item_id,
            credential=credential_data,
        )

        try:
            updated_credential = await self._update_db_credential(
                credential=credential,
                data=data,
                item_id=credential.item_id,
            )
        except Exception:
            LOG.error(
                "DB update failed after GCP Secret Manager secret was already updated. "
                "Vault data is updated but DB metadata may be stale.",
                organization_id=credential.organization_id,
                credential_id=credential.credential_id,
                item_id=credential.item_id,
            )
            raise

        return updated_credential

    async def delete_credential(self, credential: Credential) -> None:
        # Deliberate deviation from Azure (which empties the value, then deletes
        # the secret in a background task): GCP can't cheaply "empty" a secret,
        # so we delete it synchronously to guarantee the value is gone the moment
        # DELETE returns. Delete the secret BEFORE the DB row so a transient
        # Secret Manager error leaves a consistent, retryable state instead of an
        # orphaned secret. (The route still schedules the base no-op
        # post_delete_credential_item afterwards, which is harmless here.)
        await self._client.delete_secret(secret_id=credential.item_id, project_id=self._project_id)
        await app.DATABASE.credentials.delete_credential(credential.credential_id, credential.organization_id)

    async def get_credential_item(self, db_credential: Credential) -> CredentialItem:
        secret_json_str = await self._client.get_secret(secret_id=db_credential.item_id, project_id=self._project_id)
        if secret_json_str is None:
            raise ValueError(f"GCP Credential Vault secret not found for {db_credential.item_id}")

        data = GcpCredentialVaultService._CREDENTIAL_DATA_ADAPTER.validate_json(secret_json_str)
        if isinstance(data, GcpCredentialVaultService._PasswordCredentialDataImage):
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
        elif isinstance(data, GcpCredentialVaultService._CreditCardCredentialDataImage):
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
        elif isinstance(data, GcpCredentialVaultService._SecretCredentialDataImage):
            return CredentialItem(
                item_id=db_credential.item_id,
                credential=SecretCredential(secret_value=data.secret_value, secret_label=data.secret_label),
                name=db_credential.name,
                credential_type=CredentialType.SECRET,
            )
        else:
            raise TypeError(f"Invalid credential type: {type(data)}")

    def _build_data_image(self, credential: PasswordCredential | CreditCardCredential | SecretCredential) -> BaseModel:
        if isinstance(credential, PasswordCredential):
            return GcpCredentialVaultService._PasswordCredentialDataImage(
                type="password",
                username=credential.username,
                password=credential.password,
                totp=credential.totp,
            )
        elif isinstance(credential, CreditCardCredential):
            return GcpCredentialVaultService._CreditCardCredentialDataImage(
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
            return GcpCredentialVaultService._SecretCredentialDataImage(
                type="secret",
                secret_value=credential.secret_value,
                secret_label=credential.secret_label,
            )
        else:
            raise TypeError(f"Invalid credential type: {type(credential)}")

    async def _create_gcp_secret_item(
        self,
        organization_id: str,
        credential: PasswordCredential | CreditCardCredential | SecretCredential,
    ) -> str:
        data = self._build_data_image(credential)
        secret_id = f"{settings.GCP_CREDENTIAL_VAULT_PREFIX}{organization_id}-{uuid.uuid4()}"
        if not self._SECRET_ID_PATTERN.match(secret_id):
            raise ValueError(f"Constructed GCP secret id is not valid for Secret Manager: {secret_id!r}")
        secret_value = data.model_dump_json(exclude_none=True)

        return await self._client.create_or_update_secret(
            secret_id=secret_id,
            project_id=self._project_id,
            value=secret_value,
        )

    async def _update_gcp_secret_item(
        self,
        item_id: str,
        credential: PasswordCredential | CreditCardCredential | SecretCredential,
    ) -> None:
        data = self._build_data_image(credential)
        secret_value = data.model_dump_json(exclude_none=True)

        await self._client.create_or_update_secret(
            secret_id=item_id,
            project_id=self._project_id,
            value=secret_value,
        )
