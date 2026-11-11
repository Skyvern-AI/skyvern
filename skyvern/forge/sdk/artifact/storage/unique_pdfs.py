"""List each PDF content once in a run's automatic download outputs.

Copies merge only when the bytes each serves were read back unchanged, match the recorded SHA-256 and belong to the
listed organization and run; anything else stays listed, and files, rows and listings are never changed.
"""

import asyncio
import hashlib
import math
import os
import re
import stat
import threading
from collections import Counter
from collections.abc import Callable, Collection, Sequence
from contextvars import ContextVar
from pathlib import Path
from typing import Any, TypeVar

import structlog

from skyvern.config import settings
from skyvern.constants import BROWSER_DOWNLOADING_SUFFIX, GET_DOWNLOADED_FILES_TIMEOUT
from skyvern.forge import app
from skyvern.forge.sdk.api.files import download_dir_path_for_run, parse_uri_to_path
from skyvern.forge.sdk.artifact.models import Artifact
from skyvern.forge.sdk.artifact.storage.base import StreamingArtifactStorage
from skyvern.forge.sdk.core import skyvern_context
from skyvern.forge.sdk.schemas.files import FileInfo

LOG = structlog.get_logger()

_T = TypeVar("_T")
_SHA256_HEX = re.compile(r"[0-9a-f]{64}")
_CHUNK_BYTES = 1024 * 1024
# A PDF ends with an %%EOF marker after its trailer, so a cut-off copy lacks it. This is a completion hint used only to
# decide which equal copies may be merged; it does not parse or validate the document.
_PDF_EOF_SEARCH_BYTES = 1024
# The loop time a timeline response matching downloads to Outputs must end by; storage work it reaches may honour it.
response_deadline: ContextVar[float | None] = ContextVar("response_deadline", default=None)


class _PdfDigest:
    def __init__(self) -> None:
        self.sha256 = hashlib.sha256()
        self.size = 0
        self.head = b""
        self.tail = b""

    def update(self, chunk: bytes) -> None:
        self.sha256.update(chunk)
        self.size += len(chunk)
        if len(self.head) < 8:
            self.head = (self.head + chunk)[:8]
        self.tail = (self.tail + chunk)[-_PDF_EOF_SEARCH_BYTES:]

    def hexdigest(self) -> str | None:
        if not _starts_like_pdf(self.head) or b"%%EOF" not in self.tail:
            return None
        return self.sha256.hexdigest()


def _starts_like_pdf(head: bytes) -> bool:
    return head.removeprefix(b"\xef\xbb\xbf").startswith(b"%PDF-")


def starts_like_pdf(path: str) -> bool | None:
    """Whether the file's first bytes start a PDF; None when they cannot be read."""
    try:
        with open(path, "rb") as file:
            return _starts_like_pdf(file.read(8))
    except OSError:
        return None


class ResponseComparison:
    """One response's PDF comparison: one deadline, the artifact rows already read, and every verdict reached."""

    def __init__(self) -> None:
        self.deadline = asyncio.get_running_loop().time() + GET_DOWNLOADED_FILES_TIMEOUT
        self.rows: dict[str, Artifact] = {}
        self.looked_up: set[str] = set()
        self.verdicts: dict[tuple[str, str, int], tuple[str, float] | None] = {}
        self.stop = threading.Event()

    def remember_rows(self, rows: Sequence[Artifact], organization_id: str) -> None:
        self.rows.update({row.artifact_id: row for row in rows if row.organization_id == organization_id})

    async def read_local(self, path: str, size: int) -> tuple[str, float] | None:
        # A cancelled to_thread keeps reading, so the read is stopped at its next chunk and awaited before re-raising.
        worker = asyncio.ensure_future(asyncio.to_thread(stable_pdf_digest, path, size, self.stop))
        try:
            return await asyncio.shield(worker)
        except BaseException:
            self.stop.set()
            await settle(worker)
            raise


async def settle(worker: asyncio.Future) -> bool:
    """Wait until ``worker`` has finished, through any further cancellation, and retrieve its outcome.

    Returns whether a cancellation arrived meanwhile; the caller re-raises it, so it wins over the worker's result."""
    cancelled = False
    while not worker.done():
        try:
            await asyncio.wait({worker})
        except asyncio.CancelledError:
            cancelled = True
    if not worker.cancelled():
        worker.exception()
    return cancelled


def _comparable(name: str | None, size: int | None) -> bool:
    return (
        size is not None
        and 0 < size <= settings.BROWSER_DOWNLOAD_MAX_FILE_SIZE_BYTES
        and not (name or "").endswith(BROWSER_DOWNLOADING_SUFFIX)
    )


def stable_pdf_digest(
    path: str, size: int | None = None, stop: threading.Event | None = None
) -> tuple[str, float] | None:
    """SHA-256 and mtime of the regular PDF at ``path`` read once within ``size`` bytes, or None if unproven."""
    try:
        before = os.lstat(path)
        expected = before.st_size if size is None else size
        if not stat.S_ISREG(before.st_mode) or before.st_size != expected or not _comparable(path, expected):
            return None
        digest = _PdfDigest()
        with open(os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)), "rb") as file:
            opened = os.fstat(file.fileno())
            if (opened.st_dev, opened.st_ino) != (before.st_dev, before.st_ino):
                return None
            while chunk := file.read(min(_CHUNK_BYTES, expected + 1 - digest.size)):
                if stop is not None and stop.is_set():
                    return None
                digest.update(chunk)
            after = os.fstat(file.fileno())
        current = os.lstat(path)
    except OSError:
        return None
    unchanged = all(
        (s.st_dev, s.st_ino, s.st_size, s.st_mtime_ns) == (before.st_dev, before.st_ino, expected, before.st_mtime_ns)
        for s in (after, current)
    )
    hexdigest = digest.hexdigest() if unchanged and digest.size == expected else None
    return (hexdigest, before.st_mtime) if hexdigest else None


async def _streamed_pdf_digest(storage: StreamingArtifactStorage, artifact: Artifact, size: int) -> str | None:
    version = await storage.stat_artifact(artifact)
    if version is None or version.size != size:
        return None
    stream = await storage.open_artifact_stream(artifact, etag=version.etag)
    if stream is None:
        return None
    digest = _PdfDigest()
    try:
        async for chunk in stream.iter_chunks(_CHUNK_BYTES):
            digest.update(chunk)
            if digest.size > size:
                return None
    finally:
        await stream.aclose()
    return digest.hexdigest() if digest.size == size else None


def _first_of_each(items: Sequence[_T], digests: Sequence[str | None], order: Sequence[Any]) -> list[_T]:
    """Keep items in place, dropping any whose digest an earlier one (by ``order``, then position) already holds."""
    owners: dict[str, int] = {}
    for index in sorted(range(len(items)), key=lambda i: (order[i], i)):
        digest = digests[index]
        if digest is not None:
            owners.setdefault(digest, index)
    return [
        item for index, (item, digest) in enumerate(zip(items, digests)) if digest is None or owners[digest] == index
    ]


def unique_pdf_paths(paths: list[str], stop: threading.Event | None = None) -> list[str]:
    """``paths`` without a PDF equal to one modified earlier; a set ``stop`` ends reading and keeps every path."""
    sizes: list[int | None] = []
    for path in paths:
        try:
            sizes.append(os.lstat(path).st_size)
        except OSError:
            sizes.append(None)
    shared = Counter(size for size in sizes if size is not None)
    read = [
        stable_pdf_digest(path, stop=stop)
        if size is not None and shared[size] > 1 and not (stop is not None and stop.is_set())
        else None
        for path, size in zip(paths, sizes)
    ]
    if stop is not None and stop.is_set():
        return paths
    # A file this run gave a configured name to, still holding those bytes, outranks its twins; then a .pdf name wins,
    # as it does when the registered rows are compared, then the earliest write.
    context = skyvern_context.current()
    run_id = (context.run_id or context.workflow_run_id) if context else None
    order: list[tuple[bool, bool, float]] = []
    for path, r in zip(paths, read):
        named = (
            context is not None
            and context.organization_id is not None
            and r is not None
            and context.configured_download_digest(path, context.organization_id, run_id) == r[0]
        )
        order.append((not named, not path.lower().endswith(".pdf"), r[1] if r else math.inf))
    return _first_of_each(paths, [r[0] if r else None for r in read], order)


async def run_stoppable_comparison(work: Callable[[threading.Event], _T]) -> _T:
    """Run ``work`` in a thread whose stop event is set at the deadline or on cancellation, then await it settling."""
    stop = threading.Event()
    worker = asyncio.ensure_future(asyncio.to_thread(work, stop))
    try:
        done, _ = await asyncio.wait({worker}, timeout=GET_DOWNLOADED_FILES_TIMEOUT)
        if not done:
            LOG.warning("Stopped comparing download contents at the deadline; keeping every copy")
    finally:
        stop.set()
        if await settle(worker):
            raise asyncio.CancelledError
    return worker.result()


def _local_run_file(file_info: FileInfo, run_ids: Collection[str]) -> str | None:
    if file_info.artifact_id or not file_info.url.startswith("file://"):
        return None
    path = file_info.url.removeprefix("file://")
    run_dirs = {download_dir_path_for_run(run_id).resolve() for run_id in run_ids}
    return path if Path(path).resolve().parent in run_dirs else None


def _attributed(
    file_info: FileInfo, comparison: ResponseComparison, run_ids: Collection[str], unbound_ids: Collection[str]
) -> bool:
    if artifact := comparison.rows.get(file_info.artifact_id or ""):
        return artifact.run_id in run_ids or (artifact.run_id is None and artifact.artifact_id in unbound_ids)
    return _local_run_file(file_info, run_ids) is not None


async def _served_digest(
    file_info: FileInfo, size: int, comparison: ResponseComparison, organization_id: str
) -> tuple[str, float] | None:
    """Digest and creation order of the bytes an attributed ``file_info`` serves, read within bounded reads."""
    storage = app.STORAGE
    try:
        if artifact := comparison.rows.get(file_info.artifact_id or ""):
            created = artifact.created_at.timestamp()
            if storage.manages_local_file_uri(artifact.uri, organization_id):
                local = await comparison.read_local(parse_uri_to_path(artifact.uri), size)
                return (local[0], created) if local else None
            if isinstance(storage, StreamingArtifactStorage) and storage.can_verify_artifact(artifact):
                streamed = await _streamed_pdf_digest(storage, artifact, size)
                return (streamed, created) if streamed else None
            return None
        if file_info.url.startswith("file://"):
            return await comparison.read_local(file_info.url.removeprefix("file://"), size)
    except Exception:
        LOG.warning(
            "Could not read a download to compare its content", artifact_id=file_info.artifact_id, exc_info=True
        )
    return None


async def _verified(
    files: list[FileInfo],
    organization_id: str,
    run_ids: frozenset[str],
    comparison: ResponseComparison,
    unbound_ids: Collection[str],
) -> tuple[list[str | None], list[tuple[bool, bool, float, str]]]:
    claims: list[tuple[str, int] | None] = [
        (checksum, size)
        if (checksum := file_info.checksum)
        and (size := file_info.file_size) is not None
        and _SHA256_HEX.fullmatch(checksum)
        and _comparable(file_info.filename, size)
        else None
        for file_info in files
    ]
    shared = Counter(claim for claim in claims if claim is not None)
    candidates = [(index, claim[1]) for index, claim in enumerate(claims) if claim is not None and shared[claim] > 1]
    digests: list[str | None] = [None] * len(files)
    order = [(True, True, math.inf, "")] * len(files)
    if not candidates:
        return digests, order
    if missing := [
        aid
        for i, _ in candidates
        if (aid := files[i].artifact_id) and aid not in comparison.rows and aid not in comparison.looked_up
    ]:
        comparison.looked_up.update(missing)
        comparison.remember_rows(
            await app.DATABASE.artifacts.get_artifacts_by_ids(missing, organization_id), organization_id
        )
    # Membership is this call's own; a verdict is about the stored bytes and is shared across the response.
    owned = [(i, size) for i, size in candidates if _attributed(files[i], comparison, run_ids, unbound_ids)]
    keys = {i: (files[i].artifact_id or files[i].url, files[i].checksum or "", size) for i, size in owned}
    unread = list({keys[i]: (i, size) for i, size in owned if keys[i] not in comparison.verdicts}.values())
    served = await asyncio.gather(*(_served_digest(files[i], size, comparison, organization_id) for i, size in unread))
    comparison.verdicts.update({keys[i]: result for (i, _), result in zip(unread, served)})
    for index, _ in owned:
        result = comparison.verdicts[keys[index]]
        if result is not None and result[0] == files[index].checksum:
            digests[index] = result[0]
            order[index] = _rank(files[index], comparison.rows.get(files[index].artifact_id or ""), result[1])
    return digests, order


def _rank(file_info: FileInfo, row: Artifact | None, stored_at: float) -> tuple[bool, bool, float, str]:
    """Copies a configured name was given to first, then ``.pdf`` names, then the earliest stored, then the lowest id.

    The configured-name stamp counts only for the very bytes it was earned on."""
    configured = (
        row is not None
        and row.configured_name_sha256 is not None
        and row.configured_name_sha256 == row.checksum == file_info.checksum
    )
    pdf_name = (file_info.filename or "").lower().endswith(".pdf")
    return (not configured, not pdf_name, stored_at, file_info.artifact_id or "")


async def verified_pdf_digests(
    files: list[FileInfo], organization_id: str, run_ids: Collection[str | None], comparison: ResponseComparison
) -> list[str | None]:
    """The verified content digest of each of ``files``, None where unproven, within the response's deadline."""
    scoped_runs = frozenset(run_id for run_id in run_ids if run_id)
    if not scoped_runs:
        return [None] * len(files)
    try:
        async with asyncio.timeout_at(comparison.deadline):
            digests, _ = await _verified(files, organization_id, scoped_runs, comparison, ())
    except Exception:
        LOG.warning("Could not verify download contents; keeping the stored entries", exc_info=True)
        return [None] * len(files)
    return digests


async def unique_pdf_file_infos(
    files: list[FileInfo],
    organization_id: str | None,
    run_ids: Collection[str | None],
    comparison: ResponseComparison | None = None,
    unbound_ids: Collection[str] = (),
) -> list[FileInfo]:
    """``files`` (already scoped to ``run_ids``) without a PDF copy of an earlier one; any failure keeps every copy.

    ``unbound_ids`` admits rows with no run whose membership the caller already proved, for this call only."""
    scoped_runs = frozenset(run_id for run_id in run_ids if run_id)
    if organization_id is None or not scoped_runs or len(files) < 2:
        return files
    comparison = comparison or ResponseComparison()
    try:
        async with asyncio.timeout_at(comparison.deadline):
            digests, order = await _verified(files, organization_id, scoped_runs, comparison, unbound_ids)
    except Exception:
        LOG.warning(
            "Could not compare download contents; listing every copy", organization_id=organization_id, exc_info=True
        )
        return files
    return _first_of_each(files, digests, order) if any(digests) else files
