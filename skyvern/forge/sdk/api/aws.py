from __future__ import annotations

import asyncio
import io
import os
import socket
import time
import uuid
from collections import OrderedDict
from collections.abc import Awaitable, Callable
from enum import StrEnum
from mimetypes import add_type, guess_type
from typing import IO, TYPE_CHECKING, Any, cast
from urllib.parse import urlparse

import aioboto3
import structlog
from aiobotocore.config import AioConfig
from aiohttp.abc import AbstractResolver, ResolveResult
from boto3.s3.transfer import TransferConfig
from botocore.exceptions import ClientError, ProfileNotFound

from skyvern.config import settings

if TYPE_CHECKING:
    from types_boto3_batch.client import BatchClient
    from types_boto3_ec2.client import EC2Client
    from types_boto3_ecs.client import ECSClient
    from types_boto3_s3.client import S3Client
    from types_boto3_secretsmanager.client import SecretsManagerClient

# Register custom mime types for mimetypes guessing
add_type("application/json", ".har")
add_type("text/plain", ".log")
add_type("application/zstd", ".zst")

_S3_OPERATION_RETRIES = 2
# get_object on a missing key raises NoSuchKey; head-style paths use 404/NotFound.
S3_NOT_FOUND_ERROR_CODES = frozenset({"NoSuchKey", "NotFound", "404"})
# Expired AWS credentials surface under either exact code depending on the service/path.
_EXPIRED_TOKEN_ERROR_CODES = frozenset({"ExpiredTokenException", "ExpiredToken"})
# Aborting an upload id that no longer exists (e.g. an ambiguous Complete actually landed) returns this; it
# means the cleanup goal is already met, so it counts as a successful abort rather than a leaked orphan.
_ABORT_ALREADY_GONE_ERROR_CODES = frozenset({"NoSuchUpload", "404"})
# Long-lived holders (e.g. the storage singleton on persistent-sessions workers) must not reuse a
# session past the 1-hour projected web-identity token expiry (SKY-8743, SKY-13210).
_SESSION_TTL_SECONDS: float = 45 * 60
LOG = structlog.get_logger()

# Bound the resident memory of a streamed prefix upload. aioboto3's upload_fileobj reads parts into an
# asyncio queue of maxsize=max_io_queue_size (s3transfer default 1000) with multipart_chunksize=8MB, so a
# slow uploader lets resident bytes track the prefix size (a 400MB prefix queues >100MB). Capping the
# queue makes the resident buffer ~(queue depth x chunk size) regardless of prefix size. TransferConfig's
# max_io_queue aliases s3transfer's max_io_queue_size, so aioboto3 reads it correctly.
_STREAM_UPLOAD_IO_QUEUE_DEPTH = 8
_STREAM_UPLOAD_TRANSFER_CONFIG = TransferConfig(max_io_queue=_STREAM_UPLOAD_IO_QUEUE_DEPTH)


# We only include the storage classes that we want to use in our application.
class S3StorageClass(StrEnum):
    STANDARD = "STANDARD"
    # REDUCED_REDUNDANCY = "REDUCED_REDUNDANCY"
    # INTELLIGENT_TIERING = "INTELLIGENT_TIERING"
    ONEZONE_IA = "ONEZONE_IA"
    GLACIER = "GLACIER"
    GLACIER_IR = "GLACIER_IR"  # Glacier Instant Retrieval
    DEEP_ARCHIVE = "DEEP_ARCHIVE"
    # OUTPOSTS = "OUTPOSTS"
    # STANDARD_IA = "STANDARD_IA"


class AWSClientType(StrEnum):
    S3 = "s3"
    SECRETS_MANAGER = "secretsmanager"
    ECS = "ecs"
    EC2 = "ec2"
    BATCH = "batch"


class AWSSessionConfigurationError(RuntimeError):
    pass


class _PinnedIPResolver(AbstractResolver):
    """Resolve ``pinned_host`` only to already-validated IPs, and refuse every other name.

    A connector shares one resolver across every host it dials, so answering unconditionally
    would misroute any other name (a redirect, say) to the pinned addresses. Anything but
    ``pinned_host`` therefore fails closed rather than falling back to real DNS, which would
    reopen the rebinding window this exists to close.

    aiohttp takes TLS SNI, certificate verification, and the ``Host`` header from the request
    URL rather than from anything returned here, so redirecting the connection to an IP leaves
    SigV4 signing and virtual-host bucket addressing untouched. The ``hostname`` field below is
    the resolver contract's echo of the name asked for, not the TLS identity.
    """

    def __init__(self, pinned_host: str, resolved_ips: tuple[str, ...]) -> None:
        self._pinned_host = pinned_host.strip().lower().rstrip(".")
        self._resolved_ips = resolved_ips

    async def resolve(
        self, host: str, port: int = 0, family: socket.AddressFamily = socket.AF_INET
    ) -> list[ResolveResult]:
        if host.strip().lower().rstrip(".") != self._pinned_host:
            raise OSError(f"{host} is not the validated S3 endpoint host")
        return [
            ResolveResult(
                hostname=host,
                host=ip,
                port=port,
                family=socket.AF_INET6 if ":" in ip else socket.AF_INET,
                proto=0,
                flags=socket.AI_NUMERICHOST | socket.AI_NUMERICSERV,
            )
            for ip in self._resolved_ips
        ]

    async def close(self) -> None:
        # One instance is shared by every client this AsyncAWSClient builds; a connector
        # closing its resolver must not disarm the pin for the next client.
        return None


# S3 requires every non-final multipart part to be >= 5 MiB. A confirmed prefix below this cannot be
# copied as the (non-final) first part of an incremental compose, so such steps fall back to Phase 1.
_MIN_COMPOSE_BASE_BYTES = 5 * 1024 * 1024
# A single UploadPartCopy source range (and a single UploadPart body) may cover at most 5 GiB. A base
# prefix or a per-step tail beyond this would need multi-part splitting; we instead fall back to Phase 1.
_MAX_COMPOSE_PART_BYTES = 5 * 1024 * 1024 * 1024
# Unique per-generation token stored as object user metadata so an ambiguous Complete is reconciled by
# exact generation identity (length + token), never by length alone (a foreign write can share a length).
_COMPOSE_GEN_META_KEY = "skyvern-compose-gen"
# Bound the per-key compose-state cache: a recording's snapshots finish well within the TTL, and the cap
# stops leaked/extension-changed keys from accumulating. A miss just forces a safe full reseed next step.
_COMPOSE_STATE_MAX_ENTRIES = 512
_COMPOSE_STATE_TTL_SECONDS = 30 * 60


class ComposeUnsupportedError(Exception):
    """Signal that an incremental same-key compose cannot proceed because the remote object is UNCHANGED
    (no/too-small/not-growing base, provider limit, or a failure before Complete). The caller may safely
    perform the Phase 1 full-prefix replacement in the same serialized slot; the base reseeds next step."""


class _ComposeConflict(Exception):
    """The remote object moved under us (stale ETag, or a foreign generation surfaced during ambiguous-
    Complete reconciliation). The caller must PRESERVE the remote object — never overwrite it — and skip
    this step's write, reseeding the base from authoritative S3 state on the next step."""


class _ComposeState:
    """The last compose-confirmed object generation for a key: its exact length and current ETag. Used
    as the ``CopySourceIfMatch`` fence for the next step's UploadPartCopy. Advanced only after a
    Complete is reconciled as durable."""

    __slots__ = ("etag", "length")

    def __init__(self, length: int, etag: str) -> None:
        self.length = length
        self.etag = etag


class _RangedFileReader(io.RawIOBase):
    """A 0-based, seekable, read-only ``io.RawIOBase`` view over exactly ``[start, start+size)`` of an
    ALREADY-OPEN binary file handle. Being a real ``io.RawIOBase`` lets aiohttp/aiobotocore stream it as a
    request body (a plain duck-typed reader is rejected), so the tail/prefix upload never buffers the whole
    range. The handle is opened by the caller before any detach so a post-cancel unlink/replace of the path
    cannot strand the transfer; reads are hard-capped to ``size`` (the frozen snapshot length) so the view
    never follows a still-growing file, and ``seek(0)`` rewinds to ``start`` for a retry."""

    def __init__(self, fh: IO[bytes], start: int, size: int, owns_fh: bool = False) -> None:
        super().__init__()
        self._fh = fh
        # By default the reader is a NON-OWNING view: several readers share one fd within a single
        # store_recording_prefix call, so a reader closing on GC (its RawIOBase.__del__ -> close) must NOT
        # close the shared fd — otherwise a later same-slot fallback reads a closed file. The single caller
        # that outlives the outer call (the parked terminal fallback) passes owns_fh=True.
        self._owns_fh = owns_fh
        self._start = max(0, int(start))
        self._size = max(0, int(size))
        self._pos = 0
        self._fh.seek(self._start)

    def readinto(self, b: Any) -> int:
        remaining = self._size - self._pos
        if remaining <= 0:
            return 0
        data = self._fh.read(min(len(b), remaining))
        n = len(data)
        b[:n] = data
        self._pos += n
        return n

    def seek(self, offset: int, whence: int = os.SEEK_SET) -> int:
        if whence == os.SEEK_SET:
            target = offset
        elif whence == os.SEEK_CUR:
            target = self._pos + offset
        elif whence == os.SEEK_END:
            target = self._size + offset
        else:
            raise ValueError(f"invalid whence: {whence}")
        target = max(0, min(target, self._size))
        self._fh.seek(self._start + target)
        self._pos = target
        return self._pos

    def tell(self) -> int:
        return self._pos

    def seekable(self) -> bool:
        return True

    def readable(self) -> bool:
        return True

    def close(self) -> None:
        try:
            if self._owns_fh:
                self._fh.close()
        finally:
            super().close()


class _ComposeStateCache:
    """Bounded TTL + LRU cache of per-key compose generations.

    Unbounded retention would leak a ``_ComposeState`` per recording forever (a terminal that remuxes the
    ``.webm`` object to a different ``.mp4`` uri never clears the old key). Entries expire after ``ttl_seconds``
    and the map is capped at ``max_size`` (oldest evicted first), so a missing/stale entry simply forces a safe
    full reseed on the next step. ``clock`` is injectable for deterministic tests."""

    def __init__(self, max_size: int, ttl_seconds: float, clock: Callable[[], float]) -> None:
        self._max_size = max_size
        self._ttl = ttl_seconds
        self._clock = clock
        self._entries: OrderedDict[str, tuple[_ComposeState, float]] = OrderedDict()

    def _evict(self) -> None:
        now = self._clock()
        for key in [k for k, (_, ts) in self._entries.items() if now - ts > self._ttl]:
            self._entries.pop(key, None)
        while len(self._entries) > self._max_size:
            self._entries.popitem(last=False)

    def get(self, key: str) -> _ComposeState | None:
        entry = self._entries.get(key)
        if entry is None:
            return None
        state, ts = entry
        if self._clock() - ts > self._ttl:
            self._entries.pop(key, None)
            return None
        self._entries.move_to_end(key)
        return state

    def set(self, key: str, state: _ComposeState) -> None:
        self._entries[key] = (state, self._clock())
        self._entries.move_to_end(key)
        self._evict()

    def pop(self, key: str) -> None:
        self._entries.pop(key, None)

    def __len__(self) -> int:
        return len(self._entries)

    def __contains__(self, key: str) -> bool:
        return self.get(key) is not None


class _ParkedFallback:
    """The newest pre-terminal queued recording prefix, held so the terminal write can upload it iff the
    terminal write fails (otherwise it is discarded unread). ``upload`` streams the retained reader."""

    def __init__(self, reader: IO[bytes], upload: Callable[[], Awaitable[Any]]) -> None:
        self.reader = reader
        self.upload = upload
        self.consumed = False

    def close_reader(self) -> None:
        try:
            self.reader.close()
        except Exception:
            LOG.warning("Failed to close parked fallback reader", exc_info=True)


class AsyncAWSClient:
    def __init__(
        self,
        aws_access_key_id: str | None = None,
        aws_secret_access_key: str | None = None,
        region_name: str | None = None,
        endpoint_url: str | None = None,
        profile_name: str | None = None,
        endpoint_resolved_ips: tuple[str, ...] | None = None,
    ) -> None:
        self.region_name = region_name or settings.AWS_REGION
        # An empty endpoint_url must behave like an unset one: botocore raises
        # "ValueError: Invalid endpoint:" on "", and callers can reach here with a field that
        # was serialized empty or rendered empty from a template.
        self._endpoint_url = (endpoint_url or "").strip() or None
        self._aws_access_key_id = aws_access_key_id
        self._aws_secret_access_key = aws_secret_access_key
        self._profile_name = profile_name
        pinned_host = urlparse(self._endpoint_url).hostname if self._endpoint_url else None
        self._config = (
            AioConfig(connector_args={"resolver": _PinnedIPResolver(pinned_host, endpoint_resolved_ips)})
            if endpoint_resolved_ips and pinned_host
            else None
        )
        self._session: aioboto3.Session | None = None
        self._session_created_at: float = 0.0
        # Per-object-key write chain: serializes writes to one key in issue order so a later terminal
        # write is never overwritten by an earlier write that is still in flight (including one detached
        # by _detach_on_cancel). Holds at most one (tail) future per key with an in-flight write; each
        # entry is dropped when its key goes idle, so the map cannot grow unbounded.
        self._object_write_chains: dict[str, asyncio.Future[None]] = {}
        # Keys whose terminal (finalize) write has been reserved: queued-but-not-yet-active prefixes to
        # such a key are superseded and skipped, so the terminal waits only for the active transfer.
        # Dropped alongside the chain entry when the key goes idle.
        self._object_write_sealed: set[str] = set()
        # For a sealed key, the done future of the newest pre-terminal queued snapshot (the terminal's
        # `prev`). That one prefix parks itself as the terminal's fallback instead of being skipped.
        self._object_write_fallback_target: dict[str, asyncio.Future[None] | None] = {}
        # At most one parked fallback per key: the retained reader + a callable that uploads it. The
        # terminal discards it on success or uploads it on failure, and closes the reader either way.
        self._object_write_fallback: dict[str, _ParkedFallback] = {}
        # Phase 2: last compose-confirmed generation per serialize_key (recording uri), authored+verified by
        # this process. Read/written only inside that key's serialized write section, so steps observe a
        # consistent base. Bounded (TTL + LRU) so it cannot leak across many recordings.
        self._compose_state = _ComposeStateCache(
            max_size=_COMPOSE_STATE_MAX_ENTRIES, ttl_seconds=_COMPOSE_STATE_TTL_SECONDS, clock=time.monotonic
        )

    @property
    def session(self) -> aioboto3.Session:
        return self._get_session()

    @session.setter
    def session(self, session: aioboto3.Session) -> None:
        self._session = session
        self._session_created_at = time.monotonic()

    def _create_session(self, client_type_hint: AWSClientType | None = None) -> None:
        try:
            self._session = aioboto3.Session(
                aws_access_key_id=self._aws_access_key_id,
                aws_secret_access_key=self._aws_secret_access_key,
                profile_name=self._profile_name,
            )
            self._session_created_at = time.monotonic()
        except ProfileNotFound as e:
            profile_name = self._profile_name or os.environ.get("AWS_PROFILE") or "default"
            client_scope = f" while creating the {client_type_hint.value} client" if client_type_hint else ""
            raise AWSSessionConfigurationError(
                f"AWS profile {profile_name!r} could not be resolved{client_scope}. "
                "Unset AWS_PROFILE, create the profile, or pass explicit AWS credentials."
            ) from e

    def _get_session(self, client_type_hint: AWSClientType | None = None) -> aioboto3.Session:
        if self._session is None:
            self._create_session(client_type_hint)
        elif (time.monotonic() - self._session_created_at) > _SESSION_TTL_SECONDS:
            LOG.info("Recreating AWS session (TTL expired)", ttl_seconds=_SESSION_TTL_SECONDS)
            self._create_session(client_type_hint)
        return self._session

    def refresh_session(self) -> None:
        """Recreate the session to pick up refreshed credentials (e.g., rotated web identity tokens)."""
        LOG.info("Refreshing AWS session to pick up new credentials")
        self._create_session()

    def _is_expired_token_error(self, error: Exception) -> bool:
        """Check if an exception is an AWS expired-credentials error. S3/STS report this under either exact
        code: ``ExpiredTokenException`` (the long form) or ``ExpiredToken`` (the bare S3 form)."""
        return (
            isinstance(error, ClientError) and error.response.get("Error", {}).get("Code") in _EXPIRED_TOKEN_ERROR_CODES
        )

    def _is_not_found_error(self, error: Exception) -> bool:
        """Check if an exception is a missing-object (terminal not-found) error."""
        return (
            isinstance(error, ClientError) and error.response.get("Error", {}).get("Code") in S3_NOT_FOUND_ERROR_CODES
        )

    def _error_code(self, error: Exception) -> str:
        if isinstance(error, ClientError):
            return error.response.get("Error", {}).get("Code", error.__class__.__name__)
        return error.__class__.__name__

    async def _s3_with_retry(
        self,
        op_name: str,
        operation: Any,
        *,
        before_retry: Any | None = None,
        **log_kwargs: Any,
    ) -> Any:
        """Execute an S3 operation with automatic retry on expired AWS token.

        Args:
            op_name: Human-readable name for logging (e.g. "upload", "download").
            operation: Async callable that creates an S3 client and performs the operation.
            before_retry: Optional callable invoked before retrying. Return False to abort the retry.
            **log_kwargs: Extra fields passed to the warning log on retry.

        Raises the original exception on non-token errors or when retry is exhausted / aborted.
        """
        for attempt in range(_S3_OPERATION_RETRIES):
            try:
                return await operation()
            except Exception as e:
                if attempt == 0 and self._is_expired_token_error(e):
                    LOG.warning(
                        f"AWS token expired during {op_name}, refreshing session and retrying",
                        **log_kwargs,
                    )
                    self.refresh_session()
                    if before_retry is not None and before_retry() is False:
                        raise
                    continue
                raise

    async def _detach_on_cancel(self, op_name: str, operation: Callable[[], Awaitable[Any]], **log_kwargs: Any) -> Any:
        """Run a multipart transfer so the caller's cancellation cannot strand aioboto3's worker tasks.

        aioboto3's upload_fileobj (still true in 15.5.0) cancels its uploader tasks and aborts the multipart
        upload only on its own exit path; a CancelledError delivered at its internal asyncio.wait skips both.
        """
        # The detached transfer has no deadline of its own: botocore's per-request timeouts bound a stall,
        # and a process shutdown cancels it exactly as before.
        task = asyncio.ensure_future(operation())
        try:
            return await asyncio.shield(task)
        except asyncio.CancelledError:
            task.add_done_callback(lambda done: self._log_detached_transfer(op_name, done, **log_kwargs))
            raise

    def _begin_serialized_write(
        self, key: str, seal: bool = False, arm_fallback: bool = True
    ) -> tuple[asyncio.Future[None] | None, asyncio.Future[None]]:
        """Reserve this write's slot in ``key``'s issue-ordered write chain. Call synchronously before
        the first await so slot order matches call order. Returns ``(prev, done)``: await ``prev`` before
        writing, and complete ``done`` (via ``_finish_serialized_write``) once the real write finishes —
        for a detached transfer this must happen in the detached task, not on the cancelled awaiter.

        ``seal=True`` marks the key as being finalized: a queued prefix that has not yet begun its real
        transfer is superseded when its turn comes, so the terminal recording write waits only for the
        currently active transfer, not the whole queued backlog.

        ``arm_fallback`` (only meaningful with ``seal``) remembers the newest pre-terminal queued snapshot
        (``prev`` at seal time) as the terminal's fallback target: instead of being skipped it parks its
        reader (``_prefix_is_fallback_target``) so the terminal can upload it iff the terminal write fails.
        A fallback is only meaningful when the terminal writes the same object the prefixes queued to; when
        the finalize renames the object (``.webm`` prefixes, ``.mp4`` terminal) the parked prefix could
        never be served under the finalized uri, so the caller passes ``arm_fallback=False`` and the newest
        queued prefix is superseded like the rest."""
        prev = self._object_write_chains.get(key)
        done: asyncio.Future[None] = asyncio.get_running_loop().create_future()
        self._object_write_chains[key] = done
        if seal:
            self._object_write_sealed.add(key)
            if arm_fallback:
                self._object_write_fallback_target[key] = prev
        return prev, done

    def _prefix_superseded(self, key: str) -> bool:
        return key in self._object_write_sealed

    def _prefix_is_fallback_target(self, key: str, done: asyncio.Future[None]) -> bool:
        return self._object_write_fallback_target.get(key) is done

    def _finish_serialized_write(self, key: str, done: asyncio.Future[None]) -> None:
        if not done.done():
            done.set_result(None)
        if self._object_write_chains.get(key) is done:
            del self._object_write_chains[key]
            self._object_write_sealed.discard(key)
            self._object_write_fallback_target.pop(key, None)
            # Defensive: if a fallback was parked but its terminal never consumed it (e.g. no terminal
            # ever ran), close the retained reader when the key drains so it cannot leak.
            orphan = self._object_write_fallback.pop(key, None)
            if orphan is not None:
                orphan.close_reader()

    @staticmethod
    async def _await_prev_write(prev: asyncio.Future[None]) -> None:
        # Order this write after the previous one to the same key. The chain future only ever resolves
        # with a result, so the only exception here is this task's own cancellation, which must propagate
        # (never swallow it); a prior write's own outcome must not block us, hence the defensive guard.
        try:
            await prev
        except asyncio.CancelledError:
            raise
        except Exception:
            pass

    @staticmethod
    def _log_detached_transfer(op_name: str, task: asyncio.Task[Any], **log_kwargs: Any) -> None:
        if task.cancelled():
            LOG.warning(f"S3 {op_name} cancelled after its caller stopped waiting", **log_kwargs)
        elif (exc := task.exception()) is not None:
            LOG.warning(f"S3 {op_name} failed after its caller stopped waiting", error=str(exc), **log_kwargs)
        else:
            LOG.info(f"S3 {op_name} completed after its caller stopped waiting", **log_kwargs)

    def _ecs_client(self) -> ECSClient:
        return self._get_session(AWSClientType.ECS).client(
            AWSClientType.ECS, region_name=self.region_name, endpoint_url=self._endpoint_url, config=self._config
        )

    def _secrets_manager_client(self) -> SecretsManagerClient:
        return self._get_session(AWSClientType.SECRETS_MANAGER).client(
            AWSClientType.SECRETS_MANAGER,
            region_name=self.region_name,
            endpoint_url=self._endpoint_url,
            config=self._config,
        )

    def _s3_client(self) -> S3Client:
        return self._get_session(AWSClientType.S3).client(
            AWSClientType.S3, region_name=self.region_name, endpoint_url=self._endpoint_url, config=self._config
        )

    def _ec2_client(self) -> EC2Client:
        return self._get_session(AWSClientType.EC2).client(
            AWSClientType.EC2, region_name=self.region_name, endpoint_url=self._endpoint_url, config=self._config
        )

    def _batch_client(self) -> BatchClient:
        return self._get_session(AWSClientType.BATCH).client(
            AWSClientType.BATCH, region_name=self.region_name, endpoint_url=self._endpoint_url, config=self._config
        )

    def _create_tag_string(self, tags: dict[str, str]) -> str:
        return "&".join([f"{k}={v}" for k, v in tags.items()])

    async def get_secret(self, secret_name: str) -> str | None:
        # https://boto3.amazonaws.com/v1/documentation/api/latest/reference/services/secretsmanager/client/get_secret_value.html
        try:
            async with self._secrets_manager_client() as client:
                response = await client.get_secret_value(SecretId=secret_name)
                return response["SecretString"]
        except Exception as e:
            error_code = self._error_code(e)
            LOG.exception("Failed to get secret.", secret_name=secret_name, error_code=error_code)
            return None

    async def create_secret(self, secret_name: str, secret_value: str) -> None:
        # https://boto3.amazonaws.com/v1/documentation/api/latest/reference/services/secretsmanager/client/create_secret.html
        try:
            async with self._secrets_manager_client() as client:
                await client.create_secret(Name=secret_name, SecretString=secret_value)
        except Exception as e:
            LOG.exception("Failed to create secret.", secret_name=secret_name)
            raise e

    async def set_secret(self, secret_name: str, secret_value: str) -> None:
        # https://boto3.amazonaws.com/v1/documentation/api/latest/reference/services/secretsmanager/client/put_secret_value.html
        try:
            async with self._secrets_manager_client() as client:
                await client.put_secret_value(SecretId=secret_name, SecretString=secret_value)
        except Exception as e:
            LOG.exception("Failed to set secret.", secret_name=secret_name)
            raise e

    async def delete_secret(self, secret_name: str) -> None:
        # https://boto3.amazonaws.com/v1/documentation/api/latest/reference/services/secretsmanager/client/delete_secret.html
        try:
            async with self._secrets_manager_client() as client:
                await client.delete_secret(SecretId=secret_name)
        except Exception as e:
            LOG.exception("Failed to delete secret.", secret_name=secret_name)
            raise e

    async def upload_file(
        self,
        uri: str,
        data: bytes,
        storage_class: S3StorageClass = S3StorageClass.STANDARD,
        tags: dict[str, str] | None = None,
        serialize_key: str | None = None,
        supersede_queued: bool = False,
    ) -> str | None:
        # https://boto3.amazonaws.com/v1/documentation/api/latest/reference/services/s3/client/put_object.html
        if storage_class not in S3StorageClass:
            raise ValueError(f"Invalid storage class: {storage_class}. Must be one of {list(S3StorageClass)}")

        async def _op() -> str:
            async with self._s3_client() as client:
                parsed_uri = S3Uri(uri)
                extra_args = {"Tagging": self._create_tag_string(tags)} if tags else {}
                await client.put_object(
                    Body=data,
                    Bucket=parsed_uri.bucket,
                    Key=parsed_uri.key,
                    StorageClass=str(storage_class),
                    **extra_args,
                )
                return uri

        if serialize_key is not None:
            # A serialized write (a recording's terminal put) must both wait for older prefixes and
            # survive the wait_for_upload_aiotasks barrier that can cancel this queued task while it is
            # draining them — detach so put_object still lands last instead of being lost.
            # supersede_queued marks this the finalize: queued-not-active prefixes are then skipped so
            # this write waits only for the currently active transfer. A fallback is only armed when the
            # finalize writes the same object the prefixes queued to; when it renames the object
            # (serialize_key is the old prefix key, uri is the new object) a parked prefix could never be
            # served under the finalized uri, so we skip the fallback and let the newest prefix supersede.
            arm_fallback = serialize_key == uri
            prev, done = self._begin_serialized_write(serialize_key, seal=supersede_queued, arm_fallback=arm_fallback)

            async def _run() -> str | None:
                # The terminal owns any parked fallback (the newest pre-terminal queued prefix): discard it
                # unread on success, upload it on failure so the recording does not regress to the older
                # active prefix. The retained reader is closed on every path in the finally.
                try:
                    if prev is not None:
                        await self._await_prev_write(prev)
                    return await self._s3_with_retry("upload", _op, uri=uri)
                except Exception:
                    LOG.exception("S3 upload failed.", uri=uri)
                    fallback = self._object_write_fallback.get(serialize_key)
                    if fallback is not None and not fallback.consumed:
                        fallback.consumed = True
                        try:
                            await fallback.upload()
                        except Exception:
                            LOG.exception("S3 fallback prefix upload failed.", uri=uri)
                    return None
                finally:
                    fb = self._object_write_fallback.pop(serialize_key, None)
                    if fb is not None:
                        fb.close_reader()
                    # Any serialized upload_file is a full put_object that replaces the whole object (and its
                    # metadata), so a compose base cached from a prior streamed snapshot is now stale — its
                    # ETag no longer matches, and the next snapshot's conditional copy would 412 and be
                    # skipped. Clear it INSIDE the serialized slot AFTER draining prior writes and writing,
                    # on EVERY exit path (not only the terminal finalize; conservative even when the write
                    # outcome is ambiguous — safety beats cache-hit rate). The next snapshot reseeds.
                    self._compose_state.pop(serialize_key)
                    self._finish_serialized_write(serialize_key, done)

            return await self._detach_on_cancel("upload", _run, uri=uri)

        try:
            return await self._s3_with_retry("upload", _op, uri=uri)
        except Exception:
            LOG.exception("S3 upload failed.", uri=uri)
            return None

    async def upload_file_stream(
        self,
        uri: str,
        file_obj: IO[bytes],
        storage_class: S3StorageClass = S3StorageClass.STANDARD,
        tags: dict[str, str] | None = None,
        close_file_obj: bool = False,
        serialize_key: str | None = None,
    ) -> str | None:
        # https://boto3.amazonaws.com/v1/documentation/api/latest/reference/services/s3/client/upload_fileobj.html#upload-fileobj
        if storage_class not in S3StorageClass:
            raise ValueError(f"Invalid storage class: {storage_class}. Must be one of {list(S3StorageClass)}")

        async def _op() -> str:
            async with self._s3_client() as client:
                parsed_uri = S3Uri(uri)
                extra_args: dict[str, Any] = {"StorageClass": str(storage_class)}
                if tags:
                    extra_args["Tagging"] = self._create_tag_string(tags)
                await client.upload_fileobj(
                    file_obj,
                    parsed_uri.bucket,
                    parsed_uri.key,
                    ExtraArgs=extra_args,
                    Config=_STREAM_UPLOAD_TRANSFER_CONFIG,
                )
                LOG.debug("Upload file stream success", uri=uri)
                return uri

        def _rewind_stream() -> bool | None:
            try:
                file_obj.seek(0)
            except (OSError, io.UnsupportedOperation):
                LOG.warning("Cannot rewind stream for retry, failing upload", uri=uri)
                return False
            return None

        # Reserve the write-chain slot synchronously (before detaching) so slot order matches call order.
        prev, done = self._begin_serialized_write(serialize_key) if serialize_key else (None, None)
        parked = False

        async def _run() -> str:
            nonlocal parked
            try:
                # Held inside the detached task: a later same-key write (e.g. the terminal upload) awaits
                # `done`, so even a cancelled-then-detached prefix keeps its slot until it truly finishes.
                if prev is not None:
                    await self._await_prev_write(prev)
                if serialize_key is not None and self._prefix_superseded(serialize_key):
                    if done is not None and self._prefix_is_fallback_target(serialize_key, done):
                        # Newest pre-terminal queued snapshot: park (don't upload) so the terminal can
                        # fall back to it if its own write fails. Ownership of file_obj passes to the
                        # parked fallback, which the terminal closes (on success) or uploads then closes.
                        async def _fallback_upload() -> Any:
                            return await self._s3_with_retry(
                                "stream upload (fallback)", _op, before_retry=_rewind_stream, uri=uri
                            )

                        self._object_write_fallback[serialize_key] = _ParkedFallback(file_obj, _fallback_upload)
                        parked = True
                        return uri
                    # Superseded and not the fallback: skip the obsolete upload so the terminal waits only
                    # for the active transfer, not the whole queued backlog.
                    LOG.debug("Superseded by terminal recording write; skipping queued prefix", uri=uri)
                    return uri
                return await self._s3_with_retry("stream upload", _op, before_retry=_rewind_stream, uri=uri)
            finally:
                # The transfer can be detached and outlive this call's awaiter (see _detach_on_cancel), so
                # an owned file_obj is closed only after the real transfer finishes — never on the caller's
                # cancel path. A parked fallback keeps its reader open; the terminal owns and closes it.
                if close_file_obj and not parked:
                    file_obj.close()
                if done is not None and serialize_key is not None:
                    self._finish_serialized_write(serialize_key, done)

        try:
            return await self._detach_on_cancel("stream upload", _run, uri=uri)
        except Exception:
            LOG.exception("S3 upload stream failed.", uri=uri)
            return None

    def forget_compose_state(self, serialize_key: str) -> None:
        """Drop any cached compose base for a key (e.g. after a terminal full replacement rewrites bytes,
        so the next run's first step reseeds from S3 rather than trusting a stale generation)."""
        self._compose_state.pop(serialize_key)

    async def store_recording_prefix(
        self,
        uri: str,
        path: str,
        length: int,
        *,
        storage_class: S3StorageClass = S3StorageClass.STANDARD,
        serialize_key: str,
    ) -> str | None:
        """Advance the recording object at ``uri`` to exactly ``[0, length)``.

        Preferred path: copy the already-uploaded prefix server-side (UploadPartCopy against the confirmed
        ETag) and upload only the new tail, so per-step client bytes are the delta, not the whole prefix.
        If that is unsupported (no/too-small/not-growing base, provider limit, or a failure that left the
        remote object UNCHANGED) the Phase 1 full-prefix replacement runs IN THE SAME serialized slot, so a
        later step can never interleave between the failed compose and its fallback. If the remote object
        moved under us (stale ETag / foreign generation) the step is skipped and the remote is preserved.

        The file handle is opened before the transfer is detached so a post-cancel unlink cannot strand it;
        reads are hard-capped to the frozen ``length``. Readers only ever observe a complete old or complete
        new object. Returns ``uri`` on success, ``None`` on a swallowed transfer failure; ``OSError`` from
        opening a vanished file propagates so the caller can treat it like the Phase 1 stream did."""
        parsed_uri = S3Uri(uri)
        # Own the fd before reserving the slot / detaching (blocker: cancel-then-unlink must not strand it).
        fh = open(path, "rb")  # noqa: SIM115 — lifetime spans the detached transfer; closed in _run's finally
        prev, done = self._begin_serialized_write(serialize_key)
        parked = False

        async def _run() -> str | None:
            nonlocal parked
            try:
                if prev is not None:
                    await self._await_prev_write(prev)
                if self._prefix_superseded(serialize_key):
                    if done is not None and self._prefix_is_fallback_target(serialize_key, done):
                        # Newest pre-terminal snapshot: park an owned full-prefix reader so the terminal can
                        # upload it iff the terminal write fails (mirrors the Phase 1 stream fallback). This
                        # reader OUTLIVES the outer call, so it owns the fd; the terminal closes it on success
                        # or uploads-then-closes on failure.
                        reader = _RangedFileReader(fh, 0, length, owns_fh=True)

                        def _rewind() -> bool | None:
                            try:
                                reader.seek(0)
                            except (OSError, io.UnsupportedOperation):
                                return False
                            return None

                        async def _fallback_upload() -> Any:
                            async def _op() -> None:
                                async with self._s3_client() as client:
                                    await client.upload_fileobj(
                                        reader,
                                        parsed_uri.bucket,
                                        parsed_uri.key,
                                        ExtraArgs={"StorageClass": str(storage_class)},
                                        Config=_STREAM_UPLOAD_TRANSFER_CONFIG,
                                    )

                            return await self._s3_with_retry(
                                "recording prefix (fallback)", _op, before_retry=_rewind, uri=uri
                            )

                        self._object_write_fallback[serialize_key] = _ParkedFallback(
                            cast("IO[bytes]", reader), _fallback_upload
                        )
                        parked = True
                        return uri
                    # Superseded and not the fallback target: skip the obsolete write.
                    LOG.debug("Superseded by terminal recording write; skipping queued prefix", uri=uri)
                    return uri
                return await self._compose_or_replace(parsed_uri, uri, fh, length, serialize_key, storage_class)
            finally:
                # A parked fallback keeps its reader (and fd) open; the terminal owns and closes it.
                if not parked:
                    fh.close()
                self._finish_serialized_write(serialize_key, done)

        try:
            return await self._detach_on_cancel("recording prefix", _run, uri=uri)
        except Exception:
            LOG.exception("S3 recording prefix write failed.", uri=uri)
            return None

    async def _compose_or_replace(
        self, parsed_uri: S3Uri, uri: str, fh: IO[bytes], length: int, serialize_key: str, sc: S3StorageClass
    ) -> str:
        # Wrap the WHOLE compose-or-replace transaction in the shared expired-token refresh+retry (parity
        # with the Phase 1 stream): an ExpiredToken on head/create/copy/upload/complete refreshes the session
        # and retries the whole transaction once on a fresh client, instead of silently degrading to a
        # fallback on the same expired client and failing every snapshot until the TTL.
        # An expired-token attempt that had already created an MPU cannot abort it on its now-invalid client
        # (one dead session token invalidates the whole client, so that abort just raises); the orphan upload
        # id is carried here and aborted on the refreshed client before a replacement MPU is created, so a
        # credential expiry mid-compose does not leak an incomplete multipart upload.
        orphan_upload_ids: list[str] = []

        async def _op() -> str:
            async with self._s3_client() as client:
                unabortable: list[str] = []
                while orphan_upload_ids:
                    uid = orphan_upload_ids.pop()
                    if not await self._abort_compose(client, parsed_uri, uid):
                        unabortable.append(uid)
                if unabortable:
                    # Fail closed: the refreshed client still could not abort a carried orphan MPU. Do NOT
                    # create a replacement compose MPU on top of an un-aborted orphan. Preserve the existing
                    # remote object (never overwrite), drop stale local compose state so the next step authors
                    # a fresh verified generation, log the lifecycle-policy backstop, and skip this step. The
                    # bucket AbortIncompleteMultipartUpload lifecycle rule (the documented rollout
                    # prerequisite) reclaims the leaked upload.
                    self._compose_state.pop(serialize_key)
                    LOG.warning(
                        "Orphaned compose MPU could not be aborted on the refreshed client; skipping this "
                        "step and preserving the remote (bucket AbortIncompleteMultipartUpload lifecycle rule "
                        "is the backstop)",
                        uri=uri,
                        upload_ids=unabortable,
                    )
                    return uri
                try:
                    result = await self._compose_once(
                        client, parsed_uri, uri, fh, length, serialize_key, sc, orphan_upload_ids
                    )
                except _ComposeConflict:
                    # The remote object moved under us: never overwrite it. Drop our (now wrong) state and
                    # let the next step author a fresh verified generation; skip this write.
                    self._compose_state.pop(serialize_key)
                    LOG.warning("Compose conflict; preserving remote generation and skipping step", uri=uri)
                    return uri
                except ComposeUnsupportedError:
                    # Remote unchanged or no owned base: author a verified full replacement in this same slot
                    # (also reseeds compose state), so ordering with later steps is preserved.
                    return await self._full_replace_and_seed(client, parsed_uri, uri, fh, length, serialize_key, sc)
                if isinstance(result, str):
                    # A fresh-client fallback already replaced-or-skipped on a healthy client and managed
                    # compose state itself (a failed inline abort was cleaned up before continuing).
                    return result
                self._compose_state.set(serialize_key, result)
                return uri

        return await self._s3_with_retry("recording prefix compose", _op, uri=uri)

    async def _full_replace_and_seed(
        self,
        client: Any,
        parsed_uri: S3Uri,
        uri: str,
        fh: IO[bytes],
        length: int,
        serialize_key: str,
        sc: S3StorageClass,
    ) -> str:
        """Full-prefix replacement that AUTHORS a verified generation: write ``[0, length)`` with a unique
        token in object metadata, then HeadObject and seed local compose state ONLY if the object carries our
        exact token and length. This is the sole way compose state is seeded, so a later step can only ever
        copy a prefix this process authored and verified — a foreign/zombie writer's object is never spliced
        into. If a foreign writer raced our PUT (token/length mismatch) we do not seed and the next step
        full-replaces again."""
        token = uuid.uuid4().hex
        reader = _RangedFileReader(fh, 0, length)  # non-owning: the outer store_recording_prefix owns fh
        await client.upload_fileobj(
            reader,
            parsed_uri.bucket,
            parsed_uri.key,
            ExtraArgs={"StorageClass": str(sc), "Metadata": {_COMPOSE_GEN_META_KEY: token}},
            Config=_STREAM_UPLOAD_TRANSFER_CONFIG,
        )
        head = await client.head_object(Bucket=parsed_uri.bucket, Key=parsed_uri.key)
        hmeta = head.get("Metadata") or {}
        head_len = head.get("ContentLength")
        head_etag = head.get("ETag")
        seeded = (
            head_len is not None
            and head_etag is not None
            and int(head_len) == length
            and hmeta.get(_COMPOSE_GEN_META_KEY) == token
        )
        if seeded:
            self._compose_state.set(serialize_key, _ComposeState(length, str(head_etag).strip('"')))
        else:
            self._compose_state.pop(serialize_key)
        return uri

    async def _compose_once(
        self,
        client: Any,
        parsed_uri: S3Uri,
        uri: str,
        fh: IO[bytes],
        length: int,
        serialize_key: str,
        sc: S3StorageClass,
        orphan_upload_ids: list[str],
    ) -> _ComposeState | str:
        if self._endpoint_url is not None:
            # The compose anti-corruption fence relies on conditional CompleteMultipartUpload (IfMatch) and
            # CopySourceIfMatch, which are AWS-S3-specific. A customer-pointed S3-compatible endpoint may ignore
            # an unsupported IfMatch and silently degrade the fence to an unconditional overwrite, so refuse
            # compose entirely and fall back to the verified full-prefix replacement (fail closed).
            raise ComposeUnsupportedError()
        base = self._compose_state.get(serialize_key)
        if base is None:
            # No generation THIS process authored+verified. Never head-and-trust an unowned remote object: a
            # duplicate/zombie producer could have full-PUT a foreign >=5 MiB generation, and copying its
            # prefix + our tail would splice a mixed/unplayable object. Force a verified full reseed instead.
            raise ComposeUnsupportedError()
        if base.length < _MIN_COMPOSE_BASE_BYTES:
            # Confirmed base below the 5-MiB copy floor: fall back to a full replacement (which reseeds).
            raise ComposeUnsupportedError()
        if length < base.length:
            # The confirmed generation is LONGER than this snapshot: never truncate it back to an older,
            # shorter prefix — preserve the remote and skip.
            raise _ComposeConflict()
        if length == base.length:
            # Already at the target length: nothing to append. No-op (do not full-PUT), keep the base.
            return base
        tail_size = length - base.length
        if base.length > _MAX_COMPOSE_PART_BYTES or tail_size > _MAX_COMPOSE_PART_BYTES:
            # A single copy part / tail part cannot exceed 5 GiB; fall back rather than split.
            raise ComposeUnsupportedError()
        token = uuid.uuid4().hex
        try:
            create = await client.create_multipart_upload(
                Bucket=parsed_uri.bucket,
                Key=parsed_uri.key,
                StorageClass=str(sc),
                Metadata={_COMPOSE_GEN_META_KEY: token},
            )
        except Exception as e:
            if self._is_expired_token_error(e):
                raise  # let _s3_with_retry refresh + retry the whole transaction
            # No MPU to abort; the remote object is unchanged -> safe full-prefix fallback.
            raise ComposeUnsupportedError()
        upload_id = create["UploadId"]
        try:
            try:
                copy = await client.upload_part_copy(
                    Bucket=parsed_uri.bucket,
                    Key=parsed_uri.key,
                    UploadId=upload_id,
                    PartNumber=1,
                    CopySource={"Bucket": parsed_uri.bucket, "Key": parsed_uri.key},
                    CopySourceIfMatch=base.etag,
                    CopySourceRange=f"bytes=0-{base.length - 1}",
                )
            except ClientError as e:
                if self._error_code(e) in ("PreconditionFailed", "412"):
                    # The confirmed base moved under us: preserve the remote, do not full-PUT over it.
                    if await self._abort_compose(client, parsed_uri, upload_id):
                        raise _ComposeConflict()
                    return await self._cleanup_after_failed_abort(parsed_uri, uri, upload_id, serialize_key, None)
                raise
            part1_etag = copy["CopyPartResult"]["ETag"]
            tail_reader = _RangedFileReader(fh, base.length, tail_size)  # non-owning
            part2 = await client.upload_part(
                Bucket=parsed_uri.bucket, Key=parsed_uri.key, UploadId=upload_id, PartNumber=2, Body=tail_reader
            )
            parts = {"Parts": [{"PartNumber": 1, "ETag": part1_etag}, {"PartNumber": 2, "ETag": part2["ETag"]}]}
            try:
                # Fence the Complete on the verified base ETag (If-Match header on CompleteMultipartUpload in
                # the pinned botocore model). UploadPartCopy fences only the COPY SOURCE; a foreign producer
                # can still replace the destination object between the copy and the Complete. Without this
                # fence an unconditional Complete would overwrite that newer generation. base.etag is the
                # stripped ETag, matching the CopySourceIfMatch convention above (S3 accepts the unquoted
                # strong validator; validated in the real-S3 compose probe).
                complete = await client.complete_multipart_upload(
                    Bucket=parsed_uri.bucket,
                    Key=parsed_uri.key,
                    UploadId=upload_id,
                    MultipartUpload=parts,
                    IfMatch=base.etag,
                )
            except Exception as e:
                if self._is_expired_token_error(e):
                    # The client is dead — reconciling/aborting here is futile. Re-raise; the outer handler
                    # records the orphan MPU (once) and _s3_with_retry refreshes + retries on a fresh client.
                    raise
                if isinstance(e, ClientError) and self._error_code(e) in (
                    "PreconditionFailed",
                    "412",
                    "ConditionalRequestConflict",
                    "409",
                ):
                    # The IfMatch fence rejected the Complete: the destination changed under us (a foreign
                    # generation replaced it after our copy). Preserve that generation — never overwrite it.
                    if await self._abort_compose(client, parsed_uri, upload_id):
                        raise _ComposeConflict()
                    return await self._cleanup_after_failed_abort(parsed_uri, uri, upload_id, serialize_key, None)
                # Ambiguous Complete (lost response / transport blip): reconcile by HEAD, which preserves a
                # foreign generation and only advances on a proven-durable one; never a blind full replacement.
                outcome = await self._reconcile_complete(client, parsed_uri, length, base, token)
                if isinstance(outcome, _ComposeState):
                    return outcome  # Complete actually landed (verified by length + generation token).
                if await self._abort_compose(client, parsed_uri, upload_id):
                    if outcome == "conflict":
                        raise _ComposeConflict()
                    raise ComposeUnsupportedError()
                fallback = None if outcome == "conflict" else (fh, length, sc)
                return await self._cleanup_after_failed_abort(parsed_uri, uri, upload_id, serialize_key, fallback)
            new_etag = complete.get("ETag")
            if not new_etag:
                # Complete succeeded but omitted ETag. Reconcile by HEAD and advance state ONLY if the object
                # is provably OUR generation (exact length + our token). A foreign generation that raced in
                # between Complete and HEAD must not be cached (its ETag would let the next snapshot's
                # conditional copy splice a foreign prefix onto our tail) nor overwritten (the MPU already
                # completed): preserve it and skip this step.
                outcome = await self._reconcile_complete(client, parsed_uri, length, base, token)
                if isinstance(outcome, _ComposeState):
                    return outcome
                raise _ComposeConflict()
            return _ComposeState(length, new_etag.strip('"'))
        except (_ComposeConflict, ComposeUnsupportedError):
            raise
        except Exception as e:
            if self._is_expired_token_error(e):
                # Carry the orphan MPU to the refreshed-client retry (aborting on this expired client fails);
                # do NOT abort here on the dead client.
                orphan_upload_ids.append(upload_id)
                raise
            # A non-expired failure before/around Complete that left the remote object unchanged: abort now on
            # this still-valid client, then let the caller do the safe full-prefix replacement. If this inline
            # abort fails (a transient transport blip), retry the cleanup on a freshly-created client and author
            # the replacement there — never leave the incomplete MPU to lifecycle when a fresh client can still
            # reclaim it, and never full-replace on top of an un-aborted orphan.
            if await self._abort_compose(client, parsed_uri, upload_id):
                raise ComposeUnsupportedError()
            return await self._cleanup_after_failed_abort(parsed_uri, uri, upload_id, serialize_key, (fh, length, sc))

    async def _reconcile_complete(
        self, client: Any, parsed_uri: S3Uri, length: int, base: _ComposeState, token: str
    ) -> _ComposeState | str:
        """Resolve an ambiguous Complete by HeadObject. Returns a ``_ComposeState`` only when the object is
        provably OUR generation (exact length AND our generation token); ``"unsupported"`` when the old base
        is still intact (safe to full-replace); ``"conflict"`` when a foreign/other generation is present or
        the head cannot be read (must preserve, never overwrite)."""
        try:
            head = await client.head_object(Bucket=parsed_uri.bucket, Key=parsed_uri.key)
            hlen = int(head["ContentLength"])
            hetag = str(head["ETag"]).strip('"')
            hmeta = head.get("Metadata") or {}
        except asyncio.CancelledError:
            raise
        except Exception as e:
            if self._is_expired_token_error(e):
                # An expired token on the reconciliation HEAD is recoverable, not a conflict: re-raise so the
                # outer handler carries the orphan MPU and _s3_with_retry refreshes the session and retries the
                # whole transaction on a fresh client (preserving expired-token handling).
                raise
            # ANY OTHER unreadable HEAD after an ambiguous Complete — a non-token ClientError, a botocore
            # transport/read/endpoint failure (EndpointConnectionError, ReadTimeoutError, ...), OR a 200 whose
            # body omits ContentLength/ETag — leaves the Complete outcome unknown. A foreign generation may
            # have landed during the same blip, so preserve the remote and resolve conflict; never fall
            # through to a full replacement that could clobber it.
            return "conflict"
        if hlen == length and hmeta.get(_COMPOSE_GEN_META_KEY) == token:
            LOG.info("Ambiguous compose Complete reconciled as durable (length + token)", uri=parsed_uri.uri)
            return _ComposeState(length, hetag)
        if hlen == base.length and hetag == base.etag:
            return "unsupported"
        return "conflict"

    async def _abort_compose(self, client: Any, parsed_uri: S3Uri, upload_id: str) -> bool:
        """Best-effort abort of a compose MPU. Returns True iff the incomplete upload is gone afterward, so a
        caller cleaning up an orphan on the refreshed client can tell it is actually cleaned up. A
        ``NoSuchUpload`` (the upload was already completed/aborted — e.g. an ambiguous Complete actually
        landed) counts as success: the cleanup goal is met, so it must not be reported as an unabortable
        orphan (which would trigger a false lifecycle-leak warning / fail-closed skip). A genuine failure
        (AccessDenied, transport, expired token, ...) still returns False so it is not masked."""
        try:
            await client.abort_multipart_upload(Bucket=parsed_uri.bucket, Key=parsed_uri.key, UploadId=upload_id)
            return True
        except ClientError as e:
            if self._error_code(e) in _ABORT_ALREADY_GONE_ERROR_CODES:
                return True
            LOG.warning("Failed to abort compose multipart upload", uri=parsed_uri.uri, upload_id=upload_id)
            return False
        except Exception:
            LOG.warning("Failed to abort compose multipart upload", uri=parsed_uri.uri, upload_id=upload_id)
            return False

    async def _cleanup_after_failed_abort(
        self,
        parsed_uri: S3Uri,
        uri: str,
        upload_id: str,
        serialize_key: str,
        fallback_replace: tuple[IO[bytes], int, S3StorageClass] | None,
    ) -> str:
        """An in-line abort of our compose MPU failed on a still-valid (non-expired) client. Retry the cleanup
        exactly once on a freshly-created client BEFORE disposing of this snapshot, so a transient abort blip
        does not strand the incomplete MPU until the bucket lifecycle rule reclaims it. On cleanup success a
        safe fallback path (``fallback_replace`` given, remote unchanged) authors its full replacement on that
        same healthy fresh client; a conflict path (``fallback_replace`` None) just preserves the remote. On
        cleanup failure, fail closed: preserve the remote, clear stale compose state, author no replacement /
        new MPU, and leave the bucket AbortIncompleteMultipartUpload lifecycle rule as the only backstop. This
        never loops."""
        # Disposing of this snapshot: drop our (now-suspect) compose state up front so even a fresh-client
        # construction failure cannot strand it. A successful fallback replacement below re-seeds a verified
        # generation via _full_replace_and_seed; every other outcome intentionally leaves it cleared.
        self._compose_state.pop(serialize_key)
        # The in-line abort may have failed because the token expired between the primary op and the abort.
        # _s3_client reuses the cached session (recreated only on the 45-min TTL), so refresh it here to give
        # the fresh client refreshed credentials; otherwise a token-expired abort fails identically and, since
        # this returns normally, _s3_with_retry never sees the token error and never refreshes.
        self.refresh_session()
        async with self._s3_client() as fresh:
            cleaned = await self._abort_compose(fresh, parsed_uri, upload_id)
            if cleaned and fallback_replace is not None:
                fh, length, sc = fallback_replace
                return await self._full_replace_and_seed(fresh, parsed_uri, uri, fh, length, serialize_key, sc)
        if not cleaned:
            LOG.warning(
                "Failed to abort compose MPU on both the in-line and a freshly-created client; preserving the "
                "remote and skipping this step (bucket AbortIncompleteMultipartUpload lifecycle rule is the "
                "backstop)",
                uri=uri,
                upload_id=upload_id,
            )
        return uri

    async def upload_file_from_path(
        self,
        uri: str,
        file_path: str,
        storage_class: S3StorageClass = S3StorageClass.STANDARD,
        metadata: dict | None = None,
        raise_exception: bool = False,
        tags: dict[str, str] | None = None,
        content_type: str | None = None,
    ) -> None:
        # https://boto3.amazonaws.com/v1/documentation/api/latest/reference/services/s3/client/upload_file.html
        async def _op() -> None:
            async with self._s3_client() as client:
                parsed_uri = S3Uri(uri)
                extra_args: dict[str, Any] = {"StorageClass": str(storage_class)}
                if metadata:
                    extra_args["Metadata"] = metadata
                if tags:
                    extra_args["Tagging"] = self._create_tag_string(tags)
                if content_type:
                    extra_args["ContentType"] = content_type
                else:
                    guessed_type, _ = guess_type(file_path)
                    if guessed_type:
                        extra_args["ContentType"] = guessed_type
                await client.upload_file(
                    Filename=file_path,
                    Bucket=parsed_uri.bucket,
                    Key=parsed_uri.key,
                    ExtraArgs=extra_args,
                )

        try:
            await self._detach_on_cancel("upload", lambda: self._s3_with_retry("upload", _op, uri=uri), uri=uri)
        except Exception as e:
            LOG.exception("S3 upload failed.", uri=uri)
            if raise_exception:
                raise e

    async def download_file(self, uri: str, log_exception: bool = True) -> bytes | None:
        # https://boto3.amazonaws.com/v1/documentation/api/latest/reference/services/s3/client/get_object.html
        async def _op() -> bytes:
            async with self._s3_client() as client:
                parsed_uri = S3Uri(uri)
                response = await client.get_object(Bucket=parsed_uri.bucket, Key=parsed_uri.key)
                return await response["Body"].read()

        try:
            return await self._s3_with_retry("download", _op, uri=uri)
        except Exception as e:
            # A missing object is terminal not-found, not a transient failure (e.g. first-run
            # profile restore before any archive exists). Log once without a traceback so callers
            # fall back immediately instead of surfacing repeated ERROR tracebacks.
            if self._is_not_found_error(e):
                if log_exception:
                    LOG.info("S3 object not found", uri=uri)
                return None
            if log_exception:
                LOG.exception("S3 download failed", uri=uri)
            return None

    async def delete_file(self, uri: str, log_exception: bool = True, raise_on_error: bool = False) -> None:
        # https://boto3.amazonaws.com/v1/documentation/api/latest/reference/services/s3/client/delete_object.html
        async def _op() -> None:
            async with self._s3_client() as client:
                parsed_uri = S3Uri(uri)
                await client.delete_object(Bucket=parsed_uri.bucket, Key=parsed_uri.key)

        try:
            await self._s3_with_retry("delete", _op, uri=uri)
        except Exception:
            if log_exception:
                LOG.exception("S3 delete failed", uri=uri)
            if raise_on_error:
                raise

    async def get_object_info(self, uri: str) -> dict:
        async def _op() -> dict:
            async with self._s3_client() as client:
                parsed_uri = S3Uri(uri)
                # Only get object metadata without the body
                return await client.head_object(Bucket=parsed_uri.bucket, Key=parsed_uri.key)

        return await self._s3_with_retry("head_object", _op, uri=uri)

    async def get_file_metadata(
        self,
        uri: str,
        log_exception: bool = True,
    ) -> dict | None:
        # https://boto3.amazonaws.com/v1/documentation/api/latest/reference/services/s3/client/head_object.html
        """
        Retrieves only the metadata of a file without downloading its content.

        Args:
            uri: The S3 URI of the file
            log_exception: Whether to log exceptions

        Returns:
            The metadata dictionary or None if the request fails
        """
        try:
            response = await self.get_object_info(uri)
            return response.get("Metadata", {})
        except Exception:
            if log_exception:
                LOG.exception("S3 metadata retrieval failed", uri=uri)
            return None

    async def create_presigned_urls(self, uris: list[str], expires_in: int | None = None) -> list[str] | None:
        # https://boto3.amazonaws.com/v1/documentation/api/latest/reference/services/s3/client/generate_presigned_url.html
        expiration = settings.PRESIGNED_URL_EXPIRATION if expires_in is None else expires_in

        async def _op() -> list[str]:
            presigned_urls = []
            async with self._s3_client() as client:
                for uri in uris:
                    parsed_uri = S3Uri(uri)
                    url = await client.generate_presigned_url(
                        "get_object",
                        Params={"Bucket": parsed_uri.bucket, "Key": parsed_uri.key},
                        ExpiresIn=expiration,
                    )
                    presigned_urls.append(url)
                return presigned_urls

        try:
            return await self._s3_with_retry("presigned URL generation", _op)
        except Exception:
            LOG.exception("Failed to create presigned url for S3 objects.", uris=uris)
            return None

    async def list_files(self, uri: str) -> list[str]:
        # https://boto3.amazonaws.com/v1/documentation/api/latest/reference/services/s3/paginator/ListObjectsV2.html
        async def _op() -> list[str]:
            object_keys: list[str] = []
            parsed_uri = S3Uri(uri)
            async with self._s3_client() as client:
                async for page in client.get_paginator("list_objects_v2").paginate(
                    Bucket=parsed_uri.bucket, Prefix=parsed_uri.key
                ):
                    if "Contents" in page:
                        for obj in page["Contents"]:
                            object_keys.append(obj["Key"])
                return object_keys

        return await self._s3_with_retry("list_files", _op, uri=uri)

    async def delete_files(self, bucket: str, keys: list[str]) -> list[str]:
        # https://boto3.amazonaws.com/v1/documentation/api/latest/reference/services/s3/client/delete_objects.html
        """
        Delete multiple objects from S3 bucket.

        Args:
            bucket: The S3 bucket name
            keys: List of object keys to delete

        Returns:
            The keys S3 refused to delete. DeleteObjects reports per-object failures in the
            response body rather than raising, so a caller that reads "no exception" as "all
            deleted" will believe objects are gone while they are still there.
        """
        if not keys:
            return []

        async def _op() -> list[str]:
            failed: list[str] = []
            async with self._s3_client() as client:
                objects = [{"Key": key} for key in keys]
                response = await client.delete_objects(
                    Bucket=bucket,
                    Delete={
                        "Objects": objects,
                        "Quiet": False,
                    },
                )
                if "Errors" in response:
                    for error in response["Errors"]:
                        LOG.error(
                            "Failed to delete object from S3",
                            bucket=bucket,
                            key=error.get("Key"),
                            code=error.get("Code"),
                            message=error.get("Message"),
                        )
                        failed.append(error["Key"])
            return failed

        try:
            return await self._s3_with_retry("delete_files", _op, bucket=bucket)
        except Exception as e:
            LOG.exception("Failed to delete files from S3", bucket=bucket, keys_count=len(keys))
            raise e

    async def restore_object(self, bucket: str, key: str, days: int = 1, tier: str = "Standard") -> None:
        # https://boto3.amazonaws.com/v1/documentation/api/latest/reference/services/s3/client/restore_object.html
        """
        Restore an archived S3 object from GLACIER storage class.

        Args:
            bucket: The S3 bucket name
            key: The S3 object key
            days: Number of days to keep the restored object available (default: 1)
            tier: Restoration tier - "Standard" (3-5 hours) or "Expedited" (1-5 minutes)
        """

        async def _op() -> None:
            async with self._s3_client() as client:
                await client.restore_object(
                    Bucket=bucket, Key=key, RestoreRequest={"Days": days, "GlacierJobParameters": {"Tier": tier}}
                )

        try:
            await self._s3_with_retry("restore_object", _op, bucket=bucket, key=key)
        except Exception as e:
            LOG.exception("Failed to restore S3 object", bucket=bucket, key=key, tier=tier)
            raise e

    async def run_task(
        self,
        cluster: str,
        launch_type: str,
        task_definition: str,
        subnets: list[str],
        security_groups: list[str],
        assign_public_ip: str = "DISABLED",
        enable_execute_command: bool = False,
    ) -> dict:
        # https://boto3.amazonaws.com/v1/documentation/api/latest/reference/services/ecs/client/run_task.html
        async with self._ecs_client() as client:
            return await client.run_task(
                cluster=cluster,
                launchType=launch_type,
                taskDefinition=task_definition,
                networkConfiguration={
                    "awsvpcConfiguration": {
                        "subnets": subnets,
                        "securityGroups": security_groups,
                        "assignPublicIp": assign_public_ip,
                    }
                },
                enableExecuteCommand=enable_execute_command,
            )

    async def stop_task(self, cluster: str, task: str, reason: str | None = None) -> dict:
        # https://boto3.amazonaws.com/v1/documentation/api/latest/reference/services/ecs/client/stop_task.html
        async with self._ecs_client() as client:
            return await client.stop_task(cluster=cluster, task=task, reason=reason)

    async def describe_tasks(self, cluster: str, tasks: list[str]) -> dict:
        # https://boto3.amazonaws.com/v1/documentation/api/latest/reference/services/ecs/client/describe_tasks.html
        async with self._ecs_client() as client:
            return await client.describe_tasks(cluster=cluster, tasks=tasks)

    async def list_tasks(self, cluster: str) -> dict:
        # https://boto3.amazonaws.com/v1/documentation/api/latest/reference/services/ecs/client/list_tasks.html
        async with self._ecs_client() as client:
            return await client.list_tasks(cluster=cluster)

    async def describe_task_definition(self, task_definition: str) -> dict:
        # https://boto3.amazonaws.com/v1/documentation/api/latest/reference/services/ecs/client/describe_task_definition.html
        async with self._ecs_client() as client:
            return await client.describe_task_definition(taskDefinition=task_definition)

    async def deregister_task_definition(self, task_definition: str) -> dict:
        # https://boto3.amazonaws.com/v1/documentation/api/latest/reference/services/ecs/client/deregister_task_definition.html
        async with self._ecs_client() as client:
            return await client.deregister_task_definition(taskDefinition=task_definition)

    ###### EC2 ######
    async def describe_network_interfaces(self, network_interface_ids: list[str]) -> dict:
        # https://boto3.amazonaws.com/v1/documentation/api/latest/reference/services/ec2/client/describe_network_interfaces.html
        async with self._ec2_client() as client:
            return await client.describe_network_interfaces(NetworkInterfaceIds=network_interface_ids)

    ###### Batch ######
    async def describe_job(self, job_id: str) -> dict:
        # https://boto3.amazonaws.com/v1/documentation/api/latest/reference/services/batch/client/describe_jobs.html
        async with self._batch_client() as client:
            response = await client.describe_jobs(jobs=[job_id])
            return response["jobs"][0] if response["jobs"] else {}

    async def list_jobs(self, job_queue: str, job_status: str) -> list[dict]:
        # NOTE: AWS batch only records the latest 7 days jobs by default
        # https://boto3.amazonaws.com/v1/documentation/api/latest/reference/services/batch/client/list_jobs.html
        async with self._batch_client() as client:
            total_jobs = []
            async for page in client.get_paginator("list_jobs").paginate(jobQueue=job_queue, jobStatus=job_status):
                for job in page["jobSummaryList"]:
                    total_jobs.append(job)

            return total_jobs

    async def submit_job(
        self,
        job_name: str,
        job_queue: str,
        job_definition: str,
        params: dict,
        job_priority: int | None = None,
        share_identifier: str | None = None,
        container_overrides: dict | None = None,
        depends_on_ids: list[str] | None = None,
    ) -> str | None:
        container_overrides = container_overrides or {}
        depends_on = [{"jobId": job_id} for job_id in depends_on_ids or []]
        async with self._batch_client() as client:
            if job_priority is None or share_identifier is None:
                response = await client.submit_job(
                    jobName=job_name,
                    jobQueue=job_queue,
                    jobDefinition=job_definition,
                    parameters=params,
                    containerOverrides=container_overrides,
                    dependsOn=depends_on,
                )
                return response.get("jobId")
            else:
                response = await client.submit_job(
                    jobName=job_name,
                    jobQueue=job_queue,
                    jobDefinition=job_definition,
                    parameters=params,
                    schedulingPriorityOverride=job_priority,
                    shareIdentifier=share_identifier,
                    containerOverrides=container_overrides,
                    dependsOn=depends_on,
                )
                return response.get("jobId")


class S3Uri:
    # From: https://stackoverflow.com/questions/42641315/s3-urls-get-bucket-name-and-path
    """
    >>> s = S3Uri("s3://bucket/hello/world")
    >>> s.bucket
    'bucket'
    >>> s.key
    'hello/world'
    >>> s.uri
    's3://bucket/hello/world'

    >>> s = S3Uri("s3://bucket/hello/world?qwe1=3#ddd")
    >>> s.bucket
    'bucket'
    >>> s.key
    'hello/world?qwe1=3#ddd'
    >>> s.uri
    's3://bucket/hello/world?qwe1=3#ddd'

    >>> s = S3Uri("s3://bucket/hello/world#foo?bar=2")
    >>> s.key
    'hello/world#foo?bar=2'
    >>> s.uri
    's3://bucket/hello/world#foo?bar=2'
    """

    def __init__(self, uri: str) -> None:
        self._parsed = urlparse(uri, allow_fragments=False)

    @property
    def bucket(self) -> str:
        return self._parsed.netloc

    @property
    def key(self) -> str:
        if self._parsed.query:
            return self._parsed.path.lstrip("/") + "?" + self._parsed.query
        else:
            return self._parsed.path.lstrip("/")

    @property
    def uri(self) -> str:
        return self._parsed.geturl()

    def __str__(self) -> str:
        return self.uri


def tag_set_to_dict(tag_set: list[dict[str, str]]) -> dict[str, str]:
    """Convert a list of tags to a dictionary."""
    return {tag["Key"]: tag["Value"] for tag in tag_set}


_aws_client: AsyncAWSClient | None = None
_aws_client_created_at: float = 0.0
_AWS_CLIENT_TTL_SECONDS: float = _SESSION_TTL_SECONDS


def get_aws_client() -> AsyncAWSClient:
    global _aws_client, _aws_client_created_at
    now = time.monotonic()
    if _aws_client is None or (now - _aws_client_created_at) > _AWS_CLIENT_TTL_SECONDS:
        if _aws_client is not None:
            LOG.info("Recreating AWS client (TTL expired)", ttl_seconds=_AWS_CLIENT_TTL_SECONDS)
        _aws_client = AsyncAWSClient()
        _aws_client_created_at = now
    return _aws_client
