import asyncio
import hashlib
import html
import mimetypes
import os
import re
import shutil
import tempfile
import zipfile
from dataclasses import dataclass
from http import HTTPStatus
from pathlib import Path
from typing import TYPE_CHECKING, TypeAlias
from urllib.parse import parse_qsl, unquote, urlencode, urlparse

import aiohttp
import filetype
import structlog
from multidict import CIMultiDictProxy
from yarl import URL

from skyvern.config import settings
from skyvern.constants import BROWSER_DOWNLOAD_TIMEOUT, BROWSER_DOWNLOADING_SUFFIX, REPO_ROOT_DIR
from skyvern.exceptions import (
    BlockedHost,
    DownloadFileMaxSizeExceeded,
    DownloadFileMaxWaitingTime,
    GoogleDriveFileNotAccessible,
    HttpException,
    SkyvernHTTPException,
)
from skyvern.forge import app
from skyvern.forge.sdk.artifact.signing import parse_artifact_content_url
from skyvern.forge.sdk.browser_action_policy import canonicalize_origin
from skyvern.forge.sdk.core.aiohttp_helper import (
    SSRFGuardedResolver,
    _url_origin,
    ssrf_guarded_tcp_connector,
    strip_cross_origin_redirect_credentials,
    validate_and_pin_fetch_url,
    validate_and_pin_redirect_url,
)
from skyvern.forge.sdk.core.http_request_authorization import (
    RedirectHopAuthorization,
    RedirectHopAuthorizer,
    authorize_request_hop_once,
)
from skyvern.forge.sdk.db.id import UPLOADED_FILE_PREFIX
from skyvern.services import uploaded_file_service
from skyvern.utils.url_validators import (
    MAX_SAFE_REDIRECTS,
    SAFE_REDIRECT_STATUS_CODES,
    encode_url,
)

if TYPE_CHECKING:
    from skyvern.forge.sdk.core.skyvern_context import SkyvernContext

LOG = structlog.get_logger()

_UPLOADED_FILE_ID_PATTERN = re.compile(rf"^{UPLOADED_FILE_PREFIX}_[0-9]+$")


def is_uploaded_file_id(value: str) -> bool:
    """Whether a value names a file uploaded through ``POST /v1/upload_file``.

    Distinguishable from a URL by construction: an id carries no scheme, and ``file_`` is not
    a parsable URL scheme, so this can never shadow a ``file://`` path.
    """
    return bool(_UPLOADED_FILE_ID_PATTERN.match(value.strip()))


async def resolve_uploaded_file_id(file_id: str, organization_id: str | None) -> str:
    """Turn a file id into the storage URI holding its bytes.

    The URI comes from the ``uploaded_files`` row rather than from input, and the org scope is
    part of the lookup, so a caller can only ever name their own organization's files.
    """
    if organization_id is None:
        raise PermissionError(f"No permission to access file: {file_id}")
    storage_uri = await uploaded_file_service.resolve_file_reference(
        file_id=file_id.strip(), organization_id=organization_id
    )
    if storage_uri is None:
        raise FileNotFoundError(f"File not found: {file_id}")
    return storage_uri


def get_file_name_and_suffix_from_headers(headers: CIMultiDictProxy[str] | dict[str, str]) -> tuple[str, str]:
    file_stem = ""
    file_suffix: str | None = ""
    # retrieve the stem and suffix from Content-Disposition
    content_disposition = headers.get("Content-Disposition")
    if content_disposition:
        filename = re.findall('filename="(.+)"', content_disposition, re.IGNORECASE)
        if len(filename) > 0:
            file_stem = Path(filename[0]).stem
            file_suffix = Path(filename[0]).suffix

    if file_suffix:
        return file_stem, file_suffix

    # retrieve the suffix from Content-Type
    content_type = headers.get("Content-Type")
    if content_type:
        if file_suffix := mimetypes.guess_extension(content_type.split(";")[0].strip()):
            return file_stem, file_suffix

    return file_stem, file_suffix or ""


def extract_google_drive_file_id(url: str) -> str | None:
    """Extract file ID from Google Drive URL."""
    # Handle format: https://drive.google.com/file/d/{file_id}/view
    match = re.search(r"/file/d/([a-zA-Z0-9_-]+)", url)
    if match:
        return match.group(1)
    return None


_GOOGLE_DRIVE_DOWNLOAD_HOSTS = ("drive.google.com", "drive.usercontent.google.com")
_GOOGLE_DRIVE_INTERSTITIAL_MAX_BYTES = 1024 * 1024
_GOOGLE_DRIVE_FORM_RE = re.compile(r'<form[^>]*\baction="([^"]+)"[^>]*>(.*?)</form>', re.IGNORECASE | re.DOTALL)
_GOOGLE_DRIVE_INPUT_RE = re.compile(r"<input\b[^>]*>", re.IGNORECASE)


def _is_google_drive_download_url(url: str) -> bool:
    return (urlparse(url).hostname or "").lower() in _GOOGLE_DRIVE_DOWNLOAD_HOSTS


def _extract_google_drive_interstitial_url(body: str) -> str | None:
    """Continuation URL from Drive's large-file/virus-scan interstitial form, or None when the
    HTML is a sign-in / permission-denied page carrying no download form."""
    for form_match in _GOOGLE_DRIVE_FORM_RE.finditer(body):
        action = html.unescape(form_match.group(1))
        params: dict[str, str] = {}
        for input_tag in _GOOGLE_DRIVE_INPUT_RE.findall(form_match.group(2)):
            name_match = re.search(r'\bname="([^"]*)"', input_tag, re.IGNORECASE)
            if not name_match:
                continue
            value_match = re.search(r'\bvalue="([^"]*)"', input_tag, re.IGNORECASE)
            params[html.unescape(name_match.group(1))] = html.unescape(value_match.group(1)) if value_match else ""
        # The confirm token distinguishes the download form from unrelated forms (e.g. sign-in).
        if "confirm" not in params:
            continue
        query = urlencode(params)
        if not query:
            return action
        return f"{action}&{query}" if "?" in action else f"{action}?{query}"

    link_match = re.search(r'href="([^"]*/uc\?[^"]*\bconfirm=[^"]*)"', body, re.IGNORECASE)
    if link_match:
        return html.unescape(link_match.group(1))
    return None


def _is_html_content_type(content_type: str | None) -> bool:
    if not content_type:
        return False
    return content_type.split(";")[0].strip().lower() == "text/html"


async def _read_bounded_response_text(response: aiohttp.ClientResponse, max_bytes: int) -> str:
    body = bytearray()
    async for chunk in response.content.iter_chunked(1024):
        body.extend(chunk)
        if len(body) >= max_bytes:
            break
    return bytes(body).decode("utf-8", errors="replace")


def is_valid_mime_type(file_path: str) -> bool:
    mime_type, _ = mimetypes.guess_type(file_path)
    return mime_type is not None


def _determine_download_filename(
    filename: str | None,
    response_headers: CIMultiDictProxy[str] | dict[str, str],
    url: str,
) -> str:
    """Determine the filename for a downloaded file."""
    if filename:
        file_name = filename
        if not os.path.splitext(file_name)[1]:
            content_type = response_headers.get("Content-Type", "")
            if content_type:
                ext = mimetypes.guess_extension(content_type.split(";")[0].strip())
                if ext:
                    file_name = file_name + ext
        return sanitize_filename(file_name)

    file_name = ""
    file_suffix = ""
    try:
        file_name, file_suffix = get_file_name_and_suffix_from_headers(response_headers)
        if not file_suffix:
            LOG.warning("No extension name retrieved from HTTP headers")
    except Exception:
        LOG.exception("Failed to retrieve the file extension from HTTP headers")

    query_params = dict(parse_qsl(urlparse(url).query))
    if "download" in query_params:
        file_name = query_params["download"]

    if not file_name:
        LOG.info("No file name retrieved from HTTP headers, using the file name from the URL")
        file_name = os.path.basename(urlparse(url).path) or "download"

    if not is_valid_mime_type(file_name) and file_suffix:
        LOG.info("No file extension detected, adding the extension from HTTP headers")
        file_name = file_name + file_suffix

    return sanitize_filename(file_name)


def _raise_download_response_for_status(response: aiohttp.ClientResponse) -> None:
    if response.status < HTTPStatus.BAD_REQUEST:
        return

    raise aiohttp.ClientResponseError(
        request_info=response.request_info,
        history=response.history,
        status=response.status,
        message=response.reason,
        headers=response.headers,
    )


@dataclass(frozen=True, slots=True)
class GuardedFileRedirect:
    """Internal hop result surfaced only through the authorization dispatcher."""

    location: str


@dataclass(frozen=True, slots=True)
class GuardedFileResponse:
    """Bounded response returned by :func:`fetch_file_bytes` to a trusted adapter."""

    body: bytes
    content_type: str
    filename: str


GuardedFileFetchHopResult: TypeAlias = GuardedFileRedirect | GuardedFileResponse


async def fetch_file_bytes(
    url: str,
    *,
    max_size_mb: int = 100,
    headers: dict[str, str] | None = None,
    filename: str | None = None,
    allowed_redirect_origin: str | None = None,
    authorize_request_hop: RedirectHopAuthorizer[GuardedFileFetchHopResult],
    download_scope: str | None = None,
    approved_initial_url: str | None = None,
) -> GuardedFileResponse:
    """Fetch a bounded HTTP file through the validated, pinned, per-hop authorization seam.

    This helper accepts HTTP(S) only. It validates and pins each target before invoking
    ``authorize_request_hop``; validation failures therefore consume no approval and perform no
    network request. The body is intentionally bounded because it is returned in memory.
    """
    if not url or not url.strip():
        raise ValueError("Download URL is empty — no file download was triggered by the browser")
    if max_size_mb <= 0:
        raise ValueError("max_size_mb must be greater than zero")

    resolver = SSRFGuardedResolver()
    current_url = await validate_and_pin_fetch_url(url, resolver)
    if canonicalize_origin(current_url) is None:
        raise HttpException(400, "[redacted]", "URL has no browser-canonicalizable HTTP origin")
    if allowed_redirect_origin is not None and _url_origin(current_url) != _url_origin(allowed_redirect_origin):
        raise HttpException(400, "[redacted]", "Cross-origin redirect blocked by policy")

    request_headers = dict(headers or {})
    source_url: str | None = None
    max_size_bytes = max_size_mb * 1024 * 1024
    async with aiohttp.ClientSession(connector=ssrf_guarded_tcp_connector(resolver)) as session:
        for _ in range(MAX_SAFE_REDIRECTS + 1):
            encoded_url = encode_url(current_url)

            async def dispatch(_resolved_values: tuple[str, ...]) -> GuardedFileFetchHopResult:
                async with session.get(
                    URL(encoded_url, encoded=True), headers=request_headers, allow_redirects=False
                ) as response:
                    location = response.headers.get("Location")
                    if response.status in SAFE_REDIRECT_STATUS_CODES and location:
                        return GuardedFileRedirect(location=location)

                    _raise_download_response_for_status(response)
                    if response.content_length and response.content_length > max_size_bytes:
                        raise DownloadFileMaxSizeExceeded(max_size_mb)

                    body = bytearray()
                    async for chunk in response.content.iter_chunked(1024):
                        body.extend(chunk)
                        if len(body) > max_size_bytes:
                            raise DownloadFileMaxSizeExceeded(max_size_mb)

                    return GuardedFileResponse(
                        body=bytes(body),
                        content_type=response.headers.get("Content-Type", ""),
                        filename=_determine_download_filename(filename, response.headers, current_url),
                    )

            result = await authorize_request_hop_once(
                authorize_request_hop,
                RedirectHopAuthorization(
                    source_url=source_url,
                    target_url=current_url,
                    method="GET",
                    download_scope=download_scope,
                    initial_url=approved_initial_url,
                ),
                dispatch,
            )
            if isinstance(result, GuardedFileResponse):
                return result

            next_url = await validate_and_pin_redirect_url(current_url, result.location, resolver)
            if canonicalize_origin(next_url) is None:
                raise HttpException(400, "[redacted]", "Redirect has no browser-canonicalizable HTTP origin")
            if allowed_redirect_origin is not None and _url_origin(next_url) != _url_origin(allowed_redirect_origin):
                raise HttpException(400, "[redacted]", "Cross-origin redirect blocked by policy")
            request_headers, _ = strip_cross_origin_redirect_credentials(
                request_headers,
                None,
                current_url,
                next_url,
            )
            source_url, current_url = current_url, next_url

    raise HttpException(400, "[redacted]", "Too many redirects while downloading file")


def _resolve_legacy_download_path(candidate_path: str) -> str:
    """Resolve a legacy file:// path and confirm it is inside the repository downloads directory.

    Containment is checked on the realpath with commonpath, so dot segments, percent-decoded dot
    segments, symlinks, and sibling directories such as ``downloads-evil`` cannot escape.
    """
    allowed_dir = os.path.realpath(os.path.join(REPO_ROOT_DIR, "downloads"))
    resolved_path = os.path.realpath(candidate_path)
    try:
        inside_allowed_dir = os.path.commonpath((allowed_dir, resolved_path)) == allowed_dir
    except ValueError:
        inside_allowed_dir = False
    if not inside_allowed_dir:
        LOG.warning(
            "Legacy local file path traversal blocked",
            candidate_path=candidate_path,
            resolved_path=resolved_path,
            allowed_dir=allowed_dir,
        )
        raise PermissionError("Local file path is outside the downloads directory")
    return resolved_path


def validate_download_url(url: str, organization_id: str | None = None) -> bool:
    """Validate if a URL is supported for downloading.

    Security validation for URL downloads to prevent:
    - File system access outside allowed directories
    - Access to local file system in non-local environments
    - Unsupported or dangerous URL schemes

    Args:
        url: The URL to validate

    Returns:
        True if valid, False otherwise.
    """
    try:
        # A file id resolves to an org-scoped storage URI at download time, and that URI is
        # re-checked against the org prefix before any bytes are read.
        if is_uploaded_file_id(url):
            return organization_id is not None

        parsed_url = urlparse(url)
        scheme = parsed_url.scheme.lower()

        # Allow http/https URLs (includes Google Drive which uses https)
        if scheme in ("http", "https"):
            return True

        if scheme in ("s3", "gs", "azure"):
            try:
                if organization_id is None:
                    return False
                app.STORAGE.assert_managed_file_access(url, organization_id)
                return True
            except (PermissionError, RuntimeError):
                return False

        # Allow file:// URLs only in local environment
        if scheme == "file":
            if settings.ENV != "local":
                return False

            # Validate the file path is within allowed directories
            try:
                _resolve_legacy_download_path(parse_uri_to_path(url))
                return True
            except (ValueError, PermissionError):
                return False

        # Reject unsupported schemes
        return False

    except Exception:
        return False


async def download_file(
    url: str,
    max_size_mb: int | None = None,
    headers: dict[str, str] | None = None,
    output_dir: str | None = None,
    filename: str | None = None,
    organization_id: str | None = None,
    allowed_redirect_origin: str | None = None,
    authorize_request_hop: RedirectHopAuthorizer[str | GuardedFileRedirect] | None = None,
) -> str:
    if not url or not url.strip():
        raise ValueError("Download URL is empty — no file download was triggered by the browser")

    # Resolved before the try below so a missing or cross-org file id fails loudly instead of
    # falling through to the HTTP fetch path with an id as the URL.
    if is_uploaded_file_id(url):
        url = await resolve_uploaded_file_id(url, organization_id)

    requested_url = url
    try:
        # Check if URL is a Google Drive link
        if "drive.google.com" in url:
            file_id = extract_google_drive_file_id(url)
            if file_id:
                # Convert to direct download URL
                url = f"https://drive.google.com/uc?export=download&id={file_id}"
                LOG.info("Converting Google Drive link to direct download", url=url)
        is_google_drive_download = _is_google_drive_download_url(url)

        # Check if URL is a cloud storage URI handled by the configured storage backend.
        parsed = urlparse(url)
        if parsed.scheme in ("s3", "gs", "azure"):
            if organization_id is None:
                raise PermissionError(f"No permission to access storage URI: {url}")

            app.STORAGE.assert_managed_file_access(url, organization_id)

            LOG.info(
                "Downloading managed storage file",
                url=url,
                organization_id=organization_id,
                storage_type=getattr(app.STORAGE, "storage_type", None),
            )
            data = await app.STORAGE.download_managed_file(url, organization_id)
            if data is None:
                raise Exception(f"Failed to download managed storage file: {url}")
            filename = url.split("/")[-1]
            temp_file = create_named_temporary_file(delete=False, file_name=filename)
            LOG.info(f"Downloaded file to {temp_file.name}")
            temp_file.write(data)
            return temp_file.name

        # Check if URL is a file:// URI
        # we only support to download local files when the environment is local
        # and the file is in the skyvern downloads directory
        if url.startswith("file://") and settings.ENV == "local":
            local_path = _resolve_legacy_download_path(parse_uri_to_path(url))
            LOG.info("Downloading file from local file system", url=url)
            return local_path

        resolver = SSRFGuardedResolver()
        current_url = await validate_and_pin_fetch_url(url, resolver)
        if canonicalize_origin(current_url) is None:
            raise HttpException(400, "[redacted]", "URL has no browser-canonicalizable HTTP origin")
        if allowed_redirect_origin is not None and _url_origin(current_url) != _url_origin(allowed_redirect_origin):
            raise HttpException(400, "[redacted]", "Cross-origin redirect blocked by policy")
        request_headers = dict(headers or {})
        source_url: str | None = None
        async with aiohttp.ClientSession(connector=ssrf_guarded_tcp_connector(resolver)) as session:
            LOG.info("Starting guarded file download")
            for _ in range(MAX_SAFE_REDIRECTS + 1):
                encoded_url = encode_url(current_url)

                async def dispatch(_resolved_values: tuple[str, ...]) -> str | GuardedFileRedirect:
                    async with session.get(
                        URL(encoded_url, encoded=True), headers=request_headers, allow_redirects=False
                    ) as response:
                        location = response.headers.get("Location")
                        if response.status in SAFE_REDIRECT_STATUS_CODES and location:
                            return GuardedFileRedirect(location=location)

                        _raise_download_response_for_status(response)
                        if is_google_drive_download and _is_html_content_type(response.headers.get("Content-Type")):
                            # Drive quirk: uc?export=download answers with an HTML interstitial — a
                            # virus-scan confirm form for large files, a sign-in page for
                            # inaccessible ones — so an HTML body is never the requested file.
                            interstitial_body = await _read_bounded_response_text(
                                response, _GOOGLE_DRIVE_INTERSTITIAL_MAX_BYTES
                            )
                            continuation_url = _extract_google_drive_interstitial_url(interstitial_body)
                            if continuation_url is None:
                                raise GoogleDriveFileNotAccessible(url=requested_url)
                            LOG.info("Following Google Drive download interstitial", url=current_url)
                            return GuardedFileRedirect(location=continuation_url)
                        if (
                            max_size_mb
                            and response.content_length
                            and response.content_length > max_size_mb * 1024 * 1024
                        ):
                            raise DownloadFileMaxSizeExceeded(max_size_mb)

                        if output_dir:
                            download_dir_path = Path(output_dir)
                            download_dir_path.mkdir(parents=True, exist_ok=True)
                        else:
                            download_dir_path = Path(make_temp_directory(prefix="skyvern_downloads_"))

                        download_dir_resolved = download_dir_path.resolve()
                        file_name = _determine_download_filename(filename, response.headers, url)
                        allowed_dir = os.path.realpath(download_dir_resolved)
                        resolved_final_path = os.path.realpath(os.path.join(allowed_dir, file_name))
                        if (
                            resolved_final_path == allowed_dir
                            or not resolved_final_path.startswith(allowed_dir + os.sep)
                            or os.path.dirname(resolved_final_path) != allowed_dir
                        ):
                            raise ValueError(f"Unsafe filename derived from download: {file_name!r}")
                        final_path = Path(resolved_final_path)

                        temp_file = tempfile.NamedTemporaryFile(mode="wb", dir=download_dir_resolved, delete=False)
                        file_path = Path(temp_file.name).resolve()
                        if file_path != download_dir_resolved and not file_path.is_relative_to(download_dir_resolved):
                            temp_file.close()
                            raise ValueError("Unsafe temporary file path created for download")

                        LOG.info("Downloading file to temporary path", file_path=str(file_path))
                        try:
                            with temp_file as f:
                                total_bytes_downloaded = 0
                                async for chunk in response.content.iter_chunked(1024):
                                    f.write(chunk)
                                    total_bytes_downloaded += len(chunk)
                                    if max_size_mb and total_bytes_downloaded > max_size_mb * 1024 * 1024:
                                        raise DownloadFileMaxSizeExceeded(max_size_mb)

                            file_path.replace(final_path)
                        except BaseException:
                            file_path.unlink(missing_ok=True)
                            raise

                        LOG.info(f"File downloaded successfully to {final_path}")
                        return str(final_path)

                authorization = RedirectHopAuthorization(
                    source_url=source_url,
                    target_url=current_url,
                    method="GET",
                )
                result = (
                    await authorize_request_hop_once(authorize_request_hop, authorization, dispatch)
                    if authorize_request_hop is not None
                    else await dispatch(())
                )
                if isinstance(result, str):
                    return result

                next_url = await validate_and_pin_redirect_url(current_url, result.location, resolver)
                if canonicalize_origin(next_url) is None:
                    raise HttpException(400, "[redacted]", "Redirect has no browser-canonicalizable HTTP origin")
                if allowed_redirect_origin is not None and _url_origin(next_url) != _url_origin(
                    allowed_redirect_origin
                ):
                    raise HttpException(400, "[redacted]", "Cross-origin redirect blocked by policy")
                request_headers, _ = strip_cross_origin_redirect_credentials(
                    request_headers,
                    None,
                    current_url,
                    next_url,
                )
                source_url, current_url = current_url, next_url
            raise SkyvernHTTPException(
                message=f"Too many redirects while downloading file: {current_url}",
                status_code=HTTPStatus.BAD_REQUEST,
            )
    except aiohttp.ClientResponseError as e:
        # Re-raised and handled at the action/block boundary; server rejections are external.
        LOG.warning("Failed to download file", status_code=e.status)
        raise
    except DownloadFileMaxSizeExceeded as e:
        LOG.warning(f"Failed to download file, max size exceeded: {e.max_size}", exc_info=True)
        raise
    except GoogleDriveFileNotAccessible:
        # User-actionable sharing problem on the customer's file, not a platform fault.
        LOG.warning("Google Drive download returned an HTML page instead of file content", url=requested_url)
        raise
    except PermissionError as e:
        LOG.warning(
            "Rejected storage URI download",
            url=url,
            organization_id=organization_id,
            reason=str(e),
        )
        raise
    except aiohttp.InvalidURL:
        # Malformed customer-provided URL - a client-data error, not a platform fault.
        LOG.warning("Failed to download file, invalid URL", exc_info=True)
        raise
    except BlockedHost:
        # SSRF guard rejected the customer-provided host; policy outcome, kept at warning.
        LOG.warning("Failed to download file, blocked host", exc_info=True)
        raise
    except Exception:
        # Dominated by the requested host erroring, which the run record already carries; a genuine
        # defect in this path therefore surfaces at warning, not error.
        LOG.warning("Failed to download file", exc_info=True)
        raise


async def resolve_local_or_download_file(
    file_url: str,
    run_id: str | None,
    organization_id: str | None = None,
    max_size_mb: int | None = None,
) -> str:
    """Resolve a file input to a local path.

    Absolute paths are validated against the run's download directory; anything else is downloaded.
    """
    # Absolute paths are the run-local convention; treating all non-remote strings as paths would misroute bad URLs.
    if file_url.startswith("/"):
        resolved = validate_local_file_path(file_url, run_id)
        if not os.path.isfile(resolved):
            raise FileNotFoundError(f"Local file not found: {file_url}")
        if max_size_mb is not None and os.path.getsize(resolved) > max_size_mb * 1024 * 1024:
            raise DownloadFileMaxSizeExceeded(max_size_mb)
        return resolved
    parsed = parse_artifact_content_url(file_url, settings.SKYVERN_BASE_URL)
    if parsed is not None:
        file_url = await app.ARTIFACT_MANAGER.remint_content_url_if_unverified(parsed, organization_id) or file_url
    return await download_file(file_url, max_size_mb=max_size_mb, organization_id=organization_id)


def zip_files(files_path: str, zip_file_path: str) -> str:
    with zipfile.ZipFile(zip_file_path, "w", zipfile.ZIP_DEFLATED) as zipf:
        for root, dirs, files in os.walk(files_path):
            for file in files:
                file_path = os.path.join(root, file)
                arcname = os.path.relpath(file_path, files_path)  # Relative path within the zip
                zipf.write(file_path, arcname)

    return zip_file_path


def unzip_files(zip_file_path: str, output_dir: str) -> None:
    with zipfile.ZipFile(zip_file_path, "r") as zip_ref:
        zip_ref.extractall(output_dir)


def unzip_bytes_to_temp_directory(zip_bytes: bytes, prefix: str) -> str:
    """Extract a downloaded archive into a fresh temp directory and return its path.

    The archive must be CLOSED before ZipFile reopens it by path: a write smaller than the io buffer
    (~8KB) is otherwise still unflushed, so ZipFile sees an empty file and raises BadZipFile.
    """
    temp_dir = make_temp_directory(prefix=prefix)
    with create_named_temporary_file(delete=False) as temp_zip_file:
        temp_zip_file.write(zip_bytes)
        temp_zip_file_path = temp_zip_file.name
    try:
        unzip_files(temp_zip_file_path, temp_dir)
    finally:
        # Cookie-bearing archives must not accumulate in TEMP_PATH on every retrieve.
        os.unlink(temp_zip_file_path)
    return temp_dir


_REMOTE_URL_PREFIXES = ("http://", "https://", "s3://", "gs://", "azure://", "www.")


def is_remote_url(path: str) -> bool:
    """Return True if the path is a remote URL (HTTP, S3, GCS, Azure) rather than a local filesystem path."""
    return path.startswith(_REMOTE_URL_PREFIXES)


def validate_local_file_path(candidate_path: str, run_id: str | None) -> str:
    """Validate that a local file path is within the workflow's download directory.

    Uses os.path.realpath() to resolve symlinks and '..' traversal before checking
    containment. Raises PermissionError if the path resolves outside the allowed directory.

    Returns the resolved canonical path on success.
    """
    if run_id is None:
        raise PermissionError("File access denied: no workflow run ID provided")

    if not candidate_path:
        LOG.warning("Empty path provided for file access validation", run_id=run_id)
        raise PermissionError(f"File access denied: path must not be empty for run {run_id}")

    allowed_dir = os.path.realpath(os.path.join(settings.DOWNLOAD_PATH, str(run_id)))
    resolved = os.path.realpath(candidate_path)

    # The resolved path must be the allowed dir itself or a child of it
    if resolved != allowed_dir and not resolved.startswith(allowed_dir + os.sep):
        LOG.warning(
            "Path traversal attempt blocked",
            candidate_path=candidate_path,
            resolved_path=resolved,
            allowed_dir=allowed_dir,
            run_id=run_id,
        )
        raise PermissionError(f"File access denied: path is outside the allowed download directory for run {run_id}")

    return resolved


def get_path_for_workflow_download_directory(run_id: str | None) -> Path:
    return Path(get_download_dir(run_id=run_id))


def get_download_dir(run_id: str | None) -> str:
    download_dir = os.path.join(settings.DOWNLOAD_PATH, str(run_id))
    os.makedirs(download_dir, exist_ok=True)
    return download_dir


RUN_TEMP_NAMESPACE = "runs"


def _is_single_path_component(value: str) -> bool:
    return bool(value) and value not in (".", "..") and Path(value).name == value


def get_run_temp_dir(organization_id: str, run_id: str) -> str:
    """The run's own scratch directory (``TEMP_PATH/runs/<org>/<run>``), created on first use.

    The one sanctioned place for per-run temp files: everything under it is deletable by run
    identity alone — activity teardown removes the finishing run's directory and the stale sweep
    reaps aged ones — so a tenant staging here needs no cleanup logic of its own. Both components
    must be single plain path elements because this path is later fed to rmtree by identity.
    """
    if not _is_single_path_component(organization_id) or not _is_single_path_component(run_id):
        raise ValueError("organization_id and run_id must be single path components")
    run_dir = os.path.join(settings.TEMP_PATH, RUN_TEMP_NAMESPACE, organization_id, run_id)
    # makedirs follows symlinked ancestors; a planted link under runs/ would materialize run dirs
    # outside TEMP_PATH and put them beyond the reapers' reach. Resolve-check before creating.
    if not Path(run_dir).resolve().is_relative_to(Path(settings.TEMP_PATH).resolve()):
        raise ValueError("run temp dir resolves outside TEMP_PATH")
    os.makedirs(run_dir, exist_ok=True)
    return run_dir


def resolve_run_download_id(context: "SkyvernContext | None", fallback_run_id: str | None = None) -> str | None:
    # Canonical key for a run's download dir: the producer (rebind) and consumers (FileUploadBlock,
    # download listener) must resolve the same key, or downloaded files are silently lost.
    if context:
        if context.run_id:
            return context.run_id
        if context.workflow_run_id:
            return context.workflow_run_id
        if context.task_id:
            return context.task_id
    return fallback_run_id


def list_files_in_directory(directory: Path, recursive: bool = False) -> list[str]:
    listed_files: list[str] = []
    for root, dirs, files in os.walk(directory):
        listed_files.extend([os.path.join(root, file) for file in files])
        if not recursive:
            break

    return listed_files


PENDING_EXTENSION_RENAME_WAIT_SECONDS = 3.0
PENDING_EXTENSION_RENAME_POLL_SECONDS = 0.1


async def wait_for_pending_extension_rename(download_dir: str, filename: str) -> str:
    """Return the file's final name, waiting out an in-flight extension-recovery rename.

    The browser finalizes a download under an extensionless name moments before the
    async download listener renames it in place (bare GUID -> GUID.pdf). Storage syncs
    must never upload the intermediate name — the same bytes would get registered
    under both names across two syncs. For an extensionless file, wait briefly for the
    rename to land and return the renamed filename; on timeout return the original so
    the file is still uploaded rather than dropped.
    """
    if Path(filename).suffix:
        return filename
    deadline = asyncio.get_event_loop().time() + PENDING_EXTENSION_RENAME_WAIT_SECONDS
    while asyncio.get_event_loop().time() < deadline:
        if not os.path.exists(os.path.join(download_dir, filename)):
            return _resolve_extension_rename_twin(download_dir, filename)
        await asyncio.sleep(PENDING_EXTENSION_RENAME_POLL_SECONDS)
    if not os.path.exists(os.path.join(download_dir, filename)):
        return _resolve_extension_rename_twin(download_dir, filename)
    return filename


def _resolve_extension_rename_twin(download_dir: str, filename: str) -> str:
    renamed = [
        candidate
        for candidate in os.listdir(download_dir)
        if candidate != filename and Path(candidate).stem == filename and Path(candidate).suffix
    ]
    return renamed[0] if renamed else filename


def list_downloading_files_in_directory(
    directory: Path, downloading_suffix: str = BROWSER_DOWNLOADING_SUFFIX
) -> list[str]:
    # check if there's any file is still downloading
    downloading_files: list[str] = []
    for file in list_files_in_directory(directory):
        path = Path(file)
        if path.suffix == downloading_suffix:
            downloading_files.append(file)
    return downloading_files


async def wait_for_download_finished(downloading_files: list[str], timeout: float = BROWSER_DOWNLOAD_TIMEOUT) -> None:
    cur_downloading_files = downloading_files
    try:
        async with asyncio.timeout(timeout):
            while len(cur_downloading_files) > 0:
                new_downloading_files: list[str] = []
                for path in cur_downloading_files:
                    # Check for cloud storage URIs (S3, GCS, or Azure)
                    parsed = urlparse(path)
                    if parsed.scheme in ("s3", "gs", "azure"):
                        if not await app.STORAGE.file_exists(path):
                            LOG.debug(
                                "downloading file is not found in cloud storage, means the file finished downloading",
                                path=path,
                            )
                            continue
                    else:
                        if not Path(path).exists():
                            LOG.debug(
                                "downloading file is not found in the local file system, means the file finished downloading",
                                path=path,
                            )
                            continue
                    new_downloading_files.append(path)
                cur_downloading_files = new_downloading_files
                await asyncio.sleep(1)
    except asyncio.TimeoutError:
        raise DownloadFileMaxWaitingTime(downloading_files=cur_downloading_files)


async def check_downloading_files_and_wait_for_download_to_complete(
    download_dir: Path,
    organization_id: str,
    browser_session_id: str | None = None,
    timeout: float = BROWSER_DOWNLOAD_TIMEOUT,
) -> None:
    # check if there's any file is still downloading
    downloading_files = list_downloading_files_in_directory(download_dir)
    if browser_session_id:
        files_in_browser_session = await app.STORAGE.list_downloading_files_in_browser_session(
            organization_id=organization_id, browser_session_id=browser_session_id
        )
        downloading_files = downloading_files + files_in_browser_session

    if len(downloading_files) == 0:
        return

    LOG.info(
        "File downloading hasn't completed, wait for a while",
        downloading_files=downloading_files,
    )
    try:
        await wait_for_download_finished(
            downloading_files=downloading_files,
            timeout=timeout,
        )
    except DownloadFileMaxWaitingTime as e:
        LOG.warning(
            "There're several long-time downloading files, these files might be broken",
            downloading_files=e.downloading_files,
        )


def get_number_of_files_in_directory(directory: Path, recursive: bool = False) -> int:
    return len(list_files_in_directory(directory, recursive))


def sanitize_filename(filename: str) -> str:
    return "".join(c for c in filename if c.isalnum() or c in ["-", "_", ".", "%", " "])


def guess_extension_from_file(file_path: str | Path) -> str:
    """Infer a file's extension (with leading dot) from its magic bytes, or "" if unreadable/unknown."""
    try:
        kind = filetype.guess(str(file_path))
    except OSError:
        return ""
    return f".{kind.extension}" if kind else ""


def recover_download_extension(file_path: str | Path, download_suffix: str | None = None) -> str:
    """Extension to append to a downloaded file that has none, sniffed from its content.

    Returns "" when ``download_suffix`` already carries its own extension, so the final
    ``download_suffix + extension`` name is not doubled (e.g. invoice.pdf + .pdf).
    """
    if download_suffix and Path(download_suffix).suffix:
        return ""
    return guess_extension_from_file(file_path)


def rename_file(file_path: str, new_file_name: str) -> str:
    try:
        new_file_name = sanitize_filename(new_file_name)
        new_file_path = os.path.join(os.path.dirname(file_path), new_file_name)
        os.rename(file_path, new_file_path)
        return new_file_path
    except Exception:
        LOG.exception(f"Failed to rename file {file_path} to {new_file_name}")
        return file_path


def calculate_sha256_for_file(file_path: str) -> str:
    """Helper function to calculate SHA256 hash of a file."""
    sha256_hash = hashlib.sha256()
    with open(file_path, "rb") as f:
        for byte_block in iter(lambda: f.read(4096), b""):
            sha256_hash.update(byte_block)
    return sha256_hash.hexdigest()


def create_folder_if_not_exist(dir: str) -> None:
    path = Path(dir)
    path.mkdir(parents=True, exist_ok=True)


def get_skyvern_temp_dir() -> str:
    temp_dir = settings.TEMP_PATH
    create_folder_if_not_exist(temp_dir)
    return temp_dir


def make_temp_directory(
    suffix: str | None = None,
    prefix: str | None = None,
) -> str:
    temp_dir = settings.TEMP_PATH
    create_folder_if_not_exist(temp_dir)
    return tempfile.mkdtemp(suffix=suffix, prefix=prefix, dir=temp_dir)


def is_temp_working_dir(path: str) -> bool:
    """A working copy under the Skyvern temp root — a fresh temp dir or a storage extraction (S3/GCS/
    Azure) — is safe to delete. LocalStorage.retrieve_browser_profile returns the LIVE profile dir
    (outside TEMP_PATH); deleting that erases saved state. Fail closed on any doubt — leaking a temp
    dir beats destroying live state."""
    try:
        return Path(path).resolve().is_relative_to(Path(settings.TEMP_PATH).resolve())
    except Exception:
        return False


def create_named_temporary_file(delete: bool = True, file_name: str | None = None) -> tempfile._TemporaryFileWrapper:
    temp_dir = settings.TEMP_PATH
    create_folder_if_not_exist(temp_dir)

    if file_name:
        # Sanitize the filename to remove any dangerous characters
        safe_file_name = sanitize_filename(file_name)
        # Create file with exact name (without random characters)
        file_path = os.path.join(temp_dir, safe_file_name)
        if not os.path.abspath(file_path).startswith(os.path.abspath(temp_dir) + os.sep):
            raise ValueError(f"Unsafe filename in temporary file creation: {safe_file_name!r}")
        # Open in binary mode and return a NamedTemporaryFile-like object
        file = open(file_path, "wb")
        return tempfile._TemporaryFileWrapper(file, file_path, delete=delete)

    return tempfile.NamedTemporaryFile(dir=temp_dir, delete=delete)


def clean_up_dir(dir: str) -> None:
    if not os.path.exists(dir):
        return

    if os.path.isfile(dir):
        os.unlink(dir)
        return

    for item in os.listdir(dir):
        item_path = os.path.join(dir, item)
        if os.path.isfile(item_path) or os.path.islink(item_path):
            os.unlink(item_path)
        elif os.path.isdir(item_path):
            shutil.rmtree(item_path)

    return


def clean_up_skyvern_temp_dir() -> None:
    return clean_up_dir(get_skyvern_temp_dir())


def parse_uri_to_path(uri: str) -> str:
    parsed_uri = urlparse(uri)
    if parsed_uri.scheme != "file":
        raise ValueError(f"Invalid URI scheme: {parsed_uri.scheme} expected: file")
    path = parsed_uri.netloc + parsed_uri.path
    return unquote(path)
