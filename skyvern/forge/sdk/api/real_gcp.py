"""Real implementation of the GCS storage client and its factory.

``google-cloud-storage`` is synchronous, so every blocking call is wrapped in
``asyncio.to_thread`` to satisfy the async ``AsyncGcsStorageClient`` interface.
"""

import asyncio
import threading
from datetime import timedelta
from mimetypes import add_type, guess_type
from typing import IO

import structlog
from google.api_core.exceptions import Conflict, NotFound
from google.auth import default as google_auth_default
from google.auth.credentials import Credentials
from google.auth.transport.requests import Request as GoogleAuthRequest
from google.cloud import secretmanager_v1, storage

from skyvern.config import settings
from skyvern.forge.sdk.api.gcp import (
    STORAGE_CLASS_STANDARD,
    AsyncGcpSecretManagerClient,
    AsyncGcsStorageClient,
    GcpClientFactory,
    GcsUri,
)

# Match the custom mime types registered for the Azure client.
add_type("application/json", ".har")
add_type("text/plain", ".log")
add_type("application/zstd", ".zst")

_SIGNING_SCOPES = ["https://www.googleapis.com/auth/cloud-platform"]

LOG = structlog.get_logger()


class RealAsyncGcsStorageClient(AsyncGcsStorageClient):
    """Async wrapper over the synchronous ``google.cloud.storage`` client."""

    def __init__(
        self,
        project_id: str | None = None,
        credentials: Credentials | None = None,
    ) -> None:
        self.project_id = project_id or settings.GCS_PROJECT_ID
        self._credentials = credentials
        self._client: storage.Client | None = None
        self._client_lock = threading.Lock()

    def _get_client(self) -> storage.Client:
        # Double-checked locking: _get_client runs inside asyncio.to_thread, so
        # concurrent first calls would otherwise each build a client and leak all
        # but the last.
        if self._client is None:
            with self._client_lock:
                if self._client is None:
                    # When credentials are omitted the client resolves Application
                    # Default Credentials (GOOGLE_APPLICATION_CREDENTIALS / Workload
                    # Identity). STORAGE_EMULATOR_HOST is honored automatically.
                    self._client = storage.Client(project=self.project_id, credentials=self._credentials)
        return self._client

    def _get_blob(self, uri: str) -> storage.Blob:
        parsed = GcsUri(uri)
        bucket = self._get_client().bucket(parsed.bucket)
        return bucket.blob(parsed.object_path)

    async def upload_file(
        self,
        uri: str,
        data: bytes,
        storage_class: str = STORAGE_CLASS_STANDARD,
        tags: dict[str, str] | None = None,
        metadata: dict[str, str] | None = None,
    ) -> None:
        def _do() -> None:
            blob = self._get_blob(uri)
            blob.storage_class = storage_class
            if metadata:
                blob.metadata = metadata
            content_type, _ = guess_type(GcsUri(uri).object_path)
            blob.upload_from_string(data, content_type=content_type)

        await asyncio.to_thread(_do)

    async def upload_file_from_path(
        self,
        uri: str,
        file_path: str,
        storage_class: str = STORAGE_CLASS_STANDARD,
        tags: dict[str, str] | None = None,
        metadata: dict[str, str] | None = None,
    ) -> None:
        def _do() -> None:
            blob = self._get_blob(uri)
            blob.storage_class = storage_class
            if metadata:
                blob.metadata = metadata
            content_type, _ = guess_type(file_path)
            blob.upload_from_filename(file_path, content_type=content_type)

        await asyncio.to_thread(_do)

    async def upload_file_stream(
        self,
        uri: str,
        file_obj: IO[bytes],
        storage_class: str = STORAGE_CLASS_STANDARD,
        tags: dict[str, str] | None = None,
        metadata: dict[str, str] | None = None,
    ) -> str:
        def _do() -> None:
            blob = self._get_blob(uri)
            blob.storage_class = storage_class
            if metadata:
                blob.metadata = metadata
            content_type, _ = guess_type(GcsUri(uri).object_path)
            blob.upload_from_file(file_obj, content_type=content_type)

        await asyncio.to_thread(_do)
        return uri

    async def download_file(self, uri: str, log_exception: bool = True) -> bytes | None:
        def _do() -> bytes | None:
            try:
                return self._get_blob(uri).download_as_bytes()
            except NotFound:
                if log_exception:
                    LOG.warning("GCS object not found", uri=uri)
                return None
            except Exception:
                if log_exception:
                    LOG.exception("Failed to download from GCS", uri=uri)
                return None

        return await asyncio.to_thread(_do)

    async def delete_file(self, uri: str) -> None:
        def _do() -> None:
            try:
                self._get_blob(uri).delete()
            except NotFound:
                LOG.debug("GCS object not found for deletion", uri=uri)
            except Exception:
                LOG.exception("Failed to delete GCS object", uri=uri)
                raise

        await asyncio.to_thread(_do)

    async def list_files(self, uri: str) -> list[str]:
        def _do() -> list[str]:
            parsed = GcsUri(uri)
            try:
                blobs = self._get_client().list_blobs(parsed.bucket, prefix=parsed.object_path)
                return [blob.name for blob in blobs]
            except NotFound:
                return []
            except Exception:
                LOG.exception("Failed to list GCS objects", bucket=parsed.bucket, prefix=parsed.object_path)
                return []

        return await asyncio.to_thread(_do)

    async def get_object_info(self, uri: str) -> dict | None:
        def _do() -> dict | None:
            parsed = GcsUri(uri)
            bucket = self._get_client().bucket(parsed.bucket)
            blob = bucket.get_blob(parsed.object_path)
            if blob is None:
                return None
            return {
                "Metadata": blob.metadata or {},
                "LastModified": blob.updated,
                "ContentLength": blob.size,
            }

        return await asyncio.to_thread(_do)

    def _signing_kwargs(self) -> dict:
        """Extra kwargs for ``generate_signed_url``.

        With a local private key (SA JSON key) signing happens locally and no
        extra kwargs are needed. Under Workload Identity there is no private
        key, so we route to the IAM ``signBlob`` API by passing the signer's
        email + a fresh access token — requires ``GCS_SIGNER_SA_EMAIL`` set and
        ``roles/iam.serviceAccountTokenCreator`` on that SA.
        """
        if not settings.GCS_SIGNER_SA_EMAIL:
            return {}
        creds, _ = google_auth_default(scopes=_SIGNING_SCOPES)
        creds.refresh(GoogleAuthRequest())
        return {"service_account_email": settings.GCS_SIGNER_SA_EMAIL, "access_token": creds.token}

    def _create_signed_url(self, uri: str, expiry_hours: int = 24, signing_kwargs: dict | None = None) -> str | None:
        # Private + synchronous: does blocking signing I/O (and, under Workload
        # Identity, a token refresh). Call only from create_signed_urls, which
        # runs it inside asyncio.to_thread so the event loop isn't blocked.
        # signing_kwargs holds the freshly-minted IAM token; the batch caller
        # passes it in once rather than refreshing per URL.
        if signing_kwargs is None:
            signing_kwargs = self._signing_kwargs()
        try:
            blob = self._get_blob(uri)
            return blob.generate_signed_url(
                version="v4",
                expiration=timedelta(hours=expiry_hours),
                method="GET",
                **signing_kwargs,
            )
        except Exception:
            LOG.exception("Failed to create signed URL", uri=uri)
            return None

    async def create_signed_urls(self, uris: list[str], expiry_hours: int = 24) -> list[str] | None:
        def _do() -> list[str] | None:
            signing_kwargs = self._signing_kwargs()
            signed_urls: list[str] = []
            for uri in uris:
                url = self._create_signed_url(uri, expiry_hours, signing_kwargs=signing_kwargs)
                if url is None:
                    LOG.warning("Signed URL generation failed, aborting batch", failed_uri=uri, uris=uris)
                    return None
                signed_urls.append(url)
            return signed_urls

        return await asyncio.to_thread(_do)

    async def close(self) -> None:
        if self._client is not None:
            self._client.close()
            self._client = None


class RealAsyncGcpSecretManagerClient(AsyncGcpSecretManagerClient):
    """Async wrapper over the synchronous Secret Manager client.

    Uses the REST transport rather than the default gRPC: REST goes over plain
    HTTPS so it works behind a TLS-intercepting proxy, and it keeps this module
    consistent with the sync-lib-in-a-thread pattern used for storage. In a
    GKE deployment gRPC would also work; REST is functionally equivalent for the
    low call volume of credential CRUD.
    """

    def __init__(self, client: secretmanager_v1.SecretManagerServiceClient | None = None) -> None:
        self._client = client
        self._client_lock = threading.Lock()

    def _get_client(self) -> secretmanager_v1.SecretManagerServiceClient:
        # Double-checked locking: _get_client runs inside asyncio.to_thread, so
        # concurrent first calls would otherwise each build a client and leak all
        # but the last.
        if self._client is None:
            with self._client_lock:
                if self._client is None:
                    self._client = secretmanager_v1.SecretManagerServiceClient(transport="rest")
        return self._client

    async def get_secret(self, secret_id: str, project_id: str) -> str | None:
        def _do() -> str | None:
            name = f"projects/{project_id}/secrets/{secret_id}/versions/latest"
            try:
                response = self._get_client().access_secret_version(request={"name": name})
                return response.payload.data.decode("UTF-8")
            except NotFound:
                return None

        return await asyncio.to_thread(_do)

    async def create_or_update_secret(self, secret_id: str, project_id: str, value: str) -> str:
        def _do() -> str:
            client = self._get_client()
            parent = f"projects/{project_id}"
            try:
                client.create_secret(
                    request={
                        "parent": parent,
                        "secret_id": secret_id,
                        "secret": {"replication": {"automatic": {}}},
                    }
                )
            except Conflict:
                # Secret already exists — proceed to add a new version. The REST
                # transport raises the generic Conflict (409); gRPC raises the
                # AlreadyExists subclass. Conflict catches both.
                pass
            # The value lives in a version; "updating" a secret means adding one.
            added = client.add_secret_version(
                request={"parent": f"{parent}/secrets/{secret_id}", "payload": {"data": value.encode("UTF-8")}}
            )
            # Revoke prior versions so a rotated/leaked credential value can't
            # be read back via an explicit version number. Only destroy versions
            # numerically OLDER than ours: a concurrent update may have added a
            # newer version between our add and this list, and destroying it
            # would race both writers into destroying each other's version
            # (destruction is permanent), leaving the secret unreadable.
            # Comparing version numbers makes concurrent updates converge to
            # last-writer-wins.
            added_number = int(added.name.rsplit("/", 1)[-1])
            destroyed_state = secretmanager_v1.SecretVersion.State.DESTROYED
            for version in client.list_secret_versions(request={"parent": f"{parent}/secrets/{secret_id}"}):
                if version.state == destroyed_state:
                    continue
                if int(version.name.rsplit("/", 1)[-1]) >= added_number:
                    continue
                client.destroy_secret_version(request={"name": version.name})
            return secret_id

        return await asyncio.to_thread(_do)

    async def delete_secret(self, secret_id: str, project_id: str) -> None:
        def _do() -> None:
            try:
                self._get_client().delete_secret(request={"name": f"projects/{project_id}/secrets/{secret_id}"})
            except NotFound:
                LOG.debug("GCP secret not found for deletion", secret_id=secret_id, project_id=project_id)

        await asyncio.to_thread(_do)

    async def close(self) -> None:
        if self._client is not None:
            await asyncio.to_thread(self._client.transport.close)
            self._client = None


class RealGcpClientFactory(GcpClientFactory):
    """Factory for creating real GCS storage and Secret Manager clients."""

    def create_storage_client(self, project_id: str | None = None) -> AsyncGcsStorageClient:
        return RealAsyncGcsStorageClient(project_id=project_id)

    def create_secret_manager_client(self) -> AsyncGcpSecretManagerClient:
        return RealAsyncGcpSecretManagerClient()


_gcs_client: RealAsyncGcsStorageClient | None = None


def get_gcs_client() -> RealAsyncGcsStorageClient:
    global _gcs_client
    if _gcs_client is None:
        _gcs_client = RealAsyncGcsStorageClient(project_id=settings.GCS_PROJECT_ID)
    return _gcs_client
