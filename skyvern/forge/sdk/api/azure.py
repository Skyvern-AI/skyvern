import structlog
from azure.identity.aio import DefaultAzureCredential
from azure.keyvault.secrets.aio import SecretClient
from azure.storage.blob.aio import BlobServiceClient

LOG = structlog.get_logger()


class AsyncAzureClient:
    def __init__(self, account_name: str, account_key: str):
        self.account_name = account_name
        self.account_key = account_key
        self.blob_service_client = BlobServiceClient(
            account_url=f"https://{account_name}.blob.core.windows.net",
            credential=account_key,
        )
        self.credential = DefaultAzureCredential()

    async def get_secret(self, secret_name: str) -> str | None:
        try:
            # Azure Key Vault URL format: https://<your-key-vault-name>.vault.azure.net
            # Assuming the secret_name is actually the Key Vault URL and the secret name
            # This needs to be clarified or passed as separate parameters
            # For now, let's assume secret_name is the actual secret name and Key Vault URL is in settings.
            key_vault_url = f"https://{self.account_name}.vault.azure.net"  # Placeholder, adjust as needed
            secret_client = SecretClient(vault_url=key_vault_url, credential=self.credential)
            secret = await secret_client.get_secret(secret_name)
            return secret.value
        except Exception as e:
            LOG.exception("Failed to get secret from Azure Key Vault.", secret_name=secret_name, error=e)
            return None
        finally:
            await self.credential.close()

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
        await self.credential.close()
