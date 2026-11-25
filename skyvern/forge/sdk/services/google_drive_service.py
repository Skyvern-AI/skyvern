from __future__ import annotations

import asyncio
import json
import os
import re
import tempfile
import uuid
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass
from mimetypes import guess_type
from pathlib import Path
from typing import Any, Literal
from urllib.parse import parse_qs, quote, urlencode, urlparse

import aiofiles
import httpx

from skyvern.config import settings
from skyvern.exceptions import DownloadFileMaxSizeExceeded
from skyvern.forge.sdk.integrations.google.client import (
    GoogleAPIError,
    error_from_response,
    is_rate_limit_reason,
    is_rate_limit_refusal,
)
from skyvern.forge.sdk.integrations.http import compute_backoff
from skyvern.forge.sdk.services.file_download_utils import publish_unique_download

DRIVE_UPLOAD_API_BASE = "https://www.googleapis.com/upload/drive/v3"
DRIVE_API_BASE = "https://www.googleapis.com/drive/v3"
DRIVE_MULTIPART_UPLOAD_MAX_BYTES = 5 * 1024 * 1024
# Reserve headroom for JSON metadata and MIME boundaries below the 5 MiB multipart request cap.
DRIVE_MULTIPART_FILE_MAX_BYTES = DRIVE_MULTIPART_UPLOAD_MAX_BYTES - 10 * 1024
# 8 MiB is a multiple of Drive's required 256 KiB unit for non-final chunks.
DRIVE_RESUMABLE_CHUNK_BYTES = 8 * 1024 * 1024
DRIVE_DOWNLOAD_CHUNK_BYTES = 64 * 1024
GOOGLE_WORKSPACE_MIME_PREFIX = "application/vnd.google-apps."
GOOGLE_DOC_MIME_TYPE = "application/vnd.google-apps.document"
GOOGLE_SHEET_MIME_TYPE = "application/vnd.google-apps.spreadsheet"
# Keyed by suffix, not guess_type(): Python's built-in mime table has no .xlsx entry, so hosts without
# /etc/mime.types would otherwise send XLSX as application/octet-stream and skip the conversion.
SHEET_CONVERTIBLE_MEDIA_TYPES = {
    ".csv": "text/csv",
    ".xls": "application/vnd.ms-excel",
    ".xlsx": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
}
_DRIVE_FILE_ID_PATTERN = re.compile(r"^[A-Za-z0-9_-]+$")

_DEFAULT_BACKOFF_SECONDS = 1.0
_FILE_PERMISSION_403_REASONS = frozenset({"insufficientFilePermissions", "teamDriveMembershipRequired"})
_sleep = asyncio.sleep


class GoogleDriveAPIError(GoogleAPIError):
    pass


class GoogleDriveNativeDocumentError(GoogleDriveAPIError):
    def __init__(self) -> None:
        super().__init__(
            status=400,
            code="fileNotDownloadable",
            message="Google Workspace documents require export before download.",
        )


class GoogleDriveResumableTransportError(Exception):
    """Retryable transport failure during a resumable chunk PUT — the loop should query offset and resume."""


@dataclass(frozen=True)
class UploadedDriveFile:
    id: str
    web_view_link: str | None = None


@dataclass(frozen=True)
class GoogleDriveFileReference:
    file_id: str
    resource_key: str | None = None


@dataclass(frozen=True)
class DownloadableDriveFile:
    name: str
    mime_type: str | None
    size: int | None


@dataclass(frozen=True)
class GoogleDriveMultipartUploadRequest:
    target_url: str
    headers: dict[str, str]
    content: bytes


@dataclass(frozen=True)
class GoogleDriveResumableInitiateRequest:
    target_url: str
    headers: dict[str, str]
    content: bytes


@dataclass(frozen=True)
class ResumableChunkResponse:
    status_code: int
    range_header: str | None
    body_text: str | None


def build_resumable_chunk_headers(
    *,
    content_type: str,
    start: int,
    end: int,
    total: int,
    chunk_len: int,
) -> dict[str, str]:
    return {
        "Content-Type": content_type,
        "Content-Length": str(chunk_len),
        "Content-Range": f"bytes {start}-{end}/{total}",
    }


def build_resumable_status_query_headers(*, total: int) -> dict[str, str]:
    return {
        "Content-Range": f"bytes */{total}",
        "Content-Length": "0",
    }


def parse_resumable_range_offset(range_header: str | None) -> int:
    if not range_header:
        return 0
    try:
        unit, byte_range = range_header.strip().split("=", 1)
        start_text, last_byte_text = byte_range.split("-", 1)
        start = int(start_text.strip())
        last_byte = int(last_byte_text.strip())
    except (TypeError, ValueError):
        return 0
    if unit.lower() != "bytes" or start != 0 or last_byte < 0:
        return 0
    return last_byte + 1


def is_retryable_resumable_status(status_code: int) -> bool:
    return status_code == 429 or 500 <= status_code < 600


def is_retryable_resumable_response(status_code: int, body_text: str | None) -> bool:
    return is_retryable_resumable_status(status_code) or is_rate_limit_refusal(status_code, body_text)


def _compute_backoff(attempt: int, retry_after: str | None) -> float:
    return compute_backoff(attempt, retry_after, base_seconds=_DEFAULT_BACKOFF_SECONDS, factor=2)


def _raise_for_error(response: httpx.Response) -> None:
    if response.is_success:
        return
    raise error_from_response(
        response,
        GoogleDriveAPIError,
        default_message="Google Drive API error",
        body_text_when_message_missing=False,
    )


def error_from_response_body(status: int, body: str | None) -> GoogleDriveAPIError:
    try:
        _raise_for_error(httpx.Response(status, content=(body or "").encode()))
    except GoogleDriveAPIError as exc:
        return exc
    return GoogleDriveAPIError(status=status, code=None, message="Google Drive API error")


def explain_access_error(
    exc: GoogleDriveAPIError, *, operation: Literal["upload", "download"]
) -> GoogleDriveAPIError | None:
    """Clearer text for a Drive permission or visibility error, or None when ``exc`` is neither."""
    # Google's own message is dropped on purpose: for notFound it echoes the file or folder ID.
    # Callers raise the result `from None` so the original error stays out of logged tracebacks too.
    if exc.status == 403 and exc.code in _FILE_PERMISSION_403_REASONS and operation == "upload":
        hint = (
            "The connected Google account cannot add or change files in this Drive folder or file. "
            "On a shared drive, its role must be Contributor or higher."
        )
    elif exc.status == 403 and exc.code in _FILE_PERMISSION_403_REASONS:
        hint = (
            "The connected Google account cannot download this Drive file. Check that it can view the file "
            "(on a shared drive, that it is a member) and that the owner has not turned off downloading."
        )
    elif exc.status == 404 and exc.code == "notFound":
        hint = (
            "The connected Google account cannot see this Drive folder or file. Check the URL, and that "
            "the account has access to it or is a member of its shared drive."
        )
    else:
        return None
    return GoogleDriveAPIError(status=exc.status, code=exc.code, message=f"{hint} (Google Drive reason: {exc.code})")


def extract_folder_id(value: str | None) -> str | None:
    """Normalize a user-entered folder ID or Drive folder URL. ``None`` means the account's My Drive root."""
    candidate = (value or "").strip()
    if not candidate:
        return None

    parsed = urlparse(candidate)
    if parsed.scheme and parsed.netloc:
        hostname = parsed.hostname or ""
        if parsed.scheme != "https" or not (hostname == "google.com" or hostname.endswith(".google.com")):
            raise ValueError("Google Drive folder URL must be an https://*.google.com URL")
        parts = [part for part in parsed.path.split("/") if part]
        for index, part in enumerate(parts):
            if part == "folders" and index + 1 < len(parts):
                return parts[index + 1]
        raise ValueError("Google Drive folder URL must contain /folders/{folder_id}")

    return candidate


def _assert_multipart_upload_size(file_path: str, body_size: int | None = None) -> None:
    file_size = Path(file_path).stat().st_size
    if file_size > DRIVE_MULTIPART_UPLOAD_MAX_BYTES:
        raise GoogleDriveAPIError(
            status=413,
            code="file_too_large",
            message="Google Drive multipart uploads are limited to 5 MB; larger files use resumable upload.",
        )
    if body_size is not None and body_size > DRIVE_MULTIPART_UPLOAD_MAX_BYTES:
        raise GoogleDriveAPIError(
            status=413,
            code="multipart_body_too_large",
            message=(
                "Google Drive multipart uploads are limited to 5 MB including metadata; larger files use resumable upload."
            ),
        )


def _multipart_body(
    *,
    metadata: dict[str, Any],
    file_path: str,
    content_type: str,
    boundary: str,
) -> bytes:
    metadata_bytes = json.dumps(metadata, separators=(",", ":")).encode("utf-8")
    return b"".join(
        [
            f"--{boundary}\r\nContent-Type: application/json; charset=UTF-8\r\n\r\n".encode(),
            metadata_bytes,
            f"\r\n--{boundary}\r\nContent-Type: {content_type}\r\n\r\n".encode(),
            Path(file_path).read_bytes(),
            f"\r\n--{boundary}--\r\n".encode(),
        ]
    )


def _converts_to_sheet(file_path: str, save_as_sheet: bool) -> bool:
    return save_as_sheet and Path(file_path).suffix.lower() in SHEET_CONVERTIBLE_MEDIA_TYPES


def upload_content_type(file_path: str, save_as_sheet: bool = False) -> str:
    if _converts_to_sheet(file_path, save_as_sheet):
        return SHEET_CONVERTIBLE_MEDIA_TYPES[Path(file_path).suffix.lower()]
    return guess_type(file_path)[0] or "application/octet-stream"


def _upload_metadata(
    *,
    file_path: str,
    folder_id: str | None,
    save_as_sheet: bool,
) -> dict[str, Any]:
    path = Path(file_path)
    metadata: dict[str, Any] = {"name": path.name}
    if _converts_to_sheet(file_path, save_as_sheet):
        # Drive converts the media to this type on create; the media part keeps its source content type.
        metadata = {"name": path.stem, "mimeType": GOOGLE_SHEET_MIME_TYPE}
    # Drive puts the file in the account's My Drive root when ``parents`` is omitted.
    if folder_id:
        metadata["parents"] = [folder_id]
    return metadata


def build_multipart_upload_request(
    *,
    access_token: str,
    file_path: str,
    folder_id: str | None,
    save_as_sheet: bool = False,
) -> GoogleDriveMultipartUploadRequest:
    """Build a bounded Google Drive multipart upload request body.

    ``folder_id`` is expected to be a normalized folder ID, or ``None`` to upload
    to the My Drive root. Call ``extract_folder_id`` on user-entered values
    before invoking this helper.
    """
    _assert_multipart_upload_size(file_path)
    content_type = upload_content_type(file_path, save_as_sheet)
    metadata = _upload_metadata(file_path=file_path, folder_id=folder_id, save_as_sheet=save_as_sheet)
    boundary = f"skyvern-{uuid.uuid4().hex}"
    content = _multipart_body(
        metadata=metadata,
        file_path=file_path,
        content_type=content_type,
        boundary=boundary,
    )
    _assert_multipart_upload_size(file_path, len(content))
    query = urlencode({"uploadType": "multipart", "fields": "id,name,webViewLink", "supportsAllDrives": "true"})
    return GoogleDriveMultipartUploadRequest(
        target_url=f"{DRIVE_UPLOAD_API_BASE}/files?{query}",
        headers={
            "Authorization": f"Bearer {access_token}",
            "Accept": "application/json",
            "Content-Type": f"multipart/related; boundary={boundary}",
        },
        content=content,
    )


def should_use_resumable_upload(file_path: str) -> bool:
    return Path(file_path).stat().st_size > DRIVE_MULTIPART_FILE_MAX_BYTES


def build_resumable_initiate_request(
    *,
    access_token: str,
    file_path: str,
    folder_id: str | None,
    save_as_sheet: bool = False,
) -> GoogleDriveResumableInitiateRequest:
    path = Path(file_path)
    file_size = path.stat().st_size
    content_type = upload_content_type(file_path, save_as_sheet)
    metadata = _upload_metadata(file_path=file_path, folder_id=folder_id, save_as_sheet=save_as_sheet)
    query = urlencode({"uploadType": "resumable", "fields": "id,name,webViewLink", "supportsAllDrives": "true"})
    return GoogleDriveResumableInitiateRequest(
        target_url=f"{DRIVE_UPLOAD_API_BASE}/files?{query}",
        headers={
            "Authorization": f"Bearer {access_token}",
            "Content-Type": "application/json; charset=UTF-8",
            "X-Upload-Content-Type": content_type,
            "X-Upload-Content-Length": str(file_size),
        },
        content=json.dumps(metadata, separators=(",", ":")).encode("utf-8"),
    )


def extract_resumable_session_uri(headers: Mapping[str, str]) -> str:
    for name, value in headers.items():
        if name.lower() == "location" and value.strip():
            session_uri = value.strip()
            parsed = urlparse(session_uri)
            hostname = parsed.hostname or ""
            try:
                port = parsed.port
            except ValueError:
                port = -1
            if (
                parsed.scheme != "https"
                or parsed.username is not None
                or parsed.password is not None
                or port not in {None, 443}
                or not (hostname == "googleapis.com" or hostname.endswith(".googleapis.com"))
            ):
                raise GoogleDriveAPIError(
                    status=502,
                    code="invalid_resumable_session",
                    message="Google Drive resumable session URI is not a googleapis.com https URL",
                )
            return session_uri
    raise GoogleDriveAPIError(
        status=502,
        code="missing_resumable_session",
        message="Google Drive resumable upload did not return a session URI",
    )


def uploaded_file_from_payload(payload: Any) -> UploadedDriveFile:
    if not isinstance(payload, dict):
        raise GoogleDriveAPIError(status=500, code="malformed_response", message="Malformed Drive upload response")
    file_id = payload.get("id")
    if not file_id:
        raise GoogleDriveAPIError(status=500, code="malformed_response", message="Drive response missing file id")
    return UploadedDriveFile(
        id=file_id,
        web_view_link=payload.get("webViewLink"),
    )


def _uploaded_file_from_resumable_response(response: ResumableChunkResponse) -> UploadedDriveFile:
    try:
        payload = json.loads(response.body_text or "{}")
    except ValueError as exc:
        raise GoogleDriveAPIError(
            status=500,
            code="malformed_response",
            message="Drive response was not valid JSON",
        ) from exc
    return uploaded_file_from_payload(payload)


def _raise_unexpected_resumable_status(status_code: int) -> None:
    raise GoogleDriveAPIError(
        status=502,
        code="resumable_unexpected_status",
        message=f"Google Drive resumable upload returned unexpected status {status_code}",
    )


async def run_chunked_resumable_upload(
    *,
    file_path: str,
    total: int,
    send: Callable[[bytes, dict[str, str]], Awaitable[ResumableChunkResponse]],
    max_attempts: int,
    save_as_sheet: bool = False,
) -> UploadedDriveFile:
    content_type = upload_content_type(file_path, save_as_sheet)
    offset = 0
    attempts = 0
    probe_first = False  # after a chunk transport failure, learn Drive's committed offset before re-sending

    async with aiofiles.open(file_path, "rb") as file:
        while True:
            if probe_first:
                try:
                    response = await send(b"", build_resumable_status_query_headers(total=total))
                except GoogleDriveResumableTransportError:
                    attempts += 1
                    if attempts >= max_attempts:
                        raise GoogleDriveAPIError(
                            status=503,
                            code="resumable_upload_failed",
                            message="Google Drive resumable upload failed after exhausting resume attempts",
                        ) from None
                    await _sleep(_compute_backoff(attempts, None))
                    continue
                probe_first = False
            else:
                await file.seek(offset)
                chunk = await file.read(DRIVE_RESUMABLE_CHUNK_BYTES)
                if not chunk:
                    raise GoogleDriveAPIError(
                        status=500,
                        code="resumable_incomplete",
                        message="Google Drive resumable upload could not read the expected file bytes",
                    )
                end = offset + len(chunk) - 1
                headers = build_resumable_chunk_headers(
                    content_type=content_type,
                    start=offset,
                    end=end,
                    total=total,
                    chunk_len=len(chunk),
                )
                try:
                    response = await send(chunk, headers)
                except GoogleDriveResumableTransportError:
                    # Do not count an attempt here: always reconcile via a status query first, so a lost-but-committed
                    # chunk is detected instead of re-uploaded into a duplicate.
                    probe_first = True
                    continue

            if response.status_code in (200, 201):
                return _uploaded_file_from_resumable_response(response)
            if response.status_code == 308:
                new_offset = parse_resumable_range_offset(response.range_header)
                if new_offset > offset:
                    offset = new_offset
                    attempts = 0
                else:
                    attempts += 1
                    if attempts >= max_attempts:
                        raise GoogleDriveAPIError(
                            status=503,
                            code="resumable_upload_failed",
                            message="Google Drive resumable upload made no progress after exhausting resume attempts",
                        ) from None
                    await _sleep(_compute_backoff(attempts, None))
                continue
            _raise_unexpected_resumable_status(response.status_code)


async def _post_multipart_with_retry(
    client: httpx.AsyncClient,
    request: GoogleDriveMultipartUploadRequest,
) -> httpx.Response:
    """POST a Drive multipart upload without replaying ambiguous creates.

    Google Drive files.create is not idempotent. Retrying after Drive has seen
    the POST can create duplicate files, so only retry failures that occur
    while acquiring a connection and fail all ambiguous mutation outcomes.
    """
    max_attempts = max(1, settings.GOOGLE_DRIVE_API_MAX_RETRIES)
    for attempt in range(1, max_attempts + 1):
        try:
            return await client.post(
                request.target_url,
                headers=request.headers,
                content=request.content,
            )
        except (httpx.ConnectError, httpx.ConnectTimeout, httpx.PoolTimeout) as exc:
            if attempt == max_attempts:
                raise GoogleDriveAPIError(
                    status=503,
                    code="upstream_unavailable",
                    message=f"Google Drive upload connection failure: {exc}",
                ) from exc
            await _sleep(_compute_backoff(attempt, None))
            continue
        except (httpx.TransportError, httpx.TimeoutException) as exc:
            raise GoogleDriveAPIError(
                status=503,
                code="ambiguous_upload_status",
                message=(
                    "Google Drive upload status is unknown after a transport failure. "
                    "Not retrying automatically to avoid creating duplicate files."
                ),
            ) from exc
    raise AssertionError("Drive upload retry loop exited without a response")


async def _post_resumable_initiate_with_retry(
    client: httpx.AsyncClient,
    request: GoogleDriveResumableInitiateRequest,
) -> httpx.Response:
    max_attempts = max(1, settings.GOOGLE_DRIVE_API_MAX_RETRIES)
    for attempt in range(1, max_attempts + 1):
        try:
            return await client.post(
                request.target_url,
                headers=request.headers,
                content=request.content,
            )
        except (httpx.ConnectError, httpx.ConnectTimeout, httpx.PoolTimeout) as exc:
            if attempt == max_attempts:
                raise GoogleDriveAPIError(
                    status=503,
                    code="upstream_unavailable",
                    message=f"Google Drive resumable upload connection failure: {exc}",
                ) from exc
            await _sleep(_compute_backoff(attempt, None))
            continue
        except (httpx.TransportError, httpx.TimeoutException) as exc:
            raise GoogleDriveAPIError(
                status=503,
                code="ambiguous_upload_status",
                message="Google Drive resumable upload initiation status is unknown after a transport failure.",
            ) from exc
    raise AssertionError("Drive resumable upload retry loop exited without a response")


async def _upload_file_resumable(
    *,
    access_token: str,
    file_path: str,
    folder_id: str | None,
    save_as_sheet: bool = False,
) -> UploadedDriveFile:
    initiate_request = build_resumable_initiate_request(
        access_token=access_token,
        file_path=file_path,
        folder_id=folder_id,
        save_as_sheet=save_as_sheet,
    )

    async with httpx.AsyncClient(timeout=settings.GOOGLE_DRIVE_API_TIMEOUT_SECONDS) as client:
        initiate_response = await _post_resumable_initiate_with_retry(client, initiate_request)
        _raise_for_error(initiate_response)
        session_uri = extract_resumable_session_uri(initiate_response.headers)

        async def send(body: bytes, headers: dict[str, str]) -> ResumableChunkResponse:
            try:
                response = await client.put(session_uri, headers=headers, content=body)
            except (
                httpx.ConnectError,
                httpx.ConnectTimeout,
                httpx.PoolTimeout,
                httpx.TransportError,
                httpx.TimeoutException,
            ) as exc:
                raise GoogleDriveResumableTransportError(str(exc)) from exc
            if response.status_code in (200, 201, 308):
                return ResumableChunkResponse(
                    status_code=response.status_code,
                    range_header=response.headers.get("Range"),
                    body_text=response.text,
                )
            if is_retryable_resumable_response(response.status_code, response.text):
                raise GoogleDriveResumableTransportError(
                    f"Google Drive returned retryable status {response.status_code}"
                )
            _raise_for_error(response)
            raise GoogleDriveAPIError(
                status=502,
                code="resumable_unexpected_status",
                message=f"Google Drive resumable upload returned unexpected status {response.status_code}",
            )

        return await run_chunked_resumable_upload(
            file_path=file_path,
            total=Path(file_path).stat().st_size,
            send=send,
            max_attempts=max(1, settings.GOOGLE_DRIVE_API_MAX_RETRIES),
            save_as_sheet=save_as_sheet,
        )


def extract_file_reference(value: str) -> GoogleDriveFileReference:
    candidate = value.strip()
    if _DRIVE_FILE_ID_PATTERN.fullmatch(candidate):
        return GoogleDriveFileReference(file_id=candidate)

    parsed = urlparse(candidate)
    if parsed.scheme.lower() != "https" or parsed.hostname not in ("drive.google.com", "docs.google.com"):
        raise ValueError("Google Drive file URL must use https://drive.google.com or https://docs.google.com")

    expected_prefix = ["file", "d"] if parsed.hostname == "drive.google.com" else ["document", "d"]
    segments = [segment for segment in parsed.path.split("/") if segment]
    if len(segments) >= 3 and segments[1] == "u" and segments[2].isdigit():
        # Signed into several accounts, Google inserts /u/<account-index>/ after the first segment.
        del segments[1:3]
    if len(segments) < 3 or segments[:2] != expected_prefix or not _DRIVE_FILE_ID_PATTERN.fullmatch(segments[2]):
        raise ValueError("Unsupported Google Drive file URL")
    if parsed.hostname == "docs.google.com" and segments[2] == "e":
        # Publish-to-web links (/document/d/e/<public-id>/pub) carry an ID Drive cannot resolve.
        raise ValueError("Published Google Docs links are not Drive file URLs")

    query = parse_qs(parsed.query, keep_blank_values=True)
    resource_keys = query.get("resourcekey", []) + query.get("resourceKey", [])
    if len(resource_keys) > 1:
        raise ValueError("Google Drive file URL contains multiple resource keys")
    resource_key = resource_keys[0] if resource_keys else None
    if resource_key is not None and not _DRIVE_FILE_ID_PATTERN.fullmatch(resource_key):
        raise ValueError("Google Drive resource key is invalid")
    return GoogleDriveFileReference(file_id=segments[2], resource_key=resource_key)


def extract_file_id(value: str) -> str:
    return extract_file_reference(value).file_id


def _downloadable_file_from_payload(payload: Mapping[str, Any], file_id: str) -> DownloadableDriveFile:
    raw_name = payload.get("name")
    name = Path(str(raw_name)).name if raw_name else file_id
    safe_name = (
        "".join(character for character in name if character.isalnum() or character in "-_.% ").strip(". ") or file_id
    )[:200]

    raw_size = payload.get("size")
    try:
        size = int(raw_size) if raw_size is not None else None
    except (TypeError, ValueError):
        size = None

    raw_mime_type = payload.get("mimeType")
    mime_type = str(raw_mime_type) if raw_mime_type is not None else None
    return DownloadableDriveFile(
        name=safe_name,
        mime_type=mime_type,
        size=size,
    )


async def _get_download_metadata_with_retry(
    client: httpx.AsyncClient,
    url: str,
    *,
    headers: Mapping[str, str],
    params: Mapping[str, str],
) -> httpx.Response:
    max_attempts = max(1, settings.GOOGLE_DRIVE_API_MAX_RETRIES)
    for attempt in range(1, max_attempts + 1):
        retry_after: str | None = None
        try:
            response = await client.get(url, headers=headers, params=params)
        except httpx.TransportError as exc:
            if attempt == max_attempts:
                raise GoogleDriveAPIError(
                    status=503,
                    code="upstream_unavailable",
                    message=f"Google Drive download transport failure after {max_attempts} attempts: {exc}",
                ) from exc
        else:
            if not is_retryable_resumable_response(response.status_code, response.text) or attempt == max_attempts:
                return response
            retry_after = response.headers.get("Retry-After")
        await _sleep(_compute_backoff(attempt, retry_after))
    raise AssertionError("Drive download metadata retry loop exited without a response")


async def download_file(
    *,
    access_token: str,
    file_id: str,
    resource_key: str | None = None,
    output_dir: str,
    max_size_mb: int,
) -> str:
    validated_file_id = extract_file_id(file_id)
    max_size_bytes = max_size_mb * 1024 * 1024
    if resource_key is not None and not _DRIVE_FILE_ID_PATTERN.fullmatch(resource_key):
        raise ValueError("Google Drive resource key is invalid")
    headers = {"Authorization": f"Bearer {access_token}"}
    if resource_key is not None:
        headers["X-Goog-Drive-Resource-Keys"] = f"{validated_file_id}/{resource_key}"
    file_url = f"{DRIVE_API_BASE}/files/{quote(validated_file_id, safe='')}"

    async with httpx.AsyncClient(
        timeout=settings.GOOGLE_DRIVE_API_TIMEOUT_SECONDS,
        follow_redirects=True,
    ) as client:
        metadata_response = await _get_download_metadata_with_retry(
            client,
            file_url,
            headers=headers,
            params={
                "fields": "id,name,mimeType,size",
                "supportsAllDrives": "true",
            },
        )
        _raise_for_error(metadata_response)
        metadata = _downloadable_file_from_payload(metadata_response.json() or {}, validated_file_id)

        media_url = file_url
        media_params = {"alt": "media", "supportsAllDrives": "true"}
        output_name = metadata.name
        if metadata.mime_type == GOOGLE_DOC_MIME_TYPE:
            # Drive caps exports at 10 MB and answers larger Docs with a non-retryable 403 exportSizeLimitExceeded.
            media_url = f"{file_url}/export"
            media_params = {"mimeType": "text/markdown"}
            output_name = f"{metadata.name}.md"
        elif metadata.mime_type and metadata.mime_type.startswith(GOOGLE_WORKSPACE_MIME_PREFIX):
            raise GoogleDriveNativeDocumentError()
        if metadata.size is not None and metadata.size > max_size_bytes:
            raise DownloadFileMaxSizeExceeded(max_size_mb)

        destination_dir = Path(output_dir)
        destination_dir.mkdir(parents=True, exist_ok=True)
        file_descriptor, temporary_name = tempfile.mkstemp(
            dir=destination_dir,
            prefix=".drive-download-",
            suffix=".part",
        )
        os.close(file_descriptor)
        temporary_path = Path(temporary_name)

        max_attempts = max(1, settings.GOOGLE_DRIVE_API_MAX_RETRIES)
        try:
            for attempt in range(1, max_attempts + 1):
                retry_response = False
                retry_after: str | None = None
                try:
                    async with client.stream(
                        "GET",
                        media_url,
                        headers=headers,
                        params=media_params,
                    ) as response:
                        if response.status_code >= 400:
                            await response.aread()
                            if (
                                is_retryable_resumable_response(response.status_code, response.text)
                                and attempt < max_attempts
                            ):
                                retry_response = True
                                retry_after = response.headers.get("Retry-After")
                            else:
                                _raise_for_error(response)
                                raise AssertionError("Drive download error response was not raised")
                        else:
                            raw_content_length = response.headers.get("Content-Length")
                            if raw_content_length is not None:
                                try:
                                    if int(raw_content_length) > max_size_bytes:
                                        raise DownloadFileMaxSizeExceeded(max_size_mb)
                                except ValueError:
                                    pass

                            bytes_written = 0
                            async with aiofiles.open(temporary_path, "wb") as destination:
                                async for chunk in response.aiter_bytes(DRIVE_DOWNLOAD_CHUNK_BYTES):
                                    bytes_written += len(chunk)
                                    if bytes_written > max_size_bytes:
                                        raise DownloadFileMaxSizeExceeded(max_size_mb)
                                    await destination.write(chunk)
                except httpx.TransportError as exc:
                    if attempt == max_attempts:
                        raise GoogleDriveAPIError(
                            status=503,
                            code="upstream_unavailable",
                            message=f"Google Drive download transport failure after {max_attempts} attempts: {exc}",
                        ) from exc
                    await _sleep(_compute_backoff(attempt, None))
                    continue
                if retry_response:
                    await _sleep(_compute_backoff(attempt, retry_after))
                    continue
                break
            else:
                raise AssertionError("Drive download media retry loop exited without a response")
            destination_path = publish_unique_download(temporary_path, destination_dir, output_name)
            return str(destination_path)
        except BaseException:
            temporary_path.unlink(missing_ok=True)
            raise


async def upload_file(
    *,
    access_token: str,
    file_path: str,
    folder_id: str | None,
    save_as_sheet: bool = False,
) -> UploadedDriveFile:
    if should_use_resumable_upload(file_path):
        return await _upload_file_resumable(
            access_token=access_token,
            file_path=file_path,
            folder_id=folder_id,
            save_as_sheet=save_as_sheet,
        )

    request = build_multipart_upload_request(
        access_token=access_token,
        file_path=file_path,
        folder_id=folder_id,
        save_as_sheet=save_as_sheet,
    )

    async with httpx.AsyncClient(timeout=settings.GOOGLE_DRIVE_API_TIMEOUT_SECONDS) as client:
        response = await _post_multipart_with_retry(client, request)

    _raise_for_error(response)
    payload = response.json() or {}
    return uploaded_file_from_payload(payload)


GOOGLE_FOLDER_MIME_TYPE = "application/vnd.google-apps.folder"
DRIVE_LIST_FOLDER_MAX_RESULTS = 1000
_DRIVE_ITEM_FIELDS = "id,name,mimeType,parents,driveId,webViewLink,trashed"
_DRIVE_LIST_FIELDS = "nextPageToken,files(id,name,mimeType,modifiedTime,size,webViewLink)"
DriveFolderOperation = Literal["list_folder", "create_folder", "move_file"]

# Reasons Drive gives for a refused move or create, each with what the person can do about it.
_FOLDER_ACTION_403_HINTS: dict[str, str] = {
    "teamDrivesFolderMoveInNotSupported": (
        "Google Drive does not let this account move a folder from My Drive into a shared drive. "
        "Move the files inside it instead."
    ),
    "teamDrivesParentLimit": "An item in a shared drive can be in only one folder.",
    "cannotMoveTrashedItemIntoTeamDrive": "This item is in the trash. Restore it before moving it.",
    "cannotMoveTrashedItemOutOfTeamDrive": "This item is in the trash. Restore it before moving it.",
    "teamDrivesCrossDomainMoveRestriction": (
        "Google Drive does not allow moving this item to a shared drive owned by another organization."
    ),
    "fileOwnerNotMemberOfTeamDrive": (
        "The item's owner is not a member of the destination shared drive, so Google Drive cannot move it there."
    ),
    "numChildrenInNonRootLimitExceeded": "The destination folder has reached Google Drive's item limit.",
    "teamDriveFileLimitExceeded": "The destination shared drive has reached Google Drive's item limit.",
}
_FOLDER_ACTION_PERMISSION_HINTS: dict[DriveFolderOperation, str] = {
    "list_folder": (
        "The connected Google account cannot view this Drive folder. On a shared drive, it must be a member."
    ),
    "create_folder": (
        "The connected Google account cannot add folders to this Drive folder. "
        "On a shared drive, its role must be Contributor or higher."
    ),
    "move_file": (
        "The connected Google account cannot move this item. Inside a shared drive its role must be Content manager "
        "or higher; moving out of a shared drive needs Manager on it."
    ),
}


def extract_item_id(value: str | None) -> str:
    """The ID of a Drive file or folder given as an ID or a drive.google.com or docs.google.com link."""
    candidate = (value or "").strip()
    if not candidate:
        raise ValueError("A Google Drive file or folder ID or link is required")
    if _DRIVE_FILE_ID_PATTERN.fullmatch(candidate):
        return candidate
    parsed = urlparse(candidate)
    if parsed.scheme.lower() != "https" or parsed.hostname not in ("drive.google.com", "docs.google.com"):
        raise ValueError("A Google Drive link must use https://drive.google.com or https://docs.google.com")
    segments = [segment for segment in parsed.path.split("/") if segment]
    for marker in ("folders", "d"):
        if marker in segments:
            index = segments.index(marker)
            item_id = segments[index + 1] if index + 1 < len(segments) else ""
            # Publish-to-web links (/d/e/<public-id>/pub) carry an ID Drive cannot resolve.
            if _DRIVE_FILE_ID_PATTERN.fullmatch(item_id) and item_id != "e":
                return item_id
    query_ids = parse_qs(parsed.query).get("id", [])
    if len(query_ids) == 1 and _DRIVE_FILE_ID_PATTERN.fullmatch(query_ids[0]):
        return query_ids[0]
    raise ValueError("Unsupported Google Drive link")


class _FolderActionInputError(GoogleDriveAPIError):
    """A refusal this module makes itself, whose message is already written for the person running the block."""


def _folder_action_error(exc: GoogleDriveAPIError, operation: DriveFolderOperation) -> GoogleDriveAPIError:
    # Google's own message is never passed on: for notFound it echoes the file or folder ID.
    if exc.code == "upstream_unavailable":
        return exc
    if exc.code == "reconnect_required" or exc.status == 401:
        hint = "Reconnect this Google account with Google Drive access, then run again."
    elif exc.status == 403 and exc.code in _FOLDER_ACTION_403_HINTS:
        hint = _FOLDER_ACTION_403_HINTS[exc.code]
    elif exc.status == 403 and exc.code in _FILE_PERMISSION_403_REASONS:
        hint = _FOLDER_ACTION_PERMISSION_HINTS[operation]
    elif exc.status == 404:
        hint = (
            "The connected Google account cannot see this Drive folder or file. Check the link, and that "
            "the account has access to it or is a member of its shared drive."
        )
    elif exc.status == 429 or is_rate_limit_reason(exc.code or ""):
        hint = "Google Drive is limiting requests from this account. Run again in a few minutes."
    else:
        hint = f"Google Drive could not complete this request (HTTP {exc.status})."
    return GoogleDriveAPIError(
        status=exc.status, code=exc.code, message=f"{hint} (Google Drive reason: {exc.code or 'unknown'})"
    )


async def _drive_request(
    client: httpx.AsyncClient,
    method: Literal["GET", "POST", "PATCH"],
    url: str,
    *,
    headers: Mapping[str, str],
    params: Mapping[str, str],
    json_body: Mapping[str, Any] | None = None,
) -> Any:
    # A write is sent once: after a dropped connection Drive may already have applied it, and a resend of a create
    # would make a second folder.
    max_attempts = max(1, settings.GOOGLE_DRIVE_API_MAX_RETRIES) if method == "GET" else 1
    for attempt in range(1, max_attempts + 1):
        retry_after: str | None = None
        try:
            response = await client.request(method, url, headers=headers, params=params, json=json_body)
        except httpx.TransportError:
            if attempt == max_attempts:
                outcome = "" if method == "GET" else " The change may or may not have been made; check Drive."
                raise GoogleDriveAPIError(
                    status=503,
                    code="upstream_unavailable",
                    message=f"Google Drive could not be reached.{outcome}",
                ) from None
        else:
            if not is_retryable_resumable_response(response.status_code, response.text) or attempt == max_attempts:
                _raise_for_error(response)
                return response.json() or {}
            retry_after = response.headers.get("Retry-After")
        await _sleep(_compute_backoff(attempt, retry_after))
    raise AssertionError("Drive request retry loop exited without a response")


def _item_url(item_id: str) -> str:
    return f"{DRIVE_API_BASE}/files/{quote(item_id, safe='')}"


async def _get_item(client: httpx.AsyncClient, headers: Mapping[str, str], item_id: str) -> dict[str, Any]:
    return await _drive_request(
        client,
        "GET",
        _item_url(item_id),
        headers=headers,
        params={"fields": _DRIVE_ITEM_FIELDS, "supportsAllDrives": "true"},
    )


async def _get_folder(
    client: httpx.AsyncClient, headers: Mapping[str, str], folder_id: str, role: str
) -> dict[str, Any]:
    folder = await _get_item(client, headers, folder_id)
    if folder.get("mimeType") != GOOGLE_FOLDER_MIME_TYPE:
        raise _FolderActionInputError(
            status=400, code="notAFolder", message=f"The {role} is not a Google Drive folder."
        )
    if folder.get("trashed"):
        raise _FolderActionInputError(status=400, code="folderTrashed", message=f"The {role} is in the Drive trash.")
    return folder


def _listed_item(payload: Mapping[str, Any]) -> dict[str, Any]:
    mime_type = payload.get("mimeType")
    raw_size = payload.get("size")
    return {
        "id": payload.get("id"),
        "name": payload.get("name"),
        "mime_type": mime_type,
        "is_folder": mime_type == GOOGLE_FOLDER_MIME_TYPE,
        "modified_time": payload.get("modifiedTime"),
        "size": int(raw_size) if isinstance(raw_size, str) and raw_size.isdigit() else None,
        "web_view_link": payload.get("webViewLink"),
    }


async def _run_folder_action(
    operation: DriveFolderOperation,
    access_token: str,
    action: Callable[[httpx.AsyncClient, Mapping[str, str]], Awaitable[dict[str, Any]]],
) -> dict[str, Any]:
    headers = {"Authorization": f"Bearer {access_token}"}
    try:
        async with httpx.AsyncClient(timeout=settings.GOOGLE_DRIVE_API_TIMEOUT_SECONDS) as client:
            return await action(client, headers)
    except _FolderActionInputError:
        raise
    except GoogleDriveAPIError as exc:
        raise _folder_action_error(exc, operation) from None


def _folder_id(value: str | None) -> str:
    candidate = (value or "").strip()
    return "root" if candidate.lower() == "root" else extract_item_id(candidate)


async def list_folder(*, access_token: str, folder: str, max_results: int = 100) -> dict[str, Any]:
    """The items directly inside a folder, in My Drive or a shared drive, folders first."""
    folder_id = _folder_id(folder)
    limit = max(1, min(max_results, DRIVE_LIST_FOLDER_MAX_RESULTS))

    async def action(client: httpx.AsyncClient, headers: Mapping[str, str]) -> dict[str, Any]:
        meta = await _get_folder(client, headers, folder_id, "folder to list")
        drive_id = meta.get("driveId")
        # A shared drive's own corpus is listed completely; the "user" corpus covers My Drive and shared-with-me.
        params = {
            # The ID is checked against Drive's ID alphabet above, so it cannot close the quoted string.
            "q": f"'{folder_id}' in parents and trashed = false",
            "fields": _DRIVE_LIST_FIELDS,
            "orderBy": "folder,name_natural",
            "supportsAllDrives": "true",
            "includeItemsFromAllDrives": "true",
            **({"corpora": "drive", "driveId": str(drive_id)} if drive_id else {"corpora": "user"}),
        }
        items: list[dict[str, Any]] = []
        page_token: str | None = None
        while True:
            page = await _drive_request(
                client,
                "GET",
                f"{DRIVE_API_BASE}/files",
                headers=headers,
                params={
                    **params,
                    "pageSize": str(limit - len(items)),
                    **({"pageToken": page_token} if page_token else {}),
                },
            )
            items.extend(_listed_item(item) for item in page.get("files") or [] if isinstance(item, dict))
            page_token = page.get("nextPageToken")
            if not page_token or len(items) >= limit:
                break
        return {
            "folder_id": meta.get("id") or folder_id,
            "shared_drive_id": drive_id,
            "files": items[:limit],
            "count": len(items[:limit]),
            "has_more": bool(page_token) or len(items) > limit,
        }

    return await _run_folder_action("list_folder", access_token, action)


async def create_folder(*, access_token: str, parent_folder: str, name: str) -> dict[str, Any]:
    """Create a subfolder of ``parent_folder``, in My Drive or a shared drive. Drive allows duplicate names."""
    parent_id = _folder_id(parent_folder)
    folder_name = name.strip()
    if not folder_name:
        raise ValueError("A folder name is required")

    async def action(client: httpx.AsyncClient, headers: Mapping[str, str]) -> dict[str, Any]:
        parent = await _get_folder(client, headers, parent_id, "parent folder")
        created = await _drive_request(
            client,
            "POST",
            f"{DRIVE_API_BASE}/files",
            headers=headers,
            params={"fields": _DRIVE_ITEM_FIELDS, "supportsAllDrives": "true"},
            json_body={"name": folder_name, "mimeType": GOOGLE_FOLDER_MIME_TYPE, "parents": [parent.get("id")]},
        )
        return {
            "id": created.get("id"),
            "name": created.get("name"),
            "web_view_link": created.get("webViewLink"),
            "parent_id": parent.get("id"),
            "shared_drive_id": created.get("driveId"),
        }

    return await _run_folder_action("create_folder", access_token, action)


async def move_file(*, access_token: str, item: str, destination_folder: str) -> dict[str, Any]:
    """Move a file or folder into ``destination_folder``, within or between My Drive and shared drives."""
    item_id = extract_item_id(item)
    destination_id = _folder_id(destination_folder)

    async def action(client: httpx.AsyncClient, headers: Mapping[str, str]) -> dict[str, Any]:
        source = await _get_item(client, headers, item_id)
        destination = await _get_folder(client, headers, destination_id, "destination")
        source_id, target_id = source.get("id") or item_id, destination.get("id") or destination_id
        if source_id == target_id:
            raise _FolderActionInputError(status=400, code="sameItem", message="A folder cannot be moved into itself.")
        parents = [parent for parent in source.get("parents") or [] if isinstance(parent, str)]
        if not parents:
            # Without the current folder Drive would add a second parent rather than move it.
            raise _FolderActionInputError(
                status=400,
                code="parentsUnknown",
                message="The connected Google account cannot see which folder this item is in, so it cannot move it.",
            )
        result = {"id": source_id, "name": source.get("name"), "parent_id": target_id, "previous_parent_ids": parents}
        if parents == [target_id]:
            return {**result, "web_view_link": source.get("webViewLink"), "moved": False}
        moved = await _drive_request(
            client,
            "PATCH",
            _item_url(source_id),
            headers=headers,
            params={
                "addParents": target_id,
                "removeParents": ",".join(parents),
                "fields": _DRIVE_ITEM_FIELDS,
                "supportsAllDrives": "true",
            },
            json_body={},
        )
        return {**result, "web_view_link": moved.get("webViewLink"), "moved": True}

    return await _run_folder_action("move_file", access_token, action)


@dataclass(frozen=True)
class DriveFilesPage:
    files: list[dict[str, Any]]
    next_page_token: str | None = None
    incomplete_search: bool = False


def _escape_drive_query(value: str) -> str:
    return value.replace("\\", "\\\\").replace("'", "\\'")


async def list_files(
    *,
    access_token: str,
    folders: bool,
    query: str | None = None,
    page_token: str | None = None,
    page_size: int = 25,
    search_all_drives: bool = False,
    downloadable_only: bool = False,
) -> DriveFilesPage:
    terms = [f"mimeType {'=' if folders else '!='} '{GOOGLE_FOLDER_MIME_TYPE}'", "trashed = false"]
    if downloadable_only:
        # download_file exports only Google Docs; every other Workspace type raises GoogleDriveNativeDocumentError.
        terms.append(f"(mimeType = '{GOOGLE_DOC_MIME_TYPE}' or not mimeType contains '{GOOGLE_WORKSPACE_MIME_PREFIX}')")
    if query:
        terms.append(f"name contains '{_escape_drive_query(query)}'")
    params = {
        "q": " and ".join(terms),
        "pageSize": str(max(1, min(100, page_size))),
        "fields": f"incompleteSearch,{_DRIVE_LIST_FIELDS}" if search_all_drives else _DRIVE_LIST_FIELDS,
        "orderBy": "modifiedTime desc",
        "spaces": "drive",
        "supportsAllDrives": "true",
        "includeItemsFromAllDrives": "true",
        # The default corpora=user only returns shared-drive files the user has already opened.
        **({"corpora": "allDrives"} if search_all_drives else {}),
        **({"pageToken": page_token} if page_token else {}),
    }
    async with httpx.AsyncClient(timeout=settings.GOOGLE_DRIVE_API_TIMEOUT_SECONDS) as client:
        payload = await _drive_request(
            client,
            "GET",
            f"{DRIVE_API_BASE}/files",
            headers={"Authorization": f"Bearer {access_token}"},
            params=params,
        )
    return DriveFilesPage(
        files=[
            {**_listed_item(item), "name": item.get("name") or item["id"]}
            for item in payload.get("files") or []
            if isinstance(item, dict) and item.get("id")
        ],
        next_page_token=payload.get("nextPageToken"),
        incomplete_search=search_all_drives and payload.get("incompleteSearch") is True,
    )
