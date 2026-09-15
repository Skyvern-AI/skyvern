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
from skyvern.forge.sdk.artifact.storage.local import LocalStorage
from skyvern.utils.url_validators import MAX_SAFE_REDIRECTS
from tests.unit.conftest import LEGACY_DOWNLOAD_ESCAPE_CASES

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


def test_validate_download_url_allows_canonical_legacy_download(legacy_download_uris: dict[str, str]) -> None:
    assert files.validate_download_url(legacy_download_uris["canonical"]) is True


@pytest.mark.parametrize("case", LEGACY_DOWNLOAD_ESCAPE_CASES)
def test_validate_download_url_rejects_legacy_path_escape(legacy_download_uris: dict[str, str], case: str) -> None:
    assert files.validate_download_url(legacy_download_uris[case]) is False


@pytest.mark.asyncio
async def test_download_file_returns_canonical_legacy_download(legacy_download_uris: dict[str, str]) -> None:
    safe_path = files.parse_uri_to_path(legacy_download_uris["canonical"])
    assert await files.download_file(legacy_download_uris["canonical"]) == os.path.realpath(safe_path)


@pytest.mark.asyncio
async def test_local_download_snapshots_remain_readable_and_contained(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    legacy_download_uris: dict[str, str],
    caplog: pytest.LogCaptureFixture,
) -> None:
    assert os.path.realpath(os.path.join(settings.ARTIFACT_STORAGE_PATH, "downloads")) in files._LOCAL_DOWNLOAD_ROOTS
    default_root = tmp_path / "artifacts" / "downloads"
    custom_root = tmp_path / "custom-artifacts" / "downloads"
    malformed_org_ids = ("/", str(tmp_path), "../..", "", ".", "..", "o_test/wr_test", "o_test\\wr_test")
    monkeypatch.setattr(files, "_LOCAL_DOWNLOAD_ROOTS", {str(default_root)})
    monkeypatch.setattr(settings, "ENV", "local")
    LocalStorage(str(custom_root.parent))
    assert (
        files.validate_download_url((custom_root / "local/o_1/wr_1/report.pdf").as_uri(), organization_id="o_1") is True
    )
    assert (
        files.validate_download_url(
            (custom_root.parent / "downloads-evil/local/o_1/wr_1/report.pdf").as_uri(), organization_id="o_1"
        )
        is False
    )

    for root in (default_root, custom_root):
        for attempt_path in ("report.txt", "attempts/2/report.txt"):
            snapshot = root / "local" / "o_test" / "wr_test" / attempt_path
            snapshot.parent.mkdir(parents=True, exist_ok=True)
            snapshot.write_text("preserved download")

            for organization_id in (None, "o_other", "o_tes", *malformed_org_ids):
                caplog.clear()
                assert files.validate_download_url(snapshot.as_uri(), organization_id=organization_id) is False
                assert "Legacy local file path traversal blocked" in caplog.text
                caplog.clear()
                with pytest.raises(PermissionError, match="outside the downloads directory"):
                    await files.download_file(snapshot.as_uri(), organization_id=organization_id)
                assert "Legacy local file path traversal blocked" in caplog.text

            assert files.validate_download_url(snapshot.as_uri(), organization_id="o_test") is True
            assert files._resolve_legacy_download_path(str(snapshot), organization_id="o_test") == str(
                snapshot.resolve()
            )
            resolved = await files.download_file(snapshot.as_uri(), organization_id="o_test")
            assert Path(resolved).read_text() == "preserved download"

        other_org = root / "local" / "o_other"
        other_org.mkdir()
        cross_org_link = other_org / "report.txt"
        cross_org_link.symlink_to(snapshot)
        for rejected_uri in (
            cross_org_link.as_uri(),
            (other_org / ".." / "o_test" / "wr_test" / "report.txt").as_uri(),
            f"{other_org.as_uri()}/%2E%2E/o_test/wr_test/report.txt",
        ):
            assert files.validate_download_url(rejected_uri, organization_id="o_other") is False
            with pytest.raises(PermissionError, match="outside the downloads directory"):
                await files.download_file(rejected_uri, organization_id="o_other")

        sibling = root.parent / "downloads-evil" / "secret.txt"
        sibling.parent.mkdir()
        sibling.write_text("outside snapshot root")
        for organization_id in malformed_org_ids:
            assert files.validate_download_url(sibling.as_uri(), organization_id=organization_id) is False
            with pytest.raises(PermissionError, match="outside the downloads directory"):
                await files.download_file(sibling.as_uri(), organization_id=organization_id)

        scope_symlink = root / "local" / "o_link"
        scope_symlink.symlink_to(sibling.parent, target_is_directory=True)
        for rejected_uri in ((scope_symlink / sibling.name).as_uri(), sibling.as_uri()):
            caplog.clear()
            assert files.validate_download_url(rejected_uri, organization_id="o_link") is False
            assert "Legacy local file path traversal blocked" in caplog.text
            caplog.clear()
            with pytest.raises(PermissionError, match="outside the downloads directory"):
                await files.download_file(rejected_uri, organization_id="o_link")
            assert "Legacy local file path traversal blocked" in caplog.text

        symlink = root / "local" / "o_test" / "escape.txt"
        symlink.symlink_to(sibling)
        for rejected_uri in (sibling.as_uri(), symlink.as_uri(), legacy_download_uris["parent_traversal"]):
            assert files.validate_download_url(rejected_uri, organization_id="o_test") is False
            with pytest.raises(PermissionError, match="outside the downloads directory"):
                await files.download_file(rejected_uri, organization_id="o_test")

    for organization_id in (None, "o_test", "o_other", *malformed_org_ids):
        canonical_uri = legacy_download_uris["canonical"]
        assert files.validate_download_url(canonical_uri, organization_id=organization_id) is True
        assert await files.download_file(canonical_uri, organization_id=organization_id) == os.path.realpath(
            files.parse_uri_to_path(canonical_uri)
        )
    monkeypatch.setattr(settings, "ENV", "prod")
    assert files.validate_download_url(snapshot.as_uri(), organization_id="o_test") is False


@pytest.mark.asyncio
@pytest.mark.parametrize("case", LEGACY_DOWNLOAD_ESCAPE_CASES)
async def test_download_file_rejects_legacy_path_escape(
    legacy_download_uris: dict[str, str], case: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    client_session = MagicMock()
    monkeypatch.setattr(files.aiohttp, "ClientSession", client_session)

    with pytest.raises(PermissionError, match="outside the downloads directory"):
        await files.download_file(legacy_download_uris[case])

    client_session.assert_not_called()


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
