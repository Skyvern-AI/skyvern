from __future__ import annotations

import asyncio
import json
import os
import re
import tempfile
import uuid
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from mimetypes import guess_type
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, quote, urlencode, urlparse

import aiofiles
import httpx

from skyvern.config import settings
from skyvern.exceptions import DownloadFileMaxSizeExceeded

DRIVE_UPLOAD_API_BASE = "https://www.googleapis.com/upload/drive/v3"
DRIVE_API_BASE = "https://www.googleapis.com/drive/v3"
DRIVE_MULTIPART_UPLOAD_MAX_BYTES = 5 * 1024 * 1024
# Reserve headroom for JSON metadata and MIME boundaries below the 5 MiB multipart request cap.
DRIVE_MULTIPART_FILE_MAX_BYTES = DRIVE_MULTIPART_UPLOAD_MAX_BYTES - 10 * 1024
# 8 MiB is a multiple of Drive's required 256 KiB unit for non-final chunks.
DRIVE_RESUMABLE_CHUNK_BYTES = 8 * 1024 * 1024
DRIVE_DOWNLOAD_CHUNK_BYTES = 64 * 1024
GOOGLE_WORKSPACE_MIME_PREFIX = "application/vnd.google-apps."
_DRIVE_FILE_ID_PATTERN = re.compile(r"^[A-Za-z0-9_-]+$")

_DEFAULT_BACKOFF_SECONDS = 1.0
_RATE_LIMIT_403_REASONS = frozenset({"ratelimitexceeded", "userratelimitexceeded"})
_sleep = asyncio.sleep


class GoogleDriveAPIError(RuntimeError):
    def __init__(self, *, status: int, code: str | None, message: str) -> None:
        super().__init__(message)
        self.status = status
        self.code = code
        self.message = message


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
    if is_retryable_resumable_status(status_code):
        return True
    if status_code == 403 and body_text:
        try:
            payload = json.loads(body_text)
        except ValueError:
            return False
        error = payload.get("error") if isinstance(payload, dict) else None
        errors = error.get("errors") if isinstance(error, dict) else None
        if isinstance(errors, list):
            return any(
                isinstance(item, dict) and str(item.get("reason", "")).lower() in _RATE_LIMIT_403_REASONS
                for item in errors
            )
    return False


def _compute_backoff(attempt: int, retry_after: str | None) -> float:
    if retry_after:
        value = retry_after.strip()
        try:
            return max(0.0, float(value))
        except ValueError:
            pass
        try:
            target = parsedate_to_datetime(value)
        except (TypeError, ValueError):
            target = None
        if target is not None:
            if target.tzinfo is None:
                target = target.replace(tzinfo=timezone.utc)
            return max(0.0, (target - datetime.now(timezone.utc)).total_seconds())
    return _DEFAULT_BACKOFF_SECONDS * (2 ** (attempt - 1))


def _raise_for_error(response: httpx.Response) -> None:
    if response.is_success:
        return
    status = response.status_code
    try:
        payload: Any = response.json() or {}
    except ValueError:
        raise GoogleDriveAPIError(
            status=status,
            code=None,
            message=response.text[:500] or "Google Drive API error",
        ) from None
    err = payload.get("error") if isinstance(payload, dict) else {}
    if not isinstance(err, dict):
        raise GoogleDriveAPIError(status=status, code=None, message="Google Drive API error")
    message = err.get("message") or "Google Drive API error"
    details = err.get("errors")
    code: str | None = None
    if isinstance(details, list) and details and isinstance(details[0], dict):
        code = details[0].get("reason")
    if status == 403 and code in {"insufficientPermissions", "insufficientScopes"}:
        code = "reconnect_required"
    raise GoogleDriveAPIError(status=status, code=code, message=message)


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


def _upload_metadata(*, file_name: str, folder_id: str | None) -> dict[str, Any]:
    metadata: dict[str, Any] = {"name": file_name}
    # Drive puts the file in the account's My Drive root when ``parents`` is omitted.
    if folder_id:
        metadata["parents"] = [folder_id]
    return metadata


def build_multipart_upload_request(
    *,
    access_token: str,
    file_path: str,
    folder_id: str | None,
) -> GoogleDriveMultipartUploadRequest:
    """Build a bounded Google Drive multipart upload request body.

    ``folder_id`` is expected to be a normalized folder ID, or ``None`` to upload
    to the My Drive root. Call ``extract_folder_id`` on user-entered values
    before invoking this helper.
    """
    _assert_multipart_upload_size(file_path)
    file_name = Path(file_path).name
    content_type = guess_type(file_path)[0] or "application/octet-stream"
    metadata = _upload_metadata(file_name=file_name, folder_id=folder_id)
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
) -> GoogleDriveResumableInitiateRequest:
    path = Path(file_path)
    file_size = path.stat().st_size
    content_type = guess_type(file_path)[0] or "application/octet-stream"
    metadata = _upload_metadata(file_name=path.name, folder_id=folder_id)
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
) -> UploadedDriveFile:
    content_type = guess_type(file_path)[0] or "application/octet-stream"
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
) -> UploadedDriveFile:
    initiate_request = build_resumable_initiate_request(
        access_token=access_token,
        file_path=file_path,
        folder_id=folder_id,
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
        )


def extract_file_reference(value: str) -> GoogleDriveFileReference:
    candidate = value.strip()
    if _DRIVE_FILE_ID_PATTERN.fullmatch(candidate):
        return GoogleDriveFileReference(file_id=candidate)

    parsed = urlparse(candidate)
    if parsed.scheme.lower() != "https" or parsed.hostname != "drive.google.com":
        raise ValueError("Google Drive file URL must use https://drive.google.com")

    segments = [segment for segment in parsed.path.split("/") if segment]
    if len(segments) < 3 or segments[:2] != ["file", "d"] or not _DRIVE_FILE_ID_PATTERN.fullmatch(segments[2]):
        raise ValueError("Unsupported Google Drive file URL")

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


def _publish_unique_download(
    temporary_path: Path,
    destination_dir: Path,
    filename: str,
) -> Path:
    requested_path = destination_dir / filename
    collision_index = 0
    while True:
        destination_path = (
            requested_path
            if collision_index == 0
            else requested_path.with_name(f"{requested_path.stem} ({collision_index}){requested_path.suffix}")
        )
        try:
            os.link(temporary_path, destination_path)
        except FileExistsError:
            collision_index += 1
            continue
        temporary_path.unlink()
        return destination_path


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

        if metadata.mime_type and metadata.mime_type.startswith(GOOGLE_WORKSPACE_MIME_PREFIX):
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
                        file_url,
                        headers=headers,
                        params={
                            "alt": "media",
                            "supportsAllDrives": "true",
                        },
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
            destination_path = _publish_unique_download(temporary_path, destination_dir, metadata.name)
            return str(destination_path)
        except BaseException:
            temporary_path.unlink(missing_ok=True)
            raise


async def upload_file(
    *,
    access_token: str,
    file_path: str,
    folder_id: str | None,
) -> UploadedDriveFile:
    if should_use_resumable_upload(file_path):
        return await _upload_file_resumable(
            access_token=access_token,
            file_path=file_path,
            folder_id=folder_id,
        )

    request = build_multipart_upload_request(
        access_token=access_token,
        file_path=file_path,
        folder_id=folder_id,
    )

    async with httpx.AsyncClient(timeout=settings.GOOGLE_DRIVE_API_TIMEOUT_SECONDS) as client:
        response = await _post_multipart_with_retry(client, request)

    _raise_for_error(response)
    payload = response.json() or {}
    return uploaded_file_from_payload(payload)
