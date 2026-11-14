"""Moto-backed S3 storage contracts separated from pure storage tests."""

from __future__ import annotations

import io
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from typing import Any, Generator
from unittest.mock import AsyncMock, MagicMock, patch
from urllib.parse import parse_qs, urlparse

import boto3
import pytest
from moto.server import ThreadedMotoServer
from structlog.testing import capture_logs
from types_boto3_s3.client import S3Client

from skyvern.config import settings
from skyvern.forge.sdk.api.files import _determine_download_filename, get_file_name_and_suffix_from_headers
from skyvern.forge.sdk.artifact.models import Artifact, ArtifactType
from skyvern.forge.sdk.artifact.storage.s3 import S3Storage
from skyvern.forge.sdk.schemas.files import UploadedFileSignedUrl
from skyvern.services import uploaded_file_service
from tests.unit._uploaded_files_fake import FakeUploadedFilesRepository
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


_PNG = b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR\x00\x00\x00\x01\x00\x00\x00\x01\x08\x02\x00\x00\x00\x90wS\xde"


@pytest.mark.asyncio
class TestUploadedFileSignedUrl:
    """Moto ignores the ``response-*`` overrides that real S3 applies, so these assert the signed query."""

    async def _mint(
        self, s3_storage: S3Storage, boto3_test_client: S3Client, filename: str, body: bytes | None, **seed: Any
    ) -> UploadedFileSignedUrl | None:
        repo = FakeUploadedFilesRepository()
        file_id = repo.seed("o_doc", filename=filename, **seed)
        if body is not None:
            parsed = urlparse(repo.rows[file_id].storage_uri)
            boto3_test_client.put_object(Bucket=parsed.netloc, Key=parsed.path.lstrip("/"), Body=body)
        fake_app = SimpleNamespace(DATABASE=SimpleNamespace(uploaded_files=repo), STORAGE=s3_storage)
        with patch.object(uploaded_file_service, "app", fake_app):
            return await uploaded_file_service.mint_file_url(file_id=file_id, organization_id="o_doc")

    async def test_a_sniffed_raster_image_renders_inline_for_one_hour(
        self, s3_storage: S3Storage, boto3_test_client: S3Client
    ) -> None:
        signed = await self._mint(s3_storage, boto3_test_client, "shot.bin", _PNG + b"\x00" * 10_000)

        assert signed is not None and signed.content_type == "image/png"
        query = parse_qs(urlparse(signed.signed_url).query)
        assert query["response-content-type"] == ["image/png"]
        assert query["response-content-disposition"] == ["inline"]
        lifetime = (
            int(query["X-Amz-Expires"][0])
            if "X-Amz-Expires" in query
            else int(query["Expires"][0]) - int(datetime.now(timezone.utc).timestamp())
        )
        assert 3590 <= lifetime <= 3600
        assert abs(signed.expires_at - int(datetime.now(timezone.utc).timestamp()) - 3600) <= 10

    @pytest.mark.parametrize(
        ("filename", "body"),
        [
            ("pic.png", b'<svg xmlns="http://www.w3.org/2000/svg"><script>alert(1)</script></svg>'),
            ("page.png", b"<!doctype html><html><script>alert(1)</script></html>"),
            ("notes.pdf", b"%PDF-1.7 body"),
            ("clip.mp4", b"\x00\x00\x00\x18ftypmp42\x00\x00\x00\x00mp42isom" + b"\x00" * 64),
        ],
    )
    async def test_every_other_type_mints_nothing(
        self, s3_storage: S3Storage, boto3_test_client: S3Client, filename: str, body: bytes
    ) -> None:
        assert await self._mint(s3_storage, boto3_test_client, filename, body) is None

    # uploaded_files.expires_at is a naive UTC column, so a row read back from the database carries no tzinfo.
    @pytest.mark.parametrize("naive", [False, True], ids=["aware", "naive"])
    async def test_a_file_expiring_sooner_than_an_hour_caps_the_url_at_its_expiry(
        self, s3_storage: S3Storage, boto3_test_client: S3Client, naive: bool
    ) -> None:
        file_expiry = datetime.now(timezone.utc) + timedelta(seconds=90)
        seeded_expiry = file_expiry.replace(tzinfo=None) if naive else file_expiry

        signed = await self._mint(s3_storage, boto3_test_client, "soon.png", _PNG, expires_at=seeded_expiry)

        assert signed is not None
        query = parse_qs(urlparse(signed.signed_url).query)
        lifetime = (
            int(query["X-Amz-Expires"][0])
            if "X-Amz-Expires" in query
            else int(query["Expires"][0]) - int(datetime.now(timezone.utc).timestamp())
        )
        assert 1 <= lifetime <= 90
        assert signed.expires_at <= int(file_expiry.timestamp())

    async def test_a_zero_byte_upload_mints_nothing_without_an_error_log(
        self, s3_storage: S3Storage, boto3_test_client: S3Client
    ) -> None:
        with capture_logs() as logs:
            signed = await self._mint(s3_storage, boto3_test_client, "empty.png", b"")

        assert signed is None
        assert [log for log in logs if log["log_level"] in ("error", "exception", "critical")] == []

    async def test_a_stream_that_fails_partway_reads_no_head(self, s3_storage: S3Storage) -> None:
        async def failing_chunks(_size: int) -> Any:
            yield b"\x89PNG"
            raise ConnectionError("connection reset")

        stream = SimpleNamespace(iter_chunks=failing_chunks, aclose=AsyncMock())
        uri = f"s3://{settings.AWS_S3_BUCKET_UPLOADS}/{settings.ENV}/o_doc/shot.png"

        with patch.object(s3_storage.async_client, "open_download_stream", AsyncMock(return_value=stream)):
            assert await s3_storage.read_managed_file_head(uri, "o_doc", 64) is None
        stream.aclose.assert_awaited_once()

    async def test_the_type_sniff_fetches_only_the_head_of_the_object(
        self, s3_storage: S3Storage, boto3_test_client: S3Client
    ) -> None:
        opened = s3_storage.async_client.open_download_stream
        fetched: list[int] = []

        async def recording_open(uri: str, **kwargs: Any) -> Any:
            stream = await opened(uri, **kwargs)
            fetched.append(stream.content_length)
            return stream

        with patch.object(s3_storage.async_client, "open_download_stream", recording_open):
            signed = await self._mint(s3_storage, boto3_test_client, "big.png", _PNG + b"\x00" * 200_000)

        assert signed is not None
        assert fetched == [uploaded_file_service._IMAGE_HEAD_BYTES]

    async def test_a_missing_object_or_an_expired_row_mints_nothing(
        self, s3_storage: S3Storage, boto3_test_client: S3Client
    ) -> None:
        assert await self._mint(s3_storage, boto3_test_client, "gone.png", None) is None
        expired = datetime.now(timezone.utc) - timedelta(minutes=1)
        assert await self._mint(s3_storage, boto3_test_client, "old.png", _PNG, expires_at=expired) is None
