from __future__ import annotations

import asyncio
import json
import uuid
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from mimetypes import guess_type
from pathlib import Path
from typing import Any
from urllib.parse import urlencode, urlparse

import aiofiles
import httpx

from skyvern.config import settings

DRIVE_UPLOAD_API_BASE = "https://www.googleapis.com/upload/drive/v3"
DRIVE_MULTIPART_UPLOAD_MAX_BYTES = 5 * 1024 * 1024
# Reserve headroom for JSON metadata and MIME boundaries below the 5 MiB multipart request cap.
DRIVE_MULTIPART_FILE_MAX_BYTES = DRIVE_MULTIPART_UPLOAD_MAX_BYTES - 10 * 1024
# 8 MiB is a multiple of Drive's required 256 KiB unit for non-final chunks.
DRIVE_RESUMABLE_CHUNK_BYTES = 8 * 1024 * 1024

_DEFAULT_BACKOFF_SECONDS = 1.0
_RATE_LIMIT_403_REASONS = frozenset({"ratelimitexceeded", "userratelimitexceeded"})


class GoogleDriveAPIError(RuntimeError):
    def __init__(self, *, status: int, code: str | None, message: str) -> None:
        super().__init__(message)
        self.status = status
        self.code = code
        self.message = message


class GoogleDriveResumableTransportError(Exception):
    """Retryable transport failure during a resumable chunk PUT — the loop should query offset and resume."""


@dataclass(frozen=True)
class UploadedDriveFile:
    id: str
    web_view_link: str | None = None


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


def extract_folder_id(value: str) -> str:
    candidate = value.strip()
    if not candidate:
        raise ValueError("Google Drive folder ID is required")

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


def build_multipart_upload_request(
    *,
    access_token: str,
    file_path: str,
    folder_id: str,
) -> GoogleDriveMultipartUploadRequest:
    """Build a bounded Google Drive multipart upload request body.

    ``folder_id`` is expected to be a normalized folder ID. Call
    ``extract_folder_id`` on user-entered values before invoking this helper.
    """
    _assert_multipart_upload_size(file_path)
    file_name = Path(file_path).name
    content_type = guess_type(file_path)[0] or "application/octet-stream"
    metadata = {"name": file_name, "parents": [folder_id]}
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
    folder_id: str,
) -> GoogleDriveResumableInitiateRequest:
    path = Path(file_path)
    file_size = path.stat().st_size
    content_type = guess_type(file_path)[0] or "application/octet-stream"
    metadata = {"name": path.name, "parents": [folder_id]}
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
                    await asyncio.sleep(_compute_backoff(attempts, None))
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
                    await asyncio.sleep(_compute_backoff(attempts, None))
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
            await asyncio.sleep(_compute_backoff(attempt, None))
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
            await asyncio.sleep(_compute_backoff(attempt, None))
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
    folder_id: str,
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


async def upload_file(
    *,
    access_token: str,
    file_path: str,
    folder_id: str,
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
