"""
CDP Fetch Download Interceptor

Intercepts download responses via the CDP Fetch domain and saves files locally.
Used for remote CDP browsers where Browser.setDownloadBehavior with a local
downloadPath does not work (e.g., Playwright bug #38805 — remote Windows Chrome
ignoring Linux paths).

Flow:
1. Enable Fetch interception for each page:
   - Response stage: detect and intercept downloads
   - Request stage: mediate every active request before dispatch and enable proxy auth challenges
2. On each paused request:
   - Request stage → authorize, then continue or fail closed before dispatch
   - Response non-download → Fetch.continueResponse (pass through)
   - Response download → extract body via stream → save to disk → Fetch.fulfillRequest
"""

from __future__ import annotations

import asyncio
import base64
import binascii
import errno
import hashlib
import inspect
import os
import re
import stat
import threading
import time
import uuid
from collections.abc import AsyncIterator, Callable, Coroutine
from contextlib import asynccontextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import IO, TYPE_CHECKING, Any, Literal
from urllib.parse import unquote, urlparse

import structlog
from playwright.async_api import Browser, BrowserContext, CDPSession, Page

from skyvern.constants import (
    BROWSER_DOWNLOADING_SUFFIX,
    BROWSER_INTERCEPTOR_DISABLE_TIMEOUT,
    BROWSER_PAGE_CLOSE_TIMEOUT,
)
from skyvern.forge.sdk.api import files as file_api
from skyvern.forge.sdk.core.hashing import diagnostic_fingerprint
from skyvern.forge.sdk.core.http_request_authorization import (
    RunScopedRedirectHopAuthorizer,
    deny_unenrolled_redirect_hop,
    is_unenrolled_redirect_hop_authorizer,
)
from skyvern.settings_manager import SettingsManager
from skyvern.webeye.utils.page import SkyvernFrame

if TYPE_CHECKING:
    from skyvern.forge.sdk.api.files import GuardedFileFetchHopResult
    from skyvern.forge.sdk.browser_network_egress_monitor import BrowserNetworkEgressMonitor
    from skyvern.forge.sdk.core.http_request_authorization import RedirectHopAuthorizer

LOG = structlog.get_logger()

CDP_DOWNLOAD_HTTP_SINK_KIND = "browser.download.http"

_DETACHED_DISABLE_TASKS: set[asyncio.Task[None]] = set()


def _own_detached_task(task: asyncio.Task[None], *, event: str, phase: str | None = None) -> None:
    """Keep a cancellation-resistant teardown task alive and retrieve its eventual error."""
    _DETACHED_DISABLE_TASKS.add(task)

    def retrieve(task: asyncio.Task[None]) -> None:
        try:
            if not task.cancelled():
                error = task.exception()
                if error is not None:
                    log_context: dict[str, Any] = {"error_type": type(error).__name__}
                    if phase is not None:
                        log_context["phase"] = phase
                    LOG.warning(event, **log_context)
        finally:
            _DETACHED_DISABLE_TASKS.discard(task)

    task.add_done_callback(retrieve)


def _own_detached_disable(task: asyncio.Task[None]) -> None:
    _own_detached_task(task, event="Previous CDP download interceptor disable failed after detach")


_DOWNLOAD_SETTINGS = SettingsManager.get_settings()
MAX_FILE_SIZE_BYTES = _DOWNLOAD_SETTINGS.BROWSER_DOWNLOAD_MAX_FILE_SIZE_BYTES
MAX_RUN_DOWNLOAD_BYTES = _DOWNLOAD_SETTINGS.BROWSER_DOWNLOAD_MAX_RUN_SIZE_BYTES
MAX_DOWNLOAD_FILES_PER_RUN = _DOWNLOAD_SETTINGS.BROWSER_DOWNLOAD_MAX_FILES_PER_RUN
# Browser.downloadWillBegin is fire-and-forget. Bound its admitted queue and serialize processing so
# a page cannot make the worker buffer many direct-download responses concurrently.
MAX_PENDING_BROWSER_DOWNLOAD_TASKS = 64
# ``Browser.downloadWillBegin`` carries the complete URL. A data URL can therefore retain hundreds
# of MiB before its task reaches the serialized worker. Bound the aggregate queued event text as
# well as task count; 4x the accepted payload cap admits one worst-case encoded payload while
# preventing a burst from retaining one such URL per task.
MAX_PENDING_BROWSER_DOWNLOAD_EVENT_BYTES = 4 * MAX_FILE_SIZE_BYTES + 64 * 1024
# Browser.downloadWillBegin can arrive just after the triggering Playwright action resolves. Hold a
# short post-action admission window before declaring quiescence; the outer CodeBlock timeout still
# bounds this and all subsequent handler work.
BROWSER_DOWNLOAD_EVENT_ADMISSION_GRACE_SECONDS = 0.25
_ARTIFACT_SCOPE_GENERATION_EVENT_KEY = "_skyvernArtifactScopeGeneration"
# Each CDP session detach gets its own bounded cleanup task. This is deliberately shorter than
# the interceptor's whole-disable budget so a stalled target cannot delay browser-monitor cleanup
# or make a replacement bind race an unfinished teardown.
CDP_SESSION_DETACH_TIMEOUT_SECONDS = BROWSER_PAGE_CLOSE_TIMEOUT

# At/above this size a captured download is streamed straight to a temp file and the browser is
# fulfilled with an empty body, instead of buffering the whole body in RAM and base64-replaying it —
# which can OOM the browser target (SKY-12642). Set just below the largest download that completed on
# the local browser stack (~67.6 MiB); larger PDFs crashed that local native-download stack,
# and a separate local harness reproduced the interceptor replay materializing ~4/3x the body as base64.
# Those are local-stack / replay-harness observations, not interceptor-path production measurements.
STREAM_TO_DISK_THRESHOLD_BYTES = 64 * 1024 * 1024  # 64 MiB
# Hard ceiling for a streamed captured download. Above this we abort: clean up the temp file, save no
# artifact, and fail the paused request so the browser neither hangs nor materializes the body.
MAX_STREAMED_FILE_SIZE_BYTES = 512 * 1024 * 1024  # 512 MiB
# IO.read chunk size for the streaming path — larger than a per-page render buffer to cut CDP round
# trips on large files (the cdp_proxy websocket is max_size=None, base64 inflation stays well bounded).
STREAM_IO_READ_CHUNK_SIZE = 256 * 1024  # 256 KiB
# Inactivity bound applied to every CDP send made while holding the single-active extraction lock —
# takeResponseBodyAsStream, each IO.read, fulfill (body replay or empty), failRequest, and IO.close. A
# healthy call completes well within this even over a slow remote-CDP transport; one that stalls past it is
# treated as dead so the extraction aborts instead of holding the lock forever (which would deadlock later
# captures and hang teardown's task drain).
STREAM_IO_READ_TIMEOUT_SECONDS = 120.0


@dataclass
class _StreamOutcome:
    """Result of streaming a captured response body. `streamed` = already on disk (empty-body fulfill);
    `buffered` = held in memory below the threshold (legacy full replay)."""

    mode: Literal["buffered", "streamed"]
    data: bytes | None
    save_path: Path | None
    total_bytes: int


@dataclass
class _ConfinedTemporaryFile:
    directory_fd: int
    name: str
    handle: IO[bytes]


class _StreamStartError(Exception):
    """Fetch.takeResponseBodyAsStream failed before any body bytes were consumed (body not taken)."""


class _StreamAborted(Exception):
    """A streamed download exceeded its configured bound; its temp file was cleaned up."""

    def __init__(self, total_bytes: int, max_size_bytes: int) -> None:
        super().__init__(f"streamed download exceeded {max_size_bytes} bytes ({total_bytes} read)")
        self.total_bytes = total_bytes
        self.max_size_bytes = max_size_bytes


class _DownloadScopeInvalidated(Exception):
    """An in-flight capture reached publication after its run binding was revoked or rotated."""


@dataclass(frozen=True)
class _DownloadAttempt:
    number: int
    intent: Literal["requested", "unsolicited"]
    request_tokens: tuple[int, ...]


# Resource types that should NEVER be treated as downloads.
# Sub-resources (Font, Stylesheet, etc.) are loaded by the page, not user-initiated.
# Real user downloads come through as "Document" (link click / navigation).
NON_DOWNLOAD_RESOURCE_TYPES = frozenset(
    {
        "Font",
        "Stylesheet",
        "Script",
        "Image",
        "Media",
        "Manifest",
        "SignedExchange",
        "Ping",
        "Preflight",
        "CSPViolationReport",
        "Prefetch",
    }
)

# XHR/Fetch are programmatic JS API calls that sometimes carry Content-Disposition:
# attachment (e.g. Google APIs on JSON responses). We don't fully block them —
# instead, we only allow them through if there's an explicit attachment header,
# and rely on NON_DOWNLOAD_CONTENT_TYPES to filter out API false-positives.
# Without an explicit attachment header, we skip XHR/Fetch to avoid MIME-only
# false positives.
XHR_FETCH_RESOURCE_TYPES = frozenset({"XHR", "Fetch"})

# Content types that are clearly API / data responses, never user-facing downloads,
# even if the server includes Content-Disposition: attachment.
NON_DOWNLOAD_CONTENT_TYPES = frozenset(
    {
        "application/json",
        "application/xml",
        "text/xml",
        "application/grpc",
        "application/grpc-web",
        "application/grpc-web+proto",
    }
)

# MIME types that are almost always downloads (even without Content-Disposition)
DOWNLOAD_MIME_TYPES = frozenset(
    {
        "application/octet-stream",
        "application/zip",
        "application/x-zip-compressed",
        "application/gzip",
        "application/x-gzip",
        "application/x-tar",
        "application/x-7z-compressed",
        "application/x-rar-compressed",
        "application/x-msdownload",
        "application/x-download",
        "application/force-download",
        "application/pdf",
        "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
        "application/vnd.ms-excel",
        "application/msword",
        "text/csv",
        "application/csv",
    }
)

# Literal Content-Type strings that some misconfigured servers send verbatim for
# file bytes (e.g. the literal "application/*"). Matched by exact string equality,
# NOT wildcard/prefix semantics. Only eligible for XHR/Fetch responses with
# Content-Length >= MIN_XHR_DOWNLOAD_BYTES; non-XHR responses must rely on stronger
# signals (attachment header or known download MIME).
GENERIC_DOWNLOAD_CONTENT_TYPE_LITERALS = frozenset(
    {
        "application/*",
    }
)

# Minimum response size (bytes) for XHR/Fetch responses with generic binary MIME to be
# treated as downloads, even without Content-Disposition: attachment.
MIN_XHR_DOWNLOAD_BYTES = 1024  # 1 KB

DOWNLOAD_EXTENSION_BY_MIME_TYPE = {
    "application/pdf": ".pdf",
}

_FILENAME_PATH_SEPARATOR_RE = re.compile(r"[\\/]+")
_FILENAME_CONTROL_CHAR_RE = re.compile(r"[\x00-\x1f\x7f]")
_WINDOWS_DRIVE_RE = re.compile(r"^[A-Za-z]:[\\/]")
_DATA_URL_TOKEN = r"[!#$%&'*+.^_`|~A-Za-z0-9-]+"
_DATA_URL_MEDIA_TYPE_RE = re.compile(rf"^{_DATA_URL_TOKEN}/{_DATA_URL_TOKEN}$")
_DATA_URL_PARAMETER_NAME_RE = re.compile(rf"^{_DATA_URL_TOKEN}$")
_DATA_URL_MAX_METADATA_LENGTH = 16 * 1024
_DATA_URL_HASH_CHUNK_SIZE = 64 * 1024

# Substrings that identify a CDP interception which was already resolved/cancelled, or whose
# target/frame detached, before our async handler could respond — a benign race between
# Fetch.requestPaused firing and us sending continue/fulfill (common for telemetry requests
# cancelled by navigation). Retrying is futile; these must not surface as error-level failures.
# Matched case-insensitively against the raised error message.
_STALE_INTERCEPTION_ERROR_SUBSTRINGS = (
    "invalid interceptionid",
    "target closed",
    "session closed",
    "has been closed",
)


def _is_stale_interception_error(error: BaseException) -> bool:
    message = str(error).lower()
    return any(substr in message for substr in _STALE_INTERCEPTION_ERROR_SUBSTRINGS)


def redacted_exception_origin(error: BaseException) -> str:
    """Name the frame that raised ``error`` as ``module:function:line``.

    Exception messages and tracebacks on the download path can carry the credential-bearing
    download URL, so this reports only where a failure came from and never what it said.
    """
    traceback = error.__traceback__
    if traceback is None:
        return "unknown"
    while traceback.tb_next is not None:
        traceback = traceback.tb_next
    code = traceback.tb_frame.f_code
    module = traceback.tb_frame.f_globals.get("__name__")
    return f"{module if isinstance(module, str) else code.co_filename}:{code.co_name}:{traceback.tb_lineno}"


def _parse_headers(raw_headers: list[dict[str, str]]) -> dict[str, str]:
    """Convert CDP header list [{name, value}] to a lowercase-keyed dict (last value wins)."""
    result: dict[str, str] = {}
    for h in raw_headers:
        result[h["name"].lower()] = h["value"]
    return result


def _parse_content_length(headers: dict[str, str]) -> int | None:
    """Extract Content-Length as int, or None if absent/invalid."""
    val = headers.get("content-length")
    if val is None:
        return None
    try:
        return int(val)
    except ValueError:
        return None


def _normalized_content_type(content_type: str) -> str:
    return content_type.split(";")[0].strip().lower()


def _download_extension_for_content_type(content_type: str) -> str:
    return DOWNLOAD_EXTENSION_BY_MIME_TYPE.get(_normalized_content_type(content_type), "")


def _download_identity(url: str) -> str:
    if not url.lower().startswith("data:"):
        return url
    digest = hashlib.sha256()
    for offset in range(0, len(url), _DATA_URL_HASH_CHUNK_SIZE):
        digest.update(url[offset : offset + _DATA_URL_HASH_CHUNK_SIZE].encode())
    return f"data:sha256:{digest.hexdigest()}"


def _bounded_data_url_comma(url: str) -> int:
    comma_index = url.find(",", 5, 6 + _DATA_URL_MAX_METADATA_LENGTH)
    if comma_index < 0 or comma_index == len(url) - 1:
        raise ValueError("missing or empty payload")
    _, is_base64 = _parse_data_url_metadata(url, comma_index)
    max_payload_length = 12 * ((MAX_FILE_SIZE_BYTES + 2) // 3) if is_base64 else 3 * MAX_FILE_SIZE_BYTES
    if len(url) > comma_index + 1 + max_payload_length:
        raise ValueError("encoded payload exceeds size limit")
    return comma_index


def _parse_data_url_metadata(url: str, comma_index: int) -> tuple[str, bool]:
    metadata_length = comma_index - 5
    if metadata_length > _DATA_URL_MAX_METADATA_LENGTH:
        raise ValueError("metadata exceeds size limit")
    metadata = url[5:comma_index]
    parts = metadata.split(";")
    media_type = parts[0]
    if media_type and not _DATA_URL_MEDIA_TYPE_RE.fullmatch(media_type):
        raise ValueError("invalid media type")

    is_base64 = False
    for index, part in enumerate(parts[1:], start=1):
        if part.lower() == "base64":
            if is_base64 or index != len(parts) - 1:
                raise ValueError("misplaced or duplicate base64 marker")
            is_base64 = True
            continue
        name, separator, value = part.partition("=")
        if not separator or not value or not _DATA_URL_PARAMETER_NAME_RE.fullmatch(name):
            raise ValueError("invalid media type parameter")

    return media_type or "text/plain", is_base64


def _percent_decoded_payload_length(url: str, payload_start: int, max_length: int) -> int:
    decoded_length = 0
    index = payload_start
    while index < len(url):
        character = url[index]
        if ord(character) > 127:
            raise ValueError("payload must be ASCII")
        if character == "%":
            if index + 2 >= len(url) or not all(
                char in "0123456789abcdefABCDEF" for char in url[index + 1 : index + 3]
            ):
                raise ValueError("invalid percent escape")
            index += 3
        else:
            index += 1
        decoded_length += 1
        if decoded_length > max_length:
            raise ValueError("decoded payload exceeds size limit")
    return decoded_length


def _percent_decode_payload(url: str, payload_start: int, decoded_length: int) -> bytearray:
    decoded = bytearray()
    index = payload_start
    while index < len(url):
        if url[index] == "%":
            decoded.append(int(url[index + 1 : index + 3], 16))
            index += 3
        else:
            decoded.append(ord(url[index]))
            index += 1
    if len(decoded) != decoded_length:
        raise ValueError("decoded payload length mismatch")
    return decoded


_HTML_FILENAME_EXTENSIONS = frozenset({".html", ".htm", ".xhtml"})
_HTML_START_TAG_RE = re.compile(rb"^<(?:html|head|body)(?:[\t\n\f\r ]|>)")


def _body_starts_with_html(data: bytes) -> bool:
    head = data[:4096].removeprefix(b"\xef\xbb\xbf").lstrip().lower()
    while True:
        if head.startswith(b"<!--"):
            marker_end = head.find(b"-->")
            if marker_end < 0:
                return False
            head = head[marker_end + 3 :].lstrip()
            continue
        if head.startswith(b"<?"):
            marker_end = head.find(b"?>")
            if marker_end < 0:
                return False
            head = head[marker_end + 2 :].lstrip()
            continue
        break
    head = head[:64]
    return head.startswith(b"<!doctype html") or bool(_HTML_START_TAG_RE.match(head))


def _payload_is_html_login_masquerade(data: bytes, content_type: str, filename: str) -> bool:
    """True when a download's bytes are an HTML document but the download does not claim to be HTML.

    A session-gated download endpoint fetched without the browser's auth cookies answers with its
    HTML login/session-gate page (HTTP 200) instead of the file. Saving that under the requested
    binary name (e.g. ``*.zip``) yields a "successful" but corrupt download, so callers reject it.
    A genuine binary payload or an honest ``.html`` download is left untouched.
    """
    # The body is the ground truth: sniff it rather than trusting Content-Type, so a real binary
    # a server mislabels as text/html is not wrongly discarded.
    if not _body_starts_with_html(data):
        return False
    suffix = Path(filename).suffix.lower()
    if suffix in _HTML_FILENAME_EXTENSIONS:
        return False
    if suffix:
        return True
    if filename:
        return True
    # Nameless download: an HTML body only masquerades if the Content-Type still claims a
    # non-HTML (binary) type. A nameless HTML-or-typeless response makes no binary claim, so
    # saving the HTML is honest, not corrupt.
    normalized_ct = _normalized_content_type(content_type)
    return bool(normalized_ct) and "html" not in normalized_ct


def normalize_download_filename(filename: str, content_type: str = "") -> str:
    """Sanitize a server-provided filename and add a trusted extension when missing."""
    filename = unquote(filename).strip()
    filename = _FILENAME_CONTROL_CHAR_RE.sub("", filename)
    if not filename:
        return ""

    path_segments = [segment for segment in _FILENAME_PATH_SEPARATOR_RE.split(filename) if segment]
    has_path_traversal = (
        filename.startswith(("/", "\\"))
        or bool(_WINDOWS_DRIVE_RE.match(filename))
        or any(segment == ".." for segment in path_segments)
    )
    if has_path_traversal:
        filename = next((segment for segment in reversed(path_segments) if segment not in {".", ".."}), "")
    else:
        filename = _FILENAME_PATH_SEPARATOR_RE.sub("_", filename)

    filename = filename.strip(" .")
    if not filename or Path(filename).suffix:
        return filename

    extension = _download_extension_for_content_type(content_type)
    if extension:
        return f"{filename}{extension}"
    return filename


def _validated_download_basename(filename: str, content_type: str = "") -> str:
    decoded_filename = filename.strip()
    while True:
        next_filename = unquote(decoded_filename)
        if next_filename == decoded_filename:
            break
        decoded_filename = next_filename
    if not decoded_filename:
        return ""
    if (
        decoded_filename in {".", ".."}
        or bool(_FILENAME_CONTROL_CHAR_RE.search(decoded_filename))
        or "/" in decoded_filename
        or "\\" in decoded_filename
        or bool(_WINDOWS_DRIVE_RE.match(decoded_filename))
        or Path(decoded_filename).is_absolute()
    ):
        raise ValueError("download filename must be a basename")
    return normalize_download_filename(decoded_filename, content_type)


def download_filename_from_suffix(download_suffix: str, source_extension: str, existing_names: set[str]) -> str:
    """Filename for a download whose block configured ``download_suffix``"""
    existing_names = {Path(n).name for n in existing_names}  # contract: dedup on basenames, never full paths
    name = Path(download_suffix).name  # defensive: never let a suffix escape the dir
    suffix_ext = Path(name).suffix
    if suffix_ext:
        stem, ext = name[: -len(suffix_ext)], suffix_ext
    else:
        stem, ext = name, source_extension or ""
    stem = stem or "download"
    candidate = f"{stem}{ext}"
    counter = 1
    while candidate in existing_names:
        candidate = f"{stem}_{counter}{ext}"
        counter += 1
    return candidate


def is_download_response(headers: dict[str, str], status_code: int, resource_type: str = "") -> bool:
    """
    Determine if a response is a file download.

    Checks:
    0. Skip error responses (status >= 400)
    1. Skip sub-resource types (Font, Stylesheet, Script, Image, etc.)
    2. Skip API content types (application/json, etc.)
    3. For XHR/Fetch: require BOTH attachment header AND download MIME type
       (prevents false positives like Google's text/plain + attachment XHR responses)
       Exception: generic binary MIME types (like application/*) where the server
       does not set a specific Content-Type but the response carries meaningful
       bytes (Content-Length >= MIN_XHR_DOWNLOAD_BYTES).
    4. Content-Disposition contains "attachment"
    5. Content-Type is a known download MIME type
    """
    if status_code >= 400:
        return False

    if resource_type in NON_DOWNLOAD_RESOURCE_TYPES:
        return False

    content_disposition = headers.get("content-disposition", "")
    content_type = _normalized_content_type(headers.get("content-type", ""))

    if content_type in NON_DOWNLOAD_CONTENT_TYPES:
        return False

    is_attachment = "attachment" in content_disposition.lower()
    is_download_mime = content_type in DOWNLOAD_MIME_TYPES
    is_generic_binary = content_type in GENERIC_DOWNLOAD_CONTENT_TYPE_LITERALS

    # XHR/Fetch require both signals to avoid false positives
    # (e.g. Google async requests: text/plain + attachment; filename="f.txt")
    if resource_type in XHR_FETCH_RESOURCE_TYPES:
        # Primary path: attachment header + known download MIME
        if is_attachment and is_download_mime:
            return True
        # Secondary path: generic binary MIME with evidence of actual file content.
        # Some sites (e.g. report exports) return XHR file responses with
        # Content-Type: application/* and no Content-Disposition header.
        content_length = _parse_content_length(headers)
        if is_generic_binary and content_length is not None and content_length >= MIN_XHR_DOWNLOAD_BYTES:
            return True
        return False

    if is_attachment:
        return True

    if is_download_mime:
        return True

    return False


def extract_filename(headers: dict[str, str], url: str) -> str:
    """
    Extract filename from response headers or URL.

    Priority:
    1. Content-Disposition filename*= (RFC 5987, UTF-8)
    2. Content-Disposition filename=
    3. URL path last segment (if it has an extension)
    4. Empty string (caller is responsible for fallback via _resolve_save_path)
    """
    content_disposition = headers.get("content-disposition", "")

    if content_disposition:
        # Try RFC 5987 filename*= first
        match = re.search(r"filename\*\s*=\s*(?:UTF-8|utf-8)''(.+?)(?:;|$)", content_disposition)
        if match:
            return unquote(match.group(1).strip())

        # Try regular filename=
        match = re.search(r'filename\s*=\s*"?([^";]+)"?', content_disposition)
        if match:
            return match.group(1).strip()

    # Try URL path
    parsed = urlparse(url)
    path_segments = [s for s in parsed.path.split("/") if s]
    if path_segments:
        last_segment = unquote(path_segments[-1])
        if "." in last_segment:
            return last_segment

    return ""


class CDPDownloadInterceptor:
    """
    Intercepts download responses via the CDP Fetch domain and optionally handles
    proxy authentication via Fetch.authRequired.

    Flow:
    1. Enable Fetch interception (Response stage for downloads; Request stage + handleAuthRequests for proxy auth)
    2. On each paused request:
       - Request stage → Fetch.continueRequest (pass through)
       - Response non-download → Fetch.continueResponse (pass through)
       - Response download → extract body → save to disk → Fetch.fulfillRequest
    """

    def __init__(
        self,
        output_dir: str | None = None,
        proxy_username: str | None = None,
        proxy_password: str | None = None,
        *,
        network_egress_monitor: BrowserNetworkEgressMonitor,
        redirect_hop_authorizer: RedirectHopAuthorizer[GuardedFileFetchHopResult],
    ) -> None:
        if network_egress_monitor is None or redirect_hop_authorizer is None:
            raise TypeError("browser download required collaborators must be enrolled")
        self._output_dir: Path | None = Path(output_dir) if output_dir else None
        self._download_directory_identities: dict[Path, tuple[int, int]] = {}
        self._proxy_username: str | None = proxy_username
        self._proxy_password: str | None = proxy_password
        self._network_egress_monitor = network_egress_monitor
        self._redirect_hop_authorizer = redirect_hop_authorizer
        self._download_scope = (
            redirect_hop_authorizer.download_scope
            if isinstance(redirect_hop_authorizer, RunScopedRedirectHopAuthorizer)
            else None
        )
        self._cdp_sessions: list[CDPSession] = []
        self._active_request_interceptors: dict[CDPSession, tuple[Page, Callable[[Page], None]]] = {}
        self._enrolling_pages: list[Page] = []
        self._enabled = False
        self._download_index = 0
        # Track auth attempts per requestId to prevent infinite retry loops
        # when proxy credentials are rejected (407 → ProvideCredentials → 407 → …)
        self._auth_attempts: dict[str, int] = {}
        # Track URLs already downloaded (dedup between Fetch interception and browser download monitor)
        self._downloaded_urls: set[str] = set()
        self._artifact_scope_lock = threading.Lock()
        self._artifact_scope_generation = 0
        self._artifact_scope_valid = True
        self._browser_download_processing_lock = asyncio.Lock()
        # Serialize CDP download body extraction: at most one capture streams/buffers at a time per
        # interceptor, so concurrent large downloads cannot each write up to the cap and exhaust the
        # worker's ephemeral disk. Bounds in-flight disk use to a single capture (<= the stream cap).
        self._download_extraction_lock = asyncio.Lock()
        self._browser_download_monitor_lock = asyncio.Lock()
        self._browser_download_tasks: set[asyncio.Task[None]] = set()
        self._browser_download_task_event_bytes: dict[asyncio.Task[None], int] = {}
        self._pending_browser_download_event_bytes = 0
        self._browser_download_generation = 0
        self._accepting_browser_downloads = False
        self._browser_download_listener: Any | None = None
        self._browser_session: CDPSession | None = None
        self._browser_context: BrowserContext | None = None
        self._page_context: BrowserContext | None = None
        self._page_listener: Any | None = None
        self._page_enable_tasks: set[asyncio.Task[None]] = set()
        self._accepting_pages = False
        self._cdp_handler_tasks: set[asyncio.Task[None]] = set()
        # Bumped when a Fetch handler is scheduled; settle drains these to quiescence (a download capture
        # runs inside one, and a just-scheduled handler may not have created its .crdownload yet) so
        # artifact collection can't finalize before a queued/scheduled capture lands and omit it.
        self._cdp_handler_generation = 0
        self._accepting_cdp_handlers = True
        self._telemetry_browser_session_id: str | None = None
        self._telemetry_provider = "unknown"
        self._reset_download_accounting()

    def _reset_download_accounting(self) -> None:
        self._download_attempt_counter = 0
        self._download_request_counter = 0
        self._run_download_file_count = 0
        self._run_download_bytes = 0
        self._captured_download_count = 0
        self._active_download_attempts: dict[int, _DownloadAttempt] = {}
        self._browser_download_attempt: _DownloadAttempt | None = None
        self._active_requested_downloads: dict[int, set[str]] = {}
        self._unsolicited_download_failures: dict[str, int] = {}
        self._artifact_outcome_recorded = False

    def begin_requested_download(self) -> int:
        self._download_request_counter += 1
        token = self._download_request_counter
        self._active_requested_downloads[token] = set()
        return token

    def finish_requested_download(self, token: int) -> dict[str, Any] | None:
        failures = self._active_requested_downloads.pop(token, set())
        has_in_flight_attempt = any(
            token in attempt.request_tokens for attempt in self._active_download_attempts.values()
        )
        if not failures and not has_in_flight_attempt:
            return None
        if any(reason.endswith("_limit") for reason in failures):
            return {
                "error_code": "BROWSER_DOWNLOAD_LIMIT_EXCEEDED",
                "reasoning": "The requested browser download exceeded the configured limit.",
                "error_type": "SYSTEM_DEFINED_ERROR",
            }
        return {
            "error_code": "BROWSER_DOWNLOAD_FAILED",
            "reasoning": "The requested browser download could not be saved.",
            "error_type": "SYSTEM_DEFINED_ERROR",
        }

    def consume_unsolicited_download_error(self) -> dict[str, Any] | None:
        if not self._unsolicited_download_failures:
            return None
        reasons = dict(sorted(self._unsolicited_download_failures.items()))
        self._unsolicited_download_failures.clear()
        failed_count = sum(reasons.values())
        noun = "download was" if failed_count == 1 else "downloads were"
        return {
            "error_code": "BROWSER_DOWNLOAD_PARTIAL_FAILURE",
            "reasoning": f"{failed_count if failed_count > 1 else 'One'} unsolicited browser {noun} not saved.",
            "error_type": "SYSTEM_DEFINED_ERROR",
            "details": {"failed_download_count": failed_count, "reasons": reasons},
        }

    def record_artifact_outcome(
        self,
        *,
        registered_file_count: int,
        outcome: Literal["registered", "partial", "failed", "unknown"],
        error: str | None = None,
    ) -> None:
        if self._artifact_outcome_recorded:
            return
        self._artifact_outcome_recorded = True
        if outcome == "registered" and registered_file_count < self._captured_download_count:
            outcome = "partial"
            error = error or "artifact_count_mismatch"
        LOG.info(
            "browser.download_artifact_outcome",
            run_id=self.download_scope,
            browser_session_id=self._telemetry_browser_session_id,
            provider=self._telemetry_provider,
            attempt_counter=self._download_attempt_counter,
            captured_file_count=self._captured_download_count,
            registered_file_count=registered_file_count,
            artifact_outcome=outcome,
            error=error,
            run_file_count=self._run_download_file_count,
            run_download_bytes=self._run_download_bytes,
        )

    def _new_download_attempt(self, content_length: int | None = None) -> _DownloadAttempt | None:
        self._download_attempt_counter += 1
        request_tokens = tuple(self._active_requested_downloads)
        attempt = _DownloadAttempt(
            number=self._download_attempt_counter,
            intent="requested" if request_tokens else "unsolicited",
            request_tokens=request_tokens,
        )
        if self._run_download_file_count >= MAX_DOWNLOAD_FILES_PER_RUN:
            self._record_download_failure(attempt, "file_count_limit", reserved=False)
            return None
        if self._run_download_bytes >= MAX_RUN_DOWNLOAD_BYTES:
            self._record_download_failure(attempt, "run_size_limit", reserved=False)
            return None
        if content_length is not None and content_length > MAX_FILE_SIZE_BYTES:
            self._record_download_failure(attempt, "file_size_limit", reserved=False)
            return None
        if content_length is not None and self._run_download_bytes + content_length > MAX_RUN_DOWNLOAD_BYTES:
            self._record_download_failure(attempt, "run_size_limit", reserved=False)
            return None
        self._run_download_file_count += 1
        self._active_download_attempts[attempt.number] = attempt
        return attempt

    def _reject_download_attempt(self, reason: str) -> None:
        self._download_attempt_counter += 1
        request_tokens = tuple(self._active_requested_downloads)
        self._record_download_failure(
            _DownloadAttempt(
                number=self._download_attempt_counter,
                intent="requested" if request_tokens else "unsolicited",
                request_tokens=request_tokens,
            ),
            reason,
            reserved=False,
        )

    def _reserve_download_bytes(self, attempt: _DownloadAttempt, size: int) -> bool:
        if size > MAX_FILE_SIZE_BYTES:
            self._record_download_failure(attempt, "file_size_limit")
            return False
        if self._run_download_bytes + size > MAX_RUN_DOWNLOAD_BYTES:
            self._record_download_failure(attempt, "run_size_limit")
            return False
        self._run_download_bytes += size
        return True

    def _record_download_saved(self, attempt: _DownloadAttempt) -> None:
        if self._active_download_attempts.get(attempt.number) is not attempt:
            return
        del self._active_download_attempts[attempt.number]
        self._captured_download_count += 1
        self._log_download_attempt(attempt, capture_outcome="saved", artifact_outcome="pending", error=None)

    def _release_download_file_slot(self, attempt: _DownloadAttempt) -> bool:
        if self._active_download_attempts.get(attempt.number) is not attempt:
            return False
        del self._active_download_attempts[attempt.number]
        self._run_download_file_count -= 1
        return True

    def publish_download_bytes(
        self,
        data: bytes | bytearray,
        suggested_filename: str,
        content_type: str = "",
    ) -> Path | None:
        """Admit and atomically publish bytes captured outside the CDP response handlers."""
        attempt = self._new_download_attempt(len(data))
        if attempt is None:
            return None
        if self._output_dir is None or not self._reserve_download_bytes(attempt, len(data)):
            if self._output_dir is None:
                self._record_download_failure(attempt, "capture_failed")
            return None
        try:
            save_path, _ = self._resolve_save_path(suggested_filename, content_type)
            self._atomically_write_bytes(save_path, data, self._artifact_scope_generation)
        except (OSError, ValueError, _DownloadScopeInvalidated):
            self._record_download_failure(attempt, "capture_failed")
            return None
        self._record_download_saved(attempt)
        return save_path

    def _record_download_failure(self, attempt: _DownloadAttempt, reason: str, *, reserved: bool = True) -> None:
        if reserved and not self._release_download_file_slot(attempt):
            return
        if attempt.request_tokens:
            for token in attempt.request_tokens:
                failures = self._active_requested_downloads.get(token)
                if failures is not None:
                    failures.add(reason)
        else:
            self._unsolicited_download_failures[reason] = self._unsolicited_download_failures.get(reason, 0) + 1
        self._log_download_attempt(
            attempt,
            capture_outcome="failed",
            artifact_outcome="not_created",
            error=reason,
        )

    def _log_download_attempt(
        self,
        attempt: _DownloadAttempt,
        *,
        capture_outcome: Literal["saved", "failed"],
        artifact_outcome: Literal["pending", "not_created"],
        error: str | None,
    ) -> None:
        LOG.info(
            "browser.download_attempt_outcome",
            run_id=self.download_scope,
            browser_session_id=self._telemetry_browser_session_id,
            provider=self._telemetry_provider,
            attempt=attempt.number,
            attempt_counter=self._download_attempt_counter,
            intent=attempt.intent,
            capture_outcome=capture_outcome,
            artifact_outcome=artifact_outcome,
            error=error,
            run_file_count=self._run_download_file_count,
            run_download_bytes=self._run_download_bytes,
        )

    def set_download_dir(self, download_dir: str) -> None:
        """Set or update the download directory. Can be called after init when run_id becomes available.

        On a genuine directory change (persistent/adopted reuse across runs), drop ``_downloaded_urls``:
        each entry names a file already written into the prior dir, so keeping it would skip an
        identical download in the new run's dir and leave its artifact missing (SKY-12769). A same-dir
        rebind keeps the set for idempotency. Synchronous by contract, so the clear needs no lock.
        """
        new_output_dir = Path(download_dir)
        dir_changed = self._output_dir is not None and self._output_dir != new_output_dir
        self._output_dir = new_output_dir
        # Clear on the logical scope change, before mkdir: a failing mkdir must not leave the new
        # scope carrying the prior run's identities, where a same-dir retry (dir_changed=False)
        # would never clear them.
        if dir_changed:
            self._downloaded_urls.clear()
            self._reset_download_accounting()
        self._output_dir.mkdir(parents=True, exist_ok=True)
        self._remember_download_directory(self._output_dir)
        LOG.info("CDP download interceptor download dir set", download_dir=download_dir, dir_changed=dir_changed)

    def rebind_download_scope(
        self,
        *,
        download_dir: str,
        redirect_hop_authorizer: RedirectHopAuthorizer[GuardedFileFetchHopResult],
    ) -> None:
        """Rotate a persistent interceptor's directory and redirect-hop authority as one scope."""
        new_output_dir = Path(download_dir)
        new_output_dir.mkdir(parents=True, exist_ok=True)
        self._remember_download_directory(new_output_dir)
        dir_changed = self._output_dir is not None and self._output_dir != new_output_dir
        with self._artifact_scope_lock:
            self._output_dir, self._redirect_hop_authorizer = new_output_dir, redirect_hop_authorizer
            self._download_scope = (
                redirect_hop_authorizer.download_scope
                if isinstance(redirect_hop_authorizer, RunScopedRedirectHopAuthorizer)
                else None
            )
            self._artifact_scope_generation += 1
            self._artifact_scope_valid = True
            if dir_changed:
                self._downloaded_urls.clear()
            self._reset_download_accounting()
            if self._browser_session is not None and self._browser_download_listener is not None:
                self._accepting_browser_downloads = True
        LOG.info("CDP download interceptor scope rebound", download_dir=download_dir, dir_changed=dir_changed)

    def invalidate_download_scope(self) -> None:
        """Revoke run authority after an interrupted/failed persistent-session rebind."""
        with self._artifact_scope_lock:
            self._redirect_hop_authorizer = deny_unenrolled_redirect_hop
            self._download_scope = None
            self._artifact_scope_generation += 1
            self._artifact_scope_valid = False
            self._accepting_browser_downloads = False
        for task in tuple(self._browser_download_tasks):
            task.cancel()
        LOG.error("CDP download interceptor run scope invalidated")

    def _artifact_scope_is_active(self, generation: int) -> bool:
        return self._artifact_scope_valid and generation == self._artifact_scope_generation

    @property
    def download_scope(self) -> str | None:
        """Return the currently enrolled run scope for ownership-bound consumers."""
        with self._artifact_scope_lock:
            return self._download_scope

    def is_monitoring_browser_downloads(self) -> bool:
        """True while the monitor owns the context's setDownloadBehavior binding ({deny, eventsEnabled:True},
        saving over HTTP), so re-sending allow/downloadPath would disable it on remote CDP."""
        return self._browser_session is not None

    def _resolve_save_path(self, filename: str = "", content_type: str = "") -> tuple[Path, str]:
        """Generate a unique save path under _output_dir.

        Rejects non-basename filenames, falls back to a UUID-based
        name when empty, increments _download_index, and logs a warning if a file with
        the same name already exists. Returns (save_path, validated_filename).

        Callers can pass a raw or empty filename — this method handles all normalization.
        """
        assert self._output_dir is not None
        self._output_dir.mkdir(parents=True, exist_ok=True)
        self._remember_download_directory(self._output_dir)

        self._download_index += 1
        filename = _validated_download_basename(filename, content_type)
        if not filename:
            filename = f"download_{uuid.uuid4().hex[:8]}{_download_extension_for_content_type(content_type)}"

        # download_suffix is NOT applied here: this runs inside CDP callbacks that don't carry the
        # step's SkyvernContext, so the suffix could be stale. Run-dir files are renamed to
        # download_suffix by _finalize_downloaded_files_for_task instead.
        save_path = self._output_dir / filename
        # TODO: implement proper filename dedup (e.g., content hash or UUID suffix)
        if save_path.exists():
            LOG.warning(
                "Download filename collision; write will fail closed",
                filename_fp=diagnostic_fingerprint(filename),
                save_path_fp=diagnostic_fingerprint(str(save_path)),
            )

        return save_path, filename

    async def enable_for_page(self, page: Page) -> None:
        """Create a CDP session for the given page and enable Fetch interception.

        When proxy credentials are configured, also enables Fetch.authRequired handling
        at the page level — matching Playwright's internal approach (CRNetworkManager).
        Playwright uses Request-stage interception with handleAuthRequests to receive
        proxy 407 challenges via Fetch.authRequired.
        """
        if any(candidate is page for candidate in self._enrolling_pages) or any(
            registered_page is page for registered_page, _ in self._active_request_interceptors.values()
        ):
            raise RuntimeError("CDP interception is already active or enrolling for this page")
        self._enrolling_pages.append(page)
        try:
            await self._enable_for_page(page)
        finally:
            self._enrolling_pages = [candidate for candidate in self._enrolling_pages if candidate is not page]

    async def _enable_for_page(self, page: Page) -> None:
        self._accepting_cdp_handlers = True
        cdp_session = await page.context.new_cdp_session(page)
        cdp_session.on("Fetch.requestPaused", lambda event: self._on_request_paused(event, cdp_session))

        has_proxy_auth = bool(self._proxy_username and self._proxy_password)

        if has_proxy_auth:
            cdp_session.on("Fetch.authRequired", lambda event: self._on_auth_required(event, cdp_session))

        # Request-stage interception is the last boundary before browser credentials go on the
        # wire. It is mandatory even without proxy auth; the active request must consume the
        # monitor's exact one-shot slot before Fetch.continueRequest can dispatch it.
        patterns: list[dict[str, str]] = [
            {"requestStage": "Response"},
            {"urlPattern": "*", "requestStage": "Request"},
        ]

        try:
            await cdp_session.send(
                "Fetch.enable",
                {
                    "patterns": patterns,
                    "handleAuthRequests": has_proxy_auth,
                },
            )
            self._network_egress_monitor.register_active_request_interceptor(page=page, owner=self)

            def close_listener(closed_page: Page) -> None:
                # This listener is bound to `page`, so a close event carrying any other page means
                # the registration is misrouted and the monitor can no longer be trusted to describe
                # this page's egress. Normal self-close takes the plain unregister path.
                if closed_page is not page:
                    self._network_egress_monitor.invalidate()
                self._unregister_active_request_interceptor(cdp_session)

            self._active_request_interceptors[cdp_session] = (page, close_listener)
            if page.is_closed() is True:
                raise RuntimeError("page closed during interceptor registration")
            page.on("close", close_listener)
        except BaseException:
            self._accepting_cdp_handlers = False
            if cdp_session in self._active_request_interceptors:
                self._unregister_active_request_interceptor(cdp_session)
            self._network_egress_monitor.invalidate()
            try:
                await cdp_session.send("Fetch.disable")
            except Exception:
                pass
            raise
        self._cdp_sessions.append(cdp_session)
        self._enabled = True
        LOG.info(
            "CDP Fetch interception enabled for page",
            page_url=page.url,
            session_count=len(self._cdp_sessions),
            output_dir=str(self._output_dir),
            proxy_auth_enabled=has_proxy_auth,
        )

    def _unregister_active_request_interceptor(self, cdp_session: CDPSession) -> None:
        registration = self._active_request_interceptors.pop(cdp_session, None)
        if registration is None:
            return
        page, close_listener = registration
        try:
            page.remove_listener("close", close_listener)
        except Exception:
            pass
        try:
            self._network_egress_monitor.unregister_active_request_interceptor(page=page, owner=self)
        except Exception as error:
            self._network_egress_monitor.invalidate()
            LOG.error("Failed to unregister active request interceptor", error_type=type(error).__name__)

    async def enable_browser_download_monitor(self, browser: Browser, browser_context: BrowserContext) -> None:
        """Monitor browser-initiated downloads and save them directly via HTTP.

        Many sites trigger downloads via mechanisms that bypass CDP Fetch
        (e.g., new tab for signed URL, <a download>, blob URLs). The browser's
        download manager handles these directly — no page-level network request occurs.

        This method uses Browser-level CDP events to detect such downloads,
        then downloads the file directly via HTTP using the BrowserContext's
        APIRequestContext (which shares cookies and outlives individual pages).
        """
        async with self._browser_download_monitor_lock:
            if self._browser_session is not None:
                LOG.warning("Browser download monitor already enabled, skipping")
                return

            browser_session = await browser.new_browser_cdp_session()
            try:
                # Deny browser-native downloads — we download files ourselves via HTTP.
                # Using "deny" instead of "allowAndName" avoids needing a downloadPath, which is
                # critical for remote CDP browsers: downloadPath is interpreted on the browser's
                # filesystem, not the client's, so a local tempdir path would be invalid.
                # Browser.downloadWillBegin events still fire with eventsEnabled=True, giving us
                # the URL to download directly.
                await browser_session.send(
                    "Browser.setDownloadBehavior",
                    {"behavior": "deny", "eventsEnabled": True},
                )
            except BaseException:
                try:
                    await browser_session.detach()
                except Exception:
                    pass
                raise

            def download_listener(event: dict[str, Any]) -> None:
                self._schedule_browser_download_handler(event)

            browser_session.on("Browser.downloadWillBegin", download_listener)
            self._browser_session = browser_session
            self._browser_context = browser_context
            self._browser_download_listener = download_listener
            self._accepting_browser_downloads = True
            LOG.info("Browser download monitor enabled")

    async def bind_to_context(
        self,
        browser_context: BrowserContext,
        *,
        enable_page_interception: bool = True,
    ) -> None:
        """Bind ownership to a context, optionally enrolling page-level Fetch.

        Adopted persistent sessions use ownership-only mode: this keeps browser-download recovery
        and run-scope rebind available without displacing the browser's provider-owned proxy auth.
        """
        bind_lock = getattr(browser_context, "_skyvern_cdp_download_interceptor_bind_lock", None)
        if not isinstance(bind_lock, asyncio.Lock):
            bind_lock = asyncio.Lock()
            browser_context._skyvern_cdp_download_interceptor_bind_lock = bind_lock  # type: ignore[attr-defined]

        async with bind_lock:
            existing: CDPDownloadInterceptor | None = getattr(
                browser_context, "_skyvern_cdp_download_interceptor", None
            )
            if existing is self and self._page_context is browser_context:
                current_page_interception = self._page_listener is not None
                if current_page_interception == enable_page_interception:
                    return
                raise RuntimeError("CDP download interceptor binding mode cannot change in place")
            if existing is not None and existing is not self:
                try:
                    await self._disable_for_rebind(existing)
                except BaseException:
                    await self._disable_after_failed_bind()
                    raise

            self._page_context = browser_context
            if enable_page_interception:

                def page_listener(page: Page) -> None:
                    if not self._accepting_pages:
                        return
                    task = asyncio.create_task(self.enable_for_page(page))
                    self._page_enable_tasks.add(task)
                    task.add_done_callback(self._page_enable_done)

                self._page_listener = page_listener
                self._accepting_pages = True
                browser_context.on("page", page_listener)
            else:
                self._page_listener = None
                self._accepting_pages = False
            browser_context._skyvern_cdp_download_interceptor = self  # type: ignore[attr-defined]

    async def _disable_for_rebind(self, existing: CDPDownloadInterceptor) -> None:
        task = asyncio.create_task(existing.disable())
        try:
            done, _ = await asyncio.wait({task}, timeout=BROWSER_INTERCEPTOR_DISABLE_TIMEOUT)
        except BaseException:
            task.cancel()
            _own_detached_disable(task)
            raise
        if task in done:
            await task
            return
        LOG.warning(
            "Previous CDP download interceptor disable exceeded rebind budget",
            timeout=BROWSER_INTERCEPTOR_DISABLE_TIMEOUT,
        )
        task.cancel()
        _own_detached_disable(task)
        raise TimeoutError("Previous CDP download interceptor teardown did not complete")

    async def _disable_after_failed_bind(self) -> None:
        """Tear down a pre-enabled replacement before exposing a rebind failure."""
        task = asyncio.create_task(self.disable())
        try:
            done, _ = await asyncio.wait({task}, timeout=BROWSER_INTERCEPTOR_DISABLE_TIMEOUT)
        except BaseException:
            task.cancel()
            _own_detached_task(task, event="Replacement CDP download interceptor disable failed after detach")
            raise
        if task in done:
            try:
                await task
            except Exception as error:
                LOG.warning("Replacement CDP download interceptor disable failed", error_type=type(error).__name__)
            return
        LOG.warning(
            "Replacement CDP download interceptor disable exceeded rebind budget; detaching",
            timeout=BROWSER_INTERCEPTOR_DISABLE_TIMEOUT,
        )
        task.cancel()
        _own_detached_task(task, event="Replacement CDP download interceptor disable failed after detach")

    def _page_enable_done(self, task: asyncio.Task[None]) -> None:
        self._page_enable_tasks.discard(task)
        if task.cancelled():
            return
        error = task.exception()
        if error is not None:
            LOG.warning("Failed to enable CDP interception for page", error_type=type(error).__name__)

    def _cdp_handler_done(self, task: asyncio.Task[None]) -> None:
        self._cdp_handler_tasks.discard(task)
        if task.cancelled():
            return
        error = task.exception()
        if error is not None:
            LOG.warning("CDP interception handler failed", error_type=type(error).__name__)

    def _browser_download_done(self, task: asyncio.Task[None]) -> None:
        event_bytes = self._browser_download_task_event_bytes.pop(task, 0)
        self._pending_browser_download_event_bytes = max(
            0,
            self._pending_browser_download_event_bytes - event_bytes,
        )
        self._browser_download_tasks.discard(task)
        if task.cancelled():
            return
        error = task.exception()
        if error is not None:
            LOG.warning("Browser download handler failed", error_type=type(error).__name__)

    @staticmethod
    async def _drain_tasks(tasks: set[asyncio.Task[None]]) -> None:
        snapshot = tuple(tasks)
        if not snapshot:
            return
        try:
            await asyncio.wait(snapshot)
        except BaseException:
            for task in snapshot:
                task.cancel()
            await asyncio.gather(*snapshot, return_exceptions=True)
            raise

    @staticmethod
    async def _detach_sessions(sessions: tuple[CDPSession, ...], *, phase: str) -> asyncio.CancelledError | None:
        """Detach every session without letting one stalled target block later teardown."""
        tasks = tuple(asyncio.create_task(session.detach()) for session in sessions)
        if not tasks:
            return None
        try:
            done, pending = await asyncio.wait(tasks, timeout=CDP_SESSION_DETACH_TIMEOUT_SECONDS)
        except asyncio.CancelledError as caught_cancellation:
            for task in tasks:
                if task.done():
                    if not task.cancelled():
                        task.exception()
                    continue
                task.cancel()
                _own_detached_task(
                    task,
                    event="CDP download interceptor teardown phase failed after detach",
                    phase=phase,
                )
            return caught_cancellation

        if pending:
            LOG.warning(
                "CDP download interceptor teardown phase exceeded its budget; detaching",
                phase=phase,
                timeout=CDP_SESSION_DETACH_TIMEOUT_SECONDS,
            )
            for task in pending:
                task.cancel()
                _own_detached_task(
                    task,
                    event="CDP download interceptor teardown phase failed after detach",
                    phase=phase,
                )

        detach_cancellation: asyncio.CancelledError | None = None
        for task in done:
            if task.cancelled():
                detach_cancellation = detach_cancellation or asyncio.CancelledError()
            else:
                task.exception()  # Detach failures are best-effort, but must be retrieved.
        return detach_cancellation

    async def _handle_browser_download(self, event: dict[str, Any]) -> None:
        artifact_scope_generation = event.get(_ARTIFACT_SCOPE_GENERATION_EVENT_KEY)
        if not isinstance(artifact_scope_generation, int):
            artifact_scope_generation = self._artifact_scope_generation
        async with self._browser_download_processing_lock:
            await self._handle_browser_download_serialized(event, artifact_scope_generation)

    async def _handle_browser_download_serialized(
        self,
        event: dict[str, Any],
        artifact_scope_generation: int,
    ) -> None:
        """Handle Browser.downloadWillBegin — save the file from its URL."""
        is_data_url = False
        attempt: _DownloadAttempt | None = None
        try:
            url = event.get("url", "")
            suggested_filename = event.get("suggestedFilename", "")
            LOG.info(
                "Browser download detected",
                url_scheme=urlparse(url).scheme.lower(),
                suggested_filename_fp=diagnostic_fingerprint(suggested_filename),
            )
            if not url:
                LOG.warning("Empty download URL, skipping")
                self._reject_download_attempt("capture_failed")
                return

            is_data_url = url.lower().startswith("data:")
            if not is_data_url and url in self._downloaded_urls:
                LOG.debug("URL already captured via Fetch, skipping direct download")
                return

            attempt = self._new_download_attempt()
            if attempt is None:
                return
            self._browser_download_attempt = attempt
            if is_data_url:
                await self._download_data_url(url, suggested_filename, artifact_scope_generation)
                return

            if url.startswith("blob:"):
                # blob: URLs are in-memory browser references — not fetchable over HTTP. When the
                # page builds the file client-side (e.g. Blob + createObjectURL), the CDP Fetch
                # path never sees a network response, so read the bytes back from a same-origin
                # page instead of dropping the download.
                await self._download_blob_url(url, suggested_filename, artifact_scope_generation)
            elif url.startswith("http"):
                await self._download_url_directly(url, suggested_filename, artifact_scope_generation)
            else:
                LOG.warning("Download URL scheme not supported, skipping", scheme=urlparse(url).scheme)
                self._record_download_failure(attempt, "unsupported_scheme")
        except asyncio.CancelledError:
            if attempt is not None:
                self._record_download_failure(attempt, "capture_failed")
            raise
        except Exception as exc:
            if attempt is not None:
                self._record_download_failure(attempt, "capture_failed")
            if is_data_url:
                LOG.warning("Error handling data URL download event", error_type=type(exc).__name__)
            else:
                LOG.warning("Error handling browser download event", error_type=type(exc).__name__)
        finally:
            self._browser_download_attempt = None

    async def _run_data_worker(self, function: Any, *args: Any) -> tuple[Any, bool]:
        worker = asyncio.create_task(asyncio.to_thread(function, *args))
        try:
            return await asyncio.shield(worker), False
        except asyncio.CancelledError:
            try:
                return await worker, True
            except BaseException as worker_error:
                raise asyncio.CancelledError from worker_error

    async def _download_data_url(
        self,
        url: str,
        suggested_filename: str,
        artifact_scope_generation: int | None = None,
    ) -> bool:
        attempt = self._browser_download_attempt
        if attempt is None:
            attempt = self._new_download_attempt()
            if attempt is None:
                return False
        if not self._output_dir:
            LOG.warning("No output_dir set, skipping data URL download")
            self._record_download_failure(attempt, "capture_failed")
            return False

        try:
            comma_index = _bounded_data_url_comma(url)
            decoded, cancelled = await self._run_data_worker(self._decode_data_url, url, comma_index)
            download_identity, content_type, data = decoded
            if cancelled:
                raise asyncio.CancelledError
            if download_identity in self._downloaded_urls:
                LOG.debug("Data URL already captured, skipping", identity=download_identity)
                self._release_download_file_slot(attempt)
                return False
            save_path, filename = self._resolve_save_path(suggested_filename, content_type)
            if not self._reserve_download_bytes(attempt, len(data)):
                return False
            if artifact_scope_generation is None:
                artifact_scope_generation = self._artifact_scope_generation
            _, cancelled = await self._run_data_worker(
                self._atomically_write_bytes,
                save_path,
                data,
                artifact_scope_generation,
            )
            # A rebind may have cleared the dedupe set and repointed _output_dir while this write
            # was off-loop. The file published into save_path's dir; only record its identity if
            # that dir is still the current scope, else it would skip an identical download in the
            # new run's dir (SKY-12769).
            if save_path.parent == self._output_dir:
                self._downloaded_urls.add(download_identity)
        except (ValueError, binascii.Error, OSError) as exc:
            LOG.warning("Malformed data URL download, skipping", reason=str(exc))
            reason = "file_size_limit" if "size limit" in str(exc) else "capture_failed"
            self._record_download_failure(attempt, reason)
            return False

        LOG.info(
            "CDP download saved (data URL)",
            filename_fp=diagnostic_fingerprint(filename),
            content_type=content_type,
            size=len(data),
            save_path_fp=diagnostic_fingerprint(str(save_path)),
            download_index=self._download_index,
        )
        self._record_download_saved(attempt)
        if cancelled:
            raise asyncio.CancelledError
        return True

    def _decode_data_url(self, url: str, comma_index: int) -> tuple[str, str, bytes | bytearray]:
        content_type, is_base64 = _parse_data_url_metadata(url, comma_index)
        payload_start = comma_index + 1
        max_encoded_size = 4 * ((MAX_FILE_SIZE_BYTES + 2) // 3) if is_base64 else MAX_FILE_SIZE_BYTES
        decoded_length = _percent_decoded_payload_length(url, payload_start, max_encoded_size)
        percent_decoded = _percent_decode_payload(url, payload_start, decoded_length)
        data: bytes | bytearray
        if is_base64:
            data = base64.b64decode(percent_decoded, validate=True)
        else:
            data = percent_decoded

        if not data:
            raise ValueError("empty decoded payload")
        if len(data) > MAX_FILE_SIZE_BYTES:
            raise ValueError("decoded payload exceeds size limit")

        return _download_identity(url), content_type, data

    @staticmethod
    def _open_download_directory(directory: Path) -> int:
        required_flags = ("O_DIRECTORY", "O_NOFOLLOW")
        if any(not hasattr(os, flag) for flag in required_flags):
            raise OSError(errno.ENOTSUP, "confined download writes are unsupported")
        flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
        flags |= getattr(os, "O_CLOEXEC", 0)
        return os.open(directory, flags)

    def _remember_download_directory(self, directory: Path) -> None:
        directory_fd = self._open_download_directory(directory)
        try:
            directory_stat = os.fstat(directory_fd)
            identity = (directory_stat.st_dev, directory_stat.st_ino)
            expected_identity = self._download_directory_identities.setdefault(directory, identity)
            if identity != expected_identity:
                raise OSError(errno.ESTALE, "download directory changed after binding")
        finally:
            os.close(directory_fd)

    def _open_confined_temporary_file(self, save_path: Path) -> _ConfinedTemporaryFile:
        expected_identity = self._download_directory_identities.get(save_path.parent)
        if expected_identity is None:
            raise OSError(errno.ESTALE, "download directory is not bound")

        directory_fd = self._open_download_directory(save_path.parent)
        try:
            directory_stat = os.fstat(directory_fd)
            if (directory_stat.st_dev, directory_stat.st_ino) != expected_identity:
                raise OSError(errno.ESTALE, "download directory changed after binding")

            temporary_name = f"{save_path.name}.{uuid.uuid4().hex}{BROWSER_DOWNLOADING_SUFFIX}"
            flags = os.O_RDWR | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW
            flags |= getattr(os, "O_CLOEXEC", 0)
            file_descriptor = os.open(temporary_name, flags, 0o600, dir_fd=directory_fd)
            try:
                handle = os.fdopen(file_descriptor, "w+b")
            except BaseException:
                os.close(file_descriptor)
                os.unlink(temporary_name, dir_fd=directory_fd)
                raise
        except BaseException:
            os.close(directory_fd)
            raise
        return _ConfinedTemporaryFile(
            directory_fd=directory_fd,
            name=temporary_name,
            handle=handle,
        )

    @staticmethod
    def _publish_confined_temporary_file(temporary_file: _ConfinedTemporaryFile, filename: str) -> None:
        temporary_file.handle.flush()
        source_stat = os.fstat(temporary_file.handle.fileno())
        if not stat.S_ISREG(source_stat.st_mode) or source_stat.st_nlink != 1:
            raise OSError(errno.EPERM, "download temporary file must be a regular single-link file")
        os.link(
            temporary_file.name,
            filename,
            src_dir_fd=temporary_file.directory_fd,
            dst_dir_fd=temporary_file.directory_fd,
            follow_symlinks=False,
        )

        try:
            # The pre-link nlink check is not atomic with os.link, so re-read the source inode:
            # the temp name plus the name we just published are the only two links that may exist.
            # A third means someone aliased the inode inside that window.
            published_stat = os.fstat(temporary_file.handle.fileno())
            if published_stat.st_nlink != 2:
                raise OSError(errno.EPERM, "download temporary file gained an unexpected link during publication")

            destination_flags = os.O_RDONLY | os.O_NONBLOCK | os.O_NOFOLLOW
            destination_flags |= getattr(os, "O_CLOEXEC", 0)
            destination_fd = os.open(filename, destination_flags, dir_fd=temporary_file.directory_fd)
            with os.fdopen(destination_fd, "rb") as destination:
                destination_stat = os.fstat(destination.fileno())
                if not stat.S_ISREG(destination_stat.st_mode) or (
                    destination_stat.st_dev,
                    destination_stat.st_ino,
                ) != (source_stat.st_dev, source_stat.st_ino):
                    raise OSError(errno.ESTALE, "download destination changed during publication")
        except BaseException:
            # Rolls back the link created above. os.link refuses collisions, so this name did not
            # exist before this call; unlinking it assumes the confined directory grants no other
            # writer the right to claim `filename` mid-publication.
            os.unlink(filename, dir_fd=temporary_file.directory_fd)
            raise

    @staticmethod
    def _cleanup_confined_temporary_file(temporary_file: _ConfinedTemporaryFile) -> None:
        try:
            temporary_file.handle.close()
        finally:
            try:
                os.unlink(temporary_file.name, dir_fd=temporary_file.directory_fd)
            finally:
                os.close(temporary_file.directory_fd)

    def _atomically_write_bytes(
        self,
        save_path: Path,
        data: bytes | bytearray,
        artifact_scope_generation: int | None = None,
    ) -> None:
        temporary_file = self._open_confined_temporary_file(save_path)
        try:
            temporary_file.handle.write(data)
            if artifact_scope_generation is None:
                self._publish_confined_temporary_file(temporary_file, save_path.name)
            else:
                with self._artifact_scope_lock:
                    if not self._artifact_scope_is_active(artifact_scope_generation):
                        raise _DownloadScopeInvalidated
                    self._publish_confined_temporary_file(temporary_file, save_path.name)
        finally:
            self._cleanup_confined_temporary_file(temporary_file)

    async def _download_url_directly(
        self,
        url: str,
        suggested_filename: str,
        artifact_scope_generation: int | None = None,
    ) -> None:
        """Download through the validated, pinned, per-hop-authorized shared fetch seam.

        This seam is the only transport; there is deliberately no raw fallback, so a fetch that the
        guard rejects (or that fails for any other reason) drops the download rather than retrying
        it unmediated.
        """
        attempt = self._browser_download_attempt
        if attempt is None:
            attempt = self._new_download_attempt()
            if attempt is None:
                return
        if not self._output_dir:
            LOG.warning("No output_dir set, skipping direct download")
            self._record_download_failure(attempt, "capture_failed")
            return
        if self._browser_context is None:
            LOG.error("Browser download context is unenrolled")
            self._record_download_failure(attempt, "capture_failed")
            return
        if is_unenrolled_redirect_hop_authorizer(self._redirect_hop_authorizer):
            LOG.error("Redirect hop authorization is unenrolled for this browser session, dropping direct download")
            self._record_download_failure(attempt, "capture_failed")
            return

        remaining_run_bytes = MAX_RUN_DOWNLOAD_BYTES - self._run_download_bytes
        direct_fetch_limit_bytes = min(MAX_FILE_SIZE_BYTES, remaining_run_bytes)
        direct_fetch_limit_mb = direct_fetch_limit_bytes // (1024 * 1024)
        direct_fetch_limit_reason = (
            "run_size_limit" if remaining_run_bytes <= MAX_FILE_SIZE_BYTES else "file_size_limit"
        )
        if direct_fetch_limit_mb < 1:
            self._record_download_failure(attempt, direct_fetch_limit_reason)
            return

        t0 = time.monotonic()
        try:
            validated_filename = _validated_download_basename(suggested_filename)
            cookie_header = await self._cookie_header_for_url(url)
            headers = {"Cookie": cookie_header} if cookie_header else None
            response = await file_api.fetch_file_bytes(  # type: ignore[attr-defined]
                url,
                max_size_mb=direct_fetch_limit_mb,
                headers=headers,
                filename=validated_filename or None,
                authorize_request_hop=self._redirect_hop_authorizer,
                download_scope=self._download_scope,
                approved_initial_url=url,
            )
        except Exception as exc:
            # The download URL is credential-bearing and can reappear inside an exception message
            # (aiohttp embeds it), so the raise site stands in for the message and traceback.
            LOG.error(
                "Guarded direct download failed",
                error_type=type(exc).__name__,
                error_origin=redacted_exception_origin(exc),
            )
            reason = (
                direct_fetch_limit_reason if type(exc).__name__ == "DownloadFileMaxSizeExceeded" else "capture_failed"
            )
            self._record_download_failure(attempt, reason)
            return

        data = response.body
        content_type = response.content_type
        response_filename = response.filename
        if not isinstance(data, bytes) or not isinstance(content_type, str) or not isinstance(response_filename, str):
            LOG.error("Guarded direct download returned an invalid response")
            self._record_download_failure(attempt, "capture_failed")
            return
        if not self._reserve_download_bytes(attempt, len(data)):
            return

        normalized_filename = normalize_download_filename(response_filename, content_type)
        if _payload_is_html_login_masquerade(data, content_type, normalized_filename):
            LOG.error(
                "Direct download returned an HTML page for a non-HTML file; not saving "
                "(likely an unauthenticated fetch landing on a login/session-gate page)",
                suggested_filename_fp=diagnostic_fingerprint(normalized_filename),
                content_type=content_type,
                size=len(data),
            )
            self._record_download_failure(attempt, "capture_failed")
            return

        save_path, filename = self._resolve_save_path(response_filename, content_type)

        if artifact_scope_generation is None:
            artifact_scope_generation = self._artifact_scope_generation
        self._atomically_write_bytes(save_path, data, artifact_scope_generation)

        elapsed_ms = (time.monotonic() - t0) * 1000
        LOG.info(
            "CDP download saved (direct HTTP)",
            filename_fp=diagnostic_fingerprint(filename),
            size=len(data),
            duration_ms=round(elapsed_ms, 1),
            save_path_fp=diagnostic_fingerprint(str(save_path)),
            download_index=self._download_index,
            method="guarded_http",
        )
        self._record_download_saved(attempt)

    async def _cookie_header_for_url(self, url: str) -> str:
        if self._browser_context is None:
            return ""
        try:
            cookies = await self._browser_context.cookies(url)
        except Exception:
            return ""
        parts: list[str] = []
        for cookie in cookies:
            name = cookie.get("name")
            value = cookie.get("value")
            if (
                isinstance(name, str)
                and name
                and isinstance(value, str)
                and not _FILENAME_CONTROL_CHAR_RE.search(name)
                and not _FILENAME_CONTROL_CHAR_RE.search(value)
            ):
                parts.append(f"{name}={value}")
        return "; ".join(parts)

    async def _download_blob_url(
        self,
        url: str,
        suggested_filename: str,
        artifact_scope_generation: int | None = None,
    ) -> None:
        """Save a blob: URL download by reading its bytes back from a same-origin page.

        blob: URLs are in-memory references owned by the document that created them, so they
        can't be fetched over HTTP. ``SkyvernFrame.read_blob_url_bytes`` runs the shared blob
        read-back script inside a same-origin frame. Best-effort: a page may revoke the object
        URL before we read it.
        """
        attempt = self._browser_download_attempt
        if attempt is None:
            attempt = self._new_download_attempt()
            if attempt is None:
                return
        if not self._output_dir or self._browser_context is None:
            LOG.warning(
                "Cannot read blob download: no output dir or browser context",
                url_scheme=urlparse(url).scheme.lower(),
                suggested_filename_fp=diagnostic_fingerprint(suggested_filename),
            )
            self._record_download_failure(attempt, "capture_failed")
            return

        # probe=True: this fans out over every open page as a best-effort fallback, so the
        # shared reader must not emit ERROR logs for pages that don't own the blob's origin.
        data: bytes | None = None
        for page in list(self._browser_context.pages):
            data = await SkyvernFrame.read_blob_url_bytes(
                page=page, blob_url=url, max_size_bytes=MAX_FILE_SIZE_BYTES, probe=True
            )
            if data is not None:
                break

        if data is None:
            LOG.warning(
                "Could not read blob download from any page",
                url_scheme=urlparse(url).scheme.lower(),
                suggested_filename_fp=diagnostic_fingerprint(suggested_filename),
            )
            self._record_download_failure(attempt, "capture_failed")
            return
        # An empty blob must never be persisted: _resolve_save_path overwrites on filename collision, so
        # a 0-byte blob re-emitted with the same name as a just-captured download (which the large-response
        # empty-body fulfill can trigger) would truncate the real artifact to zero bytes (SKY-12642).
        if not data:
            LOG.warning(
                "Blob download is empty, skipping to avoid clobbering a captured artifact",
                url_scheme=urlparse(url).scheme.lower(),
                suggested_filename_fp=diagnostic_fingerprint(suggested_filename),
            )
            self._record_download_failure(attempt, "capture_failed")
            return
        # Defense-in-depth: read_blob_url_bytes already rejects oversized blobs in-page before
        # serialization, but guard again in case a caller passes no limit.
        if not self._reserve_download_bytes(attempt, len(data)):
            return
        save_path, filename = self._resolve_save_path(suggested_filename)
        if artifact_scope_generation is None:
            artifact_scope_generation = self._artifact_scope_generation
        self._atomically_write_bytes(save_path, data, artifact_scope_generation)
        self._downloaded_urls.add(url)
        LOG.info(
            "CDP download saved (blob)",
            filename_fp=diagnostic_fingerprint(filename),
            size=len(data),
            save_path_fp=diagnostic_fingerprint(str(save_path)),
            download_index=self._download_index,
        )
        self._record_download_saved(attempt)

    async def disable(self) -> None:
        """Disable Fetch interception on all CDP sessions and clean up browser monitor."""
        self._accepting_pages = False
        page_context = self._page_context
        page_listener = self._page_listener
        if page_context is not None and page_listener is not None:
            try:
                page_context.remove_listener("page", page_listener)
            except Exception:
                pass
        self._page_listener = None
        self._accepting_cdp_handlers = False
        try:
            await self._drain_tasks(self._page_enable_tasks)
            await self._drain_tasks(self._cdp_handler_tasks)
        finally:
            sessions = tuple(self._cdp_sessions)
            for cdp_session in sessions:
                self._unregister_active_request_interceptor(cdp_session)
            for cdp_session in sessions:
                try:
                    await cdp_session.send("Fetch.disable")
                except Exception as error:
                    if _is_stale_interception_error(error):
                        # Normal teardown usually finds the target already closed/detached
                        # (SKY-11964); that benign race carries no live interception to leave
                        # untracked, so it doesn't warrant invalidating the monitor.
                        LOG.debug(
                            "CDP Fetch interception was already stale at disable (benign race)",
                            error_type=type(error).__name__,
                        )
                        continue
                    # Interception may still be live on a session we have already unregistered,
                    # which would leave requests egressing untracked. Mirror the enable path.
                    self._network_egress_monitor.invalidate()
                    LOG.warning("Failed to disable CDP Fetch interception", error_type=type(error).__name__)
            detach_cancellation = await self._detach_sessions(sessions, phase="page CDP session detach")
            self._cdp_sessions.clear()
        session_count = len(sessions)

        async with self._browser_download_monitor_lock:
            self._accepting_browser_downloads = False
            browser_session = self._browser_session
            listener = self._browser_download_listener
            if browser_session is not None and listener is not None:
                remove_listener = getattr(browser_session, "remove_listener", None)
                if remove_listener is not None:
                    try:
                        remove_listener("Browser.downloadWillBegin", listener)
                    except Exception:
                        pass
            self._browser_download_listener = None

            await self._drain_tasks(self._browser_download_tasks)

            if browser_session is not None:
                browser_detach_cancellation = await self._detach_sessions(
                    (browser_session,), phase="browser CDP session detach"
                )
                if detach_cancellation is None:
                    detach_cancellation = browser_detach_cancellation
            self._browser_session = None
            self._browser_context = None

        if page_context is not None and getattr(page_context, "_skyvern_cdp_download_interceptor", None) is self:
            page_context._skyvern_cdp_download_interceptor = None  # type: ignore[attr-defined]
        self._page_context = None

        self._enabled = False
        LOG.info(
            "CDP Fetch interception disabled",
            session_count=session_count,
            downloads_intercepted=self._download_index,
        )
        if detach_cancellation is not None:
            raise detach_cancellation

    @asynccontextmanager
    async def settle_browser_downloads(self) -> AsyncIterator[None]:
        """Drain browser downloads to a stable admission snapshot before artifact collection."""
        async with self._browser_download_monitor_lock:
            await self._drain_browser_downloads_to_quiescence()
            try:
                yield
            except BaseException:
                tasks = tuple(self._browser_download_tasks)
                for task in tasks:
                    task.cancel()
                if tasks:
                    await asyncio.gather(*tasks, return_exceptions=True)
                raise
            else:
                await self._drain_browser_downloads_to_quiescence(admit_browser_events=True)

    async def _drain_browser_downloads_to_quiescence(self, *, admit_browser_events: bool = False) -> None:
        if admit_browser_events and self._browser_session is not None and self._accepting_browser_downloads:
            await asyncio.sleep(BROWSER_DOWNLOAD_EVENT_ADMISSION_GRACE_SECONDS)
        while True:
            generation = self._browser_download_generation
            cdp_generation = self._cdp_handler_generation
            if self._browser_download_tasks:
                await asyncio.gather(*tuple(self._browser_download_tasks), return_exceptions=True)
            # A download capture runs inside a _cdp_handler_task, and a second Fetch.requestPaused may be
            # scheduled but not yet classified as a download (no .crdownload yet → invisible to the
            # file-snapshot waiter). Wait for in-flight/scheduled handlers too, or collection could finalize
            # before a queued/just-scheduled capture lands and omit it. Use a NON-cancelling wait: a
            # settle-timeout cancellation here must never cancel a handler task — gather would cancel its
            # children and destroy an in-flight capture's temp file. Only teardown (which follows with
            # Fetch.disable) may cancel handlers. The per-call send/read/close timeouts keep a dead handler
            # from making this hang.
            cdp_snapshot = tuple(self._cdp_handler_tasks)
            if cdp_snapshot:
                await asyncio.wait(cdp_snapshot)
            await asyncio.sleep(0)
            if (
                generation == self._browser_download_generation
                and cdp_generation == self._cdp_handler_generation
                and not self._browser_download_tasks
                and not self._cdp_handler_tasks
            ):
                return

    def _schedule_browser_download_handler(self, event: dict[str, Any]) -> None:
        if not self._accepting_browser_downloads:
            return
        event_url = event.get("url", "")
        event_bytes = len(event_url) if isinstance(event_url, str) else 0
        if (
            event_bytes > MAX_PENDING_BROWSER_DOWNLOAD_EVENT_BYTES
            or self._pending_browser_download_event_bytes + event_bytes > MAX_PENDING_BROWSER_DOWNLOAD_EVENT_BYTES
        ):
            LOG.warning(
                "Browser download handler queue byte budget exceeded; dropping event",
                pending_event_bytes=self._pending_browser_download_event_bytes,
                event_bytes=event_bytes,
                event_byte_limit=MAX_PENDING_BROWSER_DOWNLOAD_EVENT_BYTES,
            )
            self._reject_download_attempt("queue_limit")
            return
        if len(self._browser_download_tasks) >= MAX_PENDING_BROWSER_DOWNLOAD_TASKS:
            LOG.warning(
                "Browser download handler queue is full; dropping event",
                pending_count=len(self._browser_download_tasks),
                pending_limit=MAX_PENDING_BROWSER_DOWNLOAD_TASKS,
            )
            self._reject_download_attempt("queue_limit")
            return
        self._browser_download_generation += 1
        admitted_event = dict(event)
        admitted_event[_ARTIFACT_SCOPE_GENERATION_EVENT_KEY] = self._artifact_scope_generation
        task = asyncio.create_task(self._handle_browser_download(admitted_event))
        self._browser_download_tasks.add(task)
        self._browser_download_task_event_bytes[task] = event_bytes
        self._pending_browser_download_event_bytes += event_bytes
        task.add_done_callback(self._browser_download_done)

    def _on_request_paused(self, event: dict[str, Any], cdp_session: CDPSession) -> None:
        """Handle Fetch.requestPaused — schedule async handler with the originating session."""
        self._schedule_cdp_handler(self._handle_request_paused(event, cdp_session, self._artifact_scope_generation))

    def _on_auth_required(self, event: dict[str, Any], cdp_session: CDPSession) -> None:
        """Handle Fetch.authRequired — schedule async handler with the originating session."""
        self._schedule_cdp_handler(self._handle_auth_required(event, cdp_session))

    def _schedule_cdp_handler(self, handler: Coroutine[Any, Any, None]) -> None:
        if not self._accepting_cdp_handlers:
            handler.close()
            return
        self._cdp_handler_generation += 1
        task = asyncio.create_task(handler)
        self._cdp_handler_tasks.add(task)
        task.add_done_callback(self._cdp_handler_done)

    async def _handle_auth_required(self, event: dict[str, Any], cdp_session: CDPSession) -> None:
        """Handle proxy 407 auth challenges via CDP Fetch.continueWithAuth.

        Only responds to proxy auth challenges (source == "Proxy") when credentials are available
        and the request hasn't already been retried (to prevent infinite loops when credentials
        are rejected). All other auth challenges are cancelled to prevent hanging.
        """
        try:
            request_id = event["requestId"]
            auth_challenge = event.get("authChallenge", {})
            source = auth_challenge.get("source", "")
            url = event.get("request", {}).get("url", "<unknown>")

            # Defensive: this handler is only registered when credentials are present,
            # but we still check to guard against future refactors.
            attempts = self._auth_attempts.get(request_id, 0)
            if source == "Proxy" and self._proxy_username and self._proxy_password and attempts < 1:
                self._auth_attempts[request_id] = attempts + 1
                LOG.info(
                    "CDP proxy auth challenge received, providing credentials",
                    url=url,
                    origin=auth_challenge.get("origin", ""),
                )
                await cdp_session.send(
                    "Fetch.continueWithAuth",
                    {
                        "requestId": request_id,
                        "authChallengeResponse": {
                            "response": "ProvideCredentials",
                            "username": self._proxy_username,
                            "password": self._proxy_password,
                        },
                    },
                )
            else:
                # Clean up attempt tracking for this request
                self._auth_attempts.pop(request_id, None)
                if attempts >= 1:
                    LOG.warning(
                        "CDP proxy auth credentials rejected, cancelling to prevent retry loop",
                        url=url,
                        source=source,
                        attempts=attempts,
                    )
                else:
                    LOG.warning(
                        "CDP auth challenge received, cancelling (non-proxy or no credentials)",
                        url=url,
                        source=source,
                    )
                await cdp_session.send(
                    "Fetch.continueWithAuth",
                    {
                        "requestId": request_id,
                        "authChallengeResponse": {"response": "CancelAuth"},
                    },
                )
        except Exception as e:
            LOG.error(
                "Error handling CDP auth challenge",
                error=str(e),
                exc_info=True,
            )

    async def _handle_request_paused(
        self,
        event: dict[str, Any],
        cdp_session: CDPSession,
        artifact_scope_generation: int | None = None,
    ) -> None:
        """Async handler for paused requests.

        Handles both Request-stage and Response-stage events:
        - Request stage (no responseStatusCode): authorize before continuing, otherwise abort.
        - Response stage: check for downloads and intercept if needed.
        """
        request_id = event["requestId"]
        response_status = event.get("responseStatusCode")
        url = event.get("request", {}).get("url", "<unknown>")

        try:
            # Request stage: authorize before continueRequest can put browser credentials on the
            # wire. A missing, failed, or negative collaborator is unenrolled and fails closed.
            if response_status is None:
                request = event.get("request", {})
                try:
                    monitor = self._network_egress_monitor
                    authorized = monitor is not None and monitor.authorize_request(
                        method=request.get("method", ""),
                        url=request.get("url", ""),
                        resource_type=event.get("resourceType", "").lower(),
                        frame=event.get("frameId"),
                    )
                except Exception:
                    authorized = False
                if not authorized:
                    await self._fail_request(
                        cdp_session,
                        request_id,
                        error_reason="BlockedByClient",
                    )
                    return
                await cdp_session.send("Fetch.continueRequest", {"requestId": request_id})
                return

            # Response stage: check for downloads
            raw_response_headers = event.get("responseHeaders", [])
            response_headers = _parse_headers(raw_response_headers)
            resource_type = event.get("resourceType", "")

            LOG.debug(
                "CDP Fetch response paused",
                resource_type=resource_type,
                status_code=response_status,
                content_type=response_headers.get("content-type", ""),
                content_disposition=response_headers.get("content-disposition", ""),
            )

            if is_download_response(response_headers, response_status, resource_type):
                LOG.info(
                    "CDP download response detected",
                    resource_type=resource_type,
                    status_code=response_status,
                    content_type=response_headers.get("content-type", ""),
                    content_disposition=response_headers.get("content-disposition", ""),
                )
                await self._handle_download(
                    cdp_session,
                    request_id,
                    url,
                    response_headers,
                    response_status,
                    raw_response_headers,
                    artifact_scope_generation,
                )
            else:
                await self._continue_response(cdp_session, request_id)
        except Exception as e:
            if _is_stale_interception_error(e):
                # The interception was resolved/cancelled or its target detached before we
                # responded (SKY-11964). Retrying continue/fulfill would fail identically, so
                # drop it quietly — real download flows aren't stalled by a request that no
                # longer exists.
                LOG.debug(
                    "CDP interception went stale before response (benign race)",
                    request_id=request_id,
                    error_type=type(e).__name__,
                )
                return
            LOG.error(
                "Error handling CDP request",
                request_id=request_id,
                error_type=type(e).__name__,
            )
            # For Response-stage errors (e.g. download handling failed), try to let the
            # response through so the request doesn't hang indefinitely.
            # Request-stage errors don't need recovery here — either continueRequest already
            # succeeded (and retrying would fail on an already-continued request), or it
            # failed (and retrying the same call won't help).
            if response_status is not None:
                try:
                    await self._continue_response(cdp_session, request_id)
                except Exception:
                    pass

    async def _continue_response(self, cdp_session: CDPSession, request_id: str) -> None:
        """Let a non-download response pass through to the browser."""
        await cdp_session.send("Fetch.continueResponse", {"requestId": request_id})

    async def _handle_download(
        self,
        cdp_session: CDPSession,
        request_id: str,
        url: str,
        headers: dict[str, str],
        response_status: int,
        raw_response_headers: list[dict[str, str]],
        artifact_scope_generation: int | None = None,
    ) -> None:
        """Capture a download to disk and complete the browser side.

        Small responses (< STREAM_TO_DISK_THRESHOLD_BYTES) are buffered and replayed to the browser
        unchanged. Large responses stream straight to a temp file and are fulfilled with an empty body,
        so the browser never materializes a large base64 payload (the OOM in SKY-12642)."""
        if artifact_scope_generation is None:
            artifact_scope_generation = self._artifact_scope_generation
        if not self._artifact_scope_is_active(artifact_scope_generation):
            LOG.warning("CDP download scope changed before capture; passing response through")
            await self._continue_response(cdp_session, request_id)
            return

        # Mark before any cap fast-fail so Browser.downloadWillBegin cannot race to re-fetch the same body.
        self._downloaded_urls.add(url)
        content_length = _parse_content_length(headers)
        attempt = self._new_download_attempt(content_length)
        if attempt is None:
            await self._fail_request(cdp_session, request_id, url=url)
            return
        if not self._output_dir:
            LOG.warning("CDP download intercepted but no output_dir set, passing through")
            self._record_download_failure(attempt, "capture_failed")
            await self._continue_response(cdp_session, request_id)
            return

        content_type = _normalized_content_type(headers.get("content-type", ""))
        raw_filename = extract_filename(headers, url)
        try:
            save_path, filename = self._resolve_save_path(raw_filename, content_type)
        except (OSError, ValueError) as error:
            LOG.error("Unsafe CDP download output path", error_type=type(error).__name__)
            self._downloaded_urls.add(url)
            self._record_download_failure(attempt, "capture_failed")
            await self._fail_request(cdp_session, request_id, url=url)
            return

        LOG.info(
            "CDP download detected",
            filename=filename,
            content_type=content_type,
            content_length=content_length,
        )

        t0 = time.monotonic()
        remaining_run_bytes = max(0, MAX_RUN_DOWNLOAD_BYTES - self._run_download_bytes)
        stream_max_bytes = min(MAX_FILE_SIZE_BYTES, MAX_STREAMED_FILE_SIZE_BYTES, remaining_run_bytes)
        stream_limit_reason = (
            "run_size_limit"
            if remaining_run_bytes < min(MAX_FILE_SIZE_BYTES, MAX_STREAMED_FILE_SIZE_BYTES)
            else "file_size_limit"
        )
        if content_length is not None and content_length > stream_max_bytes:
            self._record_download_failure(attempt, stream_limit_reason)
            await self._fail_request(cdp_session, request_id, filename=filename, url=url)
            return

        # Known-large → stream to disk from the first chunk. Unknown/known-small → buffer, but spill to
        # disk the instant streamed bytes cross the threshold (never assume a missing/lying size is small).
        start_on_disk = content_length is not None and content_length >= STREAM_TO_DISK_THRESHOLD_BYTES

        # Serialize extraction so only one download streams/buffers at a time per interceptor: concurrent
        # captures cannot each write up to the cap and exhaust the worker's ephemeral disk. This capture
        # runs inside a _cdp_handler_task, which settle drains to quiescence — so a queued/scheduled capture
        # is waited on before artifact collection even before it owns a .crdownload. The lock releases on
        # every exit (success, error, abort, cancellation).
        async with self._download_extraction_lock:
            try:
                try:
                    outcome = await self._stream_response_body(
                        cdp_session,
                        request_id,
                        save_path,
                        start_on_disk=start_on_disk,
                        artifact_scope_generation=artifact_scope_generation,
                        max_size_bytes=stream_max_bytes,
                    )
                except _StreamAborted as e:
                    LOG.error(
                        "CDP download exceeds stream cap, aborting",
                        filename=filename,
                        total_bytes=e.total_bytes,
                        cap=e.max_size_bytes,
                    )
                    self._record_download_failure(attempt, stream_limit_reason)
                    await self._fail_request(cdp_session, request_id, filename=filename, url=url)
                    return
                except _StreamStartError as e:
                    # The body was never taken. We do NOT fall back to getResponseBody: it returns the whole
                    # decoded body in one shot, so a lying/understated Content-Length could materialize an
                    # unbounded payload and OOM the process — the exact failure this streaming path removes.
                    # Fail the request instead so nothing is ever materialized.
                    LOG.error(
                        "takeResponseBodyAsStream failed, failing request (no whole-body fallback)",
                        filename=filename,
                        error_type=type(e.__cause__ or e).__name__,
                    )
                    self._record_download_failure(attempt, "capture_failed")
                    await self._fail_request(cdp_session, request_id, filename=filename, url=url)
                    return
            except asyncio.CancelledError:
                # _stream_response_body's finally already removed any temp file. Never perform I/O during
                # cancellation; the following Fetch.disable tears the paused request down.
                self._record_download_failure(attempt, "capture_failed")
                raise
            except Exception as e:
                # The stream was taken and then failed mid-body (body consumed → continueResponse invalid).
                LOG.error(
                    "Failed to extract CDP download",
                    filename=filename,
                    content_type=content_type,
                    content_length=content_length,
                    error_type=type(e).__name__,
                )
                self._record_download_failure(attempt, "capture_failed")
                await self._fail_request(cdp_session, request_id, filename=filename, url=url)
                return

            if not self._reserve_download_bytes(attempt, outcome.total_bytes):
                if outcome.save_path is not None:
                    outcome.save_path.unlink(missing_ok=True)
                await self._fail_request(cdp_session, request_id, filename=filename, url=url)
                return
            try:
                await self._finalize_download(
                    cdp_session,
                    request_id,
                    response_status,
                    raw_response_headers,
                    outcome,
                    save_path,
                    filename,
                    url,
                    t0,
                    artifact_scope_generation,
                    attempt,
                )
            except asyncio.CancelledError:
                self._record_download_failure(attempt, "capture_failed")
                raise

    async def _fulfill_with_body(
        self,
        cdp_session: CDPSession,
        request_id: str,
        response_status: int,
        raw_response_headers: list[dict[str, str]],
        body: bytes,
    ) -> None:
        """Fulfill a request by replaying the original response with the extracted body.

        This allows both server-side capture AND browser-side download to happen.
        """
        await asyncio.wait_for(
            cdp_session.send(
                "Fetch.fulfillRequest",
                {
                    "requestId": request_id,
                    "responseCode": response_status,
                    "responseHeaders": raw_response_headers,
                    "body": base64.b64encode(body).decode(),
                },
            ),
            timeout=STREAM_IO_READ_TIMEOUT_SECONDS,
        )

    async def _fulfill_without_body(
        self,
        cdp_session: CDPSession,
        request_id: str,
        response_status: int,
        raw_response_headers: list[dict[str, str]],
    ) -> None:
        """Fulfill a captured download with an empty body (Content-Length forced to 0).

        The bytes are already on disk and the browser download is denied, so the browser never needs
        them. A stale non-zero Content-Length — or a Content-Encoding/Transfer-Encoding for a body that
        is not there — would make the browser wait for or try to decode bytes that never arrive, so
        those headers are dropped. Content-Disposition is deliberately kept: dropping it turns an empty
        200 on a Document request into a committed navigation to a blank page.
        """
        dropped = {"content-length", "content-encoding", "transfer-encoding", "content-range"}
        response_headers = [h for h in raw_response_headers if h.get("name", "").lower() not in dropped]
        response_headers.append({"name": "Content-Length", "value": "0"})
        await asyncio.wait_for(
            cdp_session.send(
                "Fetch.fulfillRequest",
                {
                    "requestId": request_id,
                    "responseCode": response_status,
                    "responseHeaders": response_headers,
                    "body": "",
                },
            ),
            timeout=STREAM_IO_READ_TIMEOUT_SECONDS,
        )

    async def _fail_request(
        self,
        cdp_session: CDPSession,
        request_id: str,
        *,
        filename: str = "",
        url: str = "",
        error_reason: str = "Aborted",
    ) -> None:
        """Terminate a paused download whose body was taken or is oversized.

        Once the response body stream is taken, Fetch.continueResponse is invalid, so failRequest is the
        sanctioned way to end the request without a hung Fetch or browser materialization. errorReason
        "Aborted" makes an aborted Document navigation silently ignored rather than painting an error page.
        """
        try:
            await asyncio.wait_for(
                cdp_session.send("Fetch.failRequest", {"requestId": request_id, "errorReason": error_reason}),
                timeout=STREAM_IO_READ_TIMEOUT_SECONDS,
            )
        except Exception as e:
            if _is_stale_interception_error(e):
                LOG.debug(
                    "failRequest hit stale interception (benign race)",
                    filename=filename,
                    error_type=type(e).__name__,
                )
            else:
                LOG.warning("failRequest failed", filename=filename, error_type=type(e).__name__)

    async def _finalize_download(
        self,
        cdp_session: CDPSession,
        request_id: str,
        response_status: int,
        raw_response_headers: list[dict[str, str]],
        outcome: _StreamOutcome,
        save_path: Path,
        filename: str,
        url: str,
        t0: float,
        artifact_scope_generation: int,
        attempt: _DownloadAttempt,
    ) -> None:
        """Persist a buffered body (if any) and complete the browser side: empty fulfill for large
        downloads, full replay for small ones."""
        if outcome.mode == "buffered":
            data = outcome.data or b""
            try:
                # Off-loop the write so a large buffered body never blocks the event loop,
                # matching the data-URL path (_download_data_url).
                _, cancelled = await self._run_data_worker(
                    self._atomically_write_bytes,
                    save_path,
                    data,
                    artifact_scope_generation,
                )
            except _DownloadScopeInvalidated:
                LOG.warning("CDP download scope changed before buffered publication; failing request")
                self._record_download_failure(attempt, "capture_failed")
                await self._fail_request(cdp_session, request_id)
                return
            except Exception as e:
                LOG.error("Failed to save CDP download", filename=filename, error_type=type(e).__name__)
                self._record_download_failure(attempt, "capture_failed")
                await self._fail_request(cdp_session, request_id, filename=filename, url=url)
                return
            elapsed_ms = (time.monotonic() - t0) * 1000
            LOG.info(
                "CDP download saved",
                filename=filename,
                size=len(data),
                duration_ms=round(elapsed_ms, 1),
                save_path=str(save_path),
                extraction_method="buffered",
                download_index=self._download_index,
            )
            body: bytes | None = data
        else:
            elapsed_ms = (time.monotonic() - t0) * 1000
            LOG.info(
                "CDP download saved",
                filename=filename,
                size=outcome.total_bytes,
                duration_ms=round(elapsed_ms, 1),
                save_path=str(save_path),
                extraction_method="streamed",
                download_index=self._download_index,
            )
            body = None

        self._record_download_saved(attempt)
        if outcome.mode == "buffered" and cancelled:
            raise asyncio.CancelledError

        try:
            if body is None:
                await self._fulfill_without_body(cdp_session, request_id, response_status, raw_response_headers)
            else:
                await self._fulfill_with_body(cdp_session, request_id, response_status, raw_response_headers, body)
        except Exception as e:
            # The file is already saved; only the browser-side completion failed. A stale interception
            # here (target navigated/closed) is a benign race, not an error.
            if _is_stale_interception_error(e):
                LOG.debug(
                    "fulfillRequest hit stale interception after download (benign race)",
                    filename=filename,
                    error_type=type(e).__name__,
                )
            else:
                LOG.warning("fulfillRequest failed after download", filename=filename, error_type=type(e).__name__)

    def _open_stream_temp_file(self, save_path: Path) -> _ConfinedTemporaryFile:
        """Open a descriptor-bound ``.crdownload`` temp file for streaming (hard-link publication;
        the suffix also makes the download waiter block until the stream completes)."""
        return self._open_confined_temporary_file(save_path)

    @staticmethod
    def _write_chunks(handle: IO[bytes], chunks: list[bytes]) -> None:
        for chunk in chunks:
            handle.write(chunk)

    async def _stream_response_body(
        self,
        cdp_session: CDPSession,
        request_id: str,
        save_path: Path,
        *,
        start_on_disk: bool,
        artifact_scope_generation: int,
        max_size_bytes: int | None = None,
    ) -> _StreamOutcome:
        """Read the response body via Fetch.takeResponseBodyAsStream + IO.read into either memory
        (small) or a temp file (large), enforcing the threshold and hard cap.

        Buffered chunks spill to a temp file the moment total bytes cross STREAM_TO_DISK_THRESHOLD_BYTES,
        after which no whole-body ``bytes`` is ever built. Raises _StreamStartError if the stream can't be
        taken (body untouched) and _StreamAborted if the cap is exceeded (temp cleaned up)."""
        if max_size_bytes is None:
            max_size_bytes = MAX_STREAMED_FILE_SIZE_BYTES
        try:
            result = await asyncio.wait_for(
                cdp_session.send("Fetch.takeResponseBodyAsStream", {"requestId": request_id}),
                timeout=STREAM_IO_READ_TIMEOUT_SECONDS,
            )
            stream_handle = result["stream"]
            stream_closed = False
        except Exception as e:
            raise _StreamStartError() from e

        buffer: list[bytes] | None = None if start_on_disk else []
        temp_file: _ConfinedTemporaryFile | None = None
        total = 0

        try:
            if start_on_disk:
                temp_file = self._open_stream_temp_file(save_path)

            while True:
                # Bound each read so a stalled IO.read can't hold the extraction lock (and hang teardown's
                # drain) indefinitely; on timeout the stream is aborted and the temp cleaned up in finally.
                read_result = await asyncio.wait_for(
                    cdp_session.send(
                        "IO.read",
                        {"handle": stream_handle, "size": STREAM_IO_READ_CHUNK_SIZE},
                    ),
                    timeout=STREAM_IO_READ_TIMEOUT_SECONDS,
                )
                data = read_result.get("data", "")
                is_base64 = read_result.get("base64Encoded", False)
                eof = read_result.get("eof", False)

                if data:
                    chunk = base64.b64decode(data) if is_base64 else data.encode("utf-8")
                    total += len(chunk)
                    if total > max_size_bytes:
                        raise _StreamAborted(total, max_size_bytes)

                    if temp_file is None:
                        assert buffer is not None
                        buffer.append(chunk)
                        if total >= STREAM_TO_DISK_THRESHOLD_BYTES:
                            # Spill to disk: this one-shot flush writes the whole buffered ≤64 MiB in a
                            # single loop iteration (no interleaved IO.read await), so off-load it via
                            # _run_data_worker to keep it off the event loop. The worker is shielded, so
                            # on cancellation the write finishes before the sync finally removes the temp.
                            temp_file = self._open_stream_temp_file(save_path)
                            spilled, buffer = buffer, None
                            _, cancelled = await self._run_data_worker(self._write_chunks, temp_file.handle, spilled)
                            # Release the spilled chunks (up to the threshold) now they are on disk, so a
                            # slow subsequent stream never retains a whole buffered copy in memory.
                            del spilled
                            if cancelled:
                                raise asyncio.CancelledError
                    else:
                        temp_file.handle.write(chunk)

                if eof:
                    break

            # Close the remote stream before publishing the final path. This leaves no await between
            # publication and returning the outcome, so cancellation cannot leave a saved file uncounted.
            stream_closed = True
            try:
                await asyncio.wait_for(
                    cdp_session.send("IO.close", {"handle": stream_handle}),
                    timeout=STREAM_IO_READ_TIMEOUT_SECONDS,
                )
            except Exception:
                pass

            if temp_file is not None:
                with self._artifact_scope_lock:
                    if not self._artifact_scope_is_active(artifact_scope_generation):
                        raise _DownloadScopeInvalidated
                    self._publish_confined_temporary_file(temp_file, save_path.name)
                return _StreamOutcome(mode="streamed", data=None, save_path=save_path, total_bytes=total)
            return _StreamOutcome(mode="buffered", data=b"".join(buffer or []), save_path=None, total_bytes=total)
        finally:
            # Sync cleanup first (uninterruptible): close the temp handle and remove any unfinalized temp
            # file, so no partial artifact survives an error, abort, or cancellation. Publication creates
            # the final hard link; the temporary name is always removed here.
            if temp_file is not None:
                try:
                    self._cleanup_confined_temporary_file(temp_file)
                except Exception:
                    pass
            try:
                # Bound the close too: if the session/target is dead (the same condition that stalls a
                # read), an unbounded IO.close would hold the extraction lock and hang teardown's drain.
                if not stream_closed:
                    await asyncio.wait_for(
                        cdp_session.send("IO.close", {"handle": stream_handle}),
                        timeout=STREAM_IO_READ_TIMEOUT_SECONDS,
                    )
            except Exception:
                pass


@asynccontextmanager
async def settle_browser_downloads_for_context(browser_context: BrowserContext | None) -> AsyncIterator[None]:
    interceptor: CDPDownloadInterceptor | None = (
        getattr(browser_context, "_skyvern_cdp_download_interceptor", None) if browser_context is not None else None
    )
    if interceptor is None:
        yield
        return
    async with interceptor.settle_browser_downloads():
        yield


def has_download_interceptor_for_context(browser_context: BrowserContext | None) -> bool:
    interceptor = (
        getattr(browser_context, "_skyvern_cdp_download_interceptor", None) if browser_context is not None else None
    )
    return isinstance(interceptor, CDPDownloadInterceptor)


def publish_download_bytes_for_context(
    browser_context: BrowserContext | None,
    data: bytes | bytearray,
    suggested_filename: str,
    content_type: str = "",
) -> tuple[bool, Path | None]:
    interceptor = (
        getattr(browser_context, "_skyvern_cdp_download_interceptor", None) if browser_context is not None else None
    )
    if not isinstance(interceptor, CDPDownloadInterceptor):
        return False, None
    return True, interceptor.publish_download_bytes(data, suggested_filename, content_type)


def begin_requested_download_for_context(browser_context: BrowserContext | None) -> int | None:
    interceptor = (
        getattr(browser_context, "_skyvern_cdp_download_interceptor", None) if browser_context is not None else None
    )
    return interceptor.begin_requested_download() if isinstance(interceptor, CDPDownloadInterceptor) else None


def finish_requested_download_for_context(
    browser_context: BrowserContext | None,
    token: int | None,
) -> dict[str, Any] | None:
    if browser_context is None or token is None:
        return None
    interceptor = getattr(browser_context, "_skyvern_cdp_download_interceptor", None)
    return interceptor.finish_requested_download(token) if isinstance(interceptor, CDPDownloadInterceptor) else None


def consume_unsolicited_download_error_for_context(browser_context: BrowserContext | None) -> dict[str, Any] | None:
    interceptor = (
        getattr(browser_context, "_skyvern_cdp_download_interceptor", None) if browser_context is not None else None
    )
    return interceptor.consume_unsolicited_download_error() if isinstance(interceptor, CDPDownloadInterceptor) else None


def record_download_artifact_outcome_for_context(
    browser_context: BrowserContext | None,
    *,
    registered_file_count: int,
    outcome: Literal["registered", "partial", "failed", "unknown"],
    error: str | None = None,
) -> None:
    interceptor = (
        getattr(browser_context, "_skyvern_cdp_download_interceptor", None) if browser_context is not None else None
    )
    if isinstance(interceptor, CDPDownloadInterceptor):
        interceptor.record_artifact_outcome(
            registered_file_count=registered_file_count,
            outcome=outcome,
            error=error,
        )


async def disable_download_interceptor_for_context(browser_context: BrowserContext | None) -> None:
    if browser_context is None:
        return
    interceptor: CDPDownloadInterceptor | None = getattr(browser_context, "_skyvern_cdp_download_interceptor", None)
    if interceptor is None:
        return
    browser_context._skyvern_cdp_download_interceptor = None  # type: ignore[attr-defined]
    await interceptor.disable()


async def bind_download_interceptor_to_context(
    interceptor: CDPDownloadInterceptor,
    browser_context: BrowserContext,
    *,
    enable_page_interception: bool = True,
) -> None:
    binding = interceptor.bind_to_context(
        browser_context,
        enable_page_interception=enable_page_interception,
    )
    if inspect.isawaitable(binding):
        await binding
