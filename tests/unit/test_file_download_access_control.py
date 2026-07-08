from __future__ import annotations

import os
import socket
from collections.abc import AsyncIterator
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from skyvern.config import settings
from skyvern.constants import DOWNLOAD_FILE_PREFIX
from skyvern.exceptions import BlockedHost, SkyvernHTTPException
from skyvern.forge.sdk.api import files
from skyvern.utils.url_validators import MAX_SAFE_REDIRECTS

ATTACKER_ORG_ID = "o_attacker"
VICTIM_ORG_ID = "o_victim"


def _legacy_s3_uri(organization_id: str) -> str:
    return f"s3://{settings.AWS_S3_BUCKET_UPLOADS}/{settings.ENV}/{organization_id}/secret.pdf"


def _downloads_s3_uri(organization_id: str) -> str:
    return (
        f"s3://{settings.AWS_S3_BUCKET_UPLOADS}/"
        f"{DOWNLOAD_FILE_PREFIX}/{settings.ENV}/{organization_id}/wr_123/secret.pdf"
    )


def _artifact_s3_uri(organization_id: str) -> str:
    return (
        f"s3://{settings.AWS_S3_BUCKET_ARTIFACTS}/"
        f"v1/{settings.ENV}/{organization_id}/workflow_runs/wr_123/wrb_456/artifact.pdf"
    )


def _legacy_gcs_uri(organization_id: str) -> str:
    return f"gs://{settings.GCS_BUCKET_UPLOADS}/{settings.ENV}/{organization_id}/secret.pdf"


@pytest.fixture(autouse=True)
def storage(monkeypatch: pytest.MonkeyPatch) -> SimpleNamespace:
    def assert_managed_file_access(uri: str, organization_id: str) -> None:
        if organization_id == ATTACKER_ORG_ID and (
            uri == _legacy_s3_uri(ATTACKER_ORG_ID)
            or uri == _downloads_s3_uri(ATTACKER_ORG_ID)
            or uri == _artifact_s3_uri(ATTACKER_ORG_ID)
            or uri == _legacy_gcs_uri(ATTACKER_ORG_ID)
        ):
            return
        raise PermissionError(f"No permission to access storage URI: {uri}")

    storage = SimpleNamespace(
        storage_type="test",
        assert_managed_file_access=MagicMock(side_effect=assert_managed_file_access),
        download_managed_file=AsyncMock(return_value=b"tenant-secret-bytes"),
    )
    monkeypatch.setattr(files, "app", SimpleNamespace(STORAGE=storage))
    return storage


def test_validate_download_url_rejects_cross_org_s3_uri() -> None:
    assert files.validate_download_url(_legacy_s3_uri(VICTIM_ORG_ID), organization_id=ATTACKER_ORG_ID) is False


def test_validate_download_url_allows_same_org_downloads_prefix() -> None:
    assert files.validate_download_url(_downloads_s3_uri(ATTACKER_ORG_ID), organization_id=ATTACKER_ORG_ID) is True


@pytest.mark.asyncio
async def test_download_file_rejects_cross_org_s3_uri(storage: SimpleNamespace) -> None:
    with pytest.raises(PermissionError, match="No permission to access storage URI"):
        await files.download_file(_legacy_s3_uri(VICTIM_ORG_ID), organization_id=ATTACKER_ORG_ID)

    storage.assert_managed_file_access.assert_called_once_with(_legacy_s3_uri(VICTIM_ORG_ID), ATTACKER_ORG_ID)
    storage.download_managed_file.assert_not_called()


@pytest.mark.asyncio
async def test_download_file_allows_same_org_legacy_upload(storage: SimpleNamespace) -> None:
    path = await files.download_file(_legacy_s3_uri(ATTACKER_ORG_ID), organization_id=ATTACKER_ORG_ID)

    storage.assert_managed_file_access.assert_called_once_with(_legacy_s3_uri(ATTACKER_ORG_ID), ATTACKER_ORG_ID)
    storage.download_managed_file.assert_awaited_once_with(_legacy_s3_uri(ATTACKER_ORG_ID), ATTACKER_ORG_ID)
    try:
        with open(path, "rb") as f:
            assert f.read() == b"tenant-secret-bytes"
    finally:
        os.unlink(path)


@pytest.mark.asyncio
async def test_download_file_allows_same_org_downloaded_artifact(storage: SimpleNamespace) -> None:
    path = await files.download_file(_downloads_s3_uri(ATTACKER_ORG_ID), organization_id=ATTACKER_ORG_ID)

    storage.assert_managed_file_access.assert_called_once_with(_downloads_s3_uri(ATTACKER_ORG_ID), ATTACKER_ORG_ID)
    storage.download_managed_file.assert_awaited_once_with(_downloads_s3_uri(ATTACKER_ORG_ID), ATTACKER_ORG_ID)
    try:
        with open(path, "rb") as f:
            assert f.read() == b"tenant-secret-bytes"
    finally:
        os.unlink(path)


@pytest.mark.asyncio
async def test_download_file_allows_same_org_artifact_uri(storage: SimpleNamespace) -> None:
    path = await files.download_file(_artifact_s3_uri(ATTACKER_ORG_ID), organization_id=ATTACKER_ORG_ID)

    storage.assert_managed_file_access.assert_called_once_with(_artifact_s3_uri(ATTACKER_ORG_ID), ATTACKER_ORG_ID)
    storage.download_managed_file.assert_awaited_once_with(_artifact_s3_uri(ATTACKER_ORG_ID), ATTACKER_ORG_ID)
    try:
        with open(path, "rb") as f:
            assert f.read() == b"tenant-secret-bytes"
    finally:
        os.unlink(path)


@pytest.mark.asyncio
async def test_download_file_rejects_cross_org_artifact_uri(storage: SimpleNamespace) -> None:
    with pytest.raises(PermissionError, match="No permission to access storage URI"):
        await files.download_file(_artifact_s3_uri(VICTIM_ORG_ID), organization_id=ATTACKER_ORG_ID)

    storage.download_managed_file.assert_not_called()


def test_validate_download_url_allows_same_org_artifact_uri() -> None:
    assert files.validate_download_url(_artifact_s3_uri(ATTACKER_ORG_ID), organization_id=ATTACKER_ORG_ID) is True


def test_validate_download_url_rejects_cross_org_artifact_uri() -> None:
    assert files.validate_download_url(_artifact_s3_uri(VICTIM_ORG_ID), organization_id=ATTACKER_ORG_ID) is False


def test_validate_download_url_rejects_s3_uri_without_org_id() -> None:
    assert files.validate_download_url(_legacy_s3_uri(ATTACKER_ORG_ID), organization_id=None) is False


@pytest.mark.asyncio
async def test_download_file_rejects_s3_uri_without_org_id(storage: SimpleNamespace) -> None:
    with pytest.raises(PermissionError, match="No permission to access storage URI"):
        await files.download_file(_legacy_s3_uri(ATTACKER_ORG_ID), organization_id=None)

    storage.assert_managed_file_access.assert_not_called()
    storage.download_managed_file.assert_not_called()


def test_validate_download_url_allows_same_org_legacy_prefix() -> None:
    assert files.validate_download_url(_legacy_s3_uri(ATTACKER_ORG_ID), organization_id=ATTACKER_ORG_ID) is True


def test_validate_download_url_rejects_path_traversal() -> None:
    uri = f"s3://{settings.AWS_S3_BUCKET_UPLOADS}/{settings.ENV}/{ATTACKER_ORG_ID}/../{VICTIM_ORG_ID}/secret.pdf"
    assert files.validate_download_url(uri, organization_id=ATTACKER_ORG_ID) is False


def test_validate_download_url_rejects_different_bucket() -> None:
    uri = f"s3://some-other-bucket/{settings.ENV}/{ATTACKER_ORG_ID}/file.csv"
    assert files.validate_download_url(uri, organization_id=ATTACKER_ORG_ID) is False


def test_validate_download_url_rejects_no_org_prefix() -> None:
    uri = f"s3://{settings.AWS_S3_BUCKET_UPLOADS}/{settings.ENV}/file.csv"
    assert files.validate_download_url(uri, organization_id=ATTACKER_ORG_ID) is False


def test_validate_download_url_rejects_wrong_env() -> None:
    uri = f"s3://{settings.AWS_S3_BUCKET_UPLOADS}/production/{ATTACKER_ORG_ID}/file.csv"
    assert files.validate_download_url(uri, organization_id=ATTACKER_ORG_ID) is False


@pytest.mark.asyncio
async def test_download_file_rejects_path_traversal(storage: SimpleNamespace) -> None:
    uri = f"s3://{settings.AWS_S3_BUCKET_UPLOADS}/{settings.ENV}/{ATTACKER_ORG_ID}/../{VICTIM_ORG_ID}/secret.pdf"
    with pytest.raises(PermissionError, match="No permission to access storage URI"):
        await files.download_file(uri, organization_id=ATTACKER_ORG_ID)

    storage.download_managed_file.assert_not_called()


@pytest.mark.asyncio
async def test_download_file_rejects_different_bucket(storage: SimpleNamespace) -> None:
    uri = f"s3://some-other-bucket/{settings.ENV}/{ATTACKER_ORG_ID}/file.csv"
    with pytest.raises(PermissionError, match="No permission to access storage URI"):
        await files.download_file(uri, organization_id=ATTACKER_ORG_ID)

    storage.download_managed_file.assert_not_called()


def _legacy_azure_uri(organization_id: str) -> str:
    return f"azure://{settings.AZURE_STORAGE_CONTAINER_UPLOADS}/{settings.ENV}/{organization_id}/secret.pdf"


def test_validate_download_url_rejects_cross_org_azure_uri() -> None:
    assert files.validate_download_url(_legacy_azure_uri(VICTIM_ORG_ID), organization_id=ATTACKER_ORG_ID) is False


def test_validate_download_url_rejects_azure_uri_without_org_id() -> None:
    assert files.validate_download_url(_legacy_azure_uri(ATTACKER_ORG_ID), organization_id=None) is False


@pytest.mark.asyncio
async def test_download_file_rejects_cross_org_azure_uri(storage: SimpleNamespace) -> None:
    with pytest.raises(PermissionError, match="No permission to access storage URI"):
        await files.download_file(_legacy_azure_uri(VICTIM_ORG_ID), organization_id=ATTACKER_ORG_ID)

    storage.download_managed_file.assert_not_called()


@pytest.mark.asyncio
async def test_download_file_rejects_azure_uri_without_org_id(storage: SimpleNamespace) -> None:
    with pytest.raises(PermissionError, match="No permission to access storage URI"):
        await files.download_file(_legacy_azure_uri(ATTACKER_ORG_ID), organization_id=None)

    storage.assert_managed_file_access.assert_not_called()
    storage.download_managed_file.assert_not_called()


@pytest.mark.asyncio
async def test_download_file_reraises_permission_error() -> None:
    """Verify PermissionError propagates to caller and is not silently caught."""
    with pytest.raises(PermissionError, match="No permission") as exc_info:
        await files.download_file(_legacy_s3_uri(VICTIM_ORG_ID), organization_id=ATTACKER_ORG_ID)
    assert "No permission" in str(exc_info.value)


def test_validate_download_url_allows_same_org_gcs_uri() -> None:
    # Discriminating check: a gs:// URI must be routed to the managed-storage
    # access check, not rejected as an unsupported scheme.
    assert files.validate_download_url(_legacy_gcs_uri(ATTACKER_ORG_ID), organization_id=ATTACKER_ORG_ID) is True


def test_validate_download_url_rejects_cross_org_gcs_uri() -> None:
    assert files.validate_download_url(_legacy_gcs_uri(VICTIM_ORG_ID), organization_id=ATTACKER_ORG_ID) is False


@pytest.mark.asyncio
async def test_download_file_routes_gcs_uri_to_managed_storage(storage: SimpleNamespace) -> None:
    path = await files.download_file(_legacy_gcs_uri(ATTACKER_ORG_ID), organization_id=ATTACKER_ORG_ID)

    storage.assert_managed_file_access.assert_called_once_with(_legacy_gcs_uri(ATTACKER_ORG_ID), ATTACKER_ORG_ID)
    storage.download_managed_file.assert_awaited_once_with(_legacy_gcs_uri(ATTACKER_ORG_ID), ATTACKER_ORG_ID)
    try:
        with open(path, "rb") as f:
            assert f.read() == b"tenant-secret-bytes"
    finally:
        os.unlink(path)


@pytest.mark.asyncio
async def test_download_file_blocks_hostname_resolving_to_private_ip(monkeypatch: pytest.MonkeyPatch) -> None:
    def resolves_private(host: str, port: int | None, *args: object, **kwargs: object) -> list[object]:
        return [(socket.AF_INET, socket.SOCK_STREAM, 0, "", ("10.0.0.42", port or 0))]

    client_session = MagicMock()
    monkeypatch.setattr("skyvern.utils.url_validators.socket.getaddrinfo", resolves_private)
    monkeypatch.setattr(files.aiohttp, "ClientSession", client_session)

    with pytest.raises(BlockedHost):
        await files.download_file("https://evil.example.test/secret.pdf")

    client_session.assert_not_called()


@pytest.mark.asyncio
async def test_download_file_rejects_unsafe_redirect_target(monkeypatch: pytest.MonkeyPatch) -> None:
    def resolves_public(host: str, port: int | None, *args: object, **kwargs: object) -> list[object]:
        return [(socket.AF_INET, socket.SOCK_STREAM, 0, "", ("93.184.216.34", port or 0))]

    redirect_response = AsyncMock()
    redirect_response.status = 302
    redirect_response.headers = {"Location": "http://169.254.169.254/latest/meta-data"}
    redirect_response.__aenter__ = AsyncMock(return_value=redirect_response)
    redirect_response.__aexit__ = AsyncMock(return_value=None)

    mock_session = MagicMock()
    mock_session.__aenter__ = AsyncMock(return_value=mock_session)
    mock_session.__aexit__ = AsyncMock(return_value=None)
    mock_session.get = MagicMock(return_value=redirect_response)

    monkeypatch.setattr("skyvern.utils.url_validators.socket.getaddrinfo", resolves_public)
    monkeypatch.setattr(files.aiohttp, "ClientSession", MagicMock(return_value=mock_session))

    with pytest.raises(BlockedHost):
        await files.download_file("https://example.com/start.pdf")

    mock_session.get.assert_called_once()


@pytest.mark.asyncio
async def test_download_file_strips_credentials_on_cross_origin_redirect(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def resolves_public(host: str, port: int | None, *args: object, **kwargs: object) -> list[object]:
        return [(socket.AF_INET, socket.SOCK_STREAM, 0, "", ("93.184.216.34", port or 0))]

    class Content:
        async def iter_chunked(self, chunk_size: int) -> AsyncIterator[bytes]:
            yield b"file-bytes"

    redirect_response = AsyncMock()
    redirect_response.status = 302
    redirect_response.headers = {"Location": "https://other.example.com/final.pdf"}
    redirect_response.__aenter__ = AsyncMock(return_value=redirect_response)
    redirect_response.__aexit__ = AsyncMock(return_value=None)

    final_response = AsyncMock()
    final_response.status = 200
    final_response.headers = {}
    final_response.content_length = len(b"file-bytes")
    final_response.content = Content()
    final_response.__aenter__ = AsyncMock(return_value=final_response)
    final_response.__aexit__ = AsyncMock(return_value=None)

    responses = [redirect_response, final_response]
    requested_headers: list[dict[str, str]] = []

    def capture_get(*args: object, **kwargs: object) -> AsyncMock:
        headers = kwargs["headers"]
        assert isinstance(headers, dict)
        requested_headers.append(headers)
        return responses.pop(0)

    mock_session = MagicMock()
    mock_session.__aenter__ = AsyncMock(return_value=mock_session)
    mock_session.__aexit__ = AsyncMock(return_value=None)
    mock_session.get = MagicMock(side_effect=capture_get)

    monkeypatch.setattr("skyvern.utils.url_validators.socket.getaddrinfo", resolves_public)
    monkeypatch.setattr(files.aiohttp, "ClientSession", MagicMock(return_value=mock_session))

    result = await files.download_file(
        "https://example.com/start.pdf",
        headers={"Authorization": "Bearer secret", "Cookie": "sid=abc", "X-Keep": "1"},
        output_dir=str(tmp_path),
    )

    assert Path(result).read_bytes() == b"file-bytes"
    assert requested_headers[0]["Authorization"] == "Bearer secret"
    assert requested_headers[0]["Cookie"] == "sid=abc"
    assert requested_headers[1] == {"X-Keep": "1"}


@pytest.mark.asyncio
async def test_download_file_redirect_limit_raises_http_exception(monkeypatch: pytest.MonkeyPatch) -> None:
    def resolves_public(host: str, port: int | None, *args: object, **kwargs: object) -> list[object]:
        return [(socket.AF_INET, socket.SOCK_STREAM, 0, "", ("93.184.216.34", port or 0))]

    redirect_response = AsyncMock()
    redirect_response.status = 302
    redirect_response.headers = {"Location": "https://example.com/next.pdf"}
    redirect_response.__aenter__ = AsyncMock(return_value=redirect_response)
    redirect_response.__aexit__ = AsyncMock(return_value=None)

    mock_session = MagicMock()
    mock_session.__aenter__ = AsyncMock(return_value=mock_session)
    mock_session.__aexit__ = AsyncMock(return_value=None)
    mock_session.get = MagicMock(return_value=redirect_response)

    monkeypatch.setattr("skyvern.utils.url_validators.socket.getaddrinfo", resolves_public)
    monkeypatch.setattr(files.aiohttp, "ClientSession", MagicMock(return_value=mock_session))

    with pytest.raises(SkyvernHTTPException, match="Too many redirects"):
        await files.download_file("https://example.com/start.pdf")

    assert mock_session.get.call_count == MAX_SAFE_REDIRECTS + 1
