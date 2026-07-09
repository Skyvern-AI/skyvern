"""
CDP Fetch Download Interceptor

Intercepts download responses via the CDP Fetch domain and saves files locally.
Used for remote CDP browsers where Browser.setDownloadBehavior with a local
downloadPath does not work (e.g., Playwright bug #38805 — remote Windows Chrome
ignoring Linux paths).

Flow:
1. Enable Fetch interception for each page:
   - Response stage: detect and intercept downloads
   - Request stage (when proxy auth configured): enable Fetch.authRequired for proxy 407 challenges
2. On each paused request:
   - Request stage → Fetch.continueRequest (pass through to server)
   - Response non-download → Fetch.continueResponse (pass through)
   - Response download → extract body via stream → save to disk → Fetch.fulfillRequest
"""

from __future__ import annotations

import asyncio
import base64
import re
import ssl
import time
import urllib.request
import uuid
from pathlib import Path
from typing import Any
from urllib.parse import unquote, urlparse

import structlog
from playwright.async_api import Browser, BrowserContext, CDPSession, Page

from skyvern.webeye.utils.page import SkyvernFrame

LOG = structlog.get_logger()

# Chunk size for IO.read streaming
IO_READ_CHUNK_SIZE = 64 * 1024  # 64 KB

# Maximum file size we'll attempt to download
MAX_FILE_SIZE_BYTES = 100 * 1024 * 1024  # 100 MB

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
    ) -> None:
        self._output_dir: Path | None = Path(output_dir) if output_dir else None
        self._proxy_username: str | None = proxy_username
        self._proxy_password: str | None = proxy_password
        self._cdp_sessions: list[CDPSession] = []
        self._enabled = False
        self._download_index = 0
        # Track auth attempts per requestId to prevent infinite retry loops
        # when proxy credentials are rejected (407 → ProvideCredentials → 407 → …)
        self._auth_attempts: dict[str, int] = {}
        # Track URLs already downloaded (dedup between Fetch interception and browser download monitor)
        self._downloaded_urls: set[str] = set()
        self._browser_session: CDPSession | None = None
        self._browser_context: BrowserContext | None = None

    def set_download_dir(self, download_dir: str) -> None:
        """Set or update the download directory. Can be called after init when run_id becomes available."""
        self._output_dir = Path(download_dir)
        self._output_dir.mkdir(parents=True, exist_ok=True)
        LOG.info("CDP download interceptor download dir set", download_dir=download_dir)

    def is_monitoring_browser_downloads(self) -> bool:
        """True while the monitor owns the context's setDownloadBehavior binding ({deny, eventsEnabled:True},
        saving over HTTP), so re-sending allow/downloadPath would disable it on remote CDP."""
        return self._browser_session is not None

    def _resolve_save_path(self, filename: str = "", content_type: str = "") -> tuple[Path, str]:
        """Generate a unique save path under _output_dir.

        Sanitizes the filename (path traversal prevention), falls back to a UUID-based
        name when empty, increments _download_index, and logs a warning if a file with
        the same name already exists. Returns (save_path, sanitized_filename).

        Callers can pass a raw or empty filename — this method handles all normalization.
        """
        assert self._output_dir is not None
        self._output_dir.mkdir(parents=True, exist_ok=True)

        self._download_index += 1
        filename = normalize_download_filename(filename, content_type)
        if not filename:
            filename = f"download_{uuid.uuid4().hex[:8]}{_download_extension_for_content_type(content_type)}"

        # download_suffix is NOT applied here: this runs inside CDP callbacks that don't carry the
        # step's SkyvernContext, so the suffix could be stale. Run-dir files are renamed to
        # download_suffix by _finalize_downloaded_files_for_task instead.
        save_path = self._output_dir / filename
        # TODO: implement proper filename dedup (e.g., content hash or UUID suffix)
        if save_path.exists():
            LOG.warning("Download filename collision, overwriting", filename=filename, save_path=str(save_path))

        return save_path, filename

    async def enable_for_page(self, page: Page) -> None:
        """Create a CDP session for the given page and enable Fetch interception.

        When proxy credentials are configured, also enables Fetch.authRequired handling
        at the page level — matching Playwright's internal approach (CRNetworkManager).
        Playwright uses Request-stage interception with handleAuthRequests to receive
        proxy 407 challenges via Fetch.authRequired.
        """
        cdp_session = await page.context.new_cdp_session(page)
        cdp_session.on("Fetch.requestPaused", lambda event: self._on_request_paused(event, cdp_session))

        has_proxy_auth = bool(self._proxy_username and self._proxy_password)

        if has_proxy_auth:
            cdp_session.on("Fetch.authRequired", lambda event: self._on_auth_required(event, cdp_session))

        # Always intercept Response stage for download detection.
        # When proxy auth is needed, also intercept Request stage (like Playwright's
        # CRNetworkManager) — Chrome requires Request-stage patterns for
        # Fetch.authRequired to fire on proxy 407 challenges.
        patterns: list[dict[str, str]] = [{"requestStage": "Response"}]
        if has_proxy_auth:
            # urlPattern "*" intercepts all requests at Request stage, which adds overhead.
            # This is required: Chrome only fires Fetch.authRequired for proxy 407 challenges
            # when a Request-stage pattern is registered.
            patterns.append({"urlPattern": "*", "requestStage": "Request"})

        await cdp_session.send(
            "Fetch.enable",
            {
                "patterns": patterns,
                "handleAuthRequests": has_proxy_auth,
            },
        )
        self._cdp_sessions.append(cdp_session)
        self._enabled = True
        LOG.info(
            "CDP Fetch interception enabled for page",
            page_url=page.url,
            session_count=len(self._cdp_sessions),
            output_dir=str(self._output_dir),
            proxy_auth_enabled=has_proxy_auth,
        )

    async def enable_browser_download_monitor(self, browser: Browser, browser_context: BrowserContext) -> None:
        """Monitor browser-initiated downloads and save them directly via HTTP.

        Many sites trigger downloads via mechanisms that bypass CDP Fetch
        (e.g., new tab for signed URL, <a download>, blob URLs). The browser's
        download manager handles these directly — no page-level network request occurs.

        This method uses Browser-level CDP events to detect such downloads,
        then downloads the file directly via HTTP using the BrowserContext's
        APIRequestContext (which shares cookies and outlives individual pages).
        """
        if self._browser_session is not None:
            LOG.warning("Browser download monitor already enabled, skipping")
            return

        browser_session = await browser.new_browser_cdp_session()
        self._browser_session = browser_session
        self._browser_context = browser_context

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

        browser_session.on(
            "Browser.downloadWillBegin",
            lambda event: asyncio.ensure_future(self._handle_browser_download(event)),
        )
        LOG.info("Browser download monitor enabled")

    async def _handle_browser_download(self, event: dict[str, Any]) -> None:
        """Handle Browser.downloadWillBegin — download the file via HTTP or blob read."""
        try:
            url = event.get("url", "")
            suggested_filename = event.get("suggestedFilename", "")
            LOG.info(
                "Browser download detected",
                url=url,
                suggested_filename=suggested_filename,
            )
            if not url:
                LOG.warning("Empty download URL, skipping")
                return

            # Skip if this exact URL was already captured. Both the Fetch path
            # (_handle_download) and the blob path (_download_blob_url, after a successful save)
            # record URLs here. We record only AFTER a successful save — not before the read — so
            # a transient failure can't block a later retry of the same URL; a rare duplicate
            # downloadWillBegin for the same blob URL is a benign re-save/overwrite.
            if url in self._downloaded_urls:
                LOG.debug("URL already captured via Fetch, skipping direct download", url=url)
                return

            if url.startswith("blob:"):
                # blob: URLs are in-memory browser references — not fetchable over HTTP. When the
                # page builds the file client-side (e.g. Blob + createObjectURL), the CDP Fetch
                # path never sees a network response, so read the bytes back from a same-origin
                # page instead of dropping the download.
                await self._download_blob_url(url, suggested_filename)
            elif url.startswith("http"):
                await self._download_url_directly(url, suggested_filename)
            else:
                LOG.warning("Download URL scheme not supported, skipping", url=url)
        except Exception:
            LOG.warning("Error handling browser download event", exc_info=True)

    async def _download_url_directly(self, url: str, suggested_filename: str) -> None:
        """Download a URL directly via HTTP and save to the output directory.

        Tries Playwright's APIRequestContext first (shares browser context cookies),
        falls back to urllib for pre-signed URLs or when APIRequestContext fails.
        """
        if not self._output_dir:
            LOG.warning("No output_dir set, skipping direct download", url=url)
            return

        t0 = time.monotonic()
        data: bytes | None = None
        method = ""
        content_type = ""

        # Try Playwright's APIRequestContext which shares the BrowserContext's cookies.
        # We use the BrowserContext (not a Page) so this survives individual page closes.
        if self._browser_context:
            try:
                response = await self._browser_context.request.get(url)
                if response.ok:
                    data = await response.body()
                    content_type = response.headers.get("content-type", "")
                    method = "playwright_api"
                else:
                    LOG.debug(
                        "Playwright APIRequestContext returned non-OK status, trying urllib",
                        url=url,
                        status=response.status,
                    )
            except Exception as e:
                LOG.debug("Playwright APIRequestContext download failed, trying urllib", url=url, error=str(e))

        # Fallback: direct HTTP via urllib (works for pre-signed URLs)
        if data is None:
            try:
                req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
                ssl_ctx = ssl.create_default_context()

                def _fetch() -> tuple[bytes, str]:
                    with urllib.request.urlopen(req, context=ssl_ctx) as resp:
                        return resp.read(), resp.headers.get("content-type", "")

                data, content_type = await asyncio.to_thread(_fetch)
                method = "urllib"
            except Exception as e:
                LOG.error("Direct HTTP download failed", url=url, error=str(e), exc_info=True)
                return

        if data is None:
            LOG.error("Download produced no data", url=url)
            return

        if len(data) > MAX_FILE_SIZE_BYTES:
            LOG.warning(
                "Direct download exceeds size limit, discarding",
                url=url,
                size=len(data),
                max_size=MAX_FILE_SIZE_BYTES,
            )
            return

        save_path, filename = self._resolve_save_path(suggested_filename, content_type)

        with open(save_path, "wb") as f:
            f.write(data)

        elapsed_ms = (time.monotonic() - t0) * 1000
        LOG.info(
            "CDP download saved (direct HTTP)",
            filename=filename,
            size=len(data),
            duration_ms=round(elapsed_ms, 1),
            save_path=str(save_path),
            download_index=self._download_index,
            method=method,
        )

    async def _download_blob_url(self, url: str, suggested_filename: str) -> None:
        """Save a blob: URL download by reading its bytes back from a same-origin page.

        blob: URLs are in-memory references owned by the document that created them, so they
        can't be fetched over HTTP. ``SkyvernFrame.read_blob_url_bytes`` runs the shared blob
        read-back script inside a same-origin frame. Best-effort: a page may revoke the object
        URL before we read it.
        """
        if not self._output_dir or self._browser_context is None:
            LOG.warning("Cannot read blob download: no output dir or browser context", url=url)
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
                url=url,
                suggested_filename=suggested_filename,
            )
            return
        # Defense-in-depth: read_blob_url_bytes already rejects oversized blobs in-page before
        # serialization, but guard again in case a caller passes no limit.
        if len(data) > MAX_FILE_SIZE_BYTES:
            LOG.warning(
                "Blob download exceeds size limit, discarding",
                url=url,
                size=len(data),
                max_size=MAX_FILE_SIZE_BYTES,
            )
            return
        save_path, filename = self._resolve_save_path(suggested_filename)
        with open(save_path, "wb") as f:
            f.write(data)
        self._downloaded_urls.add(url)
        LOG.info(
            "CDP download saved (blob)",
            filename=filename,
            size=len(data),
            save_path=str(save_path),
            download_index=self._download_index,
        )

    async def disable(self) -> None:
        """Disable Fetch interception on all CDP sessions and clean up browser monitor."""
        session_count = len(self._cdp_sessions)
        for cdp_session in self._cdp_sessions:
            try:
                await cdp_session.send("Fetch.disable")
            except Exception:
                pass
        self._cdp_sessions.clear()

        # Clean up browser-level download monitor session
        if self._browser_session:
            try:
                await self._browser_session.detach()
            except Exception:
                pass
            self._browser_session = None
        self._browser_context = None

        self._enabled = False
        LOG.info(
            "CDP Fetch interception disabled",
            session_count=session_count,
            downloads_intercepted=self._download_index,
        )

    def _on_request_paused(self, event: dict[str, Any], cdp_session: CDPSession) -> None:
        """Handle Fetch.requestPaused — schedule async handler with the originating session."""
        asyncio.ensure_future(self._handle_request_paused(event, cdp_session))

    def _on_auth_required(self, event: dict[str, Any], cdp_session: CDPSession) -> None:
        """Handle Fetch.authRequired — schedule async handler with the originating session."""
        asyncio.ensure_future(self._handle_auth_required(event, cdp_session))

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

    async def _handle_request_paused(self, event: dict[str, Any], cdp_session: CDPSession) -> None:
        """Async handler for paused requests.

        Handles both Request-stage and Response-stage events:
        - Request stage (no responseStatusCode): continue the request immediately.
          We intercept at Request stage only to make Fetch.authRequired fire for proxy auth.
        - Response stage: check for downloads and intercept if needed.
        """
        request_id = event["requestId"]
        response_status = event.get("responseStatusCode")
        url = event.get("request", {}).get("url", "<unknown>")

        try:
            # Request stage: no response yet (responseStatusCode absent). Continue the request
            # so it proceeds to the server. We only intercept Request stage to enable
            # Fetch.authRequired for proxy 407 challenges.
            if response_status is None:
                await cdp_session.send("Fetch.continueRequest", {"requestId": request_id})
                return

            # Response stage: check for downloads
            raw_response_headers = event.get("responseHeaders", [])
            response_headers = _parse_headers(raw_response_headers)
            resource_type = event.get("resourceType", "")

            LOG.debug(
                "CDP Fetch response paused",
                url=url,
                resource_type=resource_type,
                status_code=response_status,
                content_type=response_headers.get("content-type", ""),
                content_disposition=response_headers.get("content-disposition", ""),
            )

            if is_download_response(response_headers, response_status, resource_type):
                LOG.info(
                    "CDP download response detected",
                    url=url,
                    resource_type=resource_type,
                    status_code=response_status,
                    content_type=response_headers.get("content-type", ""),
                    content_disposition=response_headers.get("content-disposition", ""),
                )
                await self._handle_download(
                    cdp_session, request_id, url, response_headers, response_status, raw_response_headers
                )
            else:
                await self._continue_response(cdp_session, request_id)
        except Exception as e:
            LOG.error(
                "Error handling CDP request",
                request_id=request_id,
                url=url,
                exc_info=True,
                error=str(e),
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
    ) -> None:
        """Extract a download file, save it to disk, and replay the response to the browser."""
        if not self._output_dir:
            LOG.warning("CDP download intercepted but no output_dir set, passing through", url=url)
            await self._continue_response(cdp_session, request_id)
            return

        content_length = _parse_content_length(headers)
        content_type = _normalized_content_type(headers.get("content-type", ""))
        raw_filename = extract_filename(headers, url)
        save_path, filename = self._resolve_save_path(raw_filename, content_type)

        LOG.info(
            "CDP download detected",
            filename=filename,
            url=url,
            content_type=content_type,
            content_length=content_length,
        )

        if content_length and content_length > MAX_FILE_SIZE_BYTES:
            LOG.warning(
                "CDP download file exceeds size limit, passing through",
                filename=filename,
                content_length=content_length,
                max_size=MAX_FILE_SIZE_BYTES,
            )
            await self._continue_response(cdp_session, request_id)
            return

        # Mark URL as handled BEFORE starting the (potentially slow) body extraction.
        # This prevents the browser download monitor (_handle_browser_download) from
        # racing to download the same URL while we're still streaming the body.
        # We intentionally do NOT remove the URL on failure — if Fetch extraction fails,
        # a direct HTTP re-download of the same URL would likely fail too.
        self._downloaded_urls.add(url)

        t0 = time.monotonic()

        try:
            # Stream-first strategy: try takeResponseBodyAsStream, fallback to getResponseBody.
            # Note: if stream partially consumes the body before failing, the direct fallback
            # will also fail since the body is already consumed. The outer handler catches this.
            extraction_method = "stream"
            try:
                data = await self._extract_body_stream(cdp_session, request_id)
            except Exception as e:
                extraction_method = "direct"
                LOG.warning(
                    "takeResponseBodyAsStream failed, trying getResponseBody",
                    filename=filename,
                    url=url,
                    error=str(e),
                )
                data = await self._extract_body_direct(cdp_session, request_id)

            with open(save_path, "wb") as f:
                f.write(data)
            elapsed_ms = (time.monotonic() - t0) * 1000
            LOG.info(
                "CDP download saved",
                filename=filename,
                size=len(data),
                duration_ms=round(elapsed_ms, 1),
                save_path=str(save_path),
                extraction_method=extraction_method,
                download_index=self._download_index,
            )

        except Exception as e:
            LOG.error(
                "Failed to extract CDP download",
                filename=filename,
                url=url,
                content_type=content_type,
                content_length=content_length,
                error=str(e),
                exc_info=True,
            )
            try:
                await self._continue_response(cdp_session, request_id)
            except Exception:
                pass
            return

        # Replay the original response to the browser so it also gets the download.
        # After body extraction, we fulfill with the same status, headers, and body.
        try:
            await self._fulfill_with_body(cdp_session, request_id, response_status, raw_response_headers, data)
        except Exception as e:
            LOG.warning("fulfillRequest failed after download", filename=filename, url=url, error=str(e))
            # Can't continue response after body extraction, just log the error

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
        await cdp_session.send(
            "Fetch.fulfillRequest",
            {
                "requestId": request_id,
                "responseCode": response_status,
                "responseHeaders": raw_response_headers,
                "body": base64.b64encode(body).decode(),
            },
        )

    async def _extract_body_direct(self, cdp_session: CDPSession, request_id: str) -> bytes:
        """Extract response body using Fetch.getResponseBody (single call, base64)."""
        result = await cdp_session.send(
            "Fetch.getResponseBody",
            {"requestId": request_id},
        )
        body = result.get("body", "")
        is_base64 = result.get("base64Encoded", False)
        if is_base64:
            return base64.b64decode(body)
        return body.encode("utf-8")

    async def _extract_body_stream(self, cdp_session: CDPSession, request_id: str) -> bytes:
        """Extract response body using Fetch.takeResponseBodyAsStream + IO.read."""
        result = await cdp_session.send(
            "Fetch.takeResponseBodyAsStream",
            {"requestId": request_id},
        )
        stream_handle = result["stream"]

        chunks: list[bytes] = []
        total_read = 0

        try:
            while True:
                read_result = await cdp_session.send(
                    "IO.read",
                    {"handle": stream_handle, "size": IO_READ_CHUNK_SIZE},
                )
                data = read_result.get("data", "")
                is_base64 = read_result.get("base64Encoded", False)
                eof = read_result.get("eof", False)

                if data:
                    chunk = base64.b64decode(data) if is_base64 else data.encode("utf-8")
                    chunks.append(chunk)
                    total_read += len(chunk)

                    if total_read > MAX_FILE_SIZE_BYTES:
                        LOG.warning("Stream exceeded max file size during read", total_read=total_read)
                        break

                if eof:
                    break
        finally:
            try:
                await cdp_session.send("IO.close", {"handle": stream_handle})
            except Exception:
                pass

        return b"".join(chunks)
