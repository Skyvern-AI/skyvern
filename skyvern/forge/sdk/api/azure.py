from enum import StrEnum
from typing import IO, Any
from urllib.parse import urlparse
from datetime import datetime, timedelta

import structlog
from azure.storage.blob.aio import BlobServiceClient
from azure.storage.blob import BlobSasPermissions, generate_blob_sas, ContentSettings

from skyvern.config import settings

LOG = structlog.get_logger()


class AzureBlobStorageClass(StrEnum):
    HOT = "Hot"
    COOL = "Cool"
    ARCHIVE = "Archive"


class AzureBlobUri:
    """
    Parse Azure Blob Storage URIs
    
    Example:
    >>> uri = AzureBlobUri("https://account.blob.core.windows.net/container/path/to/blob")
    >>> uri.account_name
    'account'
    >>> uri.container_name
    'container'
    >>> uri.blob_name
    'path/to/blob'
    """
    
    def __init__(self, uri: str):
        self.uri = uri
        parsed = urlparse(uri)
        
        if not parsed.hostname or not parsed.hostname.endswith('.blob.core.windows.net'):
            raise ValueError(f"Invalid Azure Blob URI: {uri}")
            
        self.account_name = parsed.hostname.split('.')[0]
        path_parts = parsed.path.lstrip('/').split('/', 1)
        
        if len(path_parts) < 2:
            raise ValueError(f"Invalid Azure Blob URI - missing container or blob name: {uri}")
            
        self.container_name = path_parts[0]
        self.blob_name = path_parts[1]
    
    def __str__(self) -> str:
        return self.uri


class AsyncAzureClient:
    def __init__(
        self,
        account_name: str | None = None,
        account_key: str | None = None,
        connection_string: str | None = None,
    ):
        self.account_name = account_name or settings.AZURE_STORAGE_ACCOUNT_NAME
        self.account_key = account_key or settings.AZURE_STORAGE_ACCOUNT_KEY
        self.connection_string = connection_string or settings.AZURE_STORAGE_CONNECTION_STRING
        
        if self.connection_string:
            self.blob_service_client = BlobServiceClient.from_connection_string(self.connection_string)
        elif self.account_name and self.account_key:
            account_url = f"https://{self.account_name}.blob.core.windows.net"
            self.blob_service_client = BlobServiceClient(account_url=account_url, credential=self.account_key)
        else:
            raise ValueError("Must provide either connection_string or both account_name and account_key")

    async def upload_file(
        self,
        uri: str,
        data: bytes,
        storage_class: AzureBlobStorageClass = AzureBlobStorageClass.HOT,
        metadata: dict[str, str] | None = None,
    ) -> str | None:
        try:
            parsed_uri = AzureBlobUri(uri)
            async with self.blob_service_client:
                blob_client = self.blob_service_client.get_blob_client(
                    container=parsed_uri.container_name,
                    blob=parsed_uri.blob_name
                )
                
                await blob_client.upload_blob(
                    data,
                    blob_type="BlockBlob",
                    metadata=metadata,
                    standard_blob_tier=storage_class.value,
                    overwrite=True
                )
                return uri
        except Exception:
            LOG.exception("Azure Blob upload failed.", uri=uri)
            return None

    async def upload_file_stream(
        self,
        uri: str,
        file_obj: IO[bytes],
        storage_class: AzureBlobStorageClass = AzureBlobStorageClass.HOT,
        metadata: dict[str, str] | None = None,
    ) -> str | None:
        try:
            parsed_uri = AzureBlobUri(uri)
            async with self.blob_service_client:
                blob_client = self.blob_service_client.get_blob_client(
                    container=parsed_uri.container_name,
                    blob=parsed_uri.blob_name
                )
                
                await blob_client.upload_blob(
                    file_obj,
                    blob_type="BlockBlob",
                    metadata=metadata,
                    standard_blob_tier=storage_class.value,
                    overwrite=True
                )
                LOG.debug("Upload file stream success", uri=uri)
                return uri
        except Exception:
            LOG.exception("Azure Blob upload stream failed.", uri=uri)
            return None

    async def upload_file_from_path(
        self,
        uri: str,
        file_path: str,
        storage_class: AzureBlobStorageClass = AzureBlobStorageClass.HOT,
        metadata: dict[str, str] | None = None,
        raise_exception: bool = False,
    ) -> None:
        try:
            parsed_uri = AzureBlobUri(uri)
            async with self.blob_service_client:
                blob_client = self.blob_service_client.get_blob_client(
                    container=parsed_uri.container_name,
                    blob=parsed_uri.blob_name
                )
                
                with open(file_path, "rb") as data:
                    await blob_client.upload_blob(
                        data,
                        blob_type="BlockBlob",
                        metadata=metadata,
                        standard_blob_tier=storage_class.value,
                        overwrite=True
                    )
        except Exception as e:
            LOG.exception("Azure Blob upload failed.", uri=uri)
            if raise_exception:
                raise e

    async def download_file(self, uri: str, log_exception: bool = True) -> bytes | None:
        try:
            parsed_uri = AzureBlobUri(uri)
            async with self.blob_service_client:
                blob_client = self.blob_service_client.get_blob_client(
                    container=parsed_uri.container_name,
                    blob=parsed_uri.blob_name
                )
                
                download_stream = await blob_client.download_blob()
                return await download_stream.readall()
        except Exception:
            if log_exception:
                LOG.exception("Azure Blob download failed", uri=uri)
            return None

    async def get_blob_properties(self, uri: str) -> dict:
        parsed_uri = AzureBlobUri(uri)
        async with self.blob_service_client:
            blob_client = self.blob_service_client.get_blob_client(
                container=parsed_uri.container_name,
                blob=parsed_uri.blob_name
            )
            
            return await blob_client.get_blob_properties()

    async def get_file_metadata(
        self,
        uri: str,
        log_exception: bool = True,
    ) -> dict | None:
        """
        Retrieves only the metadata of a blob without downloading its content.
        """
        try:
            properties = await self.get_blob_properties(uri)
            return properties.metadata or {}
        except Exception:
            if log_exception:
                LOG.exception("Azure Blob metadata retrieval failed", uri=uri)
            return None

    async def create_presigned_urls(self, uris: list[str]) -> list[str] | None:
        """
        Generate SAS URLs for Azure Blobs
        """
        presigned_urls = []
        try:
            for uri in uris:
                parsed_uri = AzureBlobUri(uri)
                
                if not self.account_key:
                    LOG.warning("Cannot generate SAS URL without account key", uri=uri)
                    continue
                
                sas_token = generate_blob_sas(
                    account_name=self.account_name,
                    container_name=parsed_uri.container_name,
                    blob_name=parsed_uri.blob_name,
                    account_key=self.account_key,
                    permission=BlobSasPermissions(read=True),
                    expiry=datetime.utcnow() + timedelta(seconds=settings.PRESIGNED_URL_EXPIRATION)
                )
                
                sas_url = f"{uri}?{sas_token}"
                presigned_urls.append(sas_url)

            return presigned_urls
        except Exception:
            LOG.exception("Failed to create SAS URLs for Azure Blobs.", uris=uris)
            return None

    async def list_blobs(self, container_name: str, prefix: str = "") -> list[str]:
        """
        List blobs in a container with optional prefix filter
        """
        try:
            async with self.blob_service_client:
                container_client = self.blob_service_client.get_container_client(container_name)
                blob_names = []
                
                async for blob in container_client.list_blobs(name_starts_with=prefix):
                    blob_names.append(blob.name)
                
                return blob_names
        except Exception:
            LOG.exception("Failed to list blobs", container=container_name, prefix=prefix)
            return []

    def _create_tag_string(self, tags: dict[str, str] | None) -> str:
        """
        Create tag string for Azure Blob Storage
        Note: Azure uses different tag format than S3
        """
        if not tags:
            return ""
        return "&".join([f"{key}={value}" for key, value in tags.items()])


# Global client instance
azure_client = AsyncAzureClient() 