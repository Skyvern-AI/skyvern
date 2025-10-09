from abc import ABC, abstractmethod

from skyvern.forge.sdk.schemas.credentials import (
    CreateCredentialRequest,
    Credential,
    CredentialItem,
    CredentialResponse,
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

    @abstractmethod
    async def get_credential(self, organization_id: str, credential_id: str) -> CredentialResponse:
        """Retrieve a credential with masked sensitive data."""

    @abstractmethod
    async def get_credentials(self, organization_id: str, page: int, page_size: int) -> list[CredentialResponse]:
        """Retrieve all credentials for an organization with pagination."""

    @abstractmethod
    async def get_credential_item(self, db_credential: Credential) -> CredentialItem:
        """Retrieve the full credential data from the vault."""
