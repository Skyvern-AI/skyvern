"""Real implementations of Azure clients (Vault and Storage) and their factories."""

from datetime import datetime, timedelta, timezone
from mimetypes import add_type, guess_type
from typing import IO, Self

import structlog
from azure.core.exceptions import ResourceNotFoundError
from azure.identity.aio import ClientSecretCredential, DefaultAzureCredential
from azure.keyvault.secrets.aio import SecretClient
from azure.storage.blob import BlobSasPermissions, ContentSettings, StandardBlobTier, generate_blob_sas
from azure.storage.blob.aio import BlobServiceClient

from skyvern.config import settings
from skyvern.forge.sdk.api.azure import (
    AsyncAzureStorageClient,
    AsyncAzureVaultClient,
    AzureClientFactory,
    AzureUri,
)
from skyvern.forge.sdk.schemas.organizations import AzureClientSecretCredential

# Register custom mime types for mimetypes guessing
add_type("application/json", ".har")
add_type("text/plain", ".log")

LOG = structlog.get_logger()


class RealAsyncAzureVaultClient(AsyncAzureVaultClient):
    """Real implementation of Azure Vault client using Azure SDK."""

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


class RealAsyncAzureStorageClient(AsyncAzureStorageClient):
    """Async client for Azure Blob Storage operations. Implements AsyncAzureStorageClient protocol."""

    def __init__(
        self,
        account_name: str | None = None,
        account_key: str | None = None,
    ) -> None:
        self.account_name = account_name or settings.AZURE_STORAGE_ACCOUNT_NAME
        self.account_key = account_key or settings.AZURE_STORAGE_ACCOUNT_KEY

        if not self.account_name or not self.account_key:
            raise ValueError("Azure Storage account name and key are required")

        self._blob_service_client: BlobServiceClient | None = None
        self._verified_containers: set[str] = set()

    def _get_blob_service_client(self) -> BlobServiceClient:
        if self._blob_service_client is None:
            self._blob_service_client = BlobServiceClient(
                account_url=f"https://{self.account_name}.blob.core.windows.net",
                credential=self.account_key,
            )
        return self._blob_service_client

    async def _ensure_container_exists(self, container: str) -> None:
        if container in self._verified_containers:
            return
        client = self._get_blob_service_client()
        container_client = client.get_container_client(container)
        try:
            if not await container_client.exists():
                await container_client.create_container()
                LOG.info("Created Azure container", container=container)
        except Exception:
            LOG.debug("Container may already exist", container=container)
        self._verified_containers.add(container)

    async def upload_file(
        self,
        uri: str,
        data: bytes,
        tier: StandardBlobTier = StandardBlobTier.HOT,
        tags: dict[str, str] | None = None,
    ) -> None:
        parsed = AzureUri(uri)
        await self._ensure_container_exists(parsed.container)
        client = self._get_blob_service_client()
        container_client = client.get_container_client(parsed.container)
        await container_client.upload_blob(
            name=parsed.blob_path,
            data=data,
            overwrite=True,
            standard_blob_tier=tier,
            tags=tags,
        )

    async def upload_file_from_path(
        self,
        uri: str,
        file_path: str,
        tier: StandardBlobTier = StandardBlobTier.HOT,
        tags: dict[str, str] | None = None,
        metadata: dict[str, str] | None = None,
    ) -> None:
        parsed = AzureUri(uri)
        await self._ensure_container_exists(parsed.container)
        client = self._get_blob_service_client()
        container_client = client.get_container_client(parsed.container)
        content_type, _ = guess_type(file_path)
        content_settings = ContentSettings(content_type=content_type) if content_type else None
        with open(file_path, "rb") as f:
            await container_client.upload_blob(
                name=parsed.blob_path,
                data=f,
                overwrite=True,
                standard_blob_tier=tier,
                tags=tags,
                metadata=metadata,
                content_settings=content_settings,
            )

    async def upload_file_stream(
        self,
        uri: str,
        file_obj: IO[bytes],
        tier: StandardBlobTier = StandardBlobTier.HOT,
        tags: dict[str, str] | None = None,
        metadata: dict[str, str] | None = None,
    ) -> str:
        parsed = AzureUri(uri)
        await self._ensure_container_exists(parsed.container)
        client = self._get_blob_service_client()
        container_client = client.get_container_client(parsed.container)
        await container_client.upload_blob(
            name=parsed.blob_path,
            data=file_obj,
            overwrite=True,
            standard_blob_tier=tier,
            tags=tags,
            metadata=metadata,
        )
        return uri

    async def download_file(self, uri: str, log_exception: bool = True) -> bytes | None:
        parsed = AzureUri(uri)
        try:
            client = self._get_blob_service_client()
            container_client = client.get_container_client(parsed.container)
            blob_client = container_client.get_blob_client(parsed.blob_path)
            download = await blob_client.download_blob()
            return await download.readall()
        except ResourceNotFoundError:
            if log_exception:
                LOG.warning("Azure blob not found", uri=uri)
            return None
        except Exception:
            if log_exception:
                LOG.exception("Failed to download from Azure", uri=uri)
            return None

    async def get_blob_properties(self, uri: str) -> dict | None:
        parsed = AzureUri(uri)
        try:
            client = self._get_blob_service_client()
            container_client = client.get_container_client(parsed.container)
            blob_client = container_client.get_blob_client(parsed.blob_path)
            props = await blob_client.get_blob_properties()
            return {
                "size": props.size,
                "content_type": props.content_settings.content_type if props.content_settings else None,
                "last_modified": props.last_modified,
                "etag": props.etag,
                "metadata": props.metadata,
            }
        except ResourceNotFoundError:
            return None
        except Exception:
            LOG.exception("Failed to get blob properties", uri=uri)
            return None

    async def blob_exists(self, uri: str) -> bool:
        parsed = AzureUri(uri)
        try:
            client = self._get_blob_service_client()
            container_client = client.get_container_client(parsed.container)
            blob_client = container_client.get_blob_client(parsed.blob_path)
            return await blob_client.exists()
        except Exception:
            return False

    async def delete_blob(self, uri: str) -> None:
        parsed = AzureUri(uri)
        try:
            client = self._get_blob_service_client()
            container_client = client.get_container_client(parsed.container)
            blob_client = container_client.get_blob_client(parsed.blob_path)
            await blob_client.delete_blob()
        except ResourceNotFoundError:
            LOG.debug("Azure blob not found for deletion", uri=uri)
        except Exception:
            LOG.exception("Failed to delete Azure blob", uri=uri)
            raise

    async def list_blobs(self, container: str, prefix: str | None = None) -> list[str]:
        try:
            client = self._get_blob_service_client()
            container_client = client.get_container_client(container)
            blobs = []
            async for blob in container_client.list_blobs(name_starts_with=prefix):
                blobs.append(blob.name)
            return blobs
        except ResourceNotFoundError:
            return []
        except Exception:
            LOG.exception("Failed to list Azure blobs", container=container, prefix=prefix)
            return []

    def create_sas_url(self, uri: str, expiry_hours: int = 24) -> str | None:
        parsed = AzureUri(uri)
        try:
            sas_token = generate_blob_sas(
                account_name=self.account_name,
                container_name=parsed.container,
                blob_name=parsed.blob_path,
                account_key=self.account_key,
                permission=BlobSasPermissions(read=True),
                expiry=datetime.now(timezone.utc) + timedelta(hours=expiry_hours),
            )
            return (
                f"https://{self.account_name}.blob.core.windows.net/{parsed.container}/{parsed.blob_path}?{sas_token}"
            )
        except Exception:
            LOG.exception("Failed to create SAS URL", uri=uri)
            return None

    async def create_sas_urls(self, uris: list[str], expiry_hours: int = 24) -> list[str] | None:
        try:
            sas_urls: list[str] = []
            for uri in uris:
                url = self.create_sas_url(uri, expiry_hours)
                if url is None:
                    LOG.warning("SAS URL generation failed, aborting batch", failed_uri=uri, uris=uris)
                    return None
                sas_urls.append(url)
            return sas_urls
        except Exception:
            LOG.exception("Failed to create SAS URLs")
            return None

    async def close(self) -> None:
        if self._blob_service_client:
            await self._blob_service_client.close()
            self._blob_service_client = None

    async def list_files(self, uri: str) -> list[str]:
        """List files under a URI prefix. Returns blob names relative to container."""
        parsed = AzureUri(uri)
        return await self.list_blobs(parsed.container, parsed.blob_path)

    async def get_object_info(self, uri: str) -> dict | None:
        """Get object info including metadata. Returns dict with Metadata and LastModified keys."""
        props = await self.get_blob_properties(uri)
        if props is None:
            return None
        return {
            "Metadata": props.get("metadata", {}),
            "LastModified": props.get("last_modified"),
        }

    async def delete_file(self, uri: str) -> None:
        """Delete a file at the given URI."""
        await self.delete_blob(uri)

    async def get_file_metadata(self, uri: str, log_exception: bool = True) -> dict[str, str] | None:
        """Get only the metadata for a file."""
        parsed = AzureUri(uri)
        try:
            client = self._get_blob_service_client()
            container_client = client.get_container_client(parsed.container)
            blob_client = container_client.get_blob_client(parsed.blob_path)
            props = await blob_client.get_blob_properties()
            return props.metadata or {}
        except ResourceNotFoundError:
            if log_exception:
                LOG.warning("Azure blob not found for metadata", uri=uri)
            return None
        except Exception:
            if log_exception:
                LOG.exception("Failed to get blob metadata", uri=uri)
            return None


class RealAzureClientFactory(AzureClientFactory):
    """Factory for creating real Azure Vault and Storage clients."""

    def create_default(self) -> AsyncAzureVaultClient:
        """Create an Azure Vault client using DefaultAzureCredential."""
        return RealAsyncAzureVaultClient(DefaultAzureCredential())

    def create_from_client_secret(self, credential: AzureClientSecretCredential) -> AsyncAzureVaultClient:
        """Create an Azure Vault client using client secret credentials."""
        cred = ClientSecretCredential(
            tenant_id=credential.tenant_id,
            client_id=credential.client_id,
            client_secret=credential.client_secret,
        )
        return RealAsyncAzureVaultClient(cred)

    def create_storage_client(self, storage_account_name: str, storage_account_key: str) -> AsyncAzureStorageClient:
        """Create an Azure Storage client with the provided credentials."""
        return RealAsyncAzureStorageClient(account_name=storage_account_name, account_key=storage_account_key)
