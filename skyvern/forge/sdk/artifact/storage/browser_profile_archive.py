"""Zip and unzip saved browser profiles, leaving out state Chrome rebuilds on every launch."""

from __future__ import annotations

import asyncio
import errno
import io
import os
import shutil
import stat
import tempfile
import zipfile
from collections.abc import Awaitable, Callable, Iterator
from contextlib import contextmanager, suppress
from pathlib import Path, PurePosixPath, PureWindowsPath
from typing import Any, Protocol, TypedDict, TypeVar

import structlog
from aiohttp import ClientConnectionError, ClientSSLError
from azure.core.exceptions import ServiceRequestError, ServiceResponseError
from botocore.exceptions import ClientError, ConnectTimeoutError, EndpointConnectionError, HTTPClientError
from google.api_core.exceptions import RetryError
from google.auth.exceptions import TransportError
from requests.exceptions import ConnectionError as RequestsConnectionError
from requests.exceptions import ProxyError, SSLError, Timeout
from urllib3.exceptions import ConnectTimeoutError as Urllib3ConnectTimeoutError
from urllib3.exceptions import ReadTimeoutError

from skyvern.config import settings
from skyvern.forge.sdk.api.files import make_run_temp_directory
from skyvern.forge.sdk.artifact.storage.base import BrowserProfileSnapshot
from skyvern.forge.sdk.artifact.storage.unique_pdfs import settle
from skyvern.forge.sdk.encrypt.files import decrypt_file, encrypt_file

LOG = structlog.get_logger()
SEALED_PROFILE_SUFFIX = ".zip.enc"
PROFILE_ENCRYPTION_PURPOSE = "browser-profile"
WORKFLOW_SESSION_ENCRYPTION_PURPOSE = "workflow-browser-session"
TRANSIENT_PROFILE_FILES = {"RunningChromeVersion", "SingletonLock", "SingletonSocket", "SingletonCookie"}

_T = TypeVar("_T")
_LEGACY_ZIP_HEADERS = (b"PK\x03\x04", b"PK\x05\x06")
_SEAL_STATUS_TIMEOUT_SECONDS = 5
_WRITER_COMMENTS = {
    writer: f"skyvern:browser-profile-writer:v1:{writer}".encode("ascii")
    for writer in ("chrome", "msedge", "stealth-chromium")
}
_COMMENT_WRITERS = {comment: writer for writer, comment in _WRITER_COMMENTS.items()}


class ProfileWriterOptions(TypedDict, total=False):
    source_browser_type: str


def profile_writer_options(source_browser_type: str | None) -> ProfileWriterOptions:
    if isinstance(source_browser_type, str) and source_browser_type in _WRITER_COMMENTS:
        return {"source_browser_type": source_browser_type}
    return {}


class ProfilePublicationConflict(RuntimeError):
    """The legacy upload finished and a competing sealed object may own the profile."""


class ProfilePublicationUnconfirmed(ProfilePublicationConflict):
    """The legacy upload finished, but the winning representation could not be established."""


def should_encrypt_profile(*, protected_input: bool = False, protected_destination: bool = False) -> bool:
    return settings.SKYVERN_ENCRYPT_BROWSER_PROFILES or protected_input or protected_destination


# Chrome indexes each loaded extension's declarativeNetRequest rulesets into <profile dir>/DNR Extension Rules/<id>.
# An unpacked extension gets a new id whenever its load path changes and Chrome never prunes the old ones, so a
# profile that round-trips them grows without bound.
_REGENERATED_DIR_NAME = "DNR Extension Rules"


def is_regenerated_profile_path(relative_path: str) -> bool:
    return _REGENERATED_DIR_NAME in relative_path.replace("\\", "/").split("/")[:2]


def profile_archive_uris(base: str, organization_id: str, profile_id: str) -> tuple[str, str]:
    prefix = f"{base}/{organization_id}/profiles/{profile_id}"
    return f"{prefix}.zip", f"{prefix}{SEALED_PROFILE_SUFFIX}"


def workflow_session_archive_uris(base: str, organization_id: str, storage_key: str) -> tuple[str, str]:
    prefix = f"{base}/{organization_id}/{storage_key}"
    return f"{prefix}.zip", f"{prefix}{SEALED_PROFILE_SUFFIX}"


@contextmanager
def profile_archive_staging_dir() -> Iterator[Path]:
    directory = Path(make_run_temp_directory(prefix="skyvern_profile_archive_"))
    try:
        yield directory
    finally:
        with suppress(FileNotFoundError):
            shutil.rmtree(directory)


async def settle_profile_io(work: Awaitable[_T]) -> _T:
    """Keep the archive and its input alive until a path-owning worker or upload actually finishes."""
    worker = asyncio.ensure_future(work)
    try:
        return await asyncio.shield(worker)
    except asyncio.CancelledError as exc:
        await settle(worker)
        if not worker.cancelled() and isinstance(error := worker.exception(), ProfilePublicationConflict):
            raise exc from error
        raise


async def run_profile_io(operation: Callable[..., _T], *args: Any, **kwargs: Any) -> _T:
    return await settle_profile_io(asyncio.to_thread(operation, *args, **kwargs))


def _tree_size(directory: str) -> tuple[int, int]:
    file_count = total_bytes = 0
    for dirpath, _, filenames in os.walk(directory):
        for name in filenames:
            try:
                total_bytes += os.path.getsize(os.path.join(dirpath, name))
            except OSError:
                continue
            file_count += 1
    return file_count, total_bytes


def write_profile_archive(
    directory: str,
    zip_file_path: str,
    *,
    strict_traversal: bool | None = False,
    source_browser_type: str | None = None,
    **log_context: Any,
) -> int:
    if not Path(directory).is_dir():
        raise NotADirectoryError(directory)

    writer_comment = _WRITER_COMMENTS.get(source_browser_type or "")
    complete = True

    def traversal_error(error: OSError) -> None:
        nonlocal complete
        if strict_traversal is not False:
            raise error
        complete = False

    archived_files = archived_bytes = excluded_files = excluded_bytes = 0
    with zipfile.ZipFile(zip_file_path, "w", zipfile.ZIP_DEFLATED) as archive:
        for dirpath, dirnames, filenames in os.walk(
            directory, onerror=traversal_error if strict_traversal is not False or writer_comment else None
        ):
            relative_dir = os.path.relpath(dirpath, directory)
            kept_dirnames = []
            for name in sorted(dirnames):
                relative_path = os.path.normpath(os.path.join(relative_dir, name))
                if is_regenerated_profile_path(relative_path):
                    skipped_files, skipped_bytes = _tree_size(os.path.join(dirpath, name))
                    excluded_files += skipped_files
                    excluded_bytes += skipped_bytes
                    continue
                archive.write(os.path.join(dirpath, name), relative_path)
                kept_dirnames.append(name)
            dirnames[:] = kept_dirnames
            for name in filenames:
                if strict_traversal is None and name in TRANSIENT_PROFILE_FILES:
                    continue
                path = os.path.join(dirpath, name)
                if strict_traversal is not False or writer_comment:
                    try:
                        mode = os.stat(path, follow_symlinks=False).st_mode
                    except OSError as exc:
                        if strict_traversal is False or (
                            strict_traversal is None and isinstance(exc, FileNotFoundError)
                        ):
                            complete = False
                            continue
                        raise
                    if stat.S_ISDIR(mode):
                        # os.walk treats an is_dir() error as a file entry.
                        if strict_traversal is True:
                            raise IsADirectoryError(errno.EISDIR, "Browser profile directory was not traversed", path)
                        complete = False
                        continue
                    is_regular_file = stat.S_ISREG(mode)
                else:
                    is_regular_file = os.path.isfile(path) and not os.path.islink(path)
                if is_regular_file:
                    archive.write(path, os.path.normpath(os.path.join(relative_dir, name)))
                    archived_files += 1
                    archived_bytes += archive.filelist[-1].file_size
        if writer_comment and complete and archived_files:
            archive.comment = writer_comment
    LOG.info(
        "Archived browser profile",
        archived_files=archived_files,
        archived_bytes=archived_bytes,
        excluded_files=excluded_files,
        excluded_bytes=excluded_bytes,
        zip_bytes=os.path.getsize(zip_file_path),
        **log_context,
    )
    return archived_files


def stage_profile_archive(
    directory: str,
    staging_dir: Path,
    organization_id: str,
    profile_id: str,
    *,
    encrypted: bool,
    sealed_dir: Path | None = None,
    strict_traversal: bool | None = None,
    confirm_creation: bool = False,
    purpose: str = PROFILE_ENCRYPTION_PURPOSE,
    source_browser_type: str | None = None,
) -> Path:
    zip_path = staging_dir / "profile.zip"
    if encrypted and strict_traversal is False:
        strict_traversal = None  # Keep the live-profile traversal policy for encrypted saves.
    archived_files = write_profile_archive(
        directory,
        str(zip_path),
        strict_traversal=True if confirm_creation else strict_traversal,
        organization_id=organization_id,
        profile_id=profile_id,
        **profile_writer_options(source_browser_type),
    )
    if confirm_creation and not archived_files:
        raise ValueError("Cannot create a credential profile from an empty capture")
    if not encrypted:
        return zip_path
    sealed_path = (sealed_dir or staging_dir) / f"profile{SEALED_PROFILE_SUFFIX}"
    encrypt_file(zip_path, sealed_path, organization_id=organization_id, purpose=purpose, object_id=profile_id)
    zip_path.unlink()
    return sealed_path


def extract_profile_zip(archive: zipfile.ZipFile, destination: str) -> None:
    members = archive.infolist()
    for member in members:
        name = member.filename.replace("\\", "/")
        path = PurePosixPath(name)
        if path.is_absolute() or ".." in path.parts or PureWindowsPath(name).drive:
            raise ValueError("Zip slip detected in browser profile archive")
    archive.extractall(destination, [info for info in members if not is_regenerated_profile_path(info.filename)])


def _extract_zip(source: bytes | Path, destination: str, **log_context: Any) -> str | None:
    with zipfile.ZipFile(io.BytesIO(source) if isinstance(source, bytes) else source) as archive:
        extract_profile_zip(archive, destination)
        files = [info for info in archive.infolist() if not info.is_dir()]
        source_browser_type = _COMMENT_WRITERS.get(archive.comment)
    skipped = [info for info in files if is_regenerated_profile_path(info.filename)]
    skipped_bytes = sum(info.file_size for info in skipped)
    LOG.info(
        "Retrieved browser profile archive",
        zip_bytes=len(source) if isinstance(source, bytes) else source.stat().st_size,
        extracted_files=len(files) - len(skipped),
        extracted_bytes=sum(info.file_size for info in files) - skipped_bytes,
        skipped_files=len(skipped),
        skipped_bytes=skipped_bytes,
        **log_context,
    )
    return source_browser_type if len(files) > len(skipped) else None


def _extract_profile(
    source: bytes | Path, destination: str, organization_id: str, profile_id: str, *, encrypted: bool, purpose: str
) -> str | None:
    if not encrypted:
        if isinstance(source, bytes) and not source.startswith(_LEGACY_ZIP_HEADERS):
            raise zipfile.BadZipFile("Invalid browser profile archive header")
        return _extract_zip(source, destination, organization_id=organization_id, profile_id=profile_id)

    with profile_archive_staging_dir() as temp_dir:
        if isinstance(source, bytes):
            encrypted_path = temp_dir / "profile.enc"
            encrypted_path.write_bytes(source)
            source = encrypted_path
        zip_path = temp_dir / "profile.zip"
        decrypt_file(
            source,
            zip_path,
            organization_id=organization_id,
            purpose=purpose,
            object_id=profile_id,
        )
        return _extract_zip(zip_path, destination, organization_id=organization_id, profile_id=profile_id)


async def restore_profile_archive(
    source: bytes | Path,
    organization_id: str,
    profile_id: str,
    *,
    encrypted: bool,
    parent_dir: str | None = None,
    purpose: str = PROFILE_ENCRYPTION_PURPOSE,
    version: str | None = None,
) -> BrowserProfileSnapshot:
    prefix = "skyvern_browser_profile_"
    destination = (
        make_run_temp_directory(prefix=prefix)
        if parent_dir is None
        else tempfile.mkdtemp(prefix=prefix, dir=parent_dir)
    )
    try:
        source_browser_type = await run_profile_io(
            _extract_profile, source, destination, organization_id, profile_id, encrypted=encrypted, purpose=purpose
        )
    except BaseException:
        shutil.rmtree(destination, ignore_errors=True)
        raise
    return BrowserProfileSnapshot(destination, encrypted, version=version, source_browser_type=source_browser_type)


async def find_profile_object(
    uri: str, sealed_uri: str, read: Callable[[str], Awaitable[_T | None]]
) -> tuple[_T | None, bool]:
    sealed = await read(sealed_uri)
    if sealed is not None:
        return sealed, True
    legacy = await read(uri)
    # A writer can publish a seal while the legacy read is in flight.
    sealed = await read(sealed_uri)
    if sealed is not None or legacy is None:
        return sealed, True
    return legacy, False


class ProfileDownload(Protocol):
    def __call__(
        self, uri: str, *, log_exception: bool, raise_on_error: bool = False, on_version: Callable[[str], None]
    ) -> Awaitable[bytes | None]: ...


async def retrieve_remote_profile(
    uris: tuple[str, str],
    organization_id: str,
    profile_id: str,
    *,
    download: ProfileDownload,
    stat: Callable[[str], Awaitable[object | None]],
    parent_dir: str | None = None,
    purpose: str = PROFILE_ENCRYPTION_PURPOSE,
    allow_sealed_fallback: bool = False,
) -> BrowserProfileSnapshot | None:
    uri, sealed_uri = uris

    async def read(object_uri: str) -> tuple[bytes, str | None] | None:
        versions: list[str] = []
        try:
            data = await download(
                object_uri,
                log_exception=object_uri == uri,
                raise_on_error=object_uri == sealed_uri,
                on_version=versions.append,
            )
        except Exception as exc:
            if object_uri == uri or not _transient_profile_error(exc):
                raise
            # Each of the two sealed observations has one bounded confirmation (10s total).
            if await _confirm_seal_status(stat, sealed_uri) is not None:
                raise
            return None
        return (data, versions[-1] if versions else None) if data is not None else None

    async def restore(selected: tuple[bytes, str | None], *, encrypted: bool) -> BrowserProfileSnapshot:
        data, version = selected
        return await restore_profile_archive(
            data,
            organization_id,
            profile_id,
            encrypted=encrypted,
            parent_dir=parent_dir,
            purpose=purpose,
            version=version,
        )

    if allow_sealed_fallback:
        sealed_error_type: str | None = None

        async def restore_sealed() -> BrowserProfileSnapshot | None:
            nonlocal sealed_error_type
            try:
                sealed = await read(sealed_uri)
                return await restore(sealed, encrypted=True) if sealed is not None else None
            except Exception as exc:
                sealed_error_type = type(exc).__name__
                return None

        if (snapshot := await restore_sealed()) is not None:
            return snapshot
        legacy = await read(uri)
        # A usable seal published during the legacy read still takes precedence.
        if (snapshot := await restore_sealed()) is not None:
            return snapshot
        if sealed_error_type is not None:
            LOG.warning("Falling back to legacy workflow profile ZIP", error_type=sealed_error_type)
        return await restore(legacy, encrypted=False) if legacy is not None else None

    selected, encrypted = await find_profile_object(uri, sealed_uri, read)
    return await restore(selected, encrypted=encrypted) if selected is not None else None


def _transient_profile_error(error: Exception) -> bool:
    if isinstance(error, (RetryError, TransportError)):
        cause = error.cause if isinstance(error, RetryError) else error.__cause__
        return isinstance(cause, Exception) and cause is not error and _transient_profile_error(cause)
    if isinstance(error, (SSLError, ProxyError, ClientSSLError)):
        return False
    if isinstance(
        error,
        (
            TimeoutError,
            ConnectionError,
            ClientConnectionError,
            HTTPClientError,
            EndpointConnectionError,
            ConnectTimeoutError,
            ServiceRequestError,
            ServiceResponseError,
            RequestsConnectionError,
            Timeout,
            Urllib3ConnectTimeoutError,
            ReadTimeoutError,
        ),
    ):
        return True
    if isinstance(error, ClientError):
        code = error.response.get("Error", {}).get("Code")
        if code in {"AccessDenied", "Forbidden", "401", "403"}:
            return False
        if code in {"SlowDown", "RequestTimeout", "RequestTimeoutException", "Throttling", "ThrottlingException"}:
            return True
        status = error.response.get("ResponseMetadata", {}).get("HTTPStatusCode")
    else:
        status = getattr(error, "status_code", None) or getattr(error, "status", None) or getattr(error, "code", None)
    return isinstance(status, int) and (status in {408, 429} or 500 <= status < 600)


async def _confirm_seal_status(stat: Callable[[str], Awaitable[object | None]], uri: str) -> object | None:
    async with asyncio.timeout(_SEAL_STATUS_TIMEOUT_SECONDS):
        return await stat(uri)


async def store_remote_profile(
    uris: tuple[str, str],
    organization_id: str,
    profile_id: str,
    directory: str,
    *,
    stat: Callable[[str], Awaitable[object | None]],
    upload: Callable[[str, str], Awaitable[None]],
    delete: Callable[[str], Awaitable[None]],
    require_encryption: bool = False,
    confirm_creation: bool = False,
    strict_traversal: bool = False,
    purpose: str = PROFILE_ENCRYPTION_PURPOSE,
    source_browser_type: str | None = None,
) -> None:
    uri, sealed_uri = uris
    encrypted = should_encrypt_profile(protected_input=require_encryption) or should_encrypt_profile(
        protected_destination=await stat(sealed_uri) is not None
    )
    with profile_archive_staging_dir() as temp_dir:
        path = await run_profile_io(
            stage_profile_archive,
            directory,
            temp_dir,
            organization_id,
            profile_id,
            encrypted=encrypted,
            confirm_creation=confirm_creation,
            strict_traversal=strict_traversal,
            purpose=purpose,
            **profile_writer_options(source_browser_type),
        )

        async def publish() -> None:
            await upload(sealed_uri if encrypted else uri, str(path))
            if encrypted:
                await delete(uri)
                return
            try:
                try:
                    sealed = await stat(sealed_uri)
                except Exception as exc:
                    if not _transient_profile_error(exc):
                        raise
                    sealed = await _confirm_seal_status(stat, sealed_uri)
            except Exception as exc:
                error = ProfilePublicationUnconfirmed if _transient_profile_error(exc) else ProfilePublicationConflict
                raise error("Could not confirm absence of a competing sealed profile") from exc
            if sealed is not None:
                await discard_plaintext()
                raise ProfilePublicationConflict("Profile was encrypted by another writer during save")

        async def discard_plaintext() -> None:
            try:
                await delete(uri)
            except Exception:
                LOG.warning(
                    "Could not remove legacy profile after a conflicting upload", profile_id=profile_id, exc_info=True
                )

        await settle_profile_io(publish())
