"""
CDP Fetch Download Interceptor

Intercepts download responses via the CDP Fetch domain and saves files locally.
Used for remote CDP browsers where Browser.setDownloadBehavior with a local
downloadPath does not work (e.g., Playwright bug #38805 — remote Windows Chrome
ignoring Linux paths).

Flow:
1. Enable Fetch interception at Response stage for each page
2. On each paused request:
   - Non-download → Fetch.continueResponse (pass through)
   - Download → extract body via stream → save to disk → Fetch.fulfillRequest (empty body blocks browser save)
"""

from __future__ import annotations

import asyncio
import base64
import re
import time
from pathlib import Path
from typing import Any
from urllib.parse import unquote, urlparse

import structlog
from playwright.async_api import CDPSession, Page

LOG = structlog.get_logger()

# Chunk size for IO.read streaming
IO_READ_CHUNK_SIZE = 64 * 1024  # 64 KB

# Maximum file size we'll attempt to download
MAX_FILE_SIZE_BYTES = 100 * 1024 * 1024  # 100 MB

# Resource types that should NEVER be treated as downloads.
# Sub-resources (Font, Stylesheet, etc.) are loaded by the page, not user-initiated.
# XHR/Fetch are programmatic JS API calls (e.g. Google APIs return Content-Disposition:
# attachment on JSON responses, which would cause false positives).
# Real user downloads come through as "Document" (link click / navigation).
NON_DOWNLOAD_RESOURCE_TYPES = frozenset(
    {
        "XHR",
        "Fetch",
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
    }
)


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


def is_download_response(headers: dict[str, str], status_code: int, resource_type: str = "") -> bool:
    """
    Determine if a response is a file download.

    Checks:
    0. Skip sub-resource types (Font, Stylesheet, Script, Image, etc.)
    1. Skip API content types (application/json, etc.)
    2. Content-Disposition contains "attachment"
    3. Content-Type is a known download MIME type
    """
    if status_code >= 400:
        return False

    if resource_type in NON_DOWNLOAD_RESOURCE_TYPES:
        return False

    content_disposition = headers.get("content-disposition", "")
    content_type = headers.get("content-type", "").split(";")[0].strip().lower()

    if content_type in NON_DOWNLOAD_CONTENT_TYPES:
        return False

    if "attachment" in content_disposition.lower():
        return True

    if content_type in DOWNLOAD_MIME_TYPES:
        return True

    return False


def extract_filename(headers: dict[str, str], url: str, index: int) -> str:
    """
    Extract filename from response headers or URL.

    Priority:
    1. Content-Disposition filename*= (RFC 5987, UTF-8)
    2. Content-Disposition filename=
    3. URL path last segment (if it has an extension)
    4. Fallback: download_{timestamp}_{index}
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

    return f"download_{int(time.time())}_{index}"


class CDPDownloadInterceptor:
    """
    Intercepts download responses via the CDP Fetch domain.

    Flow:
    1. Enable Fetch interception at Response stage
    2. On each paused request:
       - Non-download → Fetch.continueResponse (pass through)
       - Download → extract body → save to disk → Fetch.fulfillRequest (block browser save)
    """

    def __init__(self, output_dir: str | None = None) -> None:
        self._output_dir: Path | None = Path(output_dir) if output_dir else None
        self._cdp_sessions: list[CDPSession] = []
        self._enabled = False
        self._download_index = 0

    def set_download_dir(self, download_dir: str) -> None:
        """Set or update the download directory. Can be called after init when run_id becomes available."""
        self._output_dir = Path(download_dir)
        self._output_dir.mkdir(parents=True, exist_ok=True)
        LOG.info("CDP download interceptor download dir set", download_dir=download_dir)

    async def enable_for_page(self, page: Page) -> None:
        """Create a CDP session for the given page and enable Fetch interception."""
        cdp_session = await page.context.new_cdp_session(page)
        # Capture cdp_session in the closure so the handler uses the correct session
        # (requestId is scoped to the session that fired Fetch.requestPaused).
        cdp_session.on("Fetch.requestPaused", lambda event: self._on_request_paused(event, cdp_session))
        await cdp_session.send(
            "Fetch.enable",
            {"patterns": [{"requestStage": "Response"}]},
        )
        self._cdp_sessions.append(cdp_session)
        self._enabled = True
        LOG.info(
            "CDP Fetch interception enabled for page",
            page_url=page.url,
            session_count=len(self._cdp_sessions),
            output_dir=str(self._output_dir),
        )

    async def disable(self) -> None:
        """Disable Fetch interception on all CDP sessions."""
        session_count = len(self._cdp_sessions)
        for cdp_session in self._cdp_sessions:
            try:
                await cdp_session.send("Fetch.disable")
            except Exception:
                pass
        self._cdp_sessions.clear()
        self._enabled = False
        LOG.info(
            "CDP Fetch interception disabled",
            session_count=session_count,
            downloads_intercepted=self._download_index,
        )

    def _on_request_paused(self, event: dict[str, Any], cdp_session: CDPSession) -> None:
        """Handle Fetch.requestPaused — schedule async handler with the originating session."""
        asyncio.ensure_future(self._handle_request_paused(event, cdp_session))

    async def _handle_request_paused(self, event: dict[str, Any], cdp_session: CDPSession) -> None:
        """Async handler for paused requests."""
        request_id = event["requestId"]
        response_status = event.get("responseStatusCode", 0)
        raw_response_headers = event.get("responseHeaders", [])
        response_headers = _parse_headers(raw_response_headers)
        url = event.get("request", {}).get("url", "<unknown>")
        resource_type = event.get("resourceType", "")

        try:
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

        self._output_dir.mkdir(parents=True, exist_ok=True)

        self._download_index += 1
        content_length = _parse_content_length(headers)
        content_type = headers.get("content-type", "").split(";")[0].strip()
        filename = extract_filename(headers, url, self._download_index)
        # Sanitize filename to prevent path traversal (e.g. "../../etc/evil")
        filename = Path(filename).name
        if not filename:
            filename = f"download_{int(time.time())}_{self._download_index}"
        save_path = self._output_dir / filename

        # Deduplicate filename if a file with the same name already exists
        if save_path.exists():
            stem = Path(filename).stem
            suffix = Path(filename).suffix
            filename = f"{stem}_{self._download_index}{suffix}"
            save_path = self._output_dir / filename

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
