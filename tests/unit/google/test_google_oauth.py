import asyncio
import datetime
from types import SimpleNamespace
from typing import Any, Awaitable, Callable
from unittest.mock import AsyncMock, call
from urllib.parse import parse_qs, urlparse

import httpx
import pytest
from fastapi import HTTPException
from pydantic import ValidationError
from sqlalchemy.sql.elements import BindParameter

from skyvern.forge.agent_functions import AgentFunction
from skyvern.forge.sdk.db.enums import OrganizationAuthTokenType
from skyvern.forge.sdk.encrypt.base import EncryptMethod
from skyvern.forge.sdk.routes import google_oauth as google_oauth_routes
from skyvern.forge.sdk.schemas.google_oauth import (
    CreateGoogleOAuthAuthorizeRequest,
    CreateGoogleOAuthCallbackRequest,
    GoogleOAuthCredentialBase,
    UpdateGoogleOAuthClientConfigRequest,
    UpdateGoogleOAuthCredentialRequest,
)
from skyvern.forge.sdk.services import google_drive_service, google_oauth_service
from skyvern.schemas.workflows import FileStorageType, FileUploadDestination


def _unwrap_bind(value: Any) -> Any:
    """SQLAlchemy wraps literal values passed to .values(...) in BindParameter; unwrap for equality checks."""
    return value.value if isinstance(value, BindParameter) else value


def _default_scopes_list() -> list[str]:
    return list(google_oauth_service.GOOGLE_SHEETS_SCOPES)


def _install_google_drive_transport(
    monkeypatch: pytest.MonkeyPatch,
    handler: Callable[[httpx.Request], httpx.Response | Awaitable[httpx.Response]],
) -> None:
    transport = httpx.MockTransport(handler)
    real_client = httpx.AsyncClient

    def fake_async_client(*args: Any, **kwargs: Any) -> httpx.AsyncClient:
        kwargs["transport"] = transport
        return real_client(*args, **kwargs)

    monkeypatch.setattr(google_drive_service.httpx, "AsyncClient", fake_async_client)


def test_coerce_scopes_accepts_strings_and_iterables() -> None:
    assert google_oauth_service._coerce_scopes("https://a/scope https://b/scope") == [
        "https://a/scope",
        "https://b/scope",
    ]
    assert google_oauth_service._coerce_scopes("https://a/scope, https://b/scope") == [
        "https://a/scope",
        "https://b/scope",
    ]
    assert google_oauth_service._coerce_scopes(["https://a", " https://b "]) == ["https://a", "https://b"]
    assert google_oauth_service._coerce_scopes(None) == _default_scopes_list()
    assert google_oauth_service._coerce_scopes("") == _default_scopes_list()


def test_google_sheets_scopes_includes_drive_file_and_metadata_readonly() -> None:
    scopes = google_oauth_service.GOOGLE_SHEETS_SCOPES
    assert "https://www.googleapis.com/auth/spreadsheets" in scopes
    assert "https://www.googleapis.com/auth/drive.file" in scopes
    assert "https://www.googleapis.com/auth/drive.metadata.readonly" in scopes


def test_google_drive_scope_profile_uses_full_drive_scope_for_folder_uploads() -> None:
    scopes = google_oauth_service.scopes_for_profile("google_drive")
    assert scopes == [
        "https://www.googleapis.com/auth/drive",
    ]


def test_google_oauth_authorize_request_accepts_google_drive_scope_profile() -> None:
    request = CreateGoogleOAuthAuthorizeRequest(
        redirect_uri="https://app.example.com/google/callback",
        scope_profile="google_drive",
    )

    assert request.scope_profile == "google_drive"


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("folder_123", "folder_123"),
        ("https://drive.google.com/drive/u/0/folders/folder_123", "folder_123"),
    ],
)
def test_google_drive_extract_folder_id(value: str, expected: str) -> None:
    assert google_drive_service.extract_folder_id(value) == expected


def test_google_drive_extract_folder_id_rejects_non_folder_url() -> None:
    with pytest.raises(ValueError, match="folder URL"):
        google_drive_service.extract_folder_id("https://drive.google.com/file/d/file_123/view")


def test_google_drive_extract_folder_id_rejects_non_google_folder_url() -> None:
    with pytest.raises(ValueError, match=r"https://\*\.google\.com"):
        google_drive_service.extract_folder_id("https://attacker.example.com/folders/folder_123")


@pytest.mark.asyncio
async def test_google_drive_uploads_multipart(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path,
) -> None:
    source = tmp_path / "report.txt"
    source.write_text("hello-drive")
    captured: dict[str, Any] = {}

    async def handler(request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        captured["url"] = url
        captured["auth"] = request.headers.get("Authorization")
        captured["content_type"] = request.headers["Content-Type"]
        captured["body"] = await request.aread()
        return httpx.Response(
            200,
            json={
                "id": "file_123",
                "name": "report.txt",
                "webViewLink": "https://drive.google.com/file/d/file_123/view",
            },
        )

    _install_google_drive_transport(monkeypatch, handler)

    uploaded = await google_drive_service.upload_file(
        access_token="at-1",
        file_path=str(source),
        folder_id="folder_123",
    )

    assert uploaded.id == "file_123"
    assert captured["auth"] == "Bearer at-1"
    assert "/upload/drive/v3/files" in captured["url"]
    assert "uploadType=multipart" in captured["url"]
    assert "supportsAllDrives=true" in captured["url"]
    assert str(captured["content_type"]).startswith("multipart/related; boundary=skyvern-")
    body = captured["body"]
    assert isinstance(body, bytes)
    assert b'"parents":["folder_123"]' in body
    assert b'"name":"report.txt"' in body


@pytest.mark.asyncio
async def test_google_drive_uploads_zero_byte_file_as_multipart(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path,
) -> None:
    source = tmp_path / "empty"
    source.write_bytes(b"")
    requests: list[str] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request.url.params["uploadType"])
        assert request.method == "POST"
        assert await request.aread()
        return httpx.Response(200, json={"id": "file_empty"})

    _install_google_drive_transport(monkeypatch, handler)

    uploaded = await google_drive_service.upload_file(
        access_token="at-1",
        file_path=str(source),
        folder_id="folder_123",
    )

    assert uploaded.id == "file_empty"
    assert requests == ["multipart"]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("file_size", "expected_mode"),
    [
        (google_drive_service.DRIVE_MULTIPART_FILE_MAX_BYTES, "multipart"),
        (google_drive_service.DRIVE_MULTIPART_FILE_MAX_BYTES + 1, "resumable"),
        (google_drive_service.DRIVE_MULTIPART_UPLOAD_MAX_BYTES, "resumable"),
    ],
)
async def test_google_drive_upload_boundary_selects_expected_mode(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path,
    file_size: int,
    expected_mode: str,
) -> None:
    source = tmp_path / "boundary.bin"
    source.write_bytes(b"x" * file_size)
    session_uri = "https://www.googleapis.com/upload/session"
    requests: list[tuple[str, str]] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        upload_type = request.url.params.get("uploadType", "session")
        requests.append((request.method, upload_type))
        if upload_type == "resumable":
            return httpx.Response(200, headers={"Location": session_uri})
        return httpx.Response(200, json={"id": "file_boundary"})

    _install_google_drive_transport(monkeypatch, handler)

    uploaded = await google_drive_service.upload_file(
        access_token="at-1",
        file_path=str(source),
        folder_id="folder_123",
    )

    assert uploaded.id == "file_boundary"
    if expected_mode == "multipart":
        assert requests == [("POST", "multipart")]
    else:
        assert requests == [("POST", "resumable"), ("PUT", "session")]


@pytest.mark.asyncio
async def test_google_drive_upload_does_not_retry_retryable_create_response(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path,
) -> None:
    source = tmp_path / "report.txt"
    source.write_text("hello-drive")
    calls = 0

    async def handler(_request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        if calls > 1:
            return httpx.Response(
                200, json={"id": "file_123", "webViewLink": "https://drive.google.com/file/d/123/view"}
            )
        return httpx.Response(503, headers={"Retry-After": "0"}, json={"error": {"message": "try later"}})

    _install_google_drive_transport(monkeypatch, handler)
    sleep_mock = AsyncMock()
    monkeypatch.setattr(google_drive_service.asyncio, "sleep", sleep_mock)

    with pytest.raises(google_drive_service.GoogleDriveAPIError) as exc_info:
        await google_drive_service.upload_file(
            access_token="at-1",
            file_path=str(source),
            folder_id="folder_123",
        )

    assert exc_info.value.status == 503
    assert calls == 1
    sleep_mock.assert_not_awaited()


@pytest.mark.asyncio
async def test_google_drive_upload_retries_connection_failures_before_request(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path,
) -> None:
    source = tmp_path / "report.txt"
    source.write_text("hello-drive")
    calls = 0

    async def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        if calls == 1:
            raise httpx.ConnectError("connect failed", request=request)
        return httpx.Response(200, json={"id": "file_123", "webViewLink": "https://drive.google.com/file/d/123/view"})

    _install_google_drive_transport(monkeypatch, handler)
    sleep_mock = AsyncMock()
    monkeypatch.setattr(google_drive_service.asyncio, "sleep", sleep_mock)

    uploaded = await google_drive_service.upload_file(
        access_token="at-1",
        file_path=str(source),
        folder_id="folder_123",
    )

    assert uploaded.id == "file_123"
    assert calls == 2
    sleep_mock.assert_awaited_once_with(1.0)


@pytest.mark.asyncio
async def test_google_drive_upload_does_not_retry_ambiguous_transport_failure(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path,
) -> None:
    source = tmp_path / "report.txt"
    source.write_text("hello-drive")
    calls = 0

    async def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        raise httpx.ReadTimeout("read timed out", request=request)

    _install_google_drive_transport(monkeypatch, handler)
    sleep_mock = AsyncMock()
    monkeypatch.setattr(google_drive_service.asyncio, "sleep", sleep_mock)

    with pytest.raises(google_drive_service.GoogleDriveAPIError) as exc_info:
        await google_drive_service.upload_file(
            access_token="at-1",
            file_path=str(source),
            folder_id="folder_123",
        )

    assert exc_info.value.status == 503
    assert exc_info.value.code == "ambiguous_upload_status"
    assert calls == 1
    sleep_mock.assert_not_awaited()


def test_google_drive_multipart_builder_rejects_files_over_google_limit(tmp_path) -> None:
    source = tmp_path / "large.bin"
    source.write_bytes(b"x" * (google_drive_service.DRIVE_MULTIPART_UPLOAD_MAX_BYTES + 1))

    with pytest.raises(google_drive_service.GoogleDriveAPIError) as exc_info:
        google_drive_service.build_multipart_upload_request(
            access_token="at-1",
            file_path=str(source),
            folder_id="folder_123",
        )

    assert exc_info.value.status == 413
    assert exc_info.value.code == "file_too_large"


def test_google_drive_should_use_resumable_upload(monkeypatch: pytest.MonkeyPatch, tmp_path) -> None:
    monkeypatch.setattr(google_drive_service, "DRIVE_MULTIPART_FILE_MAX_BYTES", 4)
    threshold_file = tmp_path / "threshold.bin"
    threshold_file.write_bytes(b"1234")
    large_file = tmp_path / "large.bin"
    large_file.write_bytes(b"12345")

    assert google_drive_service.should_use_resumable_upload(str(threshold_file)) is False
    assert google_drive_service.should_use_resumable_upload(str(large_file)) is True


def test_google_drive_file_near_multipart_cap_uses_resumable(tmp_path) -> None:
    boundary_file = tmp_path / "boundary.bin"
    boundary_file.write_bytes(b"x" * google_drive_service.DRIVE_MULTIPART_UPLOAD_MAX_BYTES)
    small_file = tmp_path / "small.bin"
    small_file.write_bytes(b"hello-drive")

    assert google_drive_service.should_use_resumable_upload(str(boundary_file)) is True
    assert google_drive_service.should_use_resumable_upload(str(small_file)) is False


@pytest.mark.parametrize(
    ("range_header", "expected"),
    [
        ("bytes=0-262143", 262144),
        (None, 0),
        ("", 0),
        ("malformed", 0),
    ],
)
def test_google_drive_parse_resumable_range_offset(range_header: str | None, expected: int) -> None:
    assert google_drive_service.parse_resumable_range_offset(range_header) == expected


def test_google_drive_is_retryable_resumable_status() -> None:
    for status_code in (429, 500, 502, 503, 504):
        assert google_drive_service.is_retryable_resumable_status(status_code) is True
    for status_code in (200, 308, 400, 403, 404):
        assert google_drive_service.is_retryable_resumable_status(status_code) is False


@pytest.mark.parametrize(
    ("status_code", "body_text", "expected"),
    [
        (429, None, True),
        (503, None, True),
        (403, '{"error":{"errors":[{"reason":"rateLimitExceeded"}]}}', True),
        (403, '{"error":{"errors":[{"reason":"insufficientPermissions"}]}}', False),
        (403, None, False),
        (200, None, False),
    ],
)
def test_google_drive_is_retryable_resumable_response(
    status_code: int,
    body_text: str | None,
    expected: bool,
) -> None:
    assert google_drive_service.is_retryable_resumable_response(status_code, body_text) is expected


def test_google_drive_builds_resumable_chunk_headers() -> None:
    assert google_drive_service.build_resumable_chunk_headers(
        content_type="application/octet-stream",
        start=262144,
        end=524287,
        total=700000,
        chunk_len=262144,
    ) == {
        "Content-Type": "application/octet-stream",
        "Content-Length": "262144",
        "Content-Range": "bytes 262144-524287/700000",
    }


def test_google_drive_builds_resumable_status_query_headers() -> None:
    assert google_drive_service.build_resumable_status_query_headers(total=700000) == {
        "Content-Range": "bytes */700000",
        "Content-Length": "0",
    }


@pytest.mark.asyncio
async def test_google_drive_uploads_resumable_for_large_files(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path,
) -> None:
    monkeypatch.setattr(google_drive_service, "DRIVE_MULTIPART_FILE_MAX_BYTES", 4)
    monkeypatch.setattr(google_drive_service, "DRIVE_RESUMABLE_CHUNK_BYTES", 4)
    source = tmp_path / "report.txt"
    file_bytes = b"hello-drive"
    source.write_bytes(file_bytes)
    session_uri = "https://www.googleapis.com/upload/drive/v3/files?uploadType=resumable&upload_id=sess_1"
    requests: list[tuple[str, str]] = []
    chunks: list[bytes] = []
    content_ranges: list[str] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        requests.append((request.method, url))
        if request.method == "POST":
            assert "uploadType=resumable" in url
            assert request.url.params["fields"] == "id,name,webViewLink"
            assert request.url.params["supportsAllDrives"] == "true"
            assert request.headers["Authorization"] == "Bearer at-1"
            assert request.headers["Content-Type"] == "application/json; charset=UTF-8"
            assert request.headers["X-Upload-Content-Type"] == "text/plain"
            assert request.headers["X-Upload-Content-Length"] == str(len(file_bytes))
            assert await request.aread() == b'{"name":"report.txt","parents":["folder_123"]}'
            return httpx.Response(200, headers={"Location": session_uri})

        assert request.method == "PUT"
        assert url == session_uri
        assert request.headers["Content-Type"] == "text/plain"
        assert "Transfer-Encoding" not in request.headers
        assert "Authorization" not in request.headers
        chunk = await request.aread()
        content_range = request.headers["Content-Range"]
        chunks.append(chunk)
        content_ranges.append(content_range)
        assert request.headers["Content-Length"] == str(len(chunk))
        if content_range != f"bytes 8-10/{len(file_bytes)}":
            end = int(content_range.split("-", 1)[1].split("/", 1)[0])
            return httpx.Response(308, headers={"Range": f"bytes=0-{end}"})
        return httpx.Response(
            200,
            json={
                "id": "file_789",
                "name": "report.txt",
                "webViewLink": "https://drive.google.com/file/d/file_789/view",
            },
        )

    _install_google_drive_transport(monkeypatch, handler)

    uploaded = await google_drive_service.upload_file(
        access_token="at-1",
        file_path=str(source),
        folder_id="folder_123",
    )

    assert uploaded.id == "file_789"
    assert [method for method, _url in requests] == ["POST", "PUT", "PUT", "PUT"]
    assert requests[1][1] == session_uri
    assert b"".join(chunks) == file_bytes
    assert content_ranges == [
        f"bytes 0-3/{len(file_bytes)}",
        f"bytes 4-7/{len(file_bytes)}",
        f"bytes 8-10/{len(file_bytes)}",
    ]


@pytest.mark.asyncio
async def test_google_drive_resumable_initiation_accepts_201_and_uploads_multiple_chunks(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path,
) -> None:
    monkeypatch.setattr(google_drive_service, "DRIVE_MULTIPART_FILE_MAX_BYTES", 1)
    monkeypatch.setattr(google_drive_service, "DRIVE_RESUMABLE_CHUNK_BYTES", 3)
    source = tmp_path / "payload"
    file_bytes = b"0123456789"
    source.write_bytes(file_bytes)
    session_uri = "https://www.googleapis.com/upload/session"
    chunks: list[bytes] = []
    content_ranges: list[str] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "POST":
            assert request.headers["X-Upload-Content-Type"] == "application/octet-stream"
            return httpx.Response(201, headers={"Location": session_uri})
        assert request.headers["Content-Type"] == "application/octet-stream"
        chunk = await request.aread()
        content_range = request.headers["Content-Range"]
        chunks.append(chunk)
        content_ranges.append(content_range)
        if content_range != f"bytes 9-9/{len(file_bytes)}":
            end = int(content_range.split("-", 1)[1].split("/", 1)[0])
            return httpx.Response(308, headers={"Range": f"bytes=0-{end}"})
        return httpx.Response(200, json={"id": "file_chunked"})

    _install_google_drive_transport(monkeypatch, handler)

    uploaded = await google_drive_service.upload_file(
        access_token="at-1",
        file_path=str(source),
        folder_id="folder_123",
    )

    assert uploaded.id == "file_chunked"
    assert b"".join(chunks) == file_bytes
    assert content_ranges == ["bytes 0-2/10", "bytes 3-5/10", "bytes 6-8/10", "bytes 9-9/10"]


@pytest.mark.asyncio
async def test_google_drive_resumable_204_without_location_raises(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path,
) -> None:
    monkeypatch.setattr(google_drive_service, "DRIVE_MULTIPART_FILE_MAX_BYTES", 1)
    source = tmp_path / "payload.bin"
    source.write_bytes(b"large")
    requests: list[str] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request.method)
        return httpx.Response(204)

    _install_google_drive_transport(monkeypatch, handler)

    with pytest.raises(google_drive_service.GoogleDriveAPIError) as exc_info:
        await google_drive_service.upload_file(
            access_token="at-1",
            file_path=str(source),
            folder_id="folder_123",
        )

    assert exc_info.value.code == "missing_resumable_session"
    assert requests == ["POST"]


@pytest.mark.asyncio
async def test_google_drive_resumable_initiation_rejects_redirect(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path,
) -> None:
    monkeypatch.setattr(google_drive_service, "DRIVE_MULTIPART_FILE_MAX_BYTES", 1)
    source = tmp_path / "payload.bin"
    source.write_bytes(b"large")
    session_uri = "https://www.googleapis.com/upload/session"
    requests: list[str] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request.method)
        return httpx.Response(302, headers={"Location": session_uri})

    _install_google_drive_transport(monkeypatch, handler)

    with pytest.raises(google_drive_service.GoogleDriveAPIError) as exc_info:
        await google_drive_service.upload_file(
            access_token="at-1",
            file_path=str(source),
            folder_id="folder_123",
        )

    assert exc_info.value.status == 302
    assert requests == ["POST"]


@pytest.mark.asyncio
async def test_google_drive_resumable_final_response_missing_id_raises(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path,
) -> None:
    monkeypatch.setattr(google_drive_service, "DRIVE_MULTIPART_FILE_MAX_BYTES", 1)
    source = tmp_path / "payload.bin"
    source.write_bytes(b"large")
    session_uri = "https://www.googleapis.com/upload/session"

    async def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "POST":
            return httpx.Response(200, headers={"Location": session_uri})
        return httpx.Response(200, json={"name": "payload.bin"})

    _install_google_drive_transport(monkeypatch, handler)

    with pytest.raises(google_drive_service.GoogleDriveAPIError) as exc_info:
        await google_drive_service.upload_file(
            access_token="at-1",
            file_path=str(source),
            folder_id="folder_123",
        )

    assert exc_info.value.code == "malformed_response"


@pytest.mark.asyncio
async def test_google_drive_resumable_missing_web_view_link_uses_fallback(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path,
) -> None:
    monkeypatch.setattr(google_drive_service, "DRIVE_MULTIPART_FILE_MAX_BYTES", 1)
    source = tmp_path / "payload.bin"
    source.write_bytes(b"large")
    session_uri = "https://www.googleapis.com/upload/session"

    async def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "POST":
            return httpx.Response(200, headers={"Location": session_uri})
        return httpx.Response(200, json={"id": "file_without_link"})

    _install_google_drive_transport(monkeypatch, handler)
    destination = FileUploadDestination(
        storage_type=FileStorageType.GOOGLE_DRIVE,
        customer_uri="https://drive.google.com/drive/folders/folder_123",
        sdk_uri="https://drive.google.com/drive/folders/folder_123",
        google_access_token="at-1",
        google_drive_folder_id="folder_123",
    )

    result = await AgentFunction().upload_file_to_customer_storage(
        file_path=str(source),
        destination=destination,
    )

    assert result == "https://drive.google.com/file/d/file_without_link/view"


@pytest.mark.asyncio
async def test_google_drive_resumable_initiation_retries_connect_error(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path,
) -> None:
    monkeypatch.setattr(google_drive_service, "DRIVE_MULTIPART_FILE_MAX_BYTES", 1)
    source = tmp_path / "payload.bin"
    source.write_bytes(b"large")
    session_uri = "https://www.googleapis.com/upload/session"
    post_calls = 0
    put_calls = 0

    async def handler(request: httpx.Request) -> httpx.Response:
        nonlocal post_calls, put_calls
        if request.method == "POST":
            post_calls += 1
            if post_calls == 1:
                raise httpx.ConnectError("connect failed", request=request)
            return httpx.Response(200, headers={"Location": session_uri})
        put_calls += 1
        return httpx.Response(200, json={"id": "file_retry"})

    _install_google_drive_transport(monkeypatch, handler)
    sleep_mock = AsyncMock()
    monkeypatch.setattr(google_drive_service.asyncio, "sleep", sleep_mock)

    uploaded = await google_drive_service.upload_file(
        access_token="at-1",
        file_path=str(source),
        folder_id="folder_123",
    )

    assert uploaded.id == "file_retry"
    assert post_calls == 2
    assert put_calls == 1
    sleep_mock.assert_awaited_once_with(1.0)


@pytest.mark.asyncio
async def test_google_drive_resumable_initiation_does_not_retry_read_timeout(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path,
) -> None:
    monkeypatch.setattr(google_drive_service, "DRIVE_MULTIPART_FILE_MAX_BYTES", 1)
    source = tmp_path / "payload.bin"
    source.write_bytes(b"large")
    calls = 0

    async def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        raise httpx.ReadTimeout("read timed out", request=request)

    _install_google_drive_transport(monkeypatch, handler)
    sleep_mock = AsyncMock()
    monkeypatch.setattr(google_drive_service.asyncio, "sleep", sleep_mock)

    with pytest.raises(google_drive_service.GoogleDriveAPIError) as exc_info:
        await google_drive_service.upload_file(
            access_token="at-1",
            file_path=str(source),
            folder_id="folder_123",
        )

    assert exc_info.value.code == "ambiguous_upload_status"
    assert calls == 1
    sleep_mock.assert_not_awaited()


@pytest.mark.asyncio
async def test_google_drive_resumable_resumes_after_put_transport_failure(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path,
) -> None:
    monkeypatch.setattr(google_drive_service, "DRIVE_MULTIPART_FILE_MAX_BYTES", 1)
    monkeypatch.setattr(google_drive_service, "DRIVE_RESUMABLE_CHUNK_BYTES", 4)
    source = tmp_path / "payload.bin"
    file_bytes = b"abcdefghijkl"
    source.write_bytes(file_bytes)
    session_uri = "https://www.googleapis.com/upload/session"
    post_calls = 0
    content_ranges: list[str] = []
    query_calls = 0
    failed_once = False

    async def handler(request: httpx.Request) -> httpx.Response:
        nonlocal failed_once, post_calls, query_calls
        if request.method == "POST":
            post_calls += 1
            return httpx.Response(200, headers={"Location": session_uri})
        content_range = request.headers["Content-Range"]
        content_ranges.append(content_range)
        if content_range == f"bytes */{len(file_bytes)}":
            query_calls += 1
            assert request.headers["Content-Length"] == "0"
            assert await request.aread() == b""
            return httpx.Response(308, headers={"Range": "bytes=0-7"})

        chunk = await request.aread()
        if content_range == f"bytes 0-3/{len(file_bytes)}":
            assert chunk == file_bytes[0:4]
            return httpx.Response(308, headers={"Range": "bytes=0-3"})
        if content_range == f"bytes 4-7/{len(file_bytes)}" and not failed_once:
            failed_once = True
            assert chunk == file_bytes[4:8]
            raise httpx.ReadTimeout("read timed out", request=request)
        assert content_range == f"bytes 8-11/{len(file_bytes)}"
        assert chunk == file_bytes[8:12]
        return httpx.Response(200, json={"id": "file_resumed"})

    _install_google_drive_transport(monkeypatch, handler)

    uploaded = await google_drive_service.upload_file(
        access_token="at-1",
        file_path=str(source),
        folder_id="folder_123",
    )

    assert uploaded.id == "file_resumed"
    assert post_calls == 1
    assert query_calls == 1
    assert content_ranges == ["bytes 0-3/12", "bytes 4-7/12", "bytes */12", "bytes 8-11/12"]


@pytest.mark.asyncio
async def test_google_drive_resumable_requeries_offset_after_double_transport_failure(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path,
) -> None:
    monkeypatch.setattr(google_drive_service, "DRIVE_MULTIPART_FILE_MAX_BYTES", 1)
    monkeypatch.setattr(google_drive_service, "DRIVE_RESUMABLE_CHUNK_BYTES", 4)
    source = tmp_path / "payload.bin"
    file_bytes = b"abcdefgh"
    source.write_bytes(file_bytes)
    session_uri = "https://www.googleapis.com/upload/session"
    content_ranges: list[str] = []
    query_calls = 0

    async def handler(request: httpx.Request) -> httpx.Response:
        nonlocal query_calls
        if request.method == "POST":
            return httpx.Response(200, headers={"Location": session_uri})
        content_range = request.headers["Content-Range"]
        content_ranges.append(content_range)
        if content_range == f"bytes */{len(file_bytes)}":
            query_calls += 1
            if query_calls == 1:
                raise httpx.ReadTimeout("status query timed out", request=request)
            return httpx.Response(308, headers={"Range": "bytes=0-3"})
        if content_range == f"bytes 0-3/{len(file_bytes)}":
            raise httpx.ReadTimeout("chunk response timed out", request=request)
        assert content_range == f"bytes 4-7/{len(file_bytes)}"
        return httpx.Response(200, json={"id": "file_requeried"})

    _install_google_drive_transport(monkeypatch, handler)
    sleep_mock = AsyncMock()
    monkeypatch.setattr(google_drive_service.asyncio, "sleep", sleep_mock)

    uploaded = await google_drive_service.upload_file(
        access_token="at-1",
        file_path=str(source),
        folder_id="folder_123",
    )

    assert uploaded.id == "file_requeried"
    assert content_ranges == ["bytes 0-3/8", "bytes */8", "bytes */8", "bytes 4-7/8"]
    sleep_mock.assert_awaited_once_with(1.0)


@pytest.mark.asyncio
async def test_google_drive_resumable_resumes_after_chunk_5xx(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path,
) -> None:
    monkeypatch.setattr(google_drive_service, "DRIVE_MULTIPART_FILE_MAX_BYTES", 1)
    monkeypatch.setattr(google_drive_service, "DRIVE_RESUMABLE_CHUNK_BYTES", 4)
    source = tmp_path / "payload.bin"
    file_bytes = b"abcdefgh"
    source.write_bytes(file_bytes)
    session_uri = "https://www.googleapis.com/upload/session"
    chunk_ranges: list[str] = []
    query_calls = 0

    async def handler(request: httpx.Request) -> httpx.Response:
        nonlocal query_calls
        if request.method == "POST":
            return httpx.Response(200, headers={"Location": session_uri})
        content_range = request.headers["Content-Range"]
        if content_range == f"bytes */{len(file_bytes)}":
            query_calls += 1
            return httpx.Response(308, headers={"Range": "bytes=0-3"})

        chunk_ranges.append(content_range)
        if content_range == f"bytes 0-3/{len(file_bytes)}":
            return httpx.Response(503, text="temporarily unavailable")
        assert content_range == f"bytes 4-7/{len(file_bytes)}"
        return httpx.Response(200, json={"id": "file_resumed_after_503"})

    _install_google_drive_transport(monkeypatch, handler)

    uploaded = await google_drive_service.upload_file(
        access_token="at-1",
        file_path=str(source),
        folder_id="folder_123",
    )

    assert uploaded.id == "file_resumed_after_503"
    assert query_calls == 1
    assert chunk_ranges == ["bytes 0-3/8", "bytes 4-7/8"]


@pytest.mark.asyncio
async def test_google_drive_resumable_resumes_after_chunk_429(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path,
) -> None:
    monkeypatch.setattr(google_drive_service, "DRIVE_MULTIPART_FILE_MAX_BYTES", 1)
    monkeypatch.setattr(google_drive_service, "DRIVE_RESUMABLE_CHUNK_BYTES", 4)
    source = tmp_path / "payload.bin"
    file_bytes = b"abcdefgh"
    source.write_bytes(file_bytes)
    session_uri = "https://www.googleapis.com/upload/session"
    chunk_ranges: list[str] = []
    query_calls = 0

    async def handler(request: httpx.Request) -> httpx.Response:
        nonlocal query_calls
        if request.method == "POST":
            return httpx.Response(200, headers={"Location": session_uri})
        content_range = request.headers["Content-Range"]
        if content_range == f"bytes */{len(file_bytes)}":
            query_calls += 1
            return httpx.Response(308, headers={"Range": "bytes=0-3"})

        chunk_ranges.append(content_range)
        if content_range == f"bytes 0-3/{len(file_bytes)}":
            return httpx.Response(429, text="rate limited")
        assert content_range == f"bytes 4-7/{len(file_bytes)}"
        return httpx.Response(200, json={"id": "file_resumed_after_429"})

    _install_google_drive_transport(monkeypatch, handler)
    sleep_mock = AsyncMock()
    monkeypatch.setattr(google_drive_service.asyncio, "sleep", sleep_mock)

    uploaded = await google_drive_service.upload_file(
        access_token="at-1",
        file_path=str(source),
        folder_id="folder_123",
    )

    assert uploaded.id == "file_resumed_after_429"
    assert query_calls == 1
    assert chunk_ranges == ["bytes 0-3/8", "bytes 4-7/8"]
    sleep_mock.assert_not_awaited()


@pytest.mark.asyncio
async def test_google_drive_resumable_resumes_after_chunk_rate_limit_403(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path,
) -> None:
    monkeypatch.setattr(google_drive_service, "DRIVE_MULTIPART_FILE_MAX_BYTES", 1)
    monkeypatch.setattr(google_drive_service, "DRIVE_RESUMABLE_CHUNK_BYTES", 4)
    source = tmp_path / "payload.bin"
    file_bytes = b"abcdefgh"
    source.write_bytes(file_bytes)
    session_uri = "https://www.googleapis.com/upload/session"
    content_ranges: list[str] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "POST":
            return httpx.Response(200, headers={"Location": session_uri})
        content_range = request.headers["Content-Range"]
        content_ranges.append(content_range)
        if content_range == f"bytes */{len(file_bytes)}":
            return httpx.Response(308, headers={"Range": "bytes=0-3"})
        if content_range == f"bytes 0-3/{len(file_bytes)}":
            return httpx.Response(
                403,
                json={"error": {"errors": [{"reason": "userRateLimitExceeded"}], "message": "rate limited"}},
            )
        assert content_range == f"bytes 4-7/{len(file_bytes)}"
        return httpx.Response(200, json={"id": "file_resumed_after_403"})

    _install_google_drive_transport(monkeypatch, handler)
    sleep_mock = AsyncMock()
    monkeypatch.setattr(google_drive_service.asyncio, "sleep", sleep_mock)

    uploaded = await google_drive_service.upload_file(
        access_token="at-1",
        file_path=str(source),
        folder_id="folder_123",
    )

    assert uploaded.id == "file_resumed_after_403"
    assert content_ranges == ["bytes 0-3/8", "bytes */8", "bytes 4-7/8"]
    sleep_mock.assert_not_awaited()


@pytest.mark.asyncio
async def test_google_drive_resumable_non_rate_limit_403_is_fatal(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path,
) -> None:
    monkeypatch.setattr(google_drive_service, "DRIVE_MULTIPART_FILE_MAX_BYTES", 1)
    monkeypatch.setattr(google_drive_service, "DRIVE_RESUMABLE_CHUNK_BYTES", 4)
    source = tmp_path / "payload.bin"
    source.write_bytes(b"abcdefgh")
    session_uri = "https://www.googleapis.com/upload/session"
    chunk_ranges: list[str] = []
    query_calls = 0

    async def handler(request: httpx.Request) -> httpx.Response:
        nonlocal query_calls
        if request.method == "POST":
            return httpx.Response(200, headers={"Location": session_uri})
        content_range = request.headers["Content-Range"]
        if content_range == "bytes */8":
            query_calls += 1
            return httpx.Response(308, headers={"Range": "bytes=0-3"})

        chunk_ranges.append(content_range)
        return httpx.Response(
            403,
            json={"error": {"message": "forbidden", "errors": [{"reason": "insufficientPermissions"}]}},
        )

    _install_google_drive_transport(monkeypatch, handler)

    with pytest.raises(google_drive_service.GoogleDriveAPIError) as exc_info:
        await google_drive_service.upload_file(
            access_token="at-1",
            file_path=str(source),
            folder_id="folder_123",
        )

    assert exc_info.value.status == 403
    assert query_calls == 0
    assert chunk_ranges == ["bytes 0-3/8"]


@pytest.mark.asyncio
async def test_google_drive_resumable_commit_but_response_lost_does_not_exhaust(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path,
) -> None:
    monkeypatch.setattr(google_drive_service, "DRIVE_MULTIPART_FILE_MAX_BYTES", 1)
    monkeypatch.setattr(google_drive_service, "DRIVE_RESUMABLE_CHUNK_BYTES", 2)
    monkeypatch.setattr(google_drive_service.settings, "GOOGLE_DRIVE_API_MAX_RETRIES", 3)
    source = tmp_path / "payload.bin"
    file_bytes = b"abcdefgh"
    source.write_bytes(file_bytes)
    session_uri = "https://www.googleapis.com/upload/session"
    committed_offset = 0
    chunk_calls = 0
    query_calls = 0

    async def handler(request: httpx.Request) -> httpx.Response:
        nonlocal chunk_calls, committed_offset, query_calls
        if request.method == "POST":
            return httpx.Response(200, headers={"Location": session_uri})
        content_range = request.headers["Content-Range"]
        if content_range == f"bytes */{len(file_bytes)}":
            query_calls += 1
            if committed_offset == len(file_bytes):
                return httpx.Response(200, json={"id": "file_committed"})
            return httpx.Response(308, headers={"Range": f"bytes=0-{committed_offset - 1}"})

        chunk_calls += 1
        start_text, remainder = content_range.removeprefix("bytes ").split("-", 1)
        end_text = remainder.split("/", 1)[0]
        assert int(start_text) == committed_offset
        committed_offset = int(end_text) + 1
        raise httpx.ReadTimeout("read timed out", request=request)

    _install_google_drive_transport(monkeypatch, handler)

    uploaded = await google_drive_service.upload_file(
        access_token="at-1",
        file_path=str(source),
        folder_id="folder_123",
    )

    assert uploaded.id == "file_committed"
    assert chunk_calls == 4
    assert query_calls == 4


@pytest.mark.asyncio
async def test_google_drive_resumable_final_chunk_completion_reconciled_after_lost_response(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path,
) -> None:
    monkeypatch.setattr(google_drive_service, "DRIVE_MULTIPART_FILE_MAX_BYTES", 1)
    monkeypatch.setattr(google_drive_service, "DRIVE_RESUMABLE_CHUNK_BYTES", 4)
    monkeypatch.setattr(google_drive_service.settings, "GOOGLE_DRIVE_API_MAX_RETRIES", 1)
    source = tmp_path / "payload.bin"
    file_bytes = b"abcdefgh"
    source.write_bytes(file_bytes)
    session_uri = "https://www.googleapis.com/upload/session"
    chunk_ranges: list[str] = []
    query_calls = 0

    async def handler(request: httpx.Request) -> httpx.Response:
        nonlocal query_calls
        if request.method == "POST":
            return httpx.Response(200, headers={"Location": session_uri})
        content_range = request.headers["Content-Range"]
        if content_range == f"bytes */{len(file_bytes)}":
            query_calls += 1
            return httpx.Response(200, json={"id": "file_completed"})

        chunk_ranges.append(content_range)
        if content_range == f"bytes 0-3/{len(file_bytes)}":
            return httpx.Response(308, headers={"Range": "bytes=0-3"})
        assert content_range == f"bytes 4-7/{len(file_bytes)}"
        raise httpx.ReadTimeout("read timed out", request=request)

    _install_google_drive_transport(monkeypatch, handler)

    uploaded = await google_drive_service.upload_file(
        access_token="at-1",
        file_path=str(source),
        folder_id="folder_123",
    )

    assert uploaded.id == "file_completed"
    assert query_calls == 1
    assert chunk_ranges == ["bytes 0-3/8", "bytes 4-7/8"]


@pytest.mark.asyncio
async def test_google_drive_resumable_non_advancing_308_bails(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path,
) -> None:
    monkeypatch.setattr(google_drive_service, "DRIVE_MULTIPART_FILE_MAX_BYTES", 1)
    monkeypatch.setattr(google_drive_service, "DRIVE_RESUMABLE_CHUNK_BYTES", 4)
    monkeypatch.setattr(google_drive_service.settings, "GOOGLE_DRIVE_API_MAX_RETRIES", 2)
    source = tmp_path / "payload.bin"
    source.write_bytes(b"abcdefgh")
    session_uri = "https://www.googleapis.com/upload/session"
    chunk_calls = 0

    async def handler(request: httpx.Request) -> httpx.Response:
        nonlocal chunk_calls
        if request.method == "POST":
            return httpx.Response(200, headers={"Location": session_uri})
        chunk_calls += 1
        return httpx.Response(308, headers={"Range": "bytes=0-3"})

    _install_google_drive_transport(monkeypatch, handler)
    sleep_mock = AsyncMock()
    monkeypatch.setattr(google_drive_service.asyncio, "sleep", sleep_mock)

    with pytest.raises(google_drive_service.GoogleDriveAPIError) as exc_info:
        await asyncio.wait_for(
            google_drive_service.upload_file(
                access_token="at-1",
                file_path=str(source),
                folder_id="folder_123",
            ),
            timeout=1,
        )

    assert exc_info.value.status == 503
    assert exc_info.value.code == "resumable_upload_failed"
    assert chunk_calls == 3
    sleep_mock.assert_awaited_once_with(1.0)


@pytest.mark.asyncio
async def test_google_drive_resumable_malformed_range_after_progress_does_not_rewind(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path,
) -> None:
    monkeypatch.setattr(google_drive_service, "DRIVE_MULTIPART_FILE_MAX_BYTES", 1)
    monkeypatch.setattr(google_drive_service, "DRIVE_RESUMABLE_CHUNK_BYTES", 4)
    monkeypatch.setattr(google_drive_service.settings, "GOOGLE_DRIVE_API_MAX_RETRIES", 2)
    source = tmp_path / "payload.bin"
    source.write_bytes(b"abcdefgh")
    session_uri = "https://www.googleapis.com/upload/session"
    content_ranges: list[str] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "POST":
            return httpx.Response(200, headers={"Location": session_uri})
        content_ranges.append(request.headers["Content-Range"])
        if len(content_ranges) == 1:
            return httpx.Response(308, headers={"Range": "bytes=0-3"})
        return httpx.Response(308)

    _install_google_drive_transport(monkeypatch, handler)
    sleep_mock = AsyncMock()
    monkeypatch.setattr(google_drive_service.asyncio, "sleep", sleep_mock)

    with pytest.raises(google_drive_service.GoogleDriveAPIError) as exc_info:
        await google_drive_service.upload_file(
            access_token="at-1",
            file_path=str(source),
            folder_id="folder_123",
        )

    assert exc_info.value.code == "resumable_upload_failed"
    assert content_ranges == ["bytes 0-3/8", "bytes 4-7/8", "bytes 4-7/8"]
    sleep_mock.assert_awaited_once_with(1.0)


@pytest.mark.asyncio
async def test_google_drive_resumable_query_reports_already_complete(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path,
) -> None:
    monkeypatch.setattr(google_drive_service, "DRIVE_MULTIPART_FILE_MAX_BYTES", 1)
    monkeypatch.setattr(google_drive_service, "DRIVE_RESUMABLE_CHUNK_BYTES", 4)
    source = tmp_path / "payload.bin"
    file_bytes = b"abcdefgh"
    source.write_bytes(file_bytes)
    session_uri = "https://www.googleapis.com/upload/session"
    chunk_ranges: list[str] = []
    query_calls = 0

    async def handler(request: httpx.Request) -> httpx.Response:
        nonlocal query_calls
        if request.method == "POST":
            return httpx.Response(200, headers={"Location": session_uri})
        content_range = request.headers["Content-Range"]
        if content_range == f"bytes */{len(file_bytes)}":
            query_calls += 1
            return httpx.Response(200, json={"id": "file_already_complete"})

        chunk_ranges.append(content_range)
        if content_range == f"bytes 0-3/{len(file_bytes)}":
            return httpx.Response(308, headers={"Range": "bytes=0-3"})
        raise httpx.ReadTimeout("read timed out", request=request)

    _install_google_drive_transport(monkeypatch, handler)

    uploaded = await google_drive_service.upload_file(
        access_token="at-1",
        file_path=str(source),
        folder_id="folder_123",
    )

    assert uploaded.id == "file_already_complete"
    assert query_calls == 1
    assert chunk_ranges == ["bytes 0-3/8", "bytes 4-7/8"]


@pytest.mark.asyncio
async def test_google_drive_resumable_exhausts_attempts_raises(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path,
) -> None:
    monkeypatch.setattr(google_drive_service, "DRIVE_MULTIPART_FILE_MAX_BYTES", 1)
    monkeypatch.setattr(google_drive_service, "DRIVE_RESUMABLE_CHUNK_BYTES", 4)
    monkeypatch.setattr(google_drive_service.settings, "GOOGLE_DRIVE_API_MAX_RETRIES", 3)
    source = tmp_path / "payload.bin"
    source.write_bytes(b"abcdefgh")
    session_uri = "https://www.googleapis.com/upload/session"
    chunk_calls = 0
    query_calls = 0

    async def handler(request: httpx.Request) -> httpx.Response:
        nonlocal chunk_calls, query_calls
        if request.method == "POST":
            return httpx.Response(200, headers={"Location": session_uri})
        if request.headers["Content-Range"] == "bytes */8":
            query_calls += 1
            raise httpx.ReadTimeout("status query timed out", request=request)
        chunk_calls += 1
        raise httpx.ReadTimeout("read timed out", request=request)

    _install_google_drive_transport(monkeypatch, handler)
    sleep_mock = AsyncMock()
    monkeypatch.setattr(google_drive_service.asyncio, "sleep", sleep_mock)

    with pytest.raises(google_drive_service.GoogleDriveAPIError) as exc_info:
        await google_drive_service.upload_file(
            access_token="at-1",
            file_path=str(source),
            folder_id="folder_123",
        )

    assert exc_info.value.status == 503
    assert exc_info.value.code == "resumable_upload_failed"
    assert chunk_calls == 1
    assert query_calls == 3
    sleep_mock.assert_has_awaits([call(1.0), call(2.0)])
    assert sleep_mock.await_count == 2


@pytest.mark.asyncio
async def test_google_drive_resumable_missing_location_raises(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path,
) -> None:
    monkeypatch.setattr(google_drive_service, "DRIVE_MULTIPART_FILE_MAX_BYTES", 4)
    source = tmp_path / "report.txt"
    source.write_text("hello-drive")

    async def handler(request: httpx.Request) -> httpx.Response:
        assert request.method == "POST"
        return httpx.Response(200)

    _install_google_drive_transport(monkeypatch, handler)

    with pytest.raises(google_drive_service.GoogleDriveAPIError) as exc_info:
        await google_drive_service.upload_file(
            access_token="at-1",
            file_path=str(source),
            folder_id="folder_123",
        )

    assert exc_info.value.status == 502
    assert exc_info.value.code == "missing_resumable_session"


@pytest.mark.asyncio
async def test_google_drive_resumable_malformed_final_response_raises(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path,
) -> None:
    monkeypatch.setattr(google_drive_service, "DRIVE_MULTIPART_FILE_MAX_BYTES", 4)
    source = tmp_path / "report.txt"
    source.write_text("hello-drive")
    session_uri = "https://www.googleapis.com/upload/session"

    async def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "POST":
            return httpx.Response(200, headers={"Location": session_uri})
        assert request.method == "PUT"
        return httpx.Response(200, content=b"not json")

    _install_google_drive_transport(monkeypatch, handler)

    with pytest.raises(google_drive_service.GoogleDriveAPIError) as exc_info:
        await google_drive_service.upload_file(
            access_token="at-1",
            file_path=str(source),
            folder_id="folder_123",
        )

    assert exc_info.value.code == "malformed_response"


def test_google_drive_extracts_resumable_session_uri_case_insensitively() -> None:
    assert (
        google_drive_service.extract_resumable_session_uri({"location": "https://www.googleapis.com/upload/session"})
        == "https://www.googleapis.com/upload/session"
    )


def test_google_drive_resumable_session_uri_accepts_googleapis_subdomain() -> None:
    session_uri = "https://storage.googleapis.com/upload/session"

    assert google_drive_service.extract_resumable_session_uri({"Location": session_uri}) == session_uri


@pytest.mark.parametrize(
    "session_uri",
    [
        "https://googleapis.com.evil.com/upload",
        "https://user@evil.com/upload",
        "https://user@www.googleapis.com/upload",
        "https://www.googleapis.com:444/upload",
    ],
)
def test_google_drive_resumable_session_uri_rejects_ssrf_variants(session_uri: str) -> None:
    with pytest.raises(google_drive_service.GoogleDriveAPIError) as exc_info:
        google_drive_service.extract_resumable_session_uri({"Location": session_uri})

    assert exc_info.value.code == "invalid_resumable_session"


def test_google_drive_resumable_session_uri_rejects_non_google_host() -> None:
    with pytest.raises(google_drive_service.GoogleDriveAPIError) as exc_info:
        google_drive_service.extract_resumable_session_uri({"location": "https://evil.example.com/upload"})

    assert exc_info.value.code == "invalid_resumable_session"


def test_google_drive_resumable_session_uri_rejects_http() -> None:
    with pytest.raises(google_drive_service.GoogleDriveAPIError) as exc_info:
        google_drive_service.extract_resumable_session_uri({"location": "http://www.googleapis.com/upload"})

    assert exc_info.value.code == "invalid_resumable_session"


def test_google_drive_maps_insufficient_scope_to_reconnect() -> None:
    response = httpx.Response(
        403,
        json={
            "error": {
                "message": "Request had insufficient authentication scopes.",
                "errors": [{"reason": "insufficientPermissions"}],
            }
        },
    )
    with pytest.raises(google_drive_service.GoogleDriveAPIError) as exc_info:
        google_drive_service._raise_for_error(response)

    assert exc_info.value.status == 403
    assert exc_info.value.code == "reconnect_required"


def test_sheets_api_runtime_defaults_match_previous_hardcoded_values() -> None:
    """Google API timeout/retry settings default to known values so unset envs
    produce no behavior change for upgrading deployments."""
    from skyvern.config import Settings

    fresh = Settings()
    assert fresh.GOOGLE_SHEETS_API_TIMEOUT_SECONDS == 30.0
    assert fresh.GOOGLE_SHEETS_API_MAX_RETRIES == 3
    assert fresh.GOOGLE_DRIVE_API_TIMEOUT_SECONDS == 30.0
    assert fresh.GOOGLE_DRIVE_API_MAX_RETRIES == 3


@pytest.mark.asyncio
async def test_resolve_client_config_prefers_org_token(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        google_oauth_service.SettingsManager,
        "get_settings",
        lambda: SimpleNamespace(ENABLE_ORGANIZATION_GOOGLE_OAUTH_CLIENT_CONFIG=True),
    )
    config = google_oauth_service.GoogleOAuthClientConfig(
        client_id="org-client",
        client_secret="org-secret",
        redirect_hosts=["oss.example.com"],
        app_origins=["https://oss.example.com"],
    )
    organizations = SimpleNamespace(
        get_valid_org_auth_token=AsyncMock(return_value=SimpleNamespace(token=config.model_dump_json()))
    )
    monkeypatch.setattr(
        google_oauth_service.app,
        "DATABASE",
        SimpleNamespace(organizations=organizations),
        raising=False,
    )

    resolved = await google_oauth_service.resolve_client_config("org_1")

    assert resolved.source == "organization"
    assert resolved.config == config
    organizations.get_valid_org_auth_token.assert_awaited_once()


@pytest.mark.asyncio
async def test_resolve_client_config_ignores_org_token_when_disabled(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(google_oauth_service.settings, "GOOGLE_OAUTH_CLIENT_ID", "env-client", raising=False)
    monkeypatch.setattr(google_oauth_service.settings, "GOOGLE_OAUTH_CLIENT_SECRET", "env-secret", raising=False)
    monkeypatch.setattr(
        google_oauth_service.SettingsManager,
        "get_settings",
        lambda: SimpleNamespace(ENABLE_ORGANIZATION_GOOGLE_OAUTH_CLIENT_CONFIG=False),
    )
    organizations = SimpleNamespace(get_valid_org_auth_token=AsyncMock())
    monkeypatch.setattr(
        google_oauth_service.app,
        "DATABASE",
        SimpleNamespace(organizations=organizations),
        raising=False,
    )

    resolved = await google_oauth_service.resolve_client_config("org_1")

    assert resolved.source == "environment"
    assert resolved.config is not None
    assert resolved.config.client_id == "env-client"
    organizations.get_valid_org_auth_token.assert_not_awaited()


@pytest.mark.asyncio
async def test_resolve_client_config_returns_missing_without_org_token_or_env_credentials(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(google_oauth_service.settings, "GOOGLE_OAUTH_CLIENT_ID", "", raising=False)
    monkeypatch.setattr(google_oauth_service.settings, "GOOGLE_OAUTH_CLIENT_SECRET", "", raising=False)
    monkeypatch.setattr(
        google_oauth_service.SettingsManager,
        "get_settings",
        lambda: SimpleNamespace(ENABLE_ORGANIZATION_GOOGLE_OAUTH_CLIENT_CONFIG=True),
    )
    organizations = SimpleNamespace(get_valid_org_auth_token=AsyncMock(return_value=None))
    monkeypatch.setattr(
        google_oauth_service.app,
        "DATABASE",
        SimpleNamespace(organizations=organizations),
        raising=False,
    )

    resolved = await google_oauth_service.resolve_client_config("org_1")

    assert resolved.source == "missing"
    assert resolved.config is None
    organizations.get_valid_org_auth_token.assert_awaited_once()


@pytest.mark.asyncio
async def test_resolve_client_config_fails_closed_for_invalid_org_token(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        google_oauth_service.SettingsManager,
        "get_settings",
        lambda: SimpleNamespace(ENABLE_ORGANIZATION_GOOGLE_OAUTH_CLIENT_CONFIG=True),
    )
    monkeypatch.setattr(
        google_oauth_service,
        "_settings_client_config",
        lambda: pytest.fail("environment config must not be consulted"),
    )
    organizations = SimpleNamespace(get_valid_org_auth_token=AsyncMock(return_value=SimpleNamespace(token="not-json")))
    monkeypatch.setattr(
        google_oauth_service.app,
        "DATABASE",
        SimpleNamespace(organizations=organizations),
        raising=False,
    )

    with pytest.raises(
        google_oauth_service.OrganizationClientConfigUnavailableError,
        match="Stored organization Google OAuth client config is invalid",
    ):
        await google_oauth_service.resolve_client_config("org_1")


@pytest.mark.asyncio
@pytest.mark.parametrize("strict", [True, False])
async def test_resolve_client_config_fails_closed_for_empty_org_token_payload(
    monkeypatch: pytest.MonkeyPatch,
    strict: bool,
) -> None:
    monkeypatch.setattr(
        google_oauth_service.SettingsManager,
        "get_settings",
        lambda: SimpleNamespace(ENABLE_ORGANIZATION_GOOGLE_OAUTH_CLIENT_CONFIG=True),
    )
    monkeypatch.setattr(
        google_oauth_service,
        "_settings_client_config",
        lambda: pytest.fail("environment config must not be consulted"),
    )
    organizations = SimpleNamespace(get_valid_org_auth_token=AsyncMock(return_value=SimpleNamespace(token="")))
    monkeypatch.setattr(
        google_oauth_service.app,
        "DATABASE",
        SimpleNamespace(organizations=organizations),
        raising=False,
    )

    with pytest.raises(
        google_oauth_service.OrganizationClientConfigUnavailableError,
        match="Stored organization Google OAuth client config is invalid",
    ):
        await google_oauth_service.resolve_client_config("org_1", strict=strict)


@pytest.mark.asyncio
async def test_resolve_client_config_fails_closed_when_org_token_load_raises(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        google_oauth_service.SettingsManager,
        "get_settings",
        lambda: SimpleNamespace(ENABLE_ORGANIZATION_GOOGLE_OAUTH_CLIENT_CONFIG=True),
    )
    monkeypatch.setattr(
        google_oauth_service,
        "_settings_client_config",
        lambda: pytest.fail("environment config must not be consulted"),
    )
    organizations = SimpleNamespace(
        get_valid_org_auth_token=AsyncMock(side_effect=RuntimeError("database unavailable"))
    )
    monkeypatch.setattr(
        google_oauth_service.app,
        "DATABASE",
        SimpleNamespace(organizations=organizations),
        raising=False,
    )

    with pytest.raises(
        google_oauth_service.OrganizationClientConfigUnavailableError,
        match="Failed to load the organization Google OAuth client config",
    ):
        await google_oauth_service.resolve_client_config("org_1")


@pytest.mark.asyncio
async def test_resolve_client_config_non_strict_falls_back_to_env_when_org_token_load_raises(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(google_oauth_service.settings, "GOOGLE_OAUTH_CLIENT_ID", "env-client", raising=False)
    monkeypatch.setattr(google_oauth_service.settings, "GOOGLE_OAUTH_CLIENT_SECRET", "env-secret", raising=False)
    monkeypatch.setattr(
        google_oauth_service.SettingsManager,
        "get_settings",
        lambda: SimpleNamespace(ENABLE_ORGANIZATION_GOOGLE_OAUTH_CLIENT_CONFIG=True),
    )
    organizations = SimpleNamespace(
        get_valid_org_auth_token=AsyncMock(side_effect=RuntimeError("database unavailable"))
    )
    monkeypatch.setattr(
        google_oauth_service.app,
        "DATABASE",
        SimpleNamespace(organizations=organizations),
        raising=False,
    )
    warning_calls: list[tuple[str, dict[str, object]]] = []

    def warning(event: str, **kwargs: object) -> None:
        warning_calls.append((event, kwargs))

    monkeypatch.setattr(google_oauth_service, "LOG", SimpleNamespace(warning=warning))

    resolved = await google_oauth_service.resolve_client_config("org_1", strict=False)

    assert resolved.source == "environment"
    assert resolved.config is not None
    assert resolved.config.client_id == "env-client"
    assert warning_calls == [
        (
            "Failed to load organization Google OAuth client config; falling back to environment for token refresh",
            {"organization_id": "org_1"},
        )
    ]


@pytest.mark.asyncio
async def test_resolve_client_config_non_strict_fails_closed_for_invalid_org_token(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        google_oauth_service.SettingsManager,
        "get_settings",
        lambda: SimpleNamespace(ENABLE_ORGANIZATION_GOOGLE_OAUTH_CLIENT_CONFIG=True),
    )
    monkeypatch.setattr(
        google_oauth_service,
        "_settings_client_config",
        lambda: pytest.fail("environment config must not be consulted"),
    )
    organizations = SimpleNamespace(get_valid_org_auth_token=AsyncMock(return_value=SimpleNamespace(token="not-json")))
    monkeypatch.setattr(
        google_oauth_service.app,
        "DATABASE",
        SimpleNamespace(organizations=organizations),
        raising=False,
    )

    with pytest.raises(
        google_oauth_service.OrganizationClientConfigUnavailableError,
        match="Stored organization Google OAuth client config is invalid",
    ):
        await google_oauth_service.resolve_client_config("org_1", strict=False)


@pytest.mark.asyncio
async def test_resolve_client_config_falls_back_to_env_without_org_token(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(google_oauth_service.settings, "GOOGLE_OAUTH_CLIENT_ID", "env-client", raising=False)
    monkeypatch.setattr(google_oauth_service.settings, "GOOGLE_OAUTH_CLIENT_SECRET", "env-secret", raising=False)
    monkeypatch.setattr(
        google_oauth_service.SettingsManager,
        "get_settings",
        lambda: SimpleNamespace(ENABLE_ORGANIZATION_GOOGLE_OAUTH_CLIENT_CONFIG=True),
    )
    organizations = SimpleNamespace(get_valid_org_auth_token=AsyncMock(return_value=None))
    monkeypatch.setattr(
        google_oauth_service.app,
        "DATABASE",
        SimpleNamespace(organizations=organizations),
        raising=False,
    )

    resolved = await google_oauth_service.resolve_client_config("org_1")

    assert resolved.source == "environment"
    assert resolved.config is not None
    assert resolved.config.client_id == "env-client"


@pytest.mark.asyncio
async def test_save_client_config_requires_encryption(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        google_oauth_service.SettingsManager,
        "get_settings",
        lambda: SimpleNamespace(ENABLE_ORGANIZATION_GOOGLE_OAUTH_CLIENT_CONFIG=True),
    )
    monkeypatch.setattr(google_oauth_service.settings, "ENABLE_ENCRYPTION", False, raising=False)
    organizations = SimpleNamespace(replace_org_auth_token=AsyncMock())
    google_oauth = SimpleNamespace(mark_active_mismatched_client_as_error=AsyncMock())
    monkeypatch.setattr(
        google_oauth_service.app,
        "DATABASE",
        SimpleNamespace(organizations=organizations, google_oauth=google_oauth),
        raising=False,
    )

    with pytest.raises(google_oauth_service.EncryptionNotConfiguredError):
        await google_oauth_service.save_client_config(
            "org_1",
            google_oauth_service.GoogleOAuthClientConfig(client_id="cid", client_secret="secret"),
        )
    organizations.replace_org_auth_token.assert_not_awaited()
    google_oauth.mark_active_mismatched_client_as_error.assert_not_awaited()


@pytest.mark.asyncio
async def test_save_client_config_requires_org_config_enabled(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(google_oauth_service.settings, "ENABLE_ENCRYPTION", False, raising=False)
    monkeypatch.setattr(
        google_oauth_service.SettingsManager,
        "get_settings",
        lambda: SimpleNamespace(ENABLE_ORGANIZATION_GOOGLE_OAUTH_CLIENT_CONFIG=False),
    )
    organizations = SimpleNamespace(replace_org_auth_token=AsyncMock())
    monkeypatch.setattr(
        google_oauth_service.app,
        "DATABASE",
        SimpleNamespace(organizations=organizations),
        raising=False,
    )

    with pytest.raises(google_oauth_service.OrganizationGoogleOAuthConfigDisabledError):
        await google_oauth_service.save_client_config(
            "org_1",
            google_oauth_service.GoogleOAuthClientConfig(client_id="cid", client_secret="secret"),
        )
    organizations.replace_org_auth_token.assert_not_awaited()


@pytest.mark.asyncio
async def test_save_client_config_uses_aes(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        google_oauth_service.SettingsManager,
        "get_settings",
        lambda: SimpleNamespace(ENABLE_ORGANIZATION_GOOGLE_OAUTH_CLIENT_CONFIG=True),
    )
    monkeypatch.setattr(google_oauth_service.settings, "ENABLE_ENCRYPTION", True, raising=False)
    replace_mock = AsyncMock()
    mark_mock = AsyncMock(return_value=2)
    monkeypatch.setattr(
        google_oauth_service.app,
        "DATABASE",
        SimpleNamespace(
            organizations=SimpleNamespace(replace_org_auth_token=replace_mock),
            google_oauth=SimpleNamespace(mark_active_mismatched_client_as_error=mark_mock),
        ),
        raising=False,
    )

    config = google_oauth_service.GoogleOAuthClientConfig(client_id="cid", client_secret="secret")
    resolved = await google_oauth_service.save_client_config("org_1", config)

    assert resolved.source == "organization"
    assert resolved.config == config
    replace_mock.assert_awaited_once()
    assert replace_mock.await_args.kwargs["encrypted_method"] is EncryptMethod.AES
    mark_mock.assert_awaited_once()
    assert mark_mock.await_args.kwargs["organization_id"] == "org_1"
    assert mark_mock.await_args.kwargs["new_client_id"] == "cid"
    assert isinstance(mark_mock.await_args.kwargs["now"], datetime.datetime)


@pytest.mark.asyncio
async def test_save_client_config_does_not_mark_mismatched_when_save_fails(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        google_oauth_service.SettingsManager,
        "get_settings",
        lambda: SimpleNamespace(ENABLE_ORGANIZATION_GOOGLE_OAUTH_CLIENT_CONFIG=True),
    )
    monkeypatch.setattr(google_oauth_service.settings, "ENABLE_ENCRYPTION", True, raising=False)
    replace_mock = AsyncMock(side_effect=RuntimeError("save failed"))
    mark_mock = AsyncMock()
    monkeypatch.setattr(
        google_oauth_service.app,
        "DATABASE",
        SimpleNamespace(
            organizations=SimpleNamespace(replace_org_auth_token=replace_mock),
            google_oauth=SimpleNamespace(mark_active_mismatched_client_as_error=mark_mock),
        ),
        raising=False,
    )

    with pytest.raises(RuntimeError, match="save failed"):
        await google_oauth_service.save_client_config(
            "org_1",
            google_oauth_service.GoogleOAuthClientConfig(client_id="cid", client_secret="secret"),
        )

    replace_mock.assert_awaited_once()
    mark_mock.assert_not_awaited()


@pytest.mark.asyncio
async def test_delete_client_config_invalidates_org_token_when_enabled(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        google_oauth_service.SettingsManager,
        "get_settings",
        lambda: SimpleNamespace(ENABLE_ORGANIZATION_GOOGLE_OAUTH_CLIENT_CONFIG=True),
    )
    monkeypatch.setattr(google_oauth_service.settings, "GOOGLE_OAUTH_CLIENT_ID", "env-client", raising=False)
    monkeypatch.setattr(google_oauth_service.settings, "GOOGLE_OAUTH_CLIENT_SECRET", "env-secret", raising=False)
    invalidate_mock = AsyncMock()
    mark_mock = AsyncMock(return_value=1)
    monkeypatch.setattr(
        google_oauth_service.app,
        "DATABASE",
        SimpleNamespace(
            organizations=SimpleNamespace(invalidate_org_auth_tokens=invalidate_mock),
            google_oauth=SimpleNamespace(mark_active_mismatched_client_as_error=mark_mock),
        ),
        raising=False,
    )

    await google_oauth_service.delete_client_config("org_1")

    invalidate_mock.assert_awaited_once_with(
        organization_id="org_1",
        token_type=OrganizationAuthTokenType.google_oauth_client_config,
    )
    mark_mock.assert_awaited_once()
    assert mark_mock.await_args.kwargs["organization_id"] == "org_1"
    assert mark_mock.await_args.kwargs["new_client_id"] == "env-client"
    assert isinstance(mark_mock.await_args.kwargs["now"], datetime.datetime)


@pytest.mark.asyncio
async def test_delete_client_config_marks_mismatched_against_missing_env_config(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        google_oauth_service.SettingsManager,
        "get_settings",
        lambda: SimpleNamespace(ENABLE_ORGANIZATION_GOOGLE_OAUTH_CLIENT_CONFIG=True),
    )
    monkeypatch.setattr(google_oauth_service.settings, "GOOGLE_OAUTH_CLIENT_ID", "", raising=False)
    monkeypatch.setattr(google_oauth_service.settings, "GOOGLE_OAUTH_CLIENT_SECRET", "", raising=False)
    invalidate_mock = AsyncMock()
    mark_mock = AsyncMock(return_value=1)
    monkeypatch.setattr(
        google_oauth_service.app,
        "DATABASE",
        SimpleNamespace(
            organizations=SimpleNamespace(invalidate_org_auth_tokens=invalidate_mock),
            google_oauth=SimpleNamespace(mark_active_mismatched_client_as_error=mark_mock),
        ),
        raising=False,
    )

    await google_oauth_service.delete_client_config("org_1")

    invalidate_mock.assert_awaited_once_with(
        organization_id="org_1",
        token_type=OrganizationAuthTokenType.google_oauth_client_config,
    )
    mark_mock.assert_awaited_once()
    assert mark_mock.await_args.kwargs["organization_id"] == "org_1"
    assert mark_mock.await_args.kwargs["new_client_id"] is None


@pytest.mark.asyncio
async def test_delete_client_config_requires_org_config_enabled(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        google_oauth_service.SettingsManager,
        "get_settings",
        lambda: SimpleNamespace(ENABLE_ORGANIZATION_GOOGLE_OAUTH_CLIENT_CONFIG=False),
    )
    invalidate_mock = AsyncMock()
    monkeypatch.setattr(
        google_oauth_service.app,
        "DATABASE",
        SimpleNamespace(organizations=SimpleNamespace(invalidate_org_auth_tokens=invalidate_mock)),
        raising=False,
    )

    with pytest.raises(google_oauth_service.OrganizationGoogleOAuthConfigDisabledError):
        await google_oauth_service.delete_client_config("org_1")
    invalidate_mock.assert_not_awaited()


@pytest.mark.asyncio
async def test_update_client_config_does_not_reuse_environment_secret(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        google_oauth_routes.SettingsManager,
        "get_settings",
        lambda: SimpleNamespace(ENABLE_ORGANIZATION_GOOGLE_OAUTH_CLIENT_CONFIG=True),
    )
    env_config = google_oauth_service.GoogleOAuthClientConfig(
        client_id="env-client",
        client_secret="env-secret",
        redirect_hosts=["env.example.com"],
    )
    save_mock = AsyncMock()
    monkeypatch.setattr(
        google_oauth_routes.google_oauth_service,
        "resolve_client_config",
        AsyncMock(
            return_value=google_oauth_service.GoogleOAuthClientConfigResolution(
                config=env_config,
                source="environment",
            )
        ),
    )
    monkeypatch.setattr(google_oauth_routes.google_oauth_service, "save_client_config", save_mock)

    with pytest.raises(HTTPException) as exc_info:
        await google_oauth_routes.update_google_oauth_client_config(
            UpdateGoogleOAuthClientConfigRequest(
                client_id="org-client",
                redirect_hosts=["org.example.com"],
            ),
            current_org=SimpleNamespace(organization_id="org_1"),
        )

    assert exc_info.value.status_code == 400
    assert exc_info.value.detail == "Google OAuth client secret is required"
    save_mock.assert_not_awaited()


@pytest.mark.asyncio
async def test_update_client_config_does_not_reuse_organization_secret_when_client_id_changes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        google_oauth_routes.SettingsManager,
        "get_settings",
        lambda: SimpleNamespace(ENABLE_ORGANIZATION_GOOGLE_OAUTH_CLIENT_CONFIG=True),
    )
    org_config = google_oauth_service.GoogleOAuthClientConfig(
        client_id="old-client",
        client_secret="old-secret",
        redirect_hosts=["org.example.com"],
    )
    save_mock = AsyncMock()
    monkeypatch.setattr(
        google_oauth_routes.google_oauth_service,
        "resolve_client_config",
        AsyncMock(
            return_value=google_oauth_service.GoogleOAuthClientConfigResolution(
                config=org_config,
                source="organization",
            )
        ),
    )
    monkeypatch.setattr(google_oauth_routes.google_oauth_service, "save_client_config", save_mock)

    with pytest.raises(HTTPException) as exc_info:
        await google_oauth_routes.update_google_oauth_client_config(
            UpdateGoogleOAuthClientConfigRequest(
                client_id="new-client",
                redirect_hosts=["org.example.com"],
            ),
            current_org=SimpleNamespace(organization_id="org_1"),
        )

    assert exc_info.value.status_code == 400
    assert exc_info.value.detail == "Google OAuth client secret is required"
    save_mock.assert_not_awaited()


@pytest.mark.asyncio
async def test_update_client_config_allows_repair_when_stored_config_is_unavailable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        google_oauth_routes.SettingsManager,
        "get_settings",
        lambda: SimpleNamespace(ENABLE_ORGANIZATION_GOOGLE_OAUTH_CLIENT_CONFIG=True),
    )
    unavailable_error = google_oauth_service.OrganizationClientConfigUnavailableError(
        "Stored organization Google OAuth client config is invalid"
    )
    monkeypatch.setattr(
        google_oauth_routes.google_oauth_service,
        "resolve_client_config",
        AsyncMock(side_effect=unavailable_error),
    )
    saved_config = google_oauth_service.GoogleOAuthClientConfig(
        client_id="replacement-client",
        client_secret="replacement-secret",
        redirect_hosts=["org.example.com"],
    )
    save_mock = AsyncMock(
        return_value=google_oauth_service.GoogleOAuthClientConfigResolution(
            config=saved_config,
            source="organization",
        )
    )
    monkeypatch.setattr(google_oauth_routes.google_oauth_service, "save_client_config", save_mock)

    response = await google_oauth_routes.update_google_oauth_client_config(
        UpdateGoogleOAuthClientConfigRequest(
            client_id="replacement-client",
            client_secret="replacement-secret",
            redirect_hosts=["org.example.com"],
        ),
        current_org=SimpleNamespace(organization_id="org_1"),
    )

    assert response.config.source == "organization"
    save_mock.assert_awaited_once_with("org_1", saved_config)


@pytest.mark.asyncio
async def test_update_client_config_requires_secret_when_stored_config_is_unavailable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        google_oauth_routes.SettingsManager,
        "get_settings",
        lambda: SimpleNamespace(ENABLE_ORGANIZATION_GOOGLE_OAUTH_CLIENT_CONFIG=True),
    )
    unavailable_error = google_oauth_service.OrganizationClientConfigUnavailableError(
        "Stored organization Google OAuth client config is invalid"
    )
    monkeypatch.setattr(
        google_oauth_routes.google_oauth_service,
        "resolve_client_config",
        AsyncMock(side_effect=unavailable_error),
    )
    save_mock = AsyncMock()
    monkeypatch.setattr(google_oauth_routes.google_oauth_service, "save_client_config", save_mock)

    with pytest.raises(HTTPException) as exc_info:
        await google_oauth_routes.update_google_oauth_client_config(
            UpdateGoogleOAuthClientConfigRequest(
                client_id="replacement-client",
                redirect_hosts=["org.example.com"],
            ),
            current_org=SimpleNamespace(organization_id="org_1"),
        )

    assert exc_info.value.status_code == 400
    assert exc_info.value.detail == "Google OAuth client secret is required"
    save_mock.assert_not_awaited()


@pytest.mark.asyncio
async def test_get_client_config_returns_503_when_org_config_is_unavailable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        google_oauth_routes.SettingsManager,
        "get_settings",
        lambda: SimpleNamespace(ENABLE_ORGANIZATION_GOOGLE_OAUTH_CLIENT_CONFIG=True),
    )
    unavailable_error = google_oauth_service.OrganizationClientConfigUnavailableError(
        "Failed to load the organization Google OAuth client config"
    )
    monkeypatch.setattr(
        google_oauth_routes.google_oauth_service,
        "resolve_client_config",
        AsyncMock(side_effect=unavailable_error),
    )

    with pytest.raises(HTTPException) as exc_info:
        await google_oauth_routes.get_google_oauth_client_config(current_org=SimpleNamespace(organization_id="org_1"))

    assert exc_info.value.status_code == 503
    assert exc_info.value.detail == "Failed to load the organization Google OAuth client config"


@pytest.mark.asyncio
async def test_config_route_handlers_return_404_when_org_config_disabled(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        google_oauth_routes.SettingsManager,
        "get_settings",
        lambda: SimpleNamespace(ENABLE_ORGANIZATION_GOOGLE_OAUTH_CLIENT_CONFIG=False),
    )
    current_org = SimpleNamespace(organization_id="org_1")

    with pytest.raises(HTTPException) as get_exc_info:
        await google_oauth_routes.get_google_oauth_client_config(current_org=current_org)
    assert get_exc_info.value.status_code == 404

    with pytest.raises(HTTPException) as delete_exc_info:
        await google_oauth_routes.delete_google_oauth_client_config(current_org=current_org)
    assert delete_exc_info.value.status_code == 404


def test_build_authorize_url_includes_required_params(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(google_oauth_service.settings, "GOOGLE_OAUTH_CLIENT_ID", "cid", raising=False)
    monkeypatch.setattr(google_oauth_service.settings, "GOOGLE_OAUTH_CLIENT_SECRET", "csecret", raising=False)
    monkeypatch.setattr(google_oauth_service.settings, "GOOGLE_OAUTH_REDIRECT_HOSTS", ["app"], raising=False)

    url, code_verifier = google_oauth_service.build_authorize_url(
        redirect_uri="https://app/settings/google/callback",
        state="abc123",
    )

    parsed = urlparse(url)
    assert f"{parsed.scheme}://{parsed.netloc}{parsed.path}" == google_oauth_service.GOOGLE_AUTHORIZE_ENDPOINT
    params = {k: v[0] for k, v in parse_qs(parsed.query).items()}
    assert params["response_type"] == "code"
    assert params["client_id"] == "cid"
    assert params["redirect_uri"] == "https://app/settings/google/callback"
    assert params["scope"] == " ".join(google_oauth_service.GOOGLE_SHEETS_SCOPES)
    assert params["access_type"] == "offline"
    assert params["prompt"] == "consent"
    assert params["state"] == "abc123"
    # PKCE: a code_challenge must be on the URL and the verifier returned for replay.
    assert params["code_challenge_method"] == "S256"
    assert params["code_challenge"]
    assert code_verifier and len(code_verifier) >= 43


def test_build_authorize_url_passes_autogenerate_code_verifier_explicitly(monkeypatch: pytest.MonkeyPatch) -> None:
    """Explicit ``autogenerate_code_verifier=True`` so a library default flip can't silently drop PKCE."""
    monkeypatch.setattr(google_oauth_service.settings, "GOOGLE_OAUTH_CLIENT_ID", "cid", raising=False)
    monkeypatch.setattr(google_oauth_service.settings, "GOOGLE_OAUTH_CLIENT_SECRET", "csecret", raising=False)
    monkeypatch.setattr(google_oauth_service.settings, "GOOGLE_OAUTH_REDIRECT_HOSTS", ["x"], raising=False)

    captured: dict = {}

    class _FakeFlow:
        code_verifier = "ver-fake"

        def authorization_url(self, **_kwargs):
            return "https://accounts.google.com/o/oauth2/v2/auth", "state"

        @classmethod
        def from_client_config(cls, *args, **kwargs):
            captured["kwargs"] = kwargs
            return cls()

    monkeypatch.setattr(google_oauth_service, "Flow", _FakeFlow)

    google_oauth_service.build_authorize_url(redirect_uri="https://x/cb", state="s")

    assert captured["kwargs"].get("autogenerate_code_verifier") is True


def test_build_authorize_url_without_client_id_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(google_oauth_service.settings, "GOOGLE_OAUTH_CLIENT_ID", None, raising=False)
    monkeypatch.setattr(google_oauth_service.settings, "GOOGLE_OAUTH_CLIENT_SECRET", "csecret", raising=False)

    with pytest.raises(ValueError, match="client credentials"):
        google_oauth_service.build_authorize_url(redirect_uri="https://x", state="s")


def test_build_authorize_url_without_client_secret_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(google_oauth_service.settings, "GOOGLE_OAUTH_CLIENT_ID", "cid", raising=False)
    monkeypatch.setattr(google_oauth_service.settings, "GOOGLE_OAUTH_CLIENT_SECRET", None, raising=False)

    with pytest.raises(ValueError, match="client credentials"):
        google_oauth_service.build_authorize_url(redirect_uri="https://x", state="s")


def test_build_authorize_url_rejects_redirect_uri_not_in_allowlist(monkeypatch: pytest.MonkeyPatch) -> None:
    """Defense-in-depth: build_authorize_url must self-validate redirect_uri so direct callers
    (outside start_authorization) cannot bypass the host allowlist."""
    monkeypatch.setattr(google_oauth_service.settings, "GOOGLE_OAUTH_CLIENT_ID", "cid", raising=False)
    monkeypatch.setattr(google_oauth_service.settings, "GOOGLE_OAUTH_CLIENT_SECRET", "csecret", raising=False)
    monkeypatch.setattr(
        google_oauth_service.settings,
        "GOOGLE_OAUTH_REDIRECT_HOSTS",
        ["app.skyvern.com"],
        raising=False,
    )

    with pytest.raises(google_oauth_service.InvalidRedirectURIError):
        google_oauth_service.build_authorize_url(
            redirect_uri="https://evil.example.com/callback",
            state="abc",
        )


def test_build_authorize_url_rejects_http_for_non_loopback_host(monkeypatch: pytest.MonkeyPatch) -> None:
    """Hostname-only allowlist plus an http URI must be rejected even when called directly."""
    monkeypatch.setattr(google_oauth_service.settings, "GOOGLE_OAUTH_CLIENT_ID", "cid", raising=False)
    monkeypatch.setattr(google_oauth_service.settings, "GOOGLE_OAUTH_CLIENT_SECRET", "csecret", raising=False)
    monkeypatch.setattr(
        google_oauth_service.settings,
        "GOOGLE_OAUTH_REDIRECT_HOSTS",
        ["app.skyvern.com"],
        raising=False,
    )

    with pytest.raises(google_oauth_service.InvalidRedirectURIError, match="https"):
        google_oauth_service.build_authorize_url(
            redirect_uri="http://app.skyvern.com/callback",
            state="abc",
        )


@pytest.mark.asyncio
async def test_start_authorization_persists_verifier_and_returns_url(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(google_oauth_service.settings, "ENABLE_ENCRYPTION", True, raising=False)
    monkeypatch.setattr(google_oauth_service.settings, "GOOGLE_OAUTH_CLIENT_ID", "cid", raising=False)
    monkeypatch.setattr(google_oauth_service.settings, "GOOGLE_OAUTH_CLIENT_SECRET", "csecret", raising=False)
    monkeypatch.setattr(google_oauth_service.settings, "GOOGLE_OAUTH_REDIRECT_HOSTS", ["x"], raising=False)
    monkeypatch.setattr(
        google_oauth_service.SettingsManager,
        "get_settings",
        lambda: SimpleNamespace(ENABLE_ORGANIZATION_GOOGLE_OAUTH_CLIENT_CONFIG=True),
    )
    monkeypatch.setattr(google_oauth_service, "generate_google_oauth_credential_id", lambda: "goac_test")

    insert_mock = AsyncMock(return_value=SimpleNamespace(id="goac_test", organization_id="org_1"))
    fake_repo = SimpleNamespace(insert_pending_credential=insert_mock)
    organizations = SimpleNamespace(get_valid_org_auth_token=AsyncMock(return_value=None))
    monkeypatch.setattr(
        google_oauth_service.app,
        "DATABASE",
        SimpleNamespace(google_oauth=fake_repo, organizations=organizations),
        raising=False,
    )

    result = await google_oauth_service.start_authorization(
        organization_id="org_1",
        redirect_uri="https://x/cb",
        credential_name="my-cred",
    )

    assert result.authorize_url.startswith(google_oauth_service.GOOGLE_AUTHORIZE_ENDPOINT)
    assert result.state
    insert_mock.assert_awaited_once()
    insert_kwargs = insert_mock.await_args.kwargs
    assert insert_kwargs["organization_id"] == "org_1"
    assert insert_kwargs["credential_name"] == "my-cred"
    assert insert_kwargs["consent_redirect_uri"] == "https://x/cb"
    assert insert_kwargs["consent_nonce"] == result.state
    assert insert_kwargs["client_id"] == "cid"
    # The verifier must be in the same insert as everything else — no second
    # round-trip — so a crash mid-flow can't leave a verifier-less pending row.
    assert insert_kwargs["consent_code_verifier"]
    assert len(insert_kwargs["consent_code_verifier"]) >= 43


@pytest.mark.asyncio
async def test_start_authorization_refuses_without_encryption(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(google_oauth_service.settings, "ENABLE_ENCRYPTION", False, raising=False)
    fake_repo = SimpleNamespace(insert_pending_credential=AsyncMock())
    monkeypatch.setattr(google_oauth_service.app, "DATABASE", SimpleNamespace(google_oauth=fake_repo), raising=False)

    with pytest.raises(google_oauth_service.EncryptionNotConfiguredError):
        await google_oauth_service.start_authorization(
            organization_id="org_1",
            redirect_uri="https://x/cb",
        )
    fake_repo.insert_pending_credential.assert_not_awaited()


@pytest.mark.asyncio
async def test_promote_pending_credential_encrypts_and_calls_repo(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(google_oauth_service.settings, "ENABLE_ENCRYPTION", True, raising=False)
    encrypt_mock = AsyncMock(return_value="ENC::rt")
    monkeypatch.setattr(google_oauth_service, "encryptor", SimpleNamespace(encrypt=encrypt_mock))

    promoted_schema = SimpleNamespace(id="goac_1", organization_id="org_1")
    promote_mock = AsyncMock(return_value=promoted_schema)
    fake_repo = SimpleNamespace(promote_pending_to_active=promote_mock)
    monkeypatch.setattr(google_oauth_service.app, "DATABASE", SimpleNamespace(google_oauth=fake_repo), raising=False)

    result = await google_oauth_service.promote_pending_credential(
        organization_id="org_1",
        nonce="nonce-xyz",
        refresh_token="rt-plain",
        scopes_granted="https://a https://b",
    )

    assert result is promoted_schema
    encrypt_mock.assert_awaited_once_with("rt-plain", EncryptMethod.AES)
    promote_mock.assert_awaited_once()
    kwargs = promote_mock.await_args.kwargs
    assert kwargs["organization_id"] == "org_1"
    assert kwargs["nonce"] == "nonce-xyz"
    assert kwargs["encrypted_refresh_token"] == "ENC::rt"
    assert kwargs["encrypted_method"] == EncryptMethod.AES
    assert kwargs["scopes_granted"] == ["https://a", "https://b"]


@pytest.mark.asyncio
async def test_load_pending_consent_context_delegates_to_repo(monkeypatch: pytest.MonkeyPatch) -> None:
    from skyvern.forge.sdk.db.repositories.google_oauth import PendingConsentContext

    expected = PendingConsentContext(
        credential_id="goac_1",
        consent_redirect_uri="https://x/cb",
        consent_code_verifier="ver-abc",
    )
    fake_repo = SimpleNamespace(load_pending_by_nonce=AsyncMock(return_value=expected))
    monkeypatch.setattr(google_oauth_service.app, "DATABASE", SimpleNamespace(google_oauth=fake_repo), raising=False)

    result = await google_oauth_service.load_pending_consent_context(
        organization_id="org_1",
        nonce="nonce-xyz",
    )
    assert result is expected
    fake_repo.load_pending_by_nonce.assert_awaited_once_with(organization_id="org_1", nonce="nonce-xyz")


class _FakeOAuth2Session:
    def __init__(self, token: dict | None = None) -> None:
        self.token = token or {}


class _FakeFlow:
    """Mirrors the real google-auth-oauthlib Flow surface exchange_code_for_tokens touches."""

    def __init__(self, credentials: Any, session_token: dict | None, captured: dict) -> None:
        self.credentials = credentials
        self.oauth2session = _FakeOAuth2Session(token=session_token)
        self._captured = captured

    def fetch_token(self, code: str, code_verifier: str | None = None) -> None:
        self._captured["code"] = code
        self._captured["fetch_token_code_verifier"] = code_verifier


def _install_fake_flow(
    monkeypatch: pytest.MonkeyPatch,
    *,
    credentials: Any,
    session_token: dict | None,
) -> dict:
    captured: dict = {}

    class _FlowFactory:
        @classmethod
        def from_client_config(
            cls, client_config: dict, scopes=None, redirect_uri=None, state=None, code_verifier=None
        ) -> _FakeFlow:
            captured["client_config"] = client_config
            captured["scopes"] = scopes
            captured["redirect_uri"] = redirect_uri
            captured["init_code_verifier"] = code_verifier
            return _FakeFlow(credentials=credentials, session_token=session_token, captured=captured)

    monkeypatch.setattr(google_oauth_service, "Flow", _FlowFactory)
    return captured


@pytest.mark.asyncio
async def test_exchange_code_for_tokens_uses_granted_scopes(monkeypatch: pytest.MonkeyPatch) -> None:
    """Real google-auth leaves Credentials.scopes=None when Flow is built with scopes=None;
    the granted scope lives on Credentials.granted_scopes (passed through from the token response)."""
    monkeypatch.setattr(google_oauth_service.settings, "GOOGLE_OAUTH_CLIENT_ID", "cid", raising=False)
    monkeypatch.setattr(google_oauth_service.settings, "GOOGLE_OAUTH_CLIENT_SECRET", "secret", raising=False)

    creds = SimpleNamespace(
        token="at-from-flow",
        refresh_token="rt-from-flow",
        scopes=None,
        granted_scopes="https://a/scope https://b/scope",
        expiry=None,
    )
    captured = _install_fake_flow(
        monkeypatch,
        credentials=creds,
        session_token={"scope": "https://a/scope https://b/scope", "access_token": "at-from-flow"},
    )

    result = await google_oauth_service.exchange_code_for_tokens(
        code="abc", redirect_uri="https://x/cb", code_verifier="ver-xyz"
    )

    assert result == {
        "access_token": "at-from-flow",
        "refresh_token": "rt-from-flow",
        "scope": "https://a/scope https://b/scope",
        "expiry": None,
    }
    assert captured["code"] == "abc"
    assert captured["redirect_uri"] == "https://x/cb"
    assert captured["client_config"]["web"]["client_id"] == "cid"
    assert captured["client_config"]["web"]["client_secret"] == "secret"
    # ``Flow.from_client_config`` ignores ``code_verifier`` — PKCE actually replays
    # via ``fetch_token``, so only the fetch-time verifier matters.
    assert captured["fetch_token_code_verifier"] == "ver-xyz"


@pytest.mark.asyncio
async def test_exchange_code_for_tokens_accepts_granted_scopes_sequence(monkeypatch: pytest.MonkeyPatch) -> None:
    """Future-proof: if upstream starts handing back granted_scopes as a list, we must still serialize it."""
    monkeypatch.setattr(google_oauth_service.settings, "GOOGLE_OAUTH_CLIENT_ID", "cid", raising=False)
    monkeypatch.setattr(google_oauth_service.settings, "GOOGLE_OAUTH_CLIENT_SECRET", "secret", raising=False)

    creds = SimpleNamespace(
        token="at",
        refresh_token="rt",
        scopes=None,
        granted_scopes=["https://a/scope", "https://b/scope"],
        expiry=None,
    )
    _install_fake_flow(monkeypatch, credentials=creds, session_token={"scope": "", "access_token": "at"})

    result = await google_oauth_service.exchange_code_for_tokens(
        code="abc", redirect_uri="https://x/cb", code_verifier="v"
    )
    assert result["scope"] == "https://a/scope https://b/scope"


@pytest.mark.asyncio
async def test_exchange_code_for_tokens_falls_back_to_session_token_scope(monkeypatch: pytest.MonkeyPatch) -> None:
    """Belt-and-suspenders: if a library variant leaves granted_scopes empty, read the raw token response."""
    monkeypatch.setattr(google_oauth_service.settings, "GOOGLE_OAUTH_CLIENT_ID", "cid", raising=False)
    monkeypatch.setattr(google_oauth_service.settings, "GOOGLE_OAUTH_CLIENT_SECRET", "secret", raising=False)

    creds = SimpleNamespace(
        token="at",
        refresh_token="rt",
        scopes=None,
        granted_scopes=None,
        expiry=None,
    )
    _install_fake_flow(
        monkeypatch,
        credentials=creds,
        session_token={"scope": "https://a/scope", "access_token": "at"},
    )

    result = await google_oauth_service.exchange_code_for_tokens(
        code="abc", redirect_uri="https://x/cb", code_verifier="v"
    )
    assert result["scope"] == "https://a/scope"


@pytest.mark.asyncio
async def test_exchange_code_for_tokens_uses_provided_client_config_without_resolving(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    resolve_mock = AsyncMock()
    monkeypatch.setattr(google_oauth_service, "resolve_client_config", resolve_mock)
    config = google_oauth_service.GoogleOAuthClientConfig(
        client_id="provided-client",
        client_secret="provided-secret",
    )
    creds = SimpleNamespace(
        token="at",
        refresh_token="rt",
        scopes=None,
        granted_scopes="https://a/scope",
        expiry=None,
    )
    captured = _install_fake_flow(
        monkeypatch,
        credentials=creds,
        session_token={"scope": "https://a/scope", "access_token": "at"},
    )

    result = await google_oauth_service.exchange_code_for_tokens(
        code="abc",
        redirect_uri="https://x/cb",
        code_verifier="v",
        organization_id="org_1",
        client_config=config,
    )

    assert result["access_token"] == "at"
    assert captured["client_config"]["web"]["client_id"] == "provided-client"
    assert captured["client_config"]["web"]["client_secret"] == "provided-secret"
    resolve_mock.assert_not_awaited()


@pytest.mark.asyncio
async def test_exchange_code_raises_without_client_creds(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(google_oauth_service.settings, "GOOGLE_OAUTH_CLIENT_ID", None, raising=False)
    monkeypatch.setattr(google_oauth_service.settings, "GOOGLE_OAUTH_CLIENT_SECRET", None, raising=False)

    with pytest.raises(ValueError, match="client credentials"):
        await google_oauth_service.exchange_code_for_tokens("code", "https://x/cb", code_verifier="v")


@pytest.mark.asyncio
async def test_revoke_credential_loads_decrypts_revokes_upstream_marks(monkeypatch: pytest.MonkeyPatch) -> None:
    from skyvern.forge.sdk.db.repositories.google_oauth import RevocableCiphertext

    load_mock = AsyncMock(
        return_value=RevocableCiphertext(
            exists=True,
            encrypted_refresh_token="ENC::token",
            encrypted_method=EncryptMethod.AES,
        )
    )
    mark_mock = AsyncMock(return_value="goac_1")
    monkeypatch.setattr(
        google_oauth_service.app,
        "DATABASE",
        SimpleNamespace(
            google_oauth=SimpleNamespace(
                load_ciphertext_for_revoke=load_mock,
                mark_revoked_and_scrub=mark_mock,
            )
        ),
        raising=False,
    )

    decrypt_mock = AsyncMock(return_value="refresh-123")
    monkeypatch.setattr(google_oauth_service, "encryptor", SimpleNamespace(decrypt=decrypt_mock))
    upstream_mock = AsyncMock()
    monkeypatch.setattr(google_oauth_service, "_revoke_refresh_token_at_google", upstream_mock)

    fake_cache = SimpleNamespace(set=AsyncMock())
    monkeypatch.setattr(google_oauth_service.app, "CACHE", fake_cache, raising=False)

    revoked = await google_oauth_service.revoke_credential(organization_id="org_1", credential_id="goac_1")
    assert revoked is True
    decrypt_mock.assert_awaited_once_with("ENC::token", EncryptMethod.AES)
    upstream_mock.assert_awaited_once_with("refresh-123")
    mark_mock.assert_awaited_once()


@pytest.mark.asyncio
async def test_revoke_credential_missing_returns_false(monkeypatch: pytest.MonkeyPatch) -> None:
    from skyvern.forge.sdk.db.repositories.google_oauth import RevocableCiphertext

    load_mock = AsyncMock(return_value=RevocableCiphertext(exists=False))
    upstream_mock = AsyncMock()
    mark_mock = AsyncMock()
    monkeypatch.setattr(
        google_oauth_service.app,
        "DATABASE",
        SimpleNamespace(
            google_oauth=SimpleNamespace(
                load_ciphertext_for_revoke=load_mock,
                mark_revoked_and_scrub=mark_mock,
            )
        ),
        raising=False,
    )
    monkeypatch.setattr(google_oauth_service, "_revoke_refresh_token_at_google", upstream_mock)

    revoked = await google_oauth_service.revoke_credential(organization_id="o", credential_id="c")
    assert revoked is False
    upstream_mock.assert_not_awaited()
    mark_mock.assert_not_awaited()


@pytest.mark.asyncio
async def test_revoke_google_endpoint_swallows_errors(monkeypatch: pytest.MonkeyPatch) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(500, text="boom")

    transport = httpx.MockTransport(handler)
    real_client = httpx.AsyncClient
    monkeypatch.setattr(
        google_oauth_service.httpx,
        "AsyncClient",
        lambda *a, **kw: real_client(transport=transport),
    )

    await google_oauth_service._revoke_refresh_token_at_google("rt")


@pytest.mark.asyncio
async def test_load_credential_secrets_decrypts_repo_payload(monkeypatch: pytest.MonkeyPatch) -> None:
    from skyvern.forge.sdk.db.repositories.google_oauth import ActiveCredentialCiphertext

    payload = ActiveCredentialCiphertext(
        encrypted_refresh_token="ENC::rt",
        encrypted_method=EncryptMethod.AES,
        scopes_granted=["https://a", "https://b"],
        client_id="client-1",
    )
    fake_repo = SimpleNamespace(load_active_ciphertext=AsyncMock(return_value=payload))
    monkeypatch.setattr(google_oauth_service.app, "DATABASE", SimpleNamespace(google_oauth=fake_repo), raising=False)

    decrypt_mock = AsyncMock(return_value="refresh-123")
    monkeypatch.setattr(google_oauth_service, "encryptor", SimpleNamespace(decrypt=decrypt_mock))

    secrets = await google_oauth_service.load_credential_secrets(
        organization_id="org_1",
        credential_id="goac_1",
    )

    assert secrets.refresh_token == "refresh-123"
    assert secrets.scopes == ["https://a", "https://b"]
    assert secrets.client_id == "client-1"
    decrypt_mock.assert_awaited_once_with("ENC::rt", EncryptMethod.AES)


@pytest.mark.asyncio
async def test_load_credential_secrets_raises_when_missing(monkeypatch: pytest.MonkeyPatch) -> None:
    fake_repo = SimpleNamespace(load_active_ciphertext=AsyncMock(return_value=None))
    monkeypatch.setattr(google_oauth_service.app, "DATABASE", SimpleNamespace(google_oauth=fake_repo), raising=False)

    with pytest.raises(ValueError, match="No active Google OAuth credential"):
        await google_oauth_service.load_credential_secrets(
            organization_id="org_1",
            credential_id="goac_missing",
        )


@pytest.mark.asyncio
async def test_get_credentials_for_org_delegates_to_repo(monkeypatch: pytest.MonkeyPatch) -> None:
    creds = [SimpleNamespace(id="goac_1"), SimpleNamespace(id="goac_2")]
    list_mock = AsyncMock(return_value=creds)
    monkeypatch.setattr(
        google_oauth_service.app,
        "DATABASE",
        SimpleNamespace(google_oauth=SimpleNamespace(list_active_for_org=list_mock)),
        raising=False,
    )

    result = await google_oauth_service.get_credentials_for_org(organization_id="org_1")
    assert result is creds
    list_mock.assert_awaited_once_with(organization_id="org_1")


@pytest.mark.asyncio
async def test_get_visible_credentials_for_org_delegates_to_repo(monkeypatch: pytest.MonkeyPatch) -> None:
    credentials = [SimpleNamespace(id="goac_active", state="active"), SimpleNamespace(id="goac_error", state="error")]
    list_mock = AsyncMock(return_value=credentials)
    monkeypatch.setattr(
        google_oauth_service.app,
        "DATABASE",
        SimpleNamespace(google_oauth=SimpleNamespace(list_visible_for_org=list_mock)),
        raising=False,
    )

    result = await google_oauth_service.get_visible_credentials_for_org(organization_id="org_1")

    assert result is credentials
    list_mock.assert_awaited_once_with(organization_id="org_1")


@pytest.mark.asyncio
async def test_list_google_oauth_credentials_surfaces_error_state(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        google_oauth_routes.SettingsManager,
        "get_settings",
        lambda: SimpleNamespace(ENABLE_ORGANIZATION_GOOGLE_OAUTH_CLIENT_CONFIG=True),
    )
    now = datetime.datetime.now(datetime.UTC).replace(tzinfo=None)
    credential = GoogleOAuthCredentialBase(
        id="goac_error",
        organization_id="org_1",
        credential_name="Needs reconnect",
        state="error",
        created_at=now,
        modified_at=now,
    )
    list_mock = AsyncMock(return_value=[credential])
    monkeypatch.setattr(google_oauth_routes.google_oauth_service, "get_visible_credentials_for_org", list_mock)

    response = await google_oauth_routes.list_google_oauth_credentials(
        current_org=SimpleNamespace(organization_id="org_1")
    )

    assert response.credentials == [credential]
    assert response.credentials[0].state == "error"
    list_mock.assert_awaited_once_with(organization_id="org_1")


@pytest.mark.asyncio
async def test_access_token_from_secrets_calls_refresh(monkeypatch: pytest.MonkeyPatch) -> None:
    refresh_mock = AsyncMock(return_value={"access_token": "at-456"})
    monkeypatch.setattr(google_oauth_service, "refresh_access_token", refresh_mock)

    secrets = google_oauth_service.GoogleCredentialSecrets(
        refresh_token="rt-1",
        scopes=["https://a"],
    )

    token = await google_oauth_service.access_token_from_secrets(secrets)

    assert token == "at-456"
    refresh_mock.assert_awaited_once_with("rt-1")


@pytest.mark.asyncio
async def test_access_token_from_secrets_raises_on_client_config_mismatch(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        google_oauth_service.SettingsManager,
        "get_settings",
        lambda: SimpleNamespace(ENABLE_ORGANIZATION_GOOGLE_OAUTH_CLIENT_CONFIG=True),
    )
    resolve_mock = AsyncMock(
        return_value=google_oauth_service.GoogleOAuthClientConfigResolution(
            config=google_oauth_service.GoogleOAuthClientConfig(client_id="new", client_secret="secret"),
            source="organization",
        )
    )
    refresh_mock = AsyncMock()
    monkeypatch.setattr(google_oauth_service, "resolve_client_config", resolve_mock)
    monkeypatch.setattr(google_oauth_service, "refresh_access_token", refresh_mock)

    secrets = google_oauth_service.GoogleCredentialSecrets(refresh_token="rt-1", client_id="old")

    with pytest.raises(google_oauth_service.ClientConfigMismatchError, match="configuration changed"):
        await google_oauth_service.access_token_from_secrets(secrets, organization_id="org_1")

    resolve_mock.assert_awaited_once_with("org_1", strict=False)
    refresh_mock.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("stored_client_id", ["client-1", None])
async def test_access_token_from_secrets_passes_resolved_config_to_refresh(
    monkeypatch: pytest.MonkeyPatch,
    stored_client_id: str | None,
) -> None:
    monkeypatch.setattr(
        google_oauth_service.SettingsManager,
        "get_settings",
        lambda: SimpleNamespace(ENABLE_ORGANIZATION_GOOGLE_OAUTH_CLIENT_CONFIG=True),
    )
    config = google_oauth_service.GoogleOAuthClientConfig(client_id="client-1", client_secret="secret")
    resolve_mock = AsyncMock(
        return_value=google_oauth_service.GoogleOAuthClientConfigResolution(config=config, source="organization")
    )
    refresh_mock = AsyncMock(return_value={"access_token": "at-456"})
    monkeypatch.setattr(google_oauth_service, "resolve_client_config", resolve_mock)
    monkeypatch.setattr(google_oauth_service, "refresh_access_token", refresh_mock)

    secrets = google_oauth_service.GoogleCredentialSecrets(refresh_token="rt-1", client_id=stored_client_id)

    token = await google_oauth_service.access_token_from_secrets(secrets, organization_id="org_1")

    assert token == "at-456"
    resolve_mock.assert_awaited_once_with("org_1", strict=False)
    refresh_mock.assert_awaited_once_with("rt-1", organization_id="org_1", client_config=config)


@pytest.mark.asyncio
async def test_access_token_from_secrets_falls_back_to_env_when_org_config_load_raises(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(google_oauth_service.settings, "GOOGLE_OAUTH_CLIENT_ID", "env-client", raising=False)
    monkeypatch.setattr(google_oauth_service.settings, "GOOGLE_OAUTH_CLIENT_SECRET", "env-secret", raising=False)
    monkeypatch.setattr(
        google_oauth_service.SettingsManager,
        "get_settings",
        lambda: SimpleNamespace(ENABLE_ORGANIZATION_GOOGLE_OAUTH_CLIENT_CONFIG=True),
    )
    organizations = SimpleNamespace(
        get_valid_org_auth_token=AsyncMock(side_effect=RuntimeError("database unavailable"))
    )
    monkeypatch.setattr(
        google_oauth_service.app,
        "DATABASE",
        SimpleNamespace(organizations=organizations),
        raising=False,
    )
    captured: dict[str, object] = {}

    class _FakeCreds:
        def __init__(self, **kwargs: object) -> None:
            captured["init_kwargs"] = kwargs
            self.token: str | None = None
            self.expiry = None

        def refresh(self, request: object) -> None:
            captured["refresh_request"] = request
            self.token = "at-refreshed"

    monkeypatch.setattr(google_oauth_service, "Credentials", _FakeCreds)
    secrets = google_oauth_service.GoogleCredentialSecrets(refresh_token="rt-1", scopes=["https://a"])

    token = await google_oauth_service.access_token_from_secrets(secrets, organization_id="org_1")

    assert token == "at-refreshed"
    init_kwargs = captured["init_kwargs"]
    assert isinstance(init_kwargs, dict)
    assert init_kwargs["client_id"] == "env-client"
    assert init_kwargs["client_secret"] == "env-secret"
    assert "refresh_request" in captured
    organizations.get_valid_org_auth_token.assert_awaited_once()


@pytest.mark.asyncio
async def test_access_token_from_secrets_missing_access_token_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    refresh_mock = AsyncMock(return_value={"scope": "foo"})
    monkeypatch.setattr(google_oauth_service, "refresh_access_token", refresh_mock)

    secrets = google_oauth_service.GoogleCredentialSecrets(refresh_token="rt", scopes=[])

    with pytest.raises(google_oauth_service.MissingAccessTokenError):
        await google_oauth_service.access_token_from_secrets(secrets)


def test_validate_redirect_uri_allowlist(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        google_oauth_service.settings,
        "GOOGLE_OAUTH_REDIRECT_HOSTS",
        ["app.skyvern.com"],
        raising=False,
    )

    google_oauth_service._validate_redirect_uri("https://app.skyvern.com/google/callback")

    with pytest.raises(google_oauth_service.InvalidRedirectURIError):
        google_oauth_service._validate_redirect_uri("https://evil.example.com/callback")


def test_validate_redirect_uri_empty_allowlist_dev_fallback(monkeypatch: pytest.MonkeyPatch) -> None:
    """Empty allowlist + no client_id = dev fallback; keeps local dev ergonomic."""
    monkeypatch.setattr(google_oauth_service.settings, "GOOGLE_OAUTH_REDIRECT_HOSTS", [], raising=False)
    monkeypatch.setattr(google_oauth_service.settings, "GOOGLE_OAUTH_CLIENT_ID", None, raising=False)
    google_oauth_service._validate_redirect_uri("https://anywhere.example.com/cb")


def test_validate_redirect_uri_empty_allowlist_raises_when_client_id_set(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Empty allowlist + client_id configured = misconfigured prod; must fail closed."""
    monkeypatch.setattr(google_oauth_service.settings, "GOOGLE_OAUTH_REDIRECT_HOSTS", [], raising=False)
    monkeypatch.setattr(google_oauth_service.settings, "GOOGLE_OAUTH_CLIENT_ID", "cid", raising=False)
    with pytest.raises(google_oauth_service.InvalidRedirectURIError):
        google_oauth_service._validate_redirect_uri("https://app.skyvern.com/cb")


def test_validate_redirect_uri_rejects_http_for_non_loopback(monkeypatch: pytest.MonkeyPatch) -> None:
    """Hostname-only allowlist plus an http URI is the bypass claude[bot] flagged:
    an attacker on http://allowed-host.com:9999 should not satisfy a check intended
    for https://allowed-host.com."""
    monkeypatch.setattr(
        google_oauth_service.settings,
        "GOOGLE_OAUTH_REDIRECT_HOSTS",
        ["app.skyvern.com"],
        raising=False,
    )
    with pytest.raises(google_oauth_service.InvalidRedirectURIError, match="https"):
        google_oauth_service._validate_redirect_uri("http://app.skyvern.com/cb")


def test_validate_redirect_uri_allows_http_for_loopback(monkeypatch: pytest.MonkeyPatch) -> None:
    """Local dev needs http://localhost; loopback hosts are exempted from the https requirement."""
    monkeypatch.setattr(
        google_oauth_service.settings,
        "GOOGLE_OAUTH_REDIRECT_HOSTS",
        ["localhost", "127.0.0.1"],
        raising=False,
    )
    google_oauth_service._validate_redirect_uri("http://localhost:5173/cb")
    google_oauth_service._validate_redirect_uri("http://127.0.0.1:8080/cb")


def test_validate_redirect_uri_normalizes_allowlist_case(monkeypatch: pytest.MonkeyPatch) -> None:
    """``urlparse().hostname`` lowercases the URI host; the allowlist is lowercased
    too so an operator who configures mixed-case entries doesn't reject every redirect."""
    monkeypatch.setattr(
        google_oauth_service.settings,
        "GOOGLE_OAUTH_REDIRECT_HOSTS",
        ["MyApp.Example.Com"],
        raising=False,
    )
    google_oauth_service._validate_redirect_uri("https://myapp.example.com/cb")
    google_oauth_service._validate_redirect_uri("https://MYAPP.EXAMPLE.COM/cb")


@pytest.mark.asyncio
async def test_refresh_access_token_uses_credentials_refresh(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(google_oauth_service.settings, "GOOGLE_OAUTH_CLIENT_ID", "cid", raising=False)
    monkeypatch.setattr(google_oauth_service.settings, "GOOGLE_OAUTH_CLIENT_SECRET", "secret", raising=False)

    captured: dict = {}

    class _FakeCreds:
        def __init__(self, **kwargs) -> None:
            captured["init_kwargs"] = kwargs
            self.token: str | None = None
            self.expiry = None

        def refresh(self, request) -> None:
            captured["refresh_request_type"] = type(request).__name__
            self.token = "at-refreshed"

    monkeypatch.setattr(google_oauth_service, "Credentials", _FakeCreds)

    result = await google_oauth_service.refresh_access_token("rt-1")

    assert result == {"access_token": "at-refreshed", "expiry": None}
    assert captured["init_kwargs"]["refresh_token"] == "rt-1"
    assert captured["init_kwargs"]["client_id"] == "cid"
    assert captured["init_kwargs"]["token_uri"] == google_oauth_service.GOOGLE_TOKEN_ENDPOINT
    assert captured["refresh_request_type"] == "Request"


@pytest.mark.asyncio
async def test_refresh_access_token_wraps_google_auth_error(monkeypatch: pytest.MonkeyPatch) -> None:
    from google.auth.exceptions import RefreshError

    monkeypatch.setattr(google_oauth_service.settings, "GOOGLE_OAUTH_CLIENT_ID", "cid", raising=False)
    monkeypatch.setattr(google_oauth_service.settings, "GOOGLE_OAUTH_CLIENT_SECRET", "secret", raising=False)

    class _FakeCreds:
        def __init__(self, **_kwargs) -> None:
            self.token = None
            self.expiry = None

        def refresh(self, _request) -> None:
            raise RefreshError("invalid_grant")

    monkeypatch.setattr(google_oauth_service, "Credentials", _FakeCreds)

    with pytest.raises(google_oauth_service.MissingAccessTokenError, match="refresh failed"):
        await google_oauth_service.refresh_access_token("rt-bad")


@pytest.mark.asyncio
async def test_credentials_from_secrets_wraps_google_auth_error(monkeypatch: pytest.MonkeyPatch) -> None:
    from google.auth.exceptions import RefreshError

    monkeypatch.setattr(google_oauth_service.settings, "GOOGLE_OAUTH_CLIENT_ID", "cid", raising=False)
    monkeypatch.setattr(google_oauth_service.settings, "GOOGLE_OAUTH_CLIENT_SECRET", "secret", raising=False)

    class _FakeCreds:
        def __init__(self, **_kwargs) -> None:
            self.token = None

        def refresh(self, _request) -> None:
            raise RefreshError("token revoked")

    monkeypatch.setattr(google_oauth_service, "Credentials", _FakeCreds)

    secrets = google_oauth_service.GoogleCredentialSecrets(refresh_token="rt-1", scopes=["https://a"])
    with pytest.raises(google_oauth_service.MissingAccessTokenError, match="refresh failed"):
        await google_oauth_service.credentials_from_secrets(secrets)


@pytest.mark.asyncio
async def test_credentials_from_secrets_raises_on_client_config_mismatch(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        google_oauth_service.SettingsManager,
        "get_settings",
        lambda: SimpleNamespace(ENABLE_ORGANIZATION_GOOGLE_OAUTH_CLIENT_CONFIG=True),
    )
    resolve_mock = AsyncMock(
        return_value=google_oauth_service.GoogleOAuthClientConfigResolution(
            config=google_oauth_service.GoogleOAuthClientConfig(client_id="new", client_secret="secret"),
            source="organization",
        )
    )
    monkeypatch.setattr(google_oauth_service, "resolve_client_config", resolve_mock)

    class _UnexpectedCreds:
        def __init__(self, **_kwargs) -> None:
            raise AssertionError("Credentials should not be constructed")

    monkeypatch.setattr(google_oauth_service, "Credentials", _UnexpectedCreds)

    secrets = google_oauth_service.GoogleCredentialSecrets(
        refresh_token="rt-1",
        scopes=["https://a"],
        client_id="old",
    )
    with pytest.raises(google_oauth_service.ClientConfigMismatchError, match="configuration changed"):
        await google_oauth_service.credentials_from_secrets(secrets, organization_id="org_1")

    resolve_mock.assert_awaited_once_with("org_1", strict=False)


@pytest.mark.asyncio
async def test_credentials_from_secrets_allows_matching_client_config(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        google_oauth_service.SettingsManager,
        "get_settings",
        lambda: SimpleNamespace(ENABLE_ORGANIZATION_GOOGLE_OAUTH_CLIENT_CONFIG=True),
    )
    config = google_oauth_service.GoogleOAuthClientConfig(client_id="client-1", client_secret="secret")
    resolve_mock = AsyncMock(
        return_value=google_oauth_service.GoogleOAuthClientConfigResolution(config=config, source="organization")
    )
    monkeypatch.setattr(google_oauth_service, "resolve_client_config", resolve_mock)
    captured: dict = {}

    class _FakeCreds:
        def __init__(self, **kwargs) -> None:
            captured["init_kwargs"] = kwargs
            self.token: str | None = None

        def refresh(self, request) -> None:
            self.token = "at-refreshed"

    monkeypatch.setattr(google_oauth_service, "Credentials", _FakeCreds)

    secrets = google_oauth_service.GoogleCredentialSecrets(
        refresh_token="rt-decoded",
        scopes=["https://a"],
        client_id="client-1",
    )
    creds = await google_oauth_service.credentials_from_secrets(secrets, organization_id="org_1")

    assert creds.token == "at-refreshed"
    assert captured["init_kwargs"]["client_id"] == "client-1"
    assert captured["init_kwargs"]["client_secret"] == "secret"
    resolve_mock.assert_awaited_once_with("org_1", strict=False)


@pytest.mark.asyncio
async def test_credentials_from_secrets_returns_refreshed_credentials(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(google_oauth_service.settings, "GOOGLE_OAUTH_CLIENT_ID", "cid", raising=False)
    monkeypatch.setattr(google_oauth_service.settings, "GOOGLE_OAUTH_CLIENT_SECRET", "secret", raising=False)

    captured: dict = {}

    class _FakeCreds:
        def __init__(self, **kwargs) -> None:
            captured["init_kwargs"] = kwargs
            self.token: str | None = None

        def refresh(self, request) -> None:
            self.token = "at-refreshed"

    monkeypatch.setattr(google_oauth_service, "Credentials", _FakeCreds)

    secrets = google_oauth_service.GoogleCredentialSecrets(
        refresh_token="rt-decoded",
        scopes=["https://a", "https://b"],
    )
    creds = await google_oauth_service.credentials_from_secrets(secrets)

    assert creds.token == "at-refreshed"
    assert captured["init_kwargs"]["refresh_token"] == "rt-decoded"
    assert captured["init_kwargs"]["scopes"] == ["https://a", "https://b"]


def test_update_google_oauth_credential_request_strips_whitespace() -> None:
    request = UpdateGoogleOAuthCredentialRequest(credential_name="  Personal Gmail  ")
    assert request.credential_name == "Personal Gmail"


def test_update_google_oauth_credential_request_rejects_blank() -> None:
    with pytest.raises(ValidationError):
        UpdateGoogleOAuthCredentialRequest(credential_name="   ")


def test_update_google_oauth_credential_request_rejects_empty() -> None:
    with pytest.raises(ValidationError):
        UpdateGoogleOAuthCredentialRequest(credential_name="")


def test_update_google_oauth_credential_request_enforces_max_length() -> None:
    with pytest.raises(ValidationError):
        UpdateGoogleOAuthCredentialRequest(credential_name="x" * 129)


@pytest.mark.asyncio
async def test_google_oauth_callback_rejects_changed_client_before_exchange(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from skyvern.forge.sdk.db.repositories.google_oauth import PendingConsentContext

    monkeypatch.setattr(
        google_oauth_service.SettingsManager,
        "get_settings",
        lambda: SimpleNamespace(ENABLE_ORGANIZATION_GOOGLE_OAUTH_CLIENT_CONFIG=True),
    )
    load_mock = AsyncMock(
        return_value=PendingConsentContext(
            credential_id="goac_1",
            consent_redirect_uri="https://x/cb",
            consent_code_verifier="verifier",
            client_id="old-client",
        )
    )
    resolve_mock = AsyncMock(
        return_value=google_oauth_service.GoogleOAuthClientConfigResolution(
            config=google_oauth_service.GoogleOAuthClientConfig(client_id="new-client", client_secret="secret"),
            source="organization",
        )
    )
    exchange_mock = AsyncMock()
    monkeypatch.setattr(google_oauth_routes.google_oauth_service, "load_pending_consent_context", load_mock)
    monkeypatch.setattr(google_oauth_routes.google_oauth_service, "resolve_client_config", resolve_mock)
    monkeypatch.setattr(google_oauth_routes.google_oauth_service, "exchange_code_for_tokens", exchange_mock)

    with pytest.raises(HTTPException) as exc_info:
        await google_oauth_routes.google_oauth_callback(
            CreateGoogleOAuthCallbackRequest(code="code", state="nonce"),
            current_org=SimpleNamespace(organization_id="org_1"),
        )

    assert exc_info.value.status_code == 409
    assert (
        exc_info.value.detail
        == "Google OAuth client configuration changed since consent started; restart the connection"
    )
    load_mock.assert_awaited_once_with(organization_id="org_1", nonce="nonce")
    resolve_mock.assert_awaited_once_with("org_1")
    exchange_mock.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("context_client_id", "resolved_client_id"),
    [
        ("old-client", "old-client"),
        (None, None),
    ],
)
async def test_google_oauth_callback_allows_matching_or_legacy_client(
    monkeypatch: pytest.MonkeyPatch,
    context_client_id: str | None,
    resolved_client_id: str | None,
) -> None:
    from skyvern.forge.sdk.db.repositories.google_oauth import PendingConsentContext

    monkeypatch.setattr(
        google_oauth_service.SettingsManager,
        "get_settings",
        lambda: SimpleNamespace(ENABLE_ORGANIZATION_GOOGLE_OAUTH_CLIENT_CONFIG=True),
    )
    load_mock = AsyncMock(
        return_value=PendingConsentContext(
            credential_id="goac_1",
            consent_redirect_uri="https://x/cb",
            consent_code_verifier="verifier",
            client_id=context_client_id,
        )
    )
    resolved_config = (
        google_oauth_service.GoogleOAuthClientConfig(client_id=resolved_client_id, client_secret="secret")
        if resolved_client_id
        else None
    )
    resolve_mock = AsyncMock(
        return_value=google_oauth_service.GoogleOAuthClientConfigResolution(
            config=resolved_config,
            source="organization" if resolved_config else "missing",
        )
    )
    exchange_mock = AsyncMock(
        return_value={
            "refresh_token": "refresh-token",
            "scope": "https://www.googleapis.com/auth/spreadsheets",
        }
    )
    credential = GoogleOAuthCredentialBase(
        id="goac_1",
        organization_id="org_1",
        credential_name="Default",
        provider="google",
        state="active",
        scopes_requested=[],
        scopes_granted=["https://www.googleapis.com/auth/spreadsheets"],
        created_at=datetime.datetime.utcnow(),
        modified_at=datetime.datetime.utcnow(),
    )
    promote_mock = AsyncMock(return_value=credential)
    monkeypatch.setattr(google_oauth_routes.google_oauth_service, "load_pending_consent_context", load_mock)
    monkeypatch.setattr(google_oauth_routes.google_oauth_service, "resolve_client_config", resolve_mock)
    monkeypatch.setattr(google_oauth_routes.google_oauth_service, "exchange_code_for_tokens", exchange_mock)
    monkeypatch.setattr(google_oauth_routes.google_oauth_service, "promote_pending_credential", promote_mock)

    response = await google_oauth_routes.google_oauth_callback(
        CreateGoogleOAuthCallbackRequest(code="code", state="nonce"),
        current_org=SimpleNamespace(organization_id="org_1"),
    )

    assert response.credential.id == "goac_1"
    exchange_mock.assert_awaited_once_with(
        code="code",
        redirect_uri="https://x/cb",
        code_verifier="verifier",
        organization_id="org_1",
        client_config=resolved_config,
    )
    promote_mock.assert_awaited_once_with(
        organization_id="org_1",
        nonce="nonce",
        refresh_token="refresh-token",
        scopes_granted=["https://www.googleapis.com/auth/spreadsheets"],
    )


@pytest.mark.asyncio
async def test_rename_credential_delegates_to_repo(monkeypatch: pytest.MonkeyPatch) -> None:
    renamed = SimpleNamespace(id="goac_1", credential_name="Personal Gmail")
    rename_mock = AsyncMock(return_value=renamed)
    monkeypatch.setattr(
        google_oauth_service.app,
        "DATABASE",
        SimpleNamespace(google_oauth=SimpleNamespace(rename_active=rename_mock)),
        raising=False,
    )

    result = await google_oauth_service.rename_credential(
        organization_id="org_1",
        credential_id="goac_1",
        credential_name="Personal Gmail",
    )
    assert result is renamed
    rename_mock.assert_awaited_once()
    kwargs = rename_mock.await_args.kwargs
    assert kwargs["organization_id"] == "org_1"
    assert kwargs["credential_id"] == "goac_1"
    assert kwargs["credential_name"] == "Personal Gmail"


@pytest.mark.asyncio
async def test_rename_credential_returns_none_when_not_found(monkeypatch: pytest.MonkeyPatch) -> None:
    rename_mock = AsyncMock(return_value=None)
    monkeypatch.setattr(
        google_oauth_service.app,
        "DATABASE",
        SimpleNamespace(google_oauth=SimpleNamespace(rename_active=rename_mock)),
        raising=False,
    )
    result = await google_oauth_service.rename_credential(
        organization_id="org_1",
        credential_id="goac_missing",
        credential_name="Anything",
    )
    assert result is None


def test_require_scopes_from_token_returns_scope_when_present() -> None:
    from skyvern.forge.sdk.routes import google_oauth as oauth_route

    assert oauth_route._require_scopes_from_token({"scope": "https://a https://b"}) == ["https://a", "https://b"]


def test_require_scopes_from_token_raises_on_missing_scope() -> None:
    from fastapi import HTTPException

    from skyvern.forge.sdk.routes import google_oauth as oauth_route

    with pytest.raises(HTTPException) as excinfo:
        oauth_route._require_scopes_from_token({})
    assert excinfo.value.status_code == 400
    assert "scope" in excinfo.value.detail.lower()


def test_require_scopes_from_token_raises_on_empty_scope_string() -> None:
    """Google returns an empty scope string on partial consent; must fail closed, not default."""
    from fastapi import HTTPException

    from skyvern.forge.sdk.routes import google_oauth as oauth_route

    with pytest.raises(HTTPException) as excinfo:
        oauth_route._require_scopes_from_token({"scope": ""})
    assert excinfo.value.status_code == 400
    assert "scope" in excinfo.value.detail.lower()


def test_require_scopes_from_token_raises_on_whitespace_only_scope() -> None:
    from fastapi import HTTPException

    from skyvern.forge.sdk.routes import google_oauth as oauth_route

    with pytest.raises(HTTPException):
        oauth_route._require_scopes_from_token({"scope": "   "})


# ---------------------------------------------------------------------------
# _validate_app_origin tests
# ---------------------------------------------------------------------------


def test_validate_app_origin_exact_match_https(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        google_oauth_service.settings,
        "GOOGLE_OAUTH_APP_ORIGINS",
        ["https://app.skyvern.com"],
        raising=False,
    )
    google_oauth_service._validate_app_origin("https://app.skyvern.com")


def test_validate_app_origin_exact_match_http_localhost(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        google_oauth_service.settings,
        "GOOGLE_OAUTH_APP_ORIGINS",
        ["http://localhost:5173"],
        raising=False,
    )
    google_oauth_service._validate_app_origin("http://localhost:5173")


def test_validate_app_origin_rejects_non_matching_exact(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        google_oauth_service.settings,
        "GOOGLE_OAUTH_APP_ORIGINS",
        ["https://app.skyvern.com"],
        raising=False,
    )
    with pytest.raises(google_oauth_service.InvalidAppOriginError, match="not allowed"):
        google_oauth_service._validate_app_origin("https://evil.example.com")


def test_validate_app_origin_suffix_wildcard_matches(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        google_oauth_service.settings,
        "GOOGLE_OAUTH_APP_ORIGINS",
        ["*.vercel.app"],
        raising=False,
    )
    google_oauth_service._validate_app_origin("https://skyvern-cloud-git-main-skyvern.vercel.app")


def test_validate_app_origin_suffix_wildcard_matches_with_port(monkeypatch: pytest.MonkeyPatch) -> None:
    """Suffix matching strips the port — ``*.vercel.app`` accepts ``myapp.vercel.app:3000``."""
    monkeypatch.setattr(
        google_oauth_service.settings,
        "GOOGLE_OAUTH_APP_ORIGINS",
        ["*.vercel.app"],
        raising=False,
    )
    google_oauth_service._validate_app_origin("https://myapp.vercel.app:3000")


def test_validate_app_origin_suffix_wildcard_rejects_bare_suffix(monkeypatch: pytest.MonkeyPatch) -> None:
    """'vercel.app' itself must not match '*.vercel.app'."""
    monkeypatch.setattr(
        google_oauth_service.settings,
        "GOOGLE_OAUTH_APP_ORIGINS",
        ["*.vercel.app"],
        raising=False,
    )
    with pytest.raises(google_oauth_service.InvalidAppOriginError):
        google_oauth_service._validate_app_origin("https://vercel.app")


def test_validate_app_origin_suffix_wildcard_rejects_spoof_prefix(monkeypatch: pytest.MonkeyPatch) -> None:
    """'attacker-vercel.app' must not match '*.vercel.app'."""
    monkeypatch.setattr(
        google_oauth_service.settings,
        "GOOGLE_OAUTH_APP_ORIGINS",
        ["*.vercel.app"],
        raising=False,
    )
    with pytest.raises(google_oauth_service.InvalidAppOriginError):
        google_oauth_service._validate_app_origin("https://attacker-vercel.app")


def test_validate_app_origin_suffix_wildcard_rejects_spoof_suffix(monkeypatch: pytest.MonkeyPatch) -> None:
    """'vercel.app.evil.com' must not match '*.vercel.app'."""
    monkeypatch.setattr(
        google_oauth_service.settings,
        "GOOGLE_OAUTH_APP_ORIGINS",
        ["*.vercel.app"],
        raising=False,
    )
    with pytest.raises(google_oauth_service.InvalidAppOriginError):
        google_oauth_service._validate_app_origin("https://vercel.app.evil.com")


def test_validate_app_origin_wildcard_requires_https(monkeypatch: pytest.MonkeyPatch) -> None:
    """Wildcard entries only match https, not http."""
    monkeypatch.setattr(
        google_oauth_service.settings,
        "GOOGLE_OAUTH_APP_ORIGINS",
        ["*.vercel.app"],
        raising=False,
    )
    with pytest.raises(google_oauth_service.InvalidAppOriginError):
        google_oauth_service._validate_app_origin("http://skyvern-cloud-git-main-skyvern.vercel.app")


def test_validate_app_origin_empty_allowlist_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(google_oauth_service.settings, "GOOGLE_OAUTH_APP_ORIGINS", [], raising=False)
    with pytest.raises(google_oauth_service.InvalidAppOriginError, match="not configured"):
        google_oauth_service._validate_app_origin("https://app.skyvern.com")


@pytest.mark.asyncio
async def test_start_authorization_persists_app_origin(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(google_oauth_service.settings, "ENABLE_ENCRYPTION", True, raising=False)
    monkeypatch.setattr(google_oauth_service.settings, "GOOGLE_OAUTH_CLIENT_ID", "cid", raising=False)
    monkeypatch.setattr(google_oauth_service.settings, "GOOGLE_OAUTH_CLIENT_SECRET", "csecret", raising=False)
    monkeypatch.setattr(
        google_oauth_service.settings, "GOOGLE_OAUTH_REDIRECT_HOSTS", ["app-staging.skyvern.com"], raising=False
    )
    monkeypatch.setattr(google_oauth_service.settings, "GOOGLE_OAUTH_APP_ORIGINS", ["*.vercel.app"], raising=False)
    monkeypatch.setattr(
        google_oauth_service.SettingsManager,
        "get_settings",
        lambda: SimpleNamespace(ENABLE_ORGANIZATION_GOOGLE_OAUTH_CLIENT_CONFIG=True),
    )
    monkeypatch.setattr(google_oauth_service, "generate_google_oauth_credential_id", lambda: "goac_test2")

    insert_mock = AsyncMock(return_value=SimpleNamespace(id="goac_test2", organization_id="org_1"))
    fake_repo = SimpleNamespace(insert_pending_credential=insert_mock)
    organizations = SimpleNamespace(get_valid_org_auth_token=AsyncMock(return_value=None))
    monkeypatch.setattr(
        google_oauth_service.app,
        "DATABASE",
        SimpleNamespace(google_oauth=fake_repo, organizations=organizations),
        raising=False,
    )

    result = await google_oauth_service.start_authorization(
        organization_id="org_1",
        redirect_uri="https://app-staging.skyvern.com/integrations/google/callback",
        credential_name="Test",
        app_origin="https://skyvern-cloud-git-branch-skyvern.vercel.app",
    )

    assert result.state
    insert_mock.assert_awaited_once()
    insert_kwargs = insert_mock.await_args.kwargs
    assert insert_kwargs["consent_app_origin"] == "https://skyvern-cloud-git-branch-skyvern.vercel.app"


@pytest.mark.asyncio
async def test_start_authorization_rejects_bad_app_origin(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(google_oauth_service.settings, "ENABLE_ENCRYPTION", True, raising=False)
    monkeypatch.setattr(google_oauth_service.settings, "GOOGLE_OAUTH_CLIENT_ID", "cid", raising=False)
    monkeypatch.setattr(google_oauth_service.settings, "GOOGLE_OAUTH_CLIENT_SECRET", "csecret", raising=False)
    monkeypatch.setattr(
        google_oauth_service.settings, "GOOGLE_OAUTH_REDIRECT_HOSTS", ["app-staging.skyvern.com"], raising=False
    )
    monkeypatch.setattr(
        google_oauth_service.settings, "GOOGLE_OAUTH_APP_ORIGINS", ["https://app.skyvern.com"], raising=False
    )
    monkeypatch.setattr(
        google_oauth_service.SettingsManager,
        "get_settings",
        lambda: SimpleNamespace(ENABLE_ORGANIZATION_GOOGLE_OAUTH_CLIENT_CONFIG=True),
    )

    insert_mock = AsyncMock()
    fake_repo = SimpleNamespace(insert_pending_credential=insert_mock)
    organizations = SimpleNamespace(get_valid_org_auth_token=AsyncMock(return_value=None))
    monkeypatch.setattr(
        google_oauth_service.app,
        "DATABASE",
        SimpleNamespace(google_oauth=fake_repo, organizations=organizations),
        raising=False,
    )

    with pytest.raises(google_oauth_service.InvalidAppOriginError):
        await google_oauth_service.start_authorization(
            organization_id="org_1",
            redirect_uri="https://app-staging.skyvern.com/integrations/google/callback",
            app_origin="https://evil.example.com",
        )
    insert_mock.assert_not_awaited()


def test_google_oauth_credential_response_exposes_app_origin() -> None:
    import datetime

    from skyvern.forge.sdk.schemas.google_oauth import GoogleOAuthCredentialBase, GoogleOAuthCredentialResponse

    cred = GoogleOAuthCredentialBase(
        id="goac_1",
        organization_id="o_1",
        credential_name="Default",
        provider="google",
        state="active",
        scopes_requested=[],
        scopes_granted=[],
        created_at=datetime.datetime.utcnow(),
        modified_at=datetime.datetime.utcnow(),
    )
    resp = GoogleOAuthCredentialResponse(credential=cred, app_origin="https://foo.vercel.app")
    assert resp.app_origin == "https://foo.vercel.app"

    resp_no_origin = GoogleOAuthCredentialResponse(credential=cred)
    assert resp_no_origin.app_origin is None
