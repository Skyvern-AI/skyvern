"""Real implementation of the GCS storage client and its factory.

``google-cloud-storage`` is synchronous, so every blocking call is wrapped in
``asyncio.to_thread`` to satisfy the async ``AsyncGcsStorageClient`` interface.
"""

import asyncio
import copy
import os
import threading
import time
from collections.abc import Callable
from datetime import timedelta
from mimetypes import add_type, guess_type
from typing import IO, Any, TypeVar

import structlog
from google.api_core.exceptions import Conflict, NotFound
from google.auth import default as google_auth_default
from google.auth import exceptions as google_auth_exceptions
from google.auth import iam
from google.auth.credentials import Credentials, Signing
from google.auth.transport import DEFAULT_RETRYABLE_STATUS_CODES
from google.auth.transport.requests import Request as GoogleAuthRequest
from google.cloud import secretmanager_v1, storage
from google.oauth2.credentials import Credentials as TokenCredentials

from skyvern.config import settings
from skyvern.constants import GET_DOWNLOADED_FILES_TIMEOUT
from skyvern.forge.sdk.api.gcp import (
    STORAGE_CLASS_STANDARD,
    AsyncGcpSecretManagerClient,
    AsyncGcsStorageClient,
    GcpClientFactory,
    GcsUri,
)
from skyvern.forge.sdk.artifact.storage.unique_pdfs import settle
from skyvern.utils.content_disposition import build_attachment_disposition

# Match the custom mime types registered for the Azure client.
add_type("application/json", ".har")
add_type("text/plain", ".log")
add_type("application/zstd", ".zst")

_SIGNING_SCOPES = ["https://www.googleapis.com/auth/cloud-platform"]

LOG = structlog.get_logger()
# Content-comparison requests use a third of the comparison budget as connect and read timeouts, without retries.
_BOUNDED = {"timeout": (GET_DOWNLOADED_FILES_TIMEOUT / 3,) * 2, "retry": None}
_T = TypeVar("_T")


def _within(stop: threading.Event, until: float) -> dict:
    """Request options ending by the monotonic time ``until``; raises once stopped or out of time, so none starts late."""
    remaining = until - time.monotonic()
    if stop.is_set() or remaining <= 0:
        raise TimeoutError("Stopped GCS work at its deadline")
    return {"timeout": (remaining,) * 2, "retry": None}


class _DeadlineAuthRequest:
    """google-auth transport for one deadline-bound call: each request gets the remaining budget, and a failure or
    retryable status ends the call instead of entering the SDK's backoff."""

    def __init__(self, stop: threading.Event, until: float) -> None:
        self._request = GoogleAuthRequest()
        self._stop, self._until = stop, until

    def __call__(
        self, url: str, method: str = "GET", body: Any = None, headers: Any = None, timeout: Any = None, **kwargs: Any
    ) -> Any:
        remaining = self._until - time.monotonic()
        if self._stop.is_set() or remaining <= 0:
            raise TimeoutError("Stopped GCS credential work at its deadline")
        bounded = remaining if timeout is None else min(timeout, remaining)
        try:
            response = self._request(url, method=method, body=body, headers=headers, timeout=bounded, **kwargs)
        except google_auth_exceptions.TransportError as error:
            raise TimeoutError("GCS credential request failed within its deadline") from error
        if response.status in DEFAULT_RETRYABLE_STATUS_CODES or response.status in iam.IAM_RETRY_CODES:
            raise TimeoutError(f"GCS credential request returned retryable {response.status} within its deadline")
        return response


class _IamSigning(Signing):
    """Signs through IAM signBlob as ``email`` over ``request``, with the already refreshed ``token``."""

    def __init__(self, request: _DeadlineAuthRequest, token: str, email: str) -> None:
        # A token-only credential keeps signing from looking up its universe or access boundary on its own transport.
        self._signer = iam.Signer(request, TokenCredentials(token=token), email)
        self._email = email

    def sign_bytes(self, message: bytes) -> bytes:
        return self._signer.sign(message)

    @property
    def signer_email(self) -> str:
        return self._email

    @property
    def signer(self) -> iam.Signer:
        return self._signer


async def _by_deadline(deadline: float, work: Callable[[threading.Event, float], _T]) -> _T:
    """Run ``work(stop, until)`` in a thread bounded by the loop time ``deadline``; cancellation stops and awaits it."""
    stop = threading.Event()
    until = time.monotonic() + deadline - asyncio.get_running_loop().time()
    worker = asyncio.ensure_future(asyncio.to_thread(work, stop, until))
    try:
        return await asyncio.shield(worker)
    except BaseException:
        stop.set()
        await settle(worker)
        raise


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

    def _client_within(self, stop: threading.Event, until: float) -> tuple[storage.Client, Credentials]:
        """A client for this call alone, discovered and refreshed within its budget, and its source credentials.

        The shared client's session refreshes and retries on its own transport, so deadline work never uses it."""
        _within(stop, until)
        request = _DeadlineAuthRequest(stop, until)
        if self._credentials is None:
            credentials, _ = google_auth_default(scopes=storage.Client.SCOPE, request=request)
        else:
            credentials = copy.copy(self._credentials)
        if not credentials.valid:
            credentials.refresh(request)
        # A token-only credential keeps the universe check and data requests from opening their own transports.
        client = storage.Client(project=self.project_id, credentials=TokenCredentials(token=credentials.token))
        # Its bucket-metadata cache would fetch on an unowned background thread that outlives this call.
        client._bucket_metadata_cache = None
        return client, credentials

    def _get_blob(self, uri: str, client: storage.Client | None = None) -> storage.Blob:
        parsed = GcsUri(uri)
        bucket = (client or self._get_client()).bucket(parsed.bucket)
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

    async def download_range(self, uri: str, start: int, end: int, generation: int) -> bytes:
        """Bytes ``start`` to ``end`` inclusive, failing once the object is no longer ``generation``."""
        blob = self._get_blob(uri)
        # Stored bytes only: decompressive transcoding of a gzip-encoded object ignores the range.
        return await asyncio.to_thread(
            blob.download_as_bytes, start=start, end=end, if_generation_match=generation, raw_download=True, **_BOUNDED
        )

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

    async def list_files(self, uri: str, deadline: float | None = None) -> list[str]:
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

        def _paged(stop: threading.Event, until: float) -> list[str]:
            parsed = GcsUri(uri)
            names: list[str] = []
            page_token: str | None = None
            try:
                client, _ = self._client_within(stop, until)
                while True:
                    blobs = client.list_blobs(
                        parsed.bucket, prefix=parsed.object_path, page_token=page_token, **_within(stop, until)
                    )
                    names.extend(blob.name for blob in next(blobs.pages, ()))
                    if not (page_token := blobs.next_page_token):
                        return names
            except NotFound:
                return []

        if deadline is None:
            return await asyncio.to_thread(_do)
        # One request per page, so paging stops at the deadline instead of finishing the listing.
        return await _by_deadline(deadline, _paged)

    async def get_object_info(self, uri: str, bounded: bool = False, deadline: float | None = None) -> dict | None:
        def _do(options: dict, client: storage.Client | None = None) -> dict | None:
            parsed = GcsUri(uri)
            bucket = (client or self._get_client()).bucket(parsed.bucket)
            blob = bucket.get_blob(parsed.object_path, **options)
            if blob is None:
                return None
            return {
                "Metadata": blob.metadata or {},
                "LastModified": blob.updated,
                "ContentLength": blob.size,
                "Generation": blob.generation,
            }

        if deadline is None:
            return await asyncio.to_thread(_do, _BOUNDED if bounded else {})

        def _bounded(stop: threading.Event, until: float) -> dict | None:
            client, _ = self._client_within(stop, until)
            return _do(_within(stop, until), client)

        return await _by_deadline(deadline, _bounded)

    def _signing_kwargs(self, request: _DeadlineAuthRequest | None = None) -> dict:
        """Extra kwargs for ``generate_signed_url``.

        With a local private key (SA JSON key) signing happens locally and no
        extra kwargs are needed. Under Workload Identity there is no private
        key, so we route to the IAM ``signBlob`` API by passing the signer's
        email + a fresh access token — requires ``GCS_SIGNER_SA_EMAIL`` set and
        ``roles/iam.serviceAccountTokenCreator`` on that SA.
        """
        if not settings.GCS_SIGNER_SA_EMAIL:
            return {}
        if request is not None:
            # Discovery, refresh and signBlob all go through ``request``, so none outlasts the caller's deadline.
            creds, _ = google_auth_default(scopes=_SIGNING_SCOPES, request=request)
            creds.refresh(request)
            return {"credentials": _IamSigning(request, creds.token, settings.GCS_SIGNER_SA_EMAIL)}
        creds, _ = google_auth_default(scopes=_SIGNING_SCOPES)
        creds.refresh(GoogleAuthRequest())
        return {"service_account_email": settings.GCS_SIGNER_SA_EMAIL, "access_token": creds.token}

    def _create_signed_url(
        self,
        uri: str,
        expiry_hours: int = 24,
        signing_kwargs: dict | None = None,
        attachment_filename: str | None = None,
        client: storage.Client | None = None,
    ) -> str | None:
        # Private + synchronous: does blocking signing I/O (and, under Workload
        # Identity, a token refresh). Call only from create_signed_urls, which
        # runs it inside asyncio.to_thread so the event loop isn't blocked.
        # signing_kwargs holds the freshly-minted IAM token; the batch caller
        # passes it in once rather than refreshing per URL.
        if signing_kwargs is None:
            signing_kwargs = self._signing_kwargs()
        response_overrides: dict[str, str] = {}
        if attachment_filename is not None:
            response_overrides = {
                "response_disposition": build_attachment_disposition(attachment_filename),
                "response_type": "application/octet-stream",
            }
        try:
            blob = self._get_blob(uri, client)
            return blob.generate_signed_url(
                version="v4",
                expiration=timedelta(hours=expiry_hours),
                method="GET",
                **response_overrides,
                **signing_kwargs,
            )
        except Exception:
            LOG.exception("Failed to create signed URL", uri=uri)
            return None

    async def create_signed_urls(
        self,
        uris: list[str],
        expiry_hours: int = 24,
        attachment_filenames: list[str | None] | None = None,
        deadline: float | None = None,
    ) -> list[str] | None:
        if attachment_filenames is not None and len(attachment_filenames) != len(uris):
            raise ValueError("attachment_filenames must have one entry per uri")

        def _do(
            check: Callable[[], object] = lambda: None,
            request: _DeadlineAuthRequest | None = None,
            operation: tuple[storage.Client, Credentials] | None = None,
        ) -> list[str] | None:
            check()
            signing_kwargs = self._signing_kwargs(request)
            client, credentials = operation or (None, None)
            # Without an IAM signer the URL is signed locally by the discovered key, not the client's token credential.
            if operation is not None and not signing_kwargs and isinstance(credentials, Signing):
                signing_kwargs = {"credentials": credentials}
            signed_urls: list[str] = []
            for index, uri in enumerate(uris):
                check()
                attachment_filename = None
                if attachment_filenames is not None:
                    attachment_filename = attachment_filenames[index] or os.path.basename(GcsUri(uri).object_path)
                url = self._create_signed_url(
                    uri,
                    expiry_hours,
                    signing_kwargs=signing_kwargs,
                    attachment_filename=attachment_filename,
                    client=client,
                )
                if url is None:
                    if request is not None:
                        raise TimeoutError("Signing failed within its deadline")
                    LOG.warning("Signed URL generation failed, aborting batch", failed_uri=uri, uris=uris)
                    return None
                signed_urls.append(url)
            return signed_urls

        if deadline is None:
            return await asyncio.to_thread(_do)
        # Signing takes no request timeout, so the deadline stops signatures not yet begun.
        return await _by_deadline(
            deadline,
            lambda stop, until: _do(
                lambda: _within(stop, until), _DeadlineAuthRequest(stop, until), self._client_within(stop, until)
            ),
        )

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
