from typing import IO, Protocol
from urllib.parse import urlparse


class GcsUri:
    """Parse gs://{bucket}/{object_path} URIs."""

    def __init__(self, uri: str) -> None:
        self._parsed = urlparse(uri, allow_fragments=False)

    @property
    def bucket(self) -> str:
        return self._parsed.netloc

    @property
    def object_path(self) -> str:
        if self._parsed.query:
            return self._parsed.path.lstrip("/") + "?" + self._parsed.query
        return self._parsed.path.lstrip("/")

    @property
    def uri(self) -> str:
        return self._parsed.geturl()

    def __str__(self) -> str:
        return self.uri


# GCS storage classes (the per-org tier lever, analogous to Azure StandardBlobTier).
STORAGE_CLASS_STANDARD = "STANDARD"
STORAGE_CLASS_NEARLINE = "NEARLINE"
STORAGE_CLASS_COLDLINE = "COLDLINE"
STORAGE_CLASS_ARCHIVE = "ARCHIVE"


class AsyncGcsStorageClient(Protocol):
    """Interface for GCS storage operations. Mirrors the method surface the
    storage adapter relies on from the Azure client (upload/download/list/
    signed-url), so ``GcsStorage`` can be a near-copy of ``AzureStorage``.
    """

    async def upload_file(
        self,
        uri: str,
        data: bytes,
        storage_class: str = STORAGE_CLASS_STANDARD,
        tags: dict[str, str] | None = None,
        metadata: dict[str, str] | None = None,
    ) -> None: ...

    async def upload_file_from_path(
        self,
        uri: str,
        file_path: str,
        storage_class: str = STORAGE_CLASS_STANDARD,
        tags: dict[str, str] | None = None,
        metadata: dict[str, str] | None = None,
    ) -> None: ...

    async def upload_file_stream(
        self,
        uri: str,
        file_obj: IO[bytes],
        storage_class: str = STORAGE_CLASS_STANDARD,
        tags: dict[str, str] | None = None,
        metadata: dict[str, str] | None = None,
    ) -> str: ...

    async def download_file(self, uri: str, log_exception: bool = True) -> bytes | None: ...

    async def delete_file(self, uri: str) -> None: ...

    async def list_files(self, uri: str) -> list[str]: ...

    async def get_object_info(self, uri: str) -> dict | None: ...

    async def create_signed_urls(self, uris: list[str], expiry_hours: int = 24) -> list[str] | None: ...

    async def close(self) -> None: ...


class GcpClientFactory(Protocol):
    """Interface for creating GCS storage clients."""

    def create_storage_client(self, project_id: str | None = None) -> "AsyncGcsStorageClient": ...
