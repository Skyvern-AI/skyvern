import structlog
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
