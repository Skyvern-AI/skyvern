from typing import Protocol, Self

from skyvern.forge.sdk.schemas.organizations import AzureClientSecretCredential


class AsyncAzureVaultClient(Protocol):
    """Protocol defining the interface for Azure Vault clients.

    This client provides methods to interact with Azure Key Vault for secret management.
    """

    async def __aenter__(self) -> Self:
        """Enter async context manager."""
        ...

    async def __aexit__(
        self, exc_type: type[BaseException] | None, exc_val: BaseException | None, exc_tb: object
    ) -> None:
        """Exit async context manager and cleanup resources."""
        ...

    async def get_secret(self, secret_name: str, vault_name: str) -> str | None:
        """Retrieve a secret from Azure Key Vault.

        Args:
            secret_name: The name of the secret to retrieve
            vault_name: The name of the Azure Key Vault

        Returns:
            The secret value as a string, or None if the secret doesn't exist or an error occurs
        """
        ...

    async def create_or_update_secret(self, secret_name: str, secret_value: str, vault_name: str) -> str:
        """Create or update a secret in Azure Key Vault.

        Args:
            secret_name: The name of the secret to create or update
            secret_value: The value to store
            vault_name: The name of the Azure Key Vault

        Returns:
            The name of the created/updated secret

        Raises:
            Exception: If the operation fails
        """
        ...

    async def delete_secret(self, secret_name: str, vault_name: str) -> str:
        """Delete a secret from Azure Key Vault.

        Args:
            secret_name: The name of the secret to delete
            vault_name: The name of the Azure Key Vault

        Returns:
            The name of the deleted secret

        Raises:
            Exception: If the operation fails
        """
        ...

    async def close(self) -> None:
        """Close the client and release all resources."""
        ...


class AsyncAzureStorageClient(Protocol):
    """Protocol defining the interface for Azure Storage clients.

    This client provides methods to interact with Azure Blob Storage for file operations.
    """

    async def upload_file_from_path(self, container_name: str, blob_name: str, file_path: str) -> None:
        """Upload a file from the local filesystem to Azure Blob Storage.

        Args:
            container_name: The name of the Azure Blob container
            blob_name: The name to give the blob in storage
            file_path: The local path to the file to upload

        Raises:
            Exception: If the upload fails
        """
        ...

    async def close(self) -> None:
        """Close the storage client and release resources."""
        ...


class AzureClientFactory(Protocol):
    """Protocol defining the interface for creating Azure Vault and Storage clients."""

    def create_default(self) -> "AsyncAzureVaultClient":
        """Create an Azure Vault client using default credentials.

        Returns:
            An AsyncAzureVaultClient instance using DefaultAzureCredential
        """
        ...

    def create_from_client_secret(self, credential: AzureClientSecretCredential) -> "AsyncAzureVaultClient":
        """Create an Azure Vault client using client secret credentials.

        Args:
            credential: Azure client secret credentials containing tenant_id, client_id, and client_secret

        Returns:
            An AsyncAzureVaultClient instance
        """
        ...

    def create_storage_client(self, storage_account_name: str, storage_account_key: str) -> "AsyncAzureStorageClient":
        """Create an Azure Storage client with the provided credentials.

        Args:
            storage_account_name: The name of the Azure storage account
            storage_account_key: The access key for the storage account

        Returns:
            An AsyncAzureStorageClient instance
        """
        ...
