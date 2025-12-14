from abc import ABC, abstractmethod

from skyvern.forge import app
from skyvern.forge.sdk.schemas.credentials import (
    CreateCredentialRequest,
    Credential,
    CredentialItem,
    CredentialType,
    CredentialVaultType,
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
    async def delete_credential(self, credential: Credential) -> None:
        """Delete a credential from the vault and database."""

    async def post_delete_credential_item(self, item_id: str) -> None:
        """
        Optional hook for scheduling background cleanup tasks after credential deletion.
        Default implementation does nothing. Override in subclasses as needed.
        """

    @abstractmethod
    async def get_credential_item(self, db_credential: Credential) -> CredentialItem:
        """Retrieve the full credential data from the vault."""

    @staticmethod
    async def _create_db_credential(
        organization_id: str,
        data: CreateCredentialRequest,
        item_id: str,
        vault_type: CredentialVaultType,
    ) -> Credential:
        if data.credential_type == CredentialType.PASSWORD:
            return await app.DATABASE.create_credential(
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
            )
        elif data.credential_type == CredentialType.CREDIT_CARD:
            return await app.DATABASE.create_credential(
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
            )
        elif data.credential_type == CredentialType.SECRET:
            return await app.DATABASE.create_credential(
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
            )
        else:
            raise Exception(f"Unsupported credential type: {data.credential_type}")
