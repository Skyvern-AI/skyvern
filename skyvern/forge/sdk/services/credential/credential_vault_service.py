from abc import ABC, abstractmethod
from typing import Any

from skyvern.forge import app
from skyvern.forge.sdk.schemas.credentials import (
    CreateCredentialRequest,
    Credential,
    CredentialItem,
    CredentialType,
    CredentialVaultType,
    CreditCardBillingAddress,
    CreditCardCredential,
)


class CredentialVaultService(ABC):
    """Abstract interface for credential vault services.

    This interface defines the contract for storing and retrieving credentials
    from different vault providers (e.g., Bitwarden, OnePassword, AWS Secrets Manager).
    """

    @abstractmethod
    async def create_credential(self, organization_id: str, data: CreateCredentialRequest) -> Credential:
        """Create a new credential in the vault and database."""

    @abstractmethod
    async def update_credential(self, credential: Credential, data: CreateCredentialRequest) -> Credential:
        """Update an existing credential's vault data. Returns the updated credential."""

    @abstractmethod
    async def delete_credential(self, credential: Credential) -> None:
        """Delete a credential from the vault and database."""

    async def post_delete_credential_item(self, item_id: str, organization_id: str | None = None) -> None:
        """
        Optional hook for scheduling background cleanup tasks after credential deletion.
        Default implementation does nothing. Override in subclasses as needed.
        """

    @abstractmethod
    async def get_credential_item(self, db_credential: Credential) -> CredentialItem:
        """Retrieve the full credential data from the vault."""

    async def _preserve_omitted_credit_card_fields(
        self,
        credential: Credential,
        updated_credential: CreditCardCredential,
    ) -> CreditCardCredential:
        updated_fields = updated_credential.model_fields_set
        if {
            "billing_address",
            "billing_email",
            "billing_phone",
            "metadata",
        }.issubset(updated_fields):
            return updated_credential

        existing_item = await self.get_credential_item(credential)
        if not isinstance(existing_item.credential, CreditCardCredential):
            return updated_credential

        preserved_fields: dict[str, object] = {}
        existing_credential = existing_item.credential
        if "billing_address" not in updated_fields:
            preserved_fields["billing_address"] = existing_credential.billing_address
        elif updated_credential.billing_address and existing_credential.billing_address:
            preserved_fields["billing_address"] = self._preserve_omitted_billing_address_fields(
                existing_address=existing_credential.billing_address,
                updated_address=updated_credential.billing_address,
            )

        for field_name in ("billing_email", "billing_phone", "metadata"):
            if field_name not in updated_fields:
                preserved_fields[field_name] = getattr(existing_credential, field_name)

        return updated_credential.model_copy(update=preserved_fields)

    @staticmethod
    def _preserve_omitted_billing_address_fields(
        existing_address: CreditCardBillingAddress,
        updated_address: CreditCardBillingAddress,
    ) -> CreditCardBillingAddress:
        preserved_fields = {}
        updated_fields = updated_address.model_fields_set
        for field_name in (
            "line1",
            "line2",
            "city",
            "state",
            "state_code",
            "postal_code",
            "country",
            "country_code",
        ):
            preserved_fields[field_name] = (
                getattr(updated_address, field_name)
                if field_name in updated_fields
                else getattr(existing_address, field_name)
            )
        return updated_address.model_copy(update=preserved_fields)

    @staticmethod
    async def _create_db_credential(
        organization_id: str,
        data: CreateCredentialRequest,
        item_id: str,
        vault_type: CredentialVaultType,
    ) -> Credential:
        if data.credential_type == CredentialType.PASSWORD:
            return await app.DATABASE.credentials.create_credential(
                organization_id=organization_id,
                name=data.name,
                vault_type=vault_type,
                item_id=item_id,
                credential_type=data.credential_type,
                username=data.credential.username,
                totp_type=data.credential.totp_type,
                totp_identifier=data.credential.totp_identifier,
                card_last4=None,
                card_brand=None,
                tested_url=data.tested_url,
                proxy_location=data.proxy_location,
                proxy_session_id=data.proxy_session_id,
            )
        elif data.credential_type == CredentialType.CREDIT_CARD:
            return await app.DATABASE.credentials.create_credential(
                organization_id=organization_id,
                name=data.name,
                vault_type=vault_type,
                item_id=item_id,
                credential_type=data.credential_type,
                username=None,
                totp_type="none",
                card_last4=data.credential.card_number[-4:],
                card_brand=data.credential.card_brand,
                totp_identifier=None,
                tested_url=data.tested_url,
                proxy_location=data.proxy_location,
                proxy_session_id=data.proxy_session_id,
            )
        elif data.credential_type == CredentialType.SECRET:
            return await app.DATABASE.credentials.create_credential(
                organization_id=organization_id,
                name=data.name,
                vault_type=vault_type,
                item_id=item_id,
                credential_type=data.credential_type,
                username=None,
                totp_type="none",
                card_last4=None,
                card_brand=None,
                totp_identifier=None,
                secret_label=data.credential.secret_label,
                tested_url=data.tested_url,
                proxy_location=data.proxy_location,
                proxy_session_id=data.proxy_session_id,
            )
        else:
            raise Exception(f"Unsupported credential type: {data.credential_type}")

    @staticmethod
    async def _update_db_credential(
        credential: Credential,
        data: CreateCredentialRequest,
        item_id: str,
    ) -> Credential:
        proxy_kwargs: dict[str, Any] = {}
        if "proxy_location" in data.model_fields_set:
            proxy_kwargs["proxy_location"] = data.proxy_location
        if "proxy_session_id" in data.model_fields_set:
            proxy_kwargs["proxy_session_id"] = data.proxy_session_id
        if data.rotate_proxy_session_id:
            proxy_kwargs["rotate_proxy_session_id"] = True

        if data.credential_type == CredentialType.PASSWORD:
            return await app.DATABASE.credentials.update_credential_vault_data(
                credential_id=credential.credential_id,
                organization_id=credential.organization_id,
                item_id=item_id,
                name=data.name,
                credential_type=data.credential_type,
                username=data.credential.username,
                totp_type=data.credential.totp_type,
                totp_identifier=data.credential.totp_identifier,
                card_last4=None,
                card_brand=None,
                tested_url=data.tested_url,
                **proxy_kwargs,
            )
        elif data.credential_type == CredentialType.CREDIT_CARD:
            return await app.DATABASE.credentials.update_credential_vault_data(
                credential_id=credential.credential_id,
                organization_id=credential.organization_id,
                item_id=item_id,
                name=data.name,
                credential_type=data.credential_type,
                username=None,
                totp_type="none",
                card_last4=data.credential.card_number[-4:],
                card_brand=data.credential.card_brand,
                totp_identifier=None,
                tested_url=data.tested_url,
                **proxy_kwargs,
            )
        elif data.credential_type == CredentialType.SECRET:
            return await app.DATABASE.credentials.update_credential_vault_data(
                credential_id=credential.credential_id,
                organization_id=credential.organization_id,
                item_id=item_id,
                name=data.name,
                credential_type=data.credential_type,
                username=None,
                totp_type="none",
                card_last4=None,
                card_brand=None,
                totp_identifier=None,
                secret_label=data.credential.secret_label,
                tested_url=data.tested_url,
                **proxy_kwargs,
            )
        else:
            raise Exception(f"Unsupported credential type: {data.credential_type}")
