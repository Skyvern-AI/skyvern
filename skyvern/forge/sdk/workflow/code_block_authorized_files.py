"""Run-scoped file capabilities shared by inline and secure CodeBlock execution."""

from __future__ import annotations

import asyncio
import hashlib
import mimetypes
import os
import stat
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass
from functools import partial
from pathlib import Path, PurePath
from typing import Protocol

AUTHORIZED_FILE_UNAVAILABLE_ERROR = (
    "The declared file input could not be materialized. Check that the file_url is available and retry."
)
AUTHORIZED_FILE_CHANGED_ERROR = "The authorized file is unavailable or changed. Materialize the file again."


class AuthorizedFileAccessError(Exception):
    pass


class FileInputTarget(Protocol):
    def locator(self, selector: str) -> FileInputLocator: ...


class FileInputLocator(Protocol):
    async def set_input_files(self, files: dict[str, str | bytes]) -> None: ...


@dataclass(frozen=True, slots=True)
class MaterializedAuthorizedFile:
    workflow_run_id: str
    organization_id: str | None
    relative_path: str
    filename: str
    device: int
    inode: int
    size: int
    mtime_ns: int
    ctime_ns: int
    sha256: str


@dataclass(frozen=True, slots=True)
class AuthorizedFileMaterializationFailure:
    pass


AuthorizedFileMaterialization = MaterializedAuthorizedFile | AuthorizedFileMaterializationFailure


@dataclass(frozen=True, slots=True)
class AuthorizedFileBytes:
    filename: str
    content: bytes

    @property
    def size(self) -> int:
        return len(self.content)


def _run_directory(download_root: str | Path, workflow_run_id: str) -> Path:
    return Path(download_root) / workflow_run_id


def _relative_components(relative_path: str) -> tuple[str, ...]:
    candidate = PurePath(relative_path)
    if candidate.is_absolute() or not candidate.parts or any(part in {"", ".", ".."} for part in candidate.parts):
        raise AuthorizedFileAccessError(AUTHORIZED_FILE_CHANGED_ERROR)
    return candidate.parts


def _open_beneath_run_directory(download_root: str | Path, workflow_run_id: str, relative_path: str) -> int:
    # O_NOFOLLOW guards each component beneath the anchor; the anchor's own parents are trusted
    # because DOWNLOAD_PATH is operator-configured, not a world-writable prefix.
    components = _relative_components(relative_path)
    directory_flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
    directory_fd = os.open(_run_directory(download_root, workflow_run_id), directory_flags)
    try:
        for component in components[:-1]:
            child_fd = os.open(component, directory_flags, dir_fd=directory_fd)
            os.close(directory_fd)
            directory_fd = child_fd
        return os.open(components[-1], os.O_RDONLY | os.O_NOFOLLOW, dir_fd=directory_fd)
    finally:
        os.close(directory_fd)


def _digest_fd(file_fd: int, *, collect: bool, max_bytes: int | None = None) -> tuple[str, bytes]:
    digest = hashlib.sha256()
    content = bytearray()
    while True:
        chunk = os.read(file_fd, 1024 * 1024)
        if not chunk:
            break
        digest.update(chunk)
        if collect:
            if max_bytes is not None and len(content) + len(chunk) > max_bytes:
                raise AuthorizedFileAccessError(f"The authorized file exceeds the {max_bytes}-byte upload limit.")
            content.extend(chunk)
    return digest.hexdigest(), bytes(content)


def _changed_during_read(before: os.stat_result, after: os.stat_result) -> bool:
    return (
        before.st_dev,
        before.st_ino,
        before.st_size,
        before.st_mtime_ns,
        before.st_ctime_ns,
    ) != (
        after.st_dev,
        after.st_ino,
        after.st_size,
        after.st_mtime_ns,
        after.st_ctime_ns,
    )


def capture_authorized_file(
    path: str | Path,
    *,
    download_root: str | Path,
    workflow_run_id: str,
    organization_id: str | None,
    max_bytes: int,
    filename: str | None = None,
) -> MaterializedAuthorizedFile:
    try:
        run_directory = _run_directory(download_root, workflow_run_id).resolve()
        resolved_path = Path(path).resolve()
        relative_path = str(resolved_path.relative_to(run_directory))
        file_fd = _open_beneath_run_directory(download_root, workflow_run_id, relative_path)
    except (OSError, ValueError, AuthorizedFileAccessError):
        raise AuthorizedFileAccessError(AUTHORIZED_FILE_UNAVAILABLE_ERROR) from None
    try:
        before = os.fstat(file_fd)
        if not stat.S_ISREG(before.st_mode):
            raise AuthorizedFileAccessError(AUTHORIZED_FILE_UNAVAILABLE_ERROR)
        # An oversized file stays materialized for code that opens it by path. It is not hashed:
        # redemption refuses anything above the ceiling before it compares digests.
        digest = ""
        if before.st_size <= max_bytes:
            digest, _ = _digest_fd(file_fd, collect=False)
            if _changed_during_read(before, os.fstat(file_fd)):
                raise AuthorizedFileAccessError(AUTHORIZED_FILE_UNAVAILABLE_ERROR)
        return MaterializedAuthorizedFile(
            workflow_run_id=workflow_run_id,
            organization_id=organization_id,
            relative_path=relative_path,
            filename=filename or resolved_path.name,
            device=before.st_dev,
            inode=before.st_ino,
            size=before.st_size,
            mtime_ns=before.st_mtime_ns,
            ctime_ns=before.st_ctime_ns,
            sha256=digest,
        )
    finally:
        os.close(file_fd)


def inline_authorized_file_path(
    authorized_file: MaterializedAuthorizedFile,
    *,
    download_root: str | Path,
) -> str:
    return str(_run_directory(download_root, authorized_file.workflow_run_id).resolve() / authorized_file.relative_path)


def read_authorized_file(
    authorized_file: MaterializedAuthorizedFile,
    *,
    download_root: str | Path,
    workflow_run_id: str,
    organization_id: str | None,
    max_bytes: int,
) -> AuthorizedFileBytes:
    if (
        authorized_file.workflow_run_id != workflow_run_id
        or authorized_file.organization_id != organization_id
        or not authorized_file.filename
        # The upload name may differ from the stored one after a collision, but it is sent as a name, never a path.
        or PurePath(authorized_file.filename).name != authorized_file.filename
    ):
        raise AuthorizedFileAccessError("The authorized file capability is not valid for this run.")
    try:
        file_fd = _open_beneath_run_directory(download_root, workflow_run_id, authorized_file.relative_path)
    except (OSError, AuthorizedFileAccessError):
        raise AuthorizedFileAccessError(AUTHORIZED_FILE_CHANGED_ERROR) from None
    try:
        before = os.fstat(file_fd)
        expected_identity = (
            authorized_file.device,
            authorized_file.inode,
            authorized_file.size,
            authorized_file.mtime_ns,
            authorized_file.ctime_ns,
        )
        actual_identity = (before.st_dev, before.st_ino, before.st_size, before.st_mtime_ns, before.st_ctime_ns)
        if not stat.S_ISREG(before.st_mode) or actual_identity != expected_identity:
            raise AuthorizedFileAccessError(AUTHORIZED_FILE_CHANGED_ERROR)
        if before.st_size > max_bytes:
            raise AuthorizedFileAccessError(f"The authorized file exceeds the {max_bytes}-byte upload limit.")
        digest, content = _digest_fd(file_fd, collect=True, max_bytes=max_bytes)
        after = os.fstat(file_fd)
        if _changed_during_read(before, after) or digest != authorized_file.sha256:
            raise AuthorizedFileAccessError(AUTHORIZED_FILE_CHANGED_ERROR)
        return AuthorizedFileBytes(filename=authorized_file.filename, content=content)
    finally:
        os.close(file_fd)


async def attach_materialized_authorized_file(
    locate: Callable[[str], FileInputLocator],
    authorized_file: MaterializedAuthorizedFile,
    selector: str,
    *,
    download_root: str | Path,
    workflow_run_id: str,
    organization_id: str | None,
    max_bytes: int,
) -> dict[str, str | int]:
    # `locate` must be captured before authored code runs: inline code can reassign `page.locator`,
    # and a target resolved through the live page would hand the verified bytes to that object.
    if not isinstance(selector, str) or not selector:
        raise AuthorizedFileAccessError(
            "attach_authorized_file parameter 'selector' must be a non-empty selector string."
        )
    target = locate(selector)
    verified = await asyncio.to_thread(
        read_authorized_file,
        authorized_file,
        download_root=download_root,
        workflow_run_id=workflow_run_id,
        organization_id=organization_id,
        max_bytes=max_bytes,
    )
    mime_type = mimetypes.guess_type(verified.filename)[0] or "application/octet-stream"
    await target.set_input_files({"name": verified.filename, "mimeType": mime_type, "buffer": verified.content})
    return {"filename": verified.filename, "size": verified.size}


def bind_inline_attach_authorized_file(
    expected_page: FileInputTarget,
    materializations: Mapping[str, AuthorizedFileMaterialization],
    unmaterialized_values: Mapping[str, object] | None = None,
    *,
    download_root: str | Path,
    workflow_run_id: str,
    organization_id: str | None,
    max_bytes: int,
    locate: Callable[[str], FileInputLocator] | None = None,
) -> Callable[[FileInputTarget, str, str], Awaitable[dict[str, str | int]]]:
    authorized_by_path = {
        inline_authorized_file_path(item, download_root=download_root): item
        for item in materializations.values()
        if isinstance(item, MaterializedAuthorizedFile)
    }
    failed_values = {
        value
        for key, value in (unmaterialized_values or {}).items()
        if isinstance(value, str) and isinstance(materializations.get(key), AuthorizedFileMaterializationFailure)
    }
    # Fixed before authored code runs and looked up on the class, so shadowing `locator` on a page object it can reach
    # cannot redirect the bytes. A page that wraps another passes `locate` pinned the same way; not every page uploads.
    if locate is None and (page_locator := getattr(type(expected_page), "locator", None)) is not None:
        locate = partial(page_locator, expected_page)

    async def attach_authorized_file(
        page: FileInputTarget,
        file: str,
        selector: str,
    ) -> dict[str, str | int]:
        if page is not expected_page:
            raise AuthorizedFileAccessError("attach_authorized_file requires the current CodeBlock page.")
        authorized_file = authorized_by_path.get(file) if isinstance(file, str) else None
        if authorized_file is None:
            if isinstance(file, str) and file in failed_values:
                raise AuthorizedFileAccessError(AUTHORIZED_FILE_UNAVAILABLE_ERROR)
            raise AuthorizedFileAccessError(
                "attach_authorized_file accepts only a materialized file_url parameter from this run."
            )
        if locate is None:
            raise AuthorizedFileAccessError("attach_authorized_file requires the current CodeBlock page.")
        return await attach_materialized_authorized_file(
            locate,
            authorized_file,
            selector,
            download_root=download_root,
            workflow_run_id=workflow_run_id,
            organization_id=organization_id,
            max_bytes=max_bytes,
        )

    return attach_authorized_file


async def unbound_attach_authorized_file(
    page: FileInputTarget,
    file: str,
    selector: str,
) -> dict[str, str | int]:
    raise AuthorizedFileAccessError(
        "attach_authorized_file is only available for a materialized file_url parameter during a workflow run."
    )
