"""Moto-backed S3 storage contracts separated from pure storage tests."""

from __future__ import annotations

from typing import Generator
from unittest.mock import AsyncMock, MagicMock

import boto3
import pytest
from moto.server import ThreadedMotoServer
from types_boto3_s3.client import S3Client

from skyvern.config import settings
from skyvern.forge.sdk.artifact.storage.s3 import S3Storage
from tests.unit.forge.sdk.artifact.storage.test_s3_storage import (
    TEST_BUCKET,
    S3StorageForTests,
)
from tests.unit.forge.sdk.artifact.storage.test_s3_storage import (
    TestS3StorageBrowserSessionFiles as _BrowserSessionContracts,
)
from tests.unit.forge.sdk.artifact.storage.test_s3_storage import TestS3StorageContentType as _ContentTypeContracts
from tests.unit.forge.sdk.artifact.storage.test_s3_storage import (
    TestS3StorageHARCompression as _HARCompressionContracts,
)
from tests.unit.forge.sdk.artifact.storage.test_s3_storage import (
    TestS3StoragePerRunRecordingClips as _PerRunRecordingContracts,
)
from tests.unit.forge.sdk.artifact.storage.test_s3_storage import TestS3StorageStore as _StoreContracts
from tests.unit.forge.sdk.artifact.storage.test_s3_storage import (
    TestS3StorageZIPArchiveRetrieve as _ZIPArchiveContracts,
)


@pytest.fixture
def s3_storage(moto_server: str) -> S3Storage:
    return S3StorageForTests(bucket=TEST_BUCKET, endpoint_url=moto_server)


@pytest.fixture(autouse=True)
def aws_credentials(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("AWS_ACCESS_KEY_ID", "testing")
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "testing")


@pytest.fixture(autouse=True)
def mock_browser_session_artifact_create(monkeypatch: pytest.MonkeyPatch) -> None:
    """Stub DB artifact-row inserts while exercising real moto object storage."""
    import skyvern.forge.sdk.artifact.storage.s3 as s3_module

    fake_app = MagicMock()
    fake_app.ARTIFACT_MANAGER.create_browser_session_download_artifact = AsyncMock(return_value="a_test")
    fake_app.ARTIFACT_MANAGER.create_browser_session_recording_artifact = AsyncMock(return_value="a_test")
    monkeypatch.setattr(s3_module, "app", fake_app)


@pytest.fixture(scope="module")
def moto_server() -> Generator[str]:
    server = ThreadedMotoServer(port=0)
    server.start()
    host, port = server.get_host_and_port()
    yield f"http://{host}:{port}"
    server.stop()


@pytest.fixture(scope="module", autouse=True)
def boto3_test_client(moto_server: str) -> Generator[S3Client]:
    client = boto3.client(
        "s3",
        aws_access_key_id="testing",
        aws_secret_access_key="testing",
        region_name=settings.AWS_REGION,
        endpoint_url=moto_server,
    )
    client.create_bucket(Bucket=TEST_BUCKET)
    client.create_bucket(Bucket=settings.AWS_S3_BUCKET_UPLOADS)
    yield client


class TestS3StorageStoreMoto(_StoreContracts):
    __test__ = True


class TestS3StorageBrowserSessionFilesMoto(_BrowserSessionContracts):
    __test__ = True

    # These checks are pure URI/guard behavior and stay in the fast module below.
    test_assert_managed_file_access_accepts_org_scoped_uploads = None
    test_assert_managed_file_access_accepts_artifact_bucket = None
    test_assert_managed_file_access_rejects_other_org = None
    test_assert_managed_file_access_rejects_other_org_artifact_bucket = None
    test_download_managed_file_rejects_other_org = None
    test_storage_type_property = None


class TestS3StorageContentTypeMoto(_ContentTypeContracts):
    __test__ = True


class TestS3StorageHARCompressionMoto(_HARCompressionContracts):
    __test__ = True


class TestS3StorageZIPArchiveRetrieveMoto(_ZIPArchiveContracts):
    __test__ = True

    test_build_uri_step_archive_has_zip_extension = None
    test_build_uri_task_archive_has_zip_extension = None


class TestS3StoragePerRunRecordingClipsMoto(_PerRunRecordingContracts):
    __test__ = True
