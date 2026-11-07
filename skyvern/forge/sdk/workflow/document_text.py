"""Worker-side text reads of a file a code block or browser-code session was issued, for read_document_text."""

from __future__ import annotations

import asyncio
import codecs
import tempfile
from collections.abc import Awaitable, Callable
from concurrent.futures import Executor, ThreadPoolExecutor
from dataclasses import dataclass
from pathlib import Path, PurePath
from typing import TYPE_CHECKING, Protocol, TypedDict

import structlog

from skyvern.config import settings
from skyvern.exceptions import PDFParsingError
from skyvern.forge.sdk.utils.pdf_parser import PdfExtractStats
from skyvern.forge.sdk.workflow.code_block_authorized_files import (
    AuthorizedFileAccessError,
    BlockDownloadLog,
    MaterializedAuthorizedFile,
    RegisteredDownloadSource,
    capture_claimed_download,
    capture_registered_download,
    read_authorized_file,
)
from skyvern.forge.sdk.workflow.exceptions import FileParseTimeout
from skyvern.webeye.browser_object_predicates import DownloadLike

if TYPE_CHECKING:
    from skyvern.forge.agent_functions import DownloadClaimOutcome

LOG = structlog.get_logger()

SUPPORTED_DOCUMENT_EXTENSIONS = (".pdf", ".txt", ".csv", ".json")
_TEXT_EXTENSIONS = frozenset(SUPPORTED_DOCUMENT_EXTENSIONS) - {".pdf"}
# Per code block execution or browser-code session; the whole budget is four maximum-size files, and the text is
# cut well below the broker's result limit.
READ_DOCUMENT_TEXT_MAX_CALLS = 16
READ_DOCUMENT_TEXT_MAX_FILE_BYTES = 16 * 1024 * 1024
READ_DOCUMENT_TEXT_MAX_BYTES = 4 * READ_DOCUMENT_TEXT_MAX_FILE_BYTES
READ_DOCUMENT_TEXT_MAX_CHARS = 1_000_000
# Logged in place of the file name, which can carry customer document metadata.
_PARSE_LOG_IDENTIFIER = "read_document_text"
# Shared by every execution and session on this worker, so helper parses, including ones a timeout left running,
# never take more than two threads; a read queued behind them times out instead.
_PARSE_EXECUTOR = ThreadPoolExecutor(max_workers=2, thread_name_prefix="read_document_text")

SAVED_BLOCK_HANDLE_ERROR = (
    "read_document_text accepts only the file name click_and_claim_download returned earlier in this code block."
)
SESSION_HANDLE_ERROR = (
    "read_document_text accepts only a file_id, or the file dict, that click_and_download returned in this session."
)
_UNSUPPORTED_TYPE_ERROR = "read_document_text reads .pdf, .txt, .csv and .json files only."
_CLAIM_UNAVAILABLE_ERROR = "The claimed file could not be read."
_AMBIGUOUS_CLAIM_ERROR = "More than one claimed download returned this file name, so it cannot be read by name."
_PARSE_TIMEOUT_ERROR = "Reading the PDF took longer than its parse time limit."


class DocumentText(TypedDict):
    text: str
    page_count: int | None
    char_count: int
    truncated: bool
    text_layer_problem: str | None


class DocumentTextError(TypedDict):
    error: str


DocumentTextResult = DocumentText | DocumentTextError


class PdfTextLayerReader(Protocol):
    async def __call__(
        self,
        file_url: str,
        file_path: str,
        *,
        stats: PdfExtractStats | None = None,
        executor: Executor | None = None,
    ) -> tuple[str, str | None]: ...


class DocumentUnavailable(Exception):
    """A loader's refusal; its message is what block code receives."""


def too_large(max_bytes: int) -> DocumentUnavailable:
    return DocumentUnavailable(f"The file is larger than the {max_bytes} bytes read_document_text can still read.")


class DocumentTextReader:
    """Decodes or parses issued files under one execution's call and byte budget."""

    def __init__(self, parse_pdf: PdfTextLayerReader) -> None:
        self._parse_pdf = parse_pdf
        self._calls = 0
        self._bytes = 0
        # One read at a time, so the byte budget is checked against every earlier load and no two parses
        # share the worker's CPU at once.
        self._lock = asyncio.Lock()

    async def read(self, filename: str, load: Callable[[int], Awaitable[bytes]]) -> DocumentTextResult:
        async with self._lock:
            return await self._read(filename, load)

    async def _read(self, filename: str, load: Callable[[int], Awaitable[bytes]]) -> DocumentTextResult:
        if self._calls >= READ_DOCUMENT_TEXT_MAX_CALLS:
            return {"error": f"read_document_text can be called at most {READ_DOCUMENT_TEXT_MAX_CALLS} times."}
        self._calls += 1
        suffix = PurePath(filename).suffix.lower()
        if suffix not in SUPPORTED_DOCUMENT_EXTENSIONS:
            return {"error": _UNSUPPORTED_TYPE_ERROR}
        remaining = READ_DOCUMENT_TEXT_MAX_BYTES - self._bytes
        try:
            content = await load(min(READ_DOCUMENT_TEXT_MAX_FILE_BYTES, remaining))
        except DocumentUnavailable as unavailable:
            return {"error": str(unavailable)}
        self._bytes += len(content)
        if suffix in _TEXT_EXTENSIONS:
            try:
                text = content.removeprefix(codecs.BOM_UTF8).decode("utf-8")
            except UnicodeDecodeError:
                return {"error": "The file is not valid UTF-8 text."}
            return _document_text(text, page_count=None, truncated=False, problem=None)
        return await self._read_pdf(content)

    async def _read_pdf(self, content: bytes) -> DocumentTextResult:
        stats = PdfExtractStats()
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "document.pdf"
            await asyncio.to_thread(path.write_bytes, content)
            try:
                text, problem = await self._parse_pdf(
                    _PARSE_LOG_IDENTIFIER, str(path), stats=stats, executor=_PARSE_EXECUTOR
                )
            except PDFParsingError:
                return {"error": "The file could not be parsed as a PDF."}
            except FileParseTimeout:
                # ponytail: the timed-out parse thread keeps running, so the reader stops and leaves at most one
                # behind; a killable subprocess parse would lift that ceiling.
                self._calls = READ_DOCUMENT_TEXT_MAX_CALLS
                return {"error": _PARSE_TIMEOUT_ERROR}
        return _document_text(text, page_count=stats.page_count, truncated=stats.truncated, problem=problem)


def _document_text(text: str, *, page_count: int | None, truncated: bool, problem: str | None) -> DocumentText:
    if len(text) > READ_DOCUMENT_TEXT_MAX_CHARS:
        text = text[:READ_DOCUMENT_TEXT_MAX_CHARS]
        truncated = True
    return {
        "text": text,
        "page_count": page_count,
        "char_count": len(text),
        "truncated": truncated,
        "text_layer_problem": problem,
    }


@dataclass
class _Claim:
    source: DownloadLike | MaterializedAuthorizedFile | None
    registered: RegisteredDownloadSource | None


class ClaimedDocuments:
    """The downloads one code block execution claimed, readable by the name each claim returned."""

    def __init__(
        self,
        parse_pdf: PdfTextLayerReader,
        *,
        download_root: str | Path,
        download_run_id: str,
        organization_id: str | None,
        inherited_files: frozenset[tuple[str, int, int, int]],
        download_log: BlockDownloadLog | None = None,
    ) -> None:
        self._reader = DocumentTextReader(parse_pdf)
        self._download_root = download_root
        self._download_run_id = download_run_id
        self._organization_id = organization_id
        self._download_log = download_log
        self._inherited_files = inherited_files
        self._claims: dict[str, _Claim] = {}
        self._ambiguous: set[str] = set()
        self._captures: dict[str, MaterializedAuthorizedFile] = {}
        self._capture_lock = asyncio.Lock()

    def record(
        self,
        name: str,
        source: DownloadLike | MaterializedAuthorizedFile | None,
        *,
        outcome: DownloadClaimOutcome | None,
        registered: RegisteredDownloadSource | None = None,
    ) -> None:
        """A claim with no ``source`` (no Download event) resolves by name from the registered downloads, and is
        dropped unless registration proved delivery, so a placeholder name never becomes readable."""
        if source is None and outcome != "returned_proven":
            return
        if name in self._claims:
            self._ambiguous.add(name)
            return
        self._claims[name] = _Claim(source, registered)

    async def read(self, file: object) -> DocumentTextResult:
        if not isinstance(file, str) or file not in self._claims:
            return {"error": SAVED_BLOCK_HANDLE_ERROR}
        if file in self._ambiguous:
            return {"error": _AMBIGUOUS_CLAIM_ERROR}

        async def load(max_bytes: int) -> bytes:
            materialized = await self._materialize(file)
            if materialized.size > max_bytes:
                raise too_large(max_bytes)
            try:
                verified = await asyncio.to_thread(
                    read_authorized_file,
                    materialized,
                    download_root=self._download_root,
                    workflow_run_id=self._download_run_id,
                    organization_id=self._organization_id,
                    max_bytes=max_bytes,
                )
            except AuthorizedFileAccessError:
                raise DocumentUnavailable(_CLAIM_UNAVAILABLE_ERROR) from None
            return verified.content

        return await self._reader.read(file, load)

    async def _materialize(self, name: str) -> MaterializedAuthorizedFile:
        claim = self._claims[name]
        if isinstance(claim.source, MaterializedAuthorizedFile):
            return claim.source
        async with self._capture_lock:
            cached = self._captures.get(name)
            if cached is not None:
                return cached
            try:
                if claim.source is not None:
                    captured = await capture_claimed_download(
                        claim.source,
                        download_root=self._download_root,
                        download_run_id=self._download_run_id,
                        organization_id=self._organization_id,
                        max_bytes=settings.MAX_UPLOAD_FILE_SIZE,
                        inherited_files=self._inherited_files,
                        download_log=self._download_log,
                        registered=claim.registered,
                    )
                elif claim.registered is not None:
                    captured = await capture_registered_download(
                        name,
                        registered=claim.registered,
                        download_root=self._download_root,
                        download_run_id=self._download_run_id,
                        organization_id=self._organization_id,
                        max_bytes=settings.MAX_UPLOAD_FILE_SIZE,
                        download_log=self._download_log,
                    )
                else:
                    raise DocumentUnavailable(_CLAIM_UNAVAILABLE_ERROR)
            except AuthorizedFileAccessError:
                raise DocumentUnavailable(_CLAIM_UNAVAILABLE_ERROR) from None
            self._captures[name] = captured
            return captured


async def unbound_read_document_text(file: object) -> DocumentTextResult:
    return {"error": SAVED_BLOCK_HANDLE_ERROR}
