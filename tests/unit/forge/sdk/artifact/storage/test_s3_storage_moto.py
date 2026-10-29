"""Moto-backed S3 storage contracts separated from pure storage tests."""

from __future__ import annotations

import io
from datetime import datetime, timezone
from typing import Generator
from unittest.mock import AsyncMock, MagicMock
from urllib.parse import parse_qs, urlparse

import boto3
import pytest
from moto.server import ThreadedMotoServer
from types_boto3_s3.client import S3Client

from skyvern.config import settings
from skyvern.forge.sdk.api.files import _determine_download_filename, get_file_name_and_suffix_from_headers
from skyvern.forge.sdk.artifact.models import Artifact, ArtifactType
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
from tests.unit.forge.sdk.artifact.storage.test_s3_storage import (
    TestS3StorageRecordingContentType as _RecordingContentTypeContracts,
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
    # Loopback, not 0.0.0.0: a macOS system proxy bypasses 127.0.0.1 but would otherwise relay the S3 traffic.
    server = ThreadedMotoServer(ip_address="127.0.0.1", port=0)
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


class TestS3StorageRecordingContentTypeMoto(_RecordingContentTypeContracts):
    __test__ = True


class TestS3StorageHARCompressionMoto(_HARCompressionContracts):
    __test__ = True


class TestS3StorageZIPArchiveRetrieveMoto(_ZIPArchiveContracts):
    __test__ = True

    test_build_uri_step_archive_has_zip_extension = None
    test_build_uri_task_archive_has_zip_extension = None


class TestS3StoragePerRunRecordingClipsMoto(_PerRunRecordingContracts):
    __test__ = True


@pytest.mark.asyncio
class TestS3PresignedDownloadDisposition:
    """User-supplied bytes must download as attachments; artifacts the UI embeds must stay inline.

    Moto ignores the ``response-*`` overrides that real S3 applies, so these assert the signed query.
    """

    async def test_uploaded_file_url_downloads_as_attachment_and_keeps_its_extension(
        self, s3_storage: S3Storage
    ) -> None:
        saved = await s3_storage.save_legacy_file(
            organization_id="o_upload", filename="fil_1_page.pdf", fileObj=io.BytesIO(b"%PDF-1.7 body")
        )
        assert saved is not None
        query = parse_qs(urlparse(saved[0]).query)

        assert query["response-content-type"] == ["application/octet-stream"]
        disposition = query["response-content-disposition"][0]
        assert disposition.startswith('attachment; filename="fil_1_page.pdf"')
        # The agent derives a fetched file's extension from this header before Content-Type.
        served_headers = {"Content-Disposition": disposition, "Content-Type": "application/octet-stream"}
        assert get_file_name_and_suffix_from_headers(served_headers) == ("fil_1_page", ".pdf")

    @pytest.mark.parametrize("stored_name", ["fil_1_page.pdf", "fil_1_data.csv.pdf"])
    async def test_agent_keeps_the_full_uploaded_filename(self, s3_storage: S3Storage, stored_name: str) -> None:
        saved = await s3_storage.save_legacy_file(
            organization_id="o_upload", filename=stored_name, fileObj=io.BytesIO(b"%PDF-1.7 body")
        )
        assert saved is not None
        presigned_url = saved[0]
        query = parse_qs(urlparse(presigned_url).query)
        served_headers = {
            "Content-Disposition": query["response-content-disposition"][0],
            "Content-Type": query["response-content-type"][0],
        }

        assert _determine_download_filename(None, served_headers, presigned_url) == stored_name

    async def test_artifact_url_is_not_forced_to_download(self, s3_storage: S3Storage) -> None:
        urls = await s3_storage.async_client.create_presigned_urls([f"s3://{TEST_BUCKET}/shot.png"])
        assert urls is not None

        query = parse_qs(urlparse(urls[0]).query)

        assert "response-content-disposition" not in query
        assert "response-content-type" not in query

    async def test_browser_session_download_listing_is_forced_to_download(
        self, s3_storage: S3Storage, boto3_test_client: S3Client, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(settings, "AWS_S3_BUCKET_ARTIFACTS", TEST_BUCKET)
        monkeypatch.setattr(settings, "ARTIFACT_CONTENT_HMAC_KEYRING", None)
        key = f"{s3_storage._PATH_VERSION}/{settings.ENV}/o_dl/browser_sessions/pbs_dl/downloads/page.html"
        boto3_test_client.put_object(Bucket=TEST_BUCKET, Key=key, Body=b"<html></html>", ContentType="text/html")

        files = await s3_storage.get_shared_downloaded_files_in_browser_session("o_dl", "pbs_dl")

        query = parse_qs(urlparse(files[0].url).query)
        assert query["response-content-type"] == ["application/octet-stream"]
        assert query["response-content-disposition"][0].startswith('attachment; filename="page.html"')

    async def test_download_artifact_share_link_is_forced_to_download_but_a_screenshot_is_not(
        self, s3_storage: S3Storage
    ) -> None:
        now = datetime.now(timezone.utc)
        artifacts = [
            Artifact(
                artifact_id=f"a_{artifact_type.value}",
                artifact_type=artifact_type,
                uri=f"s3://{TEST_BUCKET}/o_dl/{name}",
                organization_id="o_dl",
                created_at=now,
                modified_at=now,
            )
            for artifact_type, name in (
                (ArtifactType.DOWNLOAD, "page.html"),
                (ArtifactType.SCREENSHOT_FINAL, "shot.png"),
            )
        ]

        urls = await s3_storage.get_share_links(artifacts)

        assert urls is not None
        download_query, screenshot_query = (parse_qs(urlparse(url).query) for url in urls)
        assert download_query["response-content-disposition"][0].startswith('attachment; filename="page.html"')
        assert "response-content-disposition" not in screenshot_query
