from __future__ import annotations

import os
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from skyvern.config import settings
from skyvern.constants import DOWNLOAD_FILE_PREFIX
from skyvern.forge.sdk.api import files

ATTACKER_ORG_ID = "o_attacker"
VICTIM_ORG_ID = "o_victim"


def _legacy_s3_uri(organization_id: str) -> str:
    return f"s3://{settings.AWS_S3_BUCKET_UPLOADS}/{settings.ENV}/{organization_id}/secret.pdf"


def _downloads_s3_uri(organization_id: str) -> str:
    return (
        f"s3://{settings.AWS_S3_BUCKET_UPLOADS}/"
        f"{DOWNLOAD_FILE_PREFIX}/{settings.ENV}/{organization_id}/wr_123/secret.pdf"
    )


@pytest.fixture(autouse=True)
def storage(monkeypatch: pytest.MonkeyPatch) -> SimpleNamespace:
    def assert_managed_file_access(uri: str, organization_id: str) -> None:
        if organization_id == ATTACKER_ORG_ID and (
            uri == _legacy_s3_uri(ATTACKER_ORG_ID) or uri == _downloads_s3_uri(ATTACKER_ORG_ID)
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
