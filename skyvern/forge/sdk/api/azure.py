from typing import Self

import structlog
from azure.identity.aio import ClientSecretCredential, DefaultAzureCredential
from azure.keyvault.secrets.aio import SecretClient
from azure.storage.blob.aio import BlobServiceClient

from skyvern.forge.sdk.schemas.organizations import AzureClientSecretCredential

LOG = structlog.get_logger()


class AsyncAzureVaultClient:
    def __init__(self, credential: ClientSecretCredential | DefaultAzureCredential) -> None:
        self.credential = credential

    async def __aenter__(self) -> Self:
        return self

    async def __aexit__(
        self, exc_type: type[BaseException] | None, exc_val: BaseException | None, exc_tb: object
    ) -> None:
        await self.credential.close()

    async def get_secret(self, secret_name: str, vault_name: str) -> str | None:
        secret_client = await self._get_secret_client(vault_name)
        try:
            secret = await secret_client.get_secret(secret_name)
            return secret.value
        except Exception as e:
            LOG.exception("Failed to get secret from Azure Key Vault.", secret_name=secret_name, error=e)
            return None
        finally:
            await secret_client.close()

    async def create_or_update_secret(self, secret_name: str, secret_value: str, vault_name: str) -> str:
        secret_client = await self._get_secret_client(vault_name)
        try:
            secret = await secret_client.set_secret(secret_name, secret_value)
            return secret.name
        except Exception as e:
            LOG.exception("Failed to create secret from Azure Key Vault.", secret_name=secret_name, error=e)
            raise e
        finally:
            await secret_client.close()

    async def delete_secret(self, secret_name: str, vault_name: str) -> str:
        secret_client = await self._get_secret_client(vault_name)
        try:
            secret = await secret_client.delete_secret(secret_name)
            return secret.name
        except Exception as e:
            LOG.exception("Failed to delete secret from Azure Key Vault.", secret_name=secret_name, error=e)
            raise e
        finally:
            await secret_client.close()

    async def _get_secret_client(self, vault_name: str) -> SecretClient:
        # Azure Key Vault URL format: https://<your-key-vault-name>.vault.azure.net
        # Assuming the secret_name is actually the Key Vault URL and the secret name
        # This needs to be clarified or passed as separate parameters
        # For now, let's assume secret_name is the actual secret name and Key Vault URL is in settings.
        key_vault_url = f"https://{vault_name}.vault.azure.net"  # Placeholder, adjust as needed
        return SecretClient(vault_url=key_vault_url, credential=self.credential)

    async def close(self) -> None:
        await self.credential.close()

    @classmethod
    def create_default(cls) -> "AsyncAzureVaultClient":
        return cls(DefaultAzureCredential())

    @classmethod
    def create_from_client_secret(
        cls,
        credential: AzureClientSecretCredential,
    ) -> "AsyncAzureVaultClient":
        cred = ClientSecretCredential(
            tenant_id=credential.tenant_id,
            client_id=credential.client_id,
            client_secret=credential.client_secret,
        )
        return cls(cred)


class AsyncAzureStorageClient:
    def __init__(self, storage_account_name: str, storage_account_key: str):
        self.blob_service_client = BlobServiceClient(
            account_url=f"https://{storage_account_name}.blob.core.windows.net",
            credential=storage_account_key,
        )

    async def upload_file_from_path(self, container_name: str, blob_name: str, file_path: str) -> None:
        try:
            container_client = self.blob_service_client.get_container_client(container_name)
            # Create the container if it doesn't exist
            try:
                await container_client.create_container()
            except Exception as e:
                LOG.info("Azure container already exists or failed to create", container_name=container_name, error=e)

            with open(file_path, "rb") as data:
                await container_client.upload_blob(name=blob_name, data=data, overwrite=True)
            LOG.info("File uploaded to Azure Blob Storage", container_name=container_name, blob_name=blob_name)
        except Exception as e:
            LOG.error(
                "Failed to upload file to Azure Blob Storage",
                container_name=container_name,
                blob_name=blob_name,
                error=e,
            )
            raise e

    async def close(self) -> None:
        await self.blob_service_client.close()
