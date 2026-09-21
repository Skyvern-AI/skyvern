"""Run-scoped file capabilities shared by inline and secure CodeBlock execution."""

from __future__ import annotations

import asyncio
import hashlib
import mimetypes
import os
import re
import shutil
import stat
import tempfile
from collections.abc import Awaitable, Callable, Mapping
from contextlib import suppress
from dataclasses import dataclass
from functools import partial
from pathlib import Path, PurePath
from types import SimpleNamespace
from typing import Any, Protocol, cast

import structlog
from playwright.async_api import BrowserContext

from skyvern.constants import SAVE_DOWNLOADED_FILES_TIMEOUT
from skyvern.forge import app
from skyvern.forge.sdk.artifact.models import ArtifactType
from skyvern.forge.sdk.core.hashing import diagnostic_fingerprint
from skyvern.forge.sdk.schemas.files import FileInfo
from skyvern.webeye.browser_factory import resolve_artifact_path
from skyvern.webeye.browser_object_predicates import DownloadLike, is_download_like
from skyvern.webeye.cdp_download_interceptor import (
    is_monitoring_browser_downloads_for_context,
    monitor_saved_download_name,
    normalize_download_filename,
    settle_browser_downloads_for_context,
)

AUTHORIZED_FILE_UNAVAILABLE_ERROR = (
    "The declared file input could not be materialized. Check that the file_url is available and retry."
)
AUTHORIZED_FILE_CHANGED_ERROR = "The authorized file is unavailable or changed. Materialize the file again."
AUTHORIZED_FILE_SHAPE_ERROR = (
    "attach_authorized_file accepts only a materialized file_url parameter or a download this run claimed."
)

LOG = structlog.get_logger()

DownloadEvidenceProbe = Callable[[], Awaitable[tuple[list[FileInfo] | None, set[str]]]]
RegisteredDownloadIdentity = tuple[str | None, str | None]
_REGISTERED_DOWNLOAD_POLL_SECONDS = 2.0
# Each probe re-runs download registration, so one probe and the whole wait both stay well inside the 180s
# settle window.
_REGISTERED_DOWNLOAD_PROBE_TIMEOUT_SECONDS = 10
_REGISTERED_DOWNLOAD_WAIT_SECONDS = 30


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
    run_directory = _run_directory(download_root, workflow_run_id)
    resolved_path = Path(path)

    def refuse(cause: str, error: BaseException | None = None) -> AuthorizedFileAccessError:
        LOG.info(
            "codeblock.authorized_file_capture_refused",
            cause=cause,
            error_type=type(error).__name__ if error is not None else None,
            errno=error.errno if isinstance(error, OSError) else None,
            run_directory=str(run_directory),
            candidate_directory=str(resolved_path.parent),
            candidate_name_fp=diagnostic_fingerprint(resolved_path.name),
            workflow_run_id=workflow_run_id,
        )
        return AuthorizedFileAccessError(AUTHORIZED_FILE_UNAVAILABLE_ERROR)

    try:
        run_directory = run_directory.resolve()
        resolved_path = resolved_path.resolve()
    except OSError as error:
        raise refuse("resolve_failed", error) from None
    try:
        relative_path = str(resolved_path.relative_to(run_directory))
    except ValueError as error:
        raise refuse("outside_run_directory", error) from None
    try:
        file_fd = _open_beneath_run_directory(download_root, workflow_run_id, relative_path)
    except OSError as error:
        raise refuse("open_failed", error) from None
    except AuthorizedFileAccessError as error:
        raise refuse("unsafe_relative_path", error) from None
    try:
        before = os.fstat(file_fd)
        if not stat.S_ISREG(before.st_mode):
            raise refuse("not_regular_file")
        # An oversized file stays materialized for code that opens it by path. It is not hashed:
        # redemption refuses anything above the ceiling before it compares digests.
        digest = ""
        if before.st_size <= max_bytes:
            digest, _ = _digest_fd(file_fd, collect=False)
            if _changed_during_read(before, os.fstat(file_fd)):
                raise refuse("changed_during_read")
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


def _download_disk_name(download: DownloadLike) -> str:
    # Chrome writes the suggested name literally, so a real "%20" stays "%20" on disk; escaping "%"
    # keeps the upload-name sanitizer's URL-decoding out of the disk match.
    return normalize_download_filename((download.suggested_filename or "").replace("%", "%25"))


def _download_name_spellings(download: DownloadLike) -> tuple[str, ...]:
    """Every name a writer could have saved this download under: Chrome keeps the escaping the site sent,
    the download monitor decodes it, so two downloads can differ by spelling alone and share one file."""
    literal = _download_disk_name(download)
    decoded = monitor_saved_download_name(download.suggested_filename or "")
    return (literal,) if decoded in {"", literal} else (literal, decoded)


class BlockDownloadLog:
    """Every download a block's pages start, in start order, and which of them have finished.

    Under the run-scoped binding Chrome overwrites a same-name download in place, and the file ends up
    holding whichever finished last. Two same-name downloads in flight together therefore leave a file no
    claim can prove is its own; one started after another has finished simply replaces it.
    """

    def __init__(self, context: Any) -> None:
        self._context = context
        self._pages: list[Any] = []
        self._downloads: list[DownloadLike] = []
        self._finished: set[int] = set()
        self._overlapped: set[int] = set()
        self._completions: list[asyncio.Future[Any]] = []
        self._closed = False
        self._original_paths: dict[int, Callable[[], Awaitable[Any]]] = {}

        # Closures, not bound methods: Playwright tags each handler with an attribute, which a bound
        # method refuses, and removal needs the same object that was registered.
        def record(download: DownloadLike) -> None:
            name = _download_disk_name(download)
            for earlier in self._downloads:
                if id(earlier) not in self._finished and _download_disk_name(earlier) == name:
                    self._overlapped.update((id(earlier), id(download)))
            # Taken as the browser emits the object, before authored code can reach it and rebind path().
            self._original_paths[id(download)] = download.path
            self._downloads.append(download)
            with suppress(Exception):
                completion = asyncio.get_running_loop().create_task(self._until_finished(download))
                completion.add_done_callback(partial(self._settle, id(download)))
                self._completions.append(completion)

        def watch(page: Any) -> None:
            page.on("download", record)
            self._pages.append(page)

        self._record = record
        self._watch = watch
        # A context that cannot be observed leaves the log empty, so every claimed download reads as
        # superseded and the attach refuses rather than guessing which bytes are its own.
        with suppress(Exception):
            context.on("page", watch)
            for page in list(context.pages):
                watch(page)

    def started(self, download: DownloadLike) -> bool:
        """True only for an object the browser itself emitted; authored code can build a lookalike."""
        return any(started is download for started in self._downloads)

    def path_of(self, download: DownloadLike) -> Callable[[], Awaitable[Any]]:
        """The path() the browser's object had when it was emitted, not whatever it holds now."""
        return self._original_paths.get(id(download), download.path)

    async def _until_finished(self, download: DownloadLike) -> None:
        # failure() alone is not proof: the raw-CDP engine's gives up after a fixed wait and reports no
        # failure for a transfer still running. Only a reported failure or a delivered path is.
        while not self._closed:
            if await download.failure() is not None:
                return
            try:
                if await self.path_of(download)() is not None:
                    return
            except Exception:
                # A path() that cannot be read (a remote Playwright browser) still follows a failure()
                # that waited for completion; only a None from the raw-CDP timeout means still running.
                return
            await asyncio.sleep(1)
        raise asyncio.CancelledError

    def _settle(self, key: int, completion: asyncio.Future[Any]) -> None:
        if completion.cancelled():
            return
        if completion.exception() is None:
            self._finished.add(key)

    def shares_name(self, download: DownloadLike) -> bool:
        """True when any other download this block started could have been saved under ``download``'s name.

        The same equivalence the registered rows are matched under: whatever spelling can select a row must
        also count as a collision here, or a sibling's row could be taken as the claim's own.
        """
        names = _download_name_spellings(download)
        patterns = [_registered_row_pattern(name) for name in names]
        for other in self._downloads:
            if other is download:
                continue
            other_names = _download_name_spellings(other)
            if any(pattern.fullmatch(other_name) for pattern in patterns for other_name in other_names):
                return True
            if any(_registered_row_pattern(other_name).fullmatch(name) for other_name in other_names for name in names):
                return True
        return False

    def superseded(self, download: DownloadLike) -> bool:
        """True unless ``download`` is the newest same-name download and none overlapped it in flight."""
        if id(download) in self._overlapped:
            return True
        for index, started in enumerate(self._downloads):
            if started is download:
                name = _download_disk_name(download)
                return any(_download_disk_name(later) == name for later in self._downloads[index + 1 :])
        return True

    def close(self) -> None:
        self._closed = True
        # A stalled transfer never resolves failure(), so the flag alone would leave each tracker holding this
        # log and its context past the block. Cancelling is safe on both engines: Playwright's failure() and
        # path() are one channel request per call, and the raw-CDP facade shields its shared future itself.
        for completion in self._completions:
            completion.cancel()
        with suppress(Exception):
            self._context.remove_listener("page", self._watch)
        for page in self._pages:
            with suppress(Exception):
                page.remove_listener("download", self._record)


def _settled_browser_context(download: DownloadLike) -> BrowserContext | None:
    try:
        return download.page.context
    except Exception:
        return None


async def _playwright_download_path(path_of: Callable[[], Awaitable[Any]], deadline: float) -> Path | None:
    # resolve_artifact_path shields path(), so expiry never cancels a future another awaiter shares.
    remaining = max(0.0, deadline - asyncio.get_running_loop().time())
    try:
        path = await resolve_artifact_path(cast(Any, SimpleNamespace(path=path_of)), remaining)
    except Exception:
        return None
    return None if path is None else Path(path)


async def _download_failure(download: DownloadLike, deadline: float) -> str | None:
    try:
        async with asyncio.timeout_at(deadline):
            return await download.failure()
    except Exception as error:
        return f"failure_unreadable:{type(error).__name__}"


def _entry_identity(path: Path) -> tuple[str, int, int, int] | None:
    try:
        info = path.lstat()
    except OSError:
        return None
    return (path.name, info.st_ino, info.st_size, info.st_mtime_ns)


def run_directory_entry_identities(download_root: str | Path, run_id: str) -> frozenset[tuple[str, int, int, int]]:
    """Identify every file already in the run directory, so a later read can tell this block's
    downloads from the ones it inherited.

    Identity, not a timestamp: Linux stamps files from a coarse clock, so a file written just after a
    fine-grained ``time.time_ns()`` reading can carry an earlier mtime and look older than the block.
    """
    identities: set[tuple[str, int, int, int]] = set()
    # Recursive: a download object's path() can be reassigned in-process, so an inherited file anywhere
    # under the run directory must be recognisable, not only one at the top level.
    for directory, _subdirectories, names in os.walk(_run_directory(download_root, run_id)):
        for name in names:
            entry = Path(directory) / name
            try:
                info = entry.lstat()
            except OSError:
                continue
            identities.add((entry.name, info.st_ino, info.st_size, info.st_mtime_ns))
    return frozenset(identities)


def _collision_pattern(filename: str) -> re.Pattern[str]:
    name = PurePath(filename)
    # Some bindings uniquify a collision as "name (1).ext"; the run-scoped one overwrites instead.
    return re.compile(rf"^{re.escape(name.stem)}( \(\d+\))?{re.escape(name.suffix)}$")


def _registered_row_pattern(filename: str) -> re.Pattern[str]:
    """As ``_collision_pattern``, but a name the site sent without an extension may have been registered
    with one the monitor derived from the response type. An extra match is ambiguity, which refuses."""
    name = PurePath(filename)
    if name.suffix:
        return _collision_pattern(filename)
    return re.compile(rf"^{re.escape(name.stem)}( \(\d+\))?(\.[A-Za-z0-9]{{1,16}})?$")


def _settled_downloads_in_run_directory(
    run_directory: Path, filename: str, inherited: frozenset[tuple[str, int, int, int]]
) -> list[Path]:
    """Every file this block could have downloaded under ``filename``, newest first.

    A download object carries no identity the worker can match against a file, so more than one
    candidate is unresolvable rather than a race to settle: the caller refuses instead of guessing.
    """
    collision_pattern = _collision_pattern(filename)
    matches: list[tuple[int, Path]] = []
    try:
        entries = list(run_directory.iterdir())
    except OSError:
        return []
    for entry in entries:
        if not collision_pattern.fullmatch(entry.name):
            continue
        try:
            info = entry.lstat()
        except OSError:
            continue
        if not stat.S_ISREG(info.st_mode):
            continue
        if (entry.name, info.st_ino, info.st_size, info.st_mtime_ns) in inherited:
            continue
        matches.append((info.st_mtime_ns, entry))
    return [entry for _, entry in sorted(matches, reverse=True)]


@dataclass(frozen=True, slots=True)
class RegisteredDownloadSource:
    """The run's registered DOWNLOAD artifacts, and which of them existed before the claim was armed."""

    probe: DownloadEvidenceProbe
    organization_id: str
    run_id: str
    baseline: Mapping[RegisteredDownloadIdentity, str] | None

    async def fetch(self, artifact_id: str) -> bytes | None:
        artifact = await app.DATABASE.artifacts.get_artifact_by_id(artifact_id, self.organization_id)
        if artifact is None or artifact.artifact_type != ArtifactType.DOWNLOAD or artifact.run_id != self.run_id:
            return None
        return await app.STORAGE.retrieve_artifact(artifact)


def _write_private_copy(run_directory: Path, content: bytes) -> Path:
    # A dot-directory beneath the run directory: containment still holds, and download registration
    # lists only the run directory's top-level files, so the copy is never registered a second time.
    run_directory.mkdir(parents=True, exist_ok=True)
    target = Path(tempfile.mkdtemp(prefix=".authorized-", dir=run_directory)) / "download"
    file_fd = os.open(target, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    with os.fdopen(file_fd, "wb") as handle:
        handle.write(content)
    return target


async def _materialize_registered_download(
    download: DownloadLike,
    registered: RegisteredDownloadSource,
    *,
    run_directory: Path,
    max_bytes: int,
    deadline: float,
    decision: dict[str, str | bool | int | None],
    decide: Callable[[str], None],
) -> Path:
    """Copy the one DOWNLOAD artifact this claim registered into the run directory, verified against its checksum."""

    def refuse(outcome: str) -> AuthorizedFileAccessError:
        decide(outcome)
        return AuthorizedFileAccessError(AUTHORIZED_FILE_UNAVAILABLE_ERROR)

    baseline = registered.baseline
    if baseline is None:
        raise refuse("refused_registration_baseline_unreadable")
    # Matched under every spelling a writer could have used: an extra match is ambiguity, which refuses.
    collision_patterns = [_registered_row_pattern(name) for name in _download_name_spellings(download)]
    matches: list[FileInfo] = []
    unreadable = False
    probe_timeouts = 0
    try:
        async with asyncio.timeout_at(
            min(deadline, asyncio.get_running_loop().time() + _REGISTERED_DOWNLOAD_WAIT_SECONDS)
        ):
            while not matches:
                try:
                    async with asyncio.timeout(_REGISTERED_DOWNLOAD_PROBE_TIMEOUT_SECONDS):
                        rows, _ = await registered.probe()
                except TimeoutError:
                    # A read can wait on a download still in flight, so a slow one is a missed poll, not a refusal.
                    probe_timeouts += 1
                    decision["registration_probe_timeouts"] = probe_timeouts
                    rows = []
                if rows is None:
                    unreadable = True
                    break
                matches = [
                    row
                    for row in rows
                    if row.filename
                    and (row.filename, row.checksum) not in baseline
                    and any(pattern.fullmatch(row.filename) for pattern in collision_patterns)
                ]
                if not matches:
                    await asyncio.sleep(_REGISTERED_DOWNLOAD_POLL_SECONDS)
    except TimeoutError:
        pass
    except Exception as error:
        decision["registration_error_type"] = type(error).__name__
        unreadable = True
    if unreadable:
        raise refuse("refused_registration_unreadable")
    decision["registered_candidates"] = len(matches)
    if len(matches) != 1:
        raise refuse("refused_no_registered_download" if not matches else "refused_ambiguous_registered_download")
    row = matches[0]
    if row.artifact_id is None:
        raise refuse("refused_registered_download_without_artifact")
    if not row.checksum:
        raise refuse("refused_registered_download_without_checksum")
    # Storage hands back one bytes object, so the row's size is the only ceiling that can be applied before the
    # whole artifact is in memory. Registration records it for every download; a row without one is refused.
    if row.file_size is None:
        raise refuse("refused_registered_download_without_size")
    if row.file_size > max_bytes:
        raise refuse("refused_registered_download_too_large")
    try:
        async with asyncio.timeout_at(deadline):
            content = await registered.fetch(row.artifact_id)
    except Exception as error:
        decision["registration_error_type"] = type(error).__name__
        raise refuse("refused_registration_unreadable") from None
    if content is None:
        raise refuse("refused_registered_artifact_unavailable")
    if len(content) > max_bytes:
        raise refuse("refused_registered_download_too_large")
    if hashlib.sha256(content).hexdigest() != row.checksum.lower():
        raise refuse("refused_registered_checksum_mismatch")
    try:
        return await asyncio.to_thread(_write_private_copy, run_directory, content)
    except OSError as error:
        decision["registration_error_type"] = type(error).__name__
        raise refuse("refused_registered_copy_failed") from None


async def capture_claimed_download(
    download: DownloadLike,
    *,
    download_root: str | Path,
    download_run_id: str,
    organization_id: str | None,
    max_bytes: int,
    inherited_files: frozenset[tuple[str, int, int, int]],
    download_log: BlockDownloadLog | None = None,
    registered: RegisteredDownloadSource | None = None,
) -> MaterializedAuthorizedFile:
    """Turn a download this run claimed into the same run-scoped capability a file_url parameter gets."""
    run_directory = _run_directory(download_root, download_run_id)
    decision: dict[str, str | bool | int | None] = {
        "download_run_id": download_run_id,
        "organization_id": organization_id,
        "run_directory": str(run_directory),
    }

    def decide(outcome: str) -> None:
        LOG.info("codeblock.download_authorize_decision", outcome=outcome, **decision)

    context = _settled_browser_context(download)
    # One window for settling, path() and failure(), so a stalled transfer cannot hold a runner past it.
    deadline = asyncio.get_running_loop().time() + SAVE_DOWNLOADED_FILES_TIMEOUT
    try:
        async with asyncio.timeout_at(deadline):
            async with settle_browser_downloads_for_context(context):
                pass
    except Exception as error:
        decision["settle_error_type"] = type(error).__name__
        decide("refused_settle_failed")
        raise AuthorizedFileAccessError(AUTHORIZED_FILE_UNAVAILABLE_ERROR) from None
    # The on-disk name is a GUID, or a collision-suffixed copy of the suggested one.
    filename = normalize_download_filename(download.suggested_filename or "")
    path_of = download.path if download_log is None else download_log.path_of(download)
    playwright_path = await _playwright_download_path(path_of, deadline)
    settled = playwright_path if playwright_path is not None and playwright_path.is_file() else None
    if settled is not None and _entry_identity(settled) in inherited_files:
        decide("refused_inherited_file")
        raise AuthorizedFileAccessError(AUTHORIZED_FILE_UNAVAILABLE_ERROR)
    decision["suggested_name_fp"] = diagnostic_fingerprint(filename)
    decision["playwright_directory"] = None if playwright_path is None else str(playwright_path.parent)
    decision["playwright_path_is_file"] = settled is not None
    from_registered_artifact = False
    if settled is not None:
        filename = filename or settled.name
    else:
        # Under the run-scoped setDownloadBehavior binding Chrome writes the file itself and Playwright's
        # own path never exists; the monitor-owned binding saves under names no Download can derive.
        monitoring = is_monitoring_browser_downloads_for_context(context)
        decision["monitor_owns_binding"] = monitoring
        decision["registered_source"] = registered is not None
        if (monitoring and registered is None) or not filename:
            decide("refused_no_suggested_name" if not filename else "refused_monitor_owned_binding")
            raise AuthorizedFileAccessError(AUTHORIZED_FILE_UNAVAILABLE_ERROR)
        # The monitor denies the browser's own transfer and fetches the file itself, so Playwright reports
        # every monitor-owned download as cancelled; the registered artifact is that binding's proof of delivery.
        failure = await _download_failure(download, deadline)
        decision["browser_failure"] = failure
        if failure is not None and not (monitoring and failure == "canceled"):
            decide("refused_browser_reported_failure")
            raise AuthorizedFileAccessError(AUTHORIZED_FILE_UNAVAILABLE_ERROR)
        if download_log is not None and download_log.superseded(download):
            decide("refused_superseded_same_name_download")
            raise AuthorizedFileAccessError(AUTHORIZED_FILE_UNAVAILABLE_ERROR)
        candidates: list[Path] = []
        if not monitoring:
            candidates = await asyncio.to_thread(
                _settled_downloads_in_run_directory, run_directory, _download_disk_name(download), inherited_files
            )
            decision["run_directory_candidates"] = len(candidates)
        if len(candidates) > 1:
            decide("refused_ambiguous_run_directory_file")
            raise AuthorizedFileAccessError(AUTHORIZED_FILE_UNAVAILABLE_ERROR)
        if candidates:
            settled = candidates[0]
        elif registered is None:
            decide("refused_no_fresh_run_directory_file")
            raise AuthorizedFileAccessError(AUTHORIZED_FILE_UNAVAILABLE_ERROR)
        else:
            # Registered rows carry no link to a Download object, so any same-name download in the block, however
            # early it started, could own the one row the claim finds.
            if download_log is None:
                decide("refused_no_download_log")
                raise AuthorizedFileAccessError(AUTHORIZED_FILE_UNAVAILABLE_ERROR)
            if download_log.shares_name(download):
                decide("refused_same_name_download_in_block")
                raise AuthorizedFileAccessError(AUTHORIZED_FILE_UNAVAILABLE_ERROR)
            from_registered_artifact = True
            settled = await _materialize_registered_download(
                download,
                registered,
                run_directory=run_directory,
                max_bytes=max_bytes,
                deadline=deadline,
                decision=decision,
                decide=decide,
            )
    decision["located_directory"] = str(settled.parent)
    # A refused claim discards the copy this capture wrote: the private directory is unreachable once the
    # token is withheld, and a block can lose the race repeatedly under names that never repeat.
    private_copy = settled.parent if from_registered_artifact else None
    try:
        try:
            materialized = await asyncio.to_thread(
                capture_authorized_file,
                settled,
                download_root=download_root,
                workflow_run_id=download_run_id,
                organization_id=organization_id,
                max_bytes=max_bytes,
                filename=filename,
            )
        except AuthorizedFileAccessError:
            decide("refused_capture")
            raise
        # Checked again once the bytes are captured: a same-name download that started during the scan could
        # have overwritten the file first. One that starts later is caught when the captured hash is re-verified.
        if download_log is not None and download_log.superseded(download):
            decide("refused_superseded_during_capture")
            raise AuthorizedFileAccessError(AUTHORIZED_FILE_UNAVAILABLE_ERROR)
        # A registered row is matched by collision-equivalent name, which supersession compares exactly, so a
        # sibling that started while the poll was waiting could own the row this claim just took.
        if from_registered_artifact and download_log is not None and download_log.shares_name(download):
            decide("refused_same_name_download_during_capture")
            raise AuthorizedFileAccessError(AUTHORIZED_FILE_UNAVAILABLE_ERROR)
    except BaseException:
        if private_copy is not None:
            with suppress(OSError):
                await asyncio.to_thread(shutil.rmtree, private_copy)
        raise
    decide("authorized_registered_artifact" if from_registered_artifact else "authorized")
    return materialized


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
    download_run_id: str | None = None,
    download_log: BlockDownloadLog | None = None,
    locate: Callable[[str], FileInputLocator] | None = None,
    registered_downloads: RegisteredDownloadSource | None = None,
) -> Callable[[FileInputTarget, str | DownloadLike, str], Awaitable[dict[str, str | int]]]:
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

    download_key = download_run_id or workflow_run_id
    inherited_files = run_directory_entry_identities(download_root, download_key)
    # One capture per claimed download: a block can attach the same download to several inputs, and the
    # registered-artifact path writes a private copy each time it runs. Identity keeps the list short and
    # exact; redemption re-verifies the copy on every attach, as it does for a file_url.
    claimed_captures: list[tuple[DownloadLike, MaterializedAuthorizedFile]] = []

    async def attach_authorized_file(
        page: FileInputTarget,
        file: str | DownloadLike,
        selector: str,
    ) -> dict[str, str | int]:
        if page is not expected_page:
            raise AuthorizedFileAccessError("attach_authorized_file requires the current CodeBlock page.")
        # Capability, not class identity: a raw-CDP download is not a Playwright ``Download``, and the
        # bytes come from the run's own download directory either way, never from this object.
        if is_download_like(file):
            # Authored code hands this object in and can build a lookalike, so the only proof of a claim
            # is that the browser emitted this exact object. The secure lane never takes one from the sandbox.
            if download_log is None or not download_log.started(file):
                LOG.info(
                    "codeblock.download_authorize_decision",
                    outcome="refused_unclaimed_download_object",
                    download_run_id=download_key,
                    organization_id=organization_id,
                )
                raise AuthorizedFileAccessError(AUTHORIZED_FILE_UNAVAILABLE_ERROR)
            run_key = download_key
            captured = next((item for claimed, item in claimed_captures if claimed is file), None)
            if captured is None:
                captured = await capture_claimed_download(
                    file,
                    download_root=download_root,
                    download_run_id=run_key,
                    organization_id=organization_id,
                    max_bytes=max_bytes,
                    inherited_files=inherited_files,
                    download_log=download_log,
                    registered=registered_downloads,
                )
                claimed_captures.append((file, captured))
            authorized_file = captured
        else:
            run_key = workflow_run_id
            resolved = authorized_by_path.get(file) if isinstance(file, str) else None
            if resolved is None:
                if isinstance(file, str) and file in failed_values:
                    raise AuthorizedFileAccessError(AUTHORIZED_FILE_UNAVAILABLE_ERROR)
                raise AuthorizedFileAccessError(AUTHORIZED_FILE_SHAPE_ERROR)
            authorized_file = resolved
        if locate is None:
            raise AuthorizedFileAccessError("attach_authorized_file requires the current CodeBlock page.")
        return await attach_materialized_authorized_file(
            locate,
            authorized_file,
            selector,
            download_root=download_root,
            workflow_run_id=run_key,
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
