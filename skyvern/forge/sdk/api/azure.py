import os
from enum import StrEnum
from typing import IO, Any
from urllib.parse import urlparse

import structlog
from azure.core.credentials import AzureKeyCredential
from azure.storage.blob import BlobServiceClient, BlobClient, ContainerClient
from azure.storage.blob.aio import BlobServiceClient as AsyncBlobServiceClient
from azure.storage.blob.aio import BlobClient as AsyncBlobClient
from azure.storage.blob.aio import ContainerClient as AsyncContainerClient
from azure.storage.blob import BlobSasPermissions, generate_blob_sas
from datetime import datetime, timedelta, timezone

from skyvern.config import settings

LOG = structlog.get_logger()


class AzureBlobTier(StrEnum):
    """Azure Blob Storage tiers"""
    HOT = "Hot"
    COOL = "Cool"
    COLD = "Cold"
    ARCHIVE = "Archive"


class AzureBlobUri:
    """Parse and handle Azure Blob Storage URIs in the format: azure://container/path/to/blob"""
    def __init__(self, uri: str) -> None:
        if not uri.startswith("azure://"):
            raise ValueError(f"Invalid Azure Blob URI format: {uri}. Must start with 'azure://'")
        
        # Remove the azure:// prefix
        path_parts = uri[8:].split("/", 1)
        if len(path_parts) < 2:
            raise ValueError(f"Invalid Azure Blob URI format: {uri}. Must include container and blob path")
        
        self.container = path_parts[0]
        self.blob_name = path_parts[1]
        self.uri = uri


class AsyncAzureClient:
    """Async client for Azure Blob Storage operations"""
    
    def __init__(self, account_name: str | None = None, account_key: str | None = None, connection_string: str | None = None) -> None:
        self.account_name = account_name or settings.AZURE_STORAGE_ACCOUNT_NAME
        self.account_key = account_key or settings.AZURE_STORAGE_ACCOUNT_KEY
        self.connection_string = connection_string or settings.AZURE_STORAGE_CONNECTION_STRING
        
        if not self.connection_string and not (self.account_name and self.account_key):
            raise ValueError("Either connection_string or both account_name and account_key must be provided")
        
        # Create the service client
        if self.connection_string:
            self._service_client = AsyncBlobServiceClient.from_connection_string(self.connection_string)
        else:
            self._service_client = AsyncBlobServiceClient(
                account_url=f"https://{self.account_name}.blob.core.windows.net",
                credential=self.account_key
            )
    
    async def __aenter__(self) -> "AsyncAzureClient":
        await self._service_client.__aenter__()
        return self
    
    async def __aexit__(self, exc_type: Any, exc_val: Any, exc_tb: Any) -> None:
        await self._service_client.__aexit__(exc_type, exc_val, exc_tb)
    
    def _get_blob_client(self, uri: str) -> AsyncBlobClient:
        """Get a blob client for the given URI"""
        parsed_uri = AzureBlobUri(uri)
        return self._service_client.get_blob_client(container=parsed_uri.container, blob=parsed_uri.blob_name)
    
    def _get_container_client(self, container_name: str) -> AsyncContainerClient:
        """Get a container client"""
        return self._service_client.get_container_client(container_name)
    
    async def upload_file(
        self,
        uri: str,
        data: bytes,
        tier: AzureBlobTier = AzureBlobTier.HOT,
        tags: dict[str, str] | None = None,
        metadata: dict[str, str] | None = None,
    ) -> str | None:
        """Upload data to Azure Blob Storage"""
        try:
            blob_client = self._get_blob_client(uri)
            await blob_client.upload_blob(
                data,
                blob_type="BlockBlob",
                overwrite=True,
                standard_blob_tier=str(tier),
                tags=tags,
                metadata=metadata,
            )
            return uri
        except Exception:
            LOG.exception("Azure Blob upload failed.", uri=uri)
            return None
    
    async def upload_file_stream(
        self,
        uri: str,
        file_obj: IO[bytes],
        tier: AzureBlobTier = AzureBlobTier.HOT,
        tags: dict[str, str] | None = None,
        metadata: dict[str, str] | None = None,
    ) -> str | None:
        """Upload a file stream to Azure Blob Storage"""
        try:
            blob_client = self._get_blob_client(uri)
            await blob_client.upload_blob(
                file_obj,
                blob_type="BlockBlob",
                overwrite=True,
                standard_blob_tier=str(tier),
                tags=tags,
                metadata=metadata,
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
        tier: AzureBlobTier = AzureBlobTier.HOT,
        metadata: dict[str, str] | None = None,
        raise_exception: bool = False,
        tags: dict[str, str] | None = None,
    ) -> None:
        """Upload a file from path to Azure Blob Storage"""
        try:
            blob_client = self._get_blob_client(uri)
            with open(file_path, "rb") as data:
                await blob_client.upload_blob(
                    data,
                    blob_type="BlockBlob",
                    overwrite=True,
                    standard_blob_tier=str(tier),
                    tags=tags,
                    metadata=metadata,
                )
        except Exception as e:
            LOG.exception("Azure Blob upload failed.", uri=uri)
            if raise_exception:
                raise e
    
    async def download_file(self, uri: str, log_exception: bool = True) -> bytes | None:
        """Download a file from Azure Blob Storage"""
        try:
            blob_client = self._get_blob_client(uri)
            download_stream = await blob_client.download_blob()
            return await download_stream.readall()
        except Exception:
            if log_exception:
                LOG.exception("Azure Blob download failed", uri=uri)
            return None
    
    async def delete_file(self, uri: str, raise_exception: bool = False) -> None:
        """Delete a file from Azure Blob Storage"""
        try:
            blob_client = self._get_blob_client(uri)
            await blob_client.delete_blob()
        except Exception as e:
            LOG.exception("Azure Blob deletion failed", uri=uri)
            if raise_exception:
                raise e
    
    async def list_files(self, uri: str) -> list[str]:
        """List files in Azure Blob Storage with the given prefix"""
        try:
            parsed_uri = AzureBlobUri(uri)
            container_client = self._get_container_client(parsed_uri.container)
            
            blob_names = []
            async for blob in container_client.list_blobs(name_starts_with=parsed_uri.blob_name):
                blob_names.append(blob.name)
            
            return blob_names
        except Exception:
            LOG.exception("Azure Blob list failed", uri=uri)
            return []
    
    def create_presigned_url(self, uri: str, expiration_seconds: int = 3600) -> str | None:
        """Create a presigned URL for Azure Blob Storage"""
        try:
            parsed_uri = AzureBlobUri(uri)
            
            # Generate SAS token
            sas_token = generate_blob_sas(
                account_name=self.account_name,
                container_name=parsed_uri.container,
                blob_name=parsed_uri.blob_name,
                account_key=self.account_key,
                permission=BlobSasPermissions(read=True),
                expiry=datetime.now(timezone.utc) + timedelta(seconds=expiration_seconds),
            )
            
            # Construct the full URL
            return f"https://{self.account_name}.blob.core.windows.net/{parsed_uri.container}/{parsed_uri.blob_name}?{sas_token}"
        except Exception:
            LOG.exception("Failed to create presigned URL", uri=uri)
            return None
    
    async def create_presigned_urls(self, uris: list[str], expiration_seconds: int = 3600) -> list[str]:
        """Create multiple presigned URLs"""
        urls = []
        for uri in uris:
            url = self.create_presigned_url(uri, expiration_seconds)
            if url:
                urls.append(url)
        return urls
    
    async def get_file_metadata(self, uri: str, log_exception: bool = True) -> dict[str, str] | None:
        """Get metadata for a file in Azure Blob Storage"""
        try:
            blob_client = self._get_blob_client(uri)
            properties = await blob_client.get_blob_properties()
            return properties.metadata
        except Exception:
            if log_exception:
                LOG.exception("Failed to get Azure Blob metadata", uri=uri)
            return None
    
    async def close(self) -> None:
        """Close the client connection"""
        await self._service_client.close()