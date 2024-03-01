from skyvern.forge.sdk.artifact.storage.base import BaseStorage
from skyvern.forge.sdk.artifact.storage.local import LocalStorage


class StorageFactory:
    __storage: BaseStorage = LocalStorage()

    @staticmethod
    def set_storage(storage: BaseStorage) -> None:
        StorageFactory.__storage = storage

    @staticmethod
    def get_storage() -> BaseStorage:
        return StorageFactory.__storage
