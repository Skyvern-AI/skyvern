from skyvern.forge.sdk.artifact.storage.base import BaseStorage
from skyvern.forge.sdk.artifact.storage.local import LocalStorage


class StorageFactory:
    __storage: BaseStorage = LocalStorage()

    @staticmethod
    def create_storage(storage_type: str) -> BaseStorage:
        if storage_type == "s3":
            from skyvern.forge.sdk.artifact.storage.s3 import S3Storage

            return S3Storage()
        elif storage_type == "azure_blob":
            from skyvern.forge.sdk.artifact.storage.azure_blob import AzureBlobStorage

            return AzureBlobStorage()
        elif storage_type == "local":
            return LocalStorage()
        else:
            raise ValueError(f"Unsupported storage type: {storage_type}")

    @staticmethod
    def set_storage(storage: BaseStorage) -> None:
        StorageFactory.__storage = storage

    @staticmethod
    def get_storage() -> BaseStorage:
        return StorageFactory.__storage
