"""Cover the OSS ``AgentFunction.upload_file_to_customer_storage`` base path.

The cloud override (NAT proxy routing) is tested in
``tests/cloud/test_nat_egress_proxy_uploads.py``. This file pins down the
direct (SDK) path: S3 via aioboto3 and Azure via the AZURE_CLIENT_FACTORY,
plus the shared 1 GB size cap.
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from skyvern.constants import CUSTOMER_STORAGE_UPLOAD_MAX_BYTES
from skyvern.exceptions import AzureConfigurationError, UploadFileMaxSizeExceeded
from skyvern.forge.agent_functions import AgentFunction
from skyvern.schemas.workflows import FileStorageType, FileUploadDestination


def _s3_destination(bucket: str = "customer-bucket", key: str = "k.bin") -> FileUploadDestination:
    return FileUploadDestination(
        storage_type=FileStorageType.S3,
        customer_uri=f"s3://{bucket}/{key}",
        sdk_uri=f"s3://{bucket}/{key}",
        s3_bucket=bucket,
        s3_key=key,
        aws_access_key_id="AKIA-test",
        aws_secret_access_key="secret-test",
    )


def _azure_destination() -> FileUploadDestination:
    return FileUploadDestination(
        storage_type=FileStorageType.AZURE,
        customer_uri="https://acc.blob.core.windows.net/c/blob",
        sdk_uri="azure://c/blob",
        azure_storage_account_name="acc",
        azure_storage_account_key="key",
        azure_blob_container_name="c",
        azure_blob_name="blob",
    )


@pytest.fixture
def small_file(tmp_path: Path) -> Path:
    fp = tmp_path / "f.bin"
    fp.write_bytes(b"abc")
    return fp


@pytest.mark.asyncio
async def test_s3_direct_path_calls_async_aws_client(small_file: Path) -> None:
    destination = _s3_destination()
    fake_aws = AsyncMock()
    fake_aws.upload_file_from_path = AsyncMock()

    with patch("skyvern.forge.agent_functions.AsyncAWSClient", return_value=fake_aws) as MockClient:
        result = await AgentFunction().upload_file_to_customer_storage(
            file_path=str(small_file),
            destination=destination,
            organization_id="o_1",
        )

    assert result == destination.customer_uri
    MockClient.assert_called_once_with(
        aws_access_key_id="AKIA-test",
        aws_secret_access_key="secret-test",
        region_name=None,
    )
    fake_aws.upload_file_from_path.assert_awaited_once_with(
        uri=destination.sdk_uri,
        file_path=str(small_file),
        raise_exception=True,
    )


@pytest.mark.asyncio
async def test_azure_direct_path_calls_factory(small_file: Path) -> None:
    destination = _azure_destination()
    fake_azure = AsyncMock()
    fake_azure.upload_file_from_path = AsyncMock()

    fake_factory = MagicMock()
    fake_factory.create_storage_client = MagicMock(return_value=fake_azure)

    with patch("skyvern.forge.agent_functions.app") as mock_app:
        mock_app.AZURE_CLIENT_FACTORY = fake_factory

        result = await AgentFunction().upload_file_to_customer_storage(
            file_path=str(small_file),
            destination=destination,
        )

    assert result == destination.customer_uri
    fake_factory.create_storage_client.assert_called_once_with(
        storage_account_name="acc",
        storage_account_key="key",
    )
    fake_azure.upload_file_from_path.assert_awaited_once_with(destination.sdk_uri, str(small_file))


@pytest.mark.asyncio
async def test_azure_missing_creds_raises(small_file: Path) -> None:
    destination = FileUploadDestination(
        storage_type=FileStorageType.AZURE,
        customer_uri="https://acc.blob.core.windows.net/c/blob",
        sdk_uri="azure://c/blob",
        azure_storage_account_name=None,
        azure_storage_account_key=None,
        azure_blob_container_name="c",
        azure_blob_name="blob",
    )
    with pytest.raises(AzureConfigurationError):
        await AgentFunction().upload_file_to_customer_storage(
            file_path=str(small_file),
            destination=destination,
        )


@pytest.mark.asyncio
async def test_size_cap_enforced_on_direct_path(small_file: Path) -> None:
    destination = _s3_destination()

    with patch(
        "skyvern.forge.agent_functions.os.path.getsize",
        return_value=CUSTOMER_STORAGE_UPLOAD_MAX_BYTES + 1,
    ):
        fake_aws = AsyncMock()
        with patch("skyvern.forge.agent_functions.AsyncAWSClient", return_value=fake_aws):
            with pytest.raises(UploadFileMaxSizeExceeded):
                await AgentFunction().upload_file_to_customer_storage(
                    file_path=str(small_file),
                    destination=destination,
                )
        fake_aws.upload_file_from_path.assert_not_awaited()
