from datetime import datetime
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

import skyvern.forge.sdk.api.real_gcp as real_gcp_module
from skyvern.config import settings
from skyvern.forge.sdk.api.gcp import STORAGE_CLASS_STANDARD
from skyvern.forge.sdk.api.real_gcp import RealAsyncGcsStorageClient
from skyvern.forge.sdk.artifact.models import Artifact, ArtifactType
from skyvern.forge.sdk.artifact.storage.gcs import GcsStorage

TEST_BUCKET = "test-gcs-bucket"
TEST_ORGANIZATION_ID = "test-org-123"
TEST_BROWSER_SESSION_ID = "bs_test_123"


def make_artifact(uri: str, artifact_id: str = "a_1") -> Artifact:
    return Artifact(
        artifact_id=artifact_id,
        artifact_type=ArtifactType.SCREENSHOT,
        uri=uri,
        organization_id=TEST_ORGANIZATION_ID,
        created_at=datetime.utcnow(),
        modified_at=datetime.utcnow(),
    )


class GcsStorageForTests(GcsStorage):
    """Test subclass that injects a mock client and bypasses real client init."""

    async_client: Any  # Allow mock attribute access

    def __init__(self, bucket: str) -> None:
        # Don't call super().__init__ to avoid creating a real RealAsyncGcsStorageClient
        self.bucket = bucket
        self.async_client = AsyncMock()


@pytest.fixture
def gcs_storage() -> GcsStorageForTests:
    return GcsStorageForTests(bucket=TEST_BUCKET)


@pytest.fixture(autouse=True)
def mock_browser_session_artifact_create(monkeypatch: pytest.MonkeyPatch) -> None:
    """Stub out the DB-side artifact-row inserts for browser-session files.

    Mirrors the azure/s3 storage test fixtures — the forge app isn't initialized
    in these storage-only tests, so patch the module-level ``app`` reference.
    """
    import skyvern.forge.sdk.artifact.storage.gcs as gcs_module

    fake_app = MagicMock()
    fake_app.ARTIFACT_MANAGER.create_browser_session_download_artifact = AsyncMock(return_value="a_test")
    fake_app.ARTIFACT_MANAGER.create_browser_session_recording_artifact = AsyncMock(return_value="a_test")
    monkeypatch.setattr(gcs_module, "app", fake_app)


@pytest.mark.asyncio
class TestGcsStorageArtifacts:
    """Round-trip and URI construction for artifacts."""

    async def test_store_artifact(self, gcs_storage: GcsStorageForTests) -> None:
        artifact = make_artifact(f"gs://{TEST_BUCKET}/v1/{settings.ENV}/{TEST_ORGANIZATION_ID}/a.png")
        await gcs_storage.store_artifact(artifact, b"payload")
        gcs_storage.async_client.upload_file.assert_called_once_with(
            artifact.uri, b"payload", storage_class=STORAGE_CLASS_STANDARD, tags={}
        )

    async def test_retrieve_artifact(self, gcs_storage: GcsStorageForTests) -> None:
        gcs_storage.async_client.download_file.return_value = b"payload"
        artifact = make_artifact(f"gs://{TEST_BUCKET}/o.png")
        assert await gcs_storage.retrieve_artifact(artifact) == b"payload"

    async def test_get_share_link(self, gcs_storage: GcsStorageForTests) -> None:
        gcs_storage.async_client.create_signed_urls.return_value = ["https://signed-url?X-Goog-Signature=abc"]
        artifact = make_artifact(f"gs://{TEST_BUCKET}/o.png")
        link = await gcs_storage.get_share_link(artifact)
        assert link == "https://signed-url?X-Goog-Signature=abc"


@pytest.mark.asyncio
class TestGcsStorageBrowserSessionFiles:
    """Browser session file methods."""

    async def test_sync_browser_session_file_with_date(self, gcs_storage: GcsStorageForTests, tmp_path: Path) -> None:
        test_file = tmp_path / "recording.webm"
        test_file.write_bytes(b"fake video data")

        uri = await gcs_storage.sync_browser_session_file(
            organization_id=TEST_ORGANIZATION_ID,
            browser_session_id=TEST_BROWSER_SESSION_ID,
            artifact_type="videos",
            local_file_path=str(test_file),
            remote_path="recording.webm",
            date="2025-01-15",
        )

        expected_uri = f"gs://{TEST_BUCKET}/v1/{settings.ENV}/{TEST_ORGANIZATION_ID}/browser_sessions/{TEST_BROWSER_SESSION_ID}/videos/2025-01-15/recording.webm"
        assert uri == expected_uri
        gcs_storage.async_client.upload_file_from_path.assert_called_once()

    async def test_sync_browser_session_file_without_date(
        self, gcs_storage: GcsStorageForTests, tmp_path: Path
    ) -> None:
        test_file = tmp_path / "document.pdf"
        test_file.write_bytes(b"fake download data")

        uri = await gcs_storage.sync_browser_session_file(
            organization_id=TEST_ORGANIZATION_ID,
            browser_session_id=TEST_BROWSER_SESSION_ID,
            artifact_type="downloads",
            local_file_path=str(test_file),
            remote_path="document.pdf",
            date=None,
        )

        expected_uri = f"gs://{TEST_BUCKET}/v1/{settings.ENV}/{TEST_ORGANIZATION_ID}/browser_sessions/{TEST_BROWSER_SESSION_ID}/downloads/document.pdf"
        assert uri == expected_uri

    async def test_browser_session_file_exists_returns_true(self, gcs_storage: GcsStorageForTests) -> None:
        gcs_storage.async_client.get_object_info.return_value = {"LastModified": "2025-01-15"}

        exists = await gcs_storage.browser_session_file_exists(
            organization_id=TEST_ORGANIZATION_ID,
            browser_session_id=TEST_BROWSER_SESSION_ID,
            artifact_type="videos",
            remote_path="exists.webm",
            date="2025-01-15",
        )

        assert exists is True

    async def test_browser_session_file_exists_returns_false_on_exception(
        self, gcs_storage: GcsStorageForTests
    ) -> None:
        gcs_storage.async_client.get_object_info.side_effect = Exception("Not found")

        exists = await gcs_storage.browser_session_file_exists(
            organization_id=TEST_ORGANIZATION_ID,
            browser_session_id=TEST_BROWSER_SESSION_ID,
            artifact_type="videos",
            remote_path="nonexistent.webm",
            date="2025-01-15",
        )

        assert exists is False

    async def test_delete_browser_session_file(self, gcs_storage: GcsStorageForTests) -> None:
        await gcs_storage.delete_browser_session_file(
            organization_id=TEST_ORGANIZATION_ID,
            browser_session_id=TEST_BROWSER_SESSION_ID,
            artifact_type="videos",
            remote_path="to_delete.webm",
            date="2025-01-15",
        )

        expected_uri = f"gs://{TEST_BUCKET}/v1/{settings.ENV}/{TEST_ORGANIZATION_ID}/browser_sessions/{TEST_BROWSER_SESSION_ID}/videos/2025-01-15/to_delete.webm"
        gcs_storage.async_client.delete_file.assert_called_once_with(expected_uri)

    async def test_delete_browser_session_nonexistent_does_not_raise(self, gcs_storage: GcsStorageForTests) -> None:
        # Mirror the client contract: delete of a missing object is a no-op.
        gcs_storage.async_client.delete_file.return_value = None
        await gcs_storage.delete_browser_session(TEST_ORGANIZATION_ID, "wpid_missing")
        gcs_storage.async_client.delete_file.assert_called_once()

    async def test_file_exists_returns_true(self, gcs_storage: GcsStorageForTests) -> None:
        gcs_storage.async_client.get_object_info.return_value = {"LastModified": "2025-01-15"}
        uri = f"gs://{TEST_BUCKET}/test/file.txt"

        assert await gcs_storage.file_exists(uri) is True

    async def test_file_exists_returns_false_on_exception(self, gcs_storage: GcsStorageForTests) -> None:
        gcs_storage.async_client.get_object_info.side_effect = Exception("Not found")
        uri = f"gs://{TEST_BUCKET}/nonexistent/file.txt"

        assert await gcs_storage.file_exists(uri) is False

    async def test_assert_managed_file_access_accepts_org_scoped_uploads(self, gcs_storage: GcsStorageForTests) -> None:
        legacy_uri = f"gs://{settings.GCS_BUCKET_UPLOADS}/{settings.ENV}/{TEST_ORGANIZATION_ID}/file.pdf"
        downloads_uri = (
            f"gs://{settings.GCS_BUCKET_UPLOADS}/downloads/{settings.ENV}/{TEST_ORGANIZATION_ID}/wr_123/file.pdf"
        )

        gcs_storage.assert_managed_file_access(legacy_uri, TEST_ORGANIZATION_ID)
        gcs_storage.assert_managed_file_access(downloads_uri, TEST_ORGANIZATION_ID)

    async def test_assert_managed_file_access_accepts_artifact_bucket(self, gcs_storage: GcsStorageForTests) -> None:
        artifact_uri = (
            f"gs://{settings.GCS_BUCKET_ARTIFACTS}/v1/{settings.ENV}/{TEST_ORGANIZATION_ID}/"
            "workflow_runs/wr_123/wrb_456/2026-03-23T17:57:58.370827_a_789_pdf.pdf"
        )
        gcs_storage.assert_managed_file_access(artifact_uri, TEST_ORGANIZATION_ID)

    async def test_assert_managed_file_access_rejects_other_org(self, gcs_storage: GcsStorageForTests) -> None:
        uri = f"gs://{settings.GCS_BUCKET_UPLOADS}/{settings.ENV}/o_other/file.pdf"
        with pytest.raises(PermissionError, match="No permission to access storage URI"):
            gcs_storage.assert_managed_file_access(uri, TEST_ORGANIZATION_ID)

    async def test_assert_managed_file_access_rejects_other_org_artifact_bucket(
        self, gcs_storage: GcsStorageForTests
    ) -> None:
        uri = (
            f"gs://{settings.GCS_BUCKET_ARTIFACTS}/v1/{settings.ENV}/o_other/workflow_runs/wr_123/wrb_456/artifact.pdf"
        )
        with pytest.raises(PermissionError, match="No permission to access storage URI"):
            gcs_storage.assert_managed_file_access(uri, TEST_ORGANIZATION_ID)

    async def test_download_managed_file(self, gcs_storage: GcsStorageForTests) -> None:
        test_data = b"uploaded file content"
        gcs_storage.async_client.download_file.return_value = test_data
        uri = f"gs://{settings.GCS_BUCKET_UPLOADS}/{settings.ENV}/{TEST_ORGANIZATION_ID}/file.pdf"

        downloaded = await gcs_storage.download_managed_file(uri, TEST_ORGANIZATION_ID)

        assert downloaded == test_data
        gcs_storage.async_client.download_file.assert_called_once_with(uri, log_exception=False)

    async def test_download_managed_file_returns_none(self, gcs_storage: GcsStorageForTests) -> None:
        gcs_storage.async_client.download_file.return_value = None
        uri = f"gs://{settings.GCS_BUCKET_UPLOADS}/{settings.ENV}/{TEST_ORGANIZATION_ID}/nonexistent/file.txt"

        assert await gcs_storage.download_managed_file(uri, TEST_ORGANIZATION_ID) is None

    async def test_download_managed_file_rejects_other_org(self, gcs_storage: GcsStorageForTests) -> None:
        uri = f"gs://{settings.GCS_BUCKET_UPLOADS}/{settings.ENV}/o_other/file.pdf"
        with pytest.raises(PermissionError, match="No permission to access storage URI"):
            await gcs_storage.download_managed_file(uri, TEST_ORGANIZATION_ID)

    async def test_storage_type_property(self, gcs_storage: GcsStorageForTests) -> None:
        assert gcs_storage.storage_type == "gcs"


class TestGcsStorageBuildUri:
    """GCS URI building methods (sync)."""

    def test_build_base_uri(self, gcs_storage: GcsStorageForTests) -> None:
        base = gcs_storage._build_base_uri(TEST_ORGANIZATION_ID)
        assert base == f"gs://{TEST_BUCKET}/v1/{settings.ENV}/{TEST_ORGANIZATION_ID}"

    def test_build_browser_session_uri_with_date(self, gcs_storage: GcsStorageForTests) -> None:
        uri = gcs_storage._build_browser_session_uri(
            organization_id=TEST_ORGANIZATION_ID,
            browser_session_id=TEST_BROWSER_SESSION_ID,
            artifact_type="videos",
            remote_path="file.webm",
            date="2025-01-15",
        )
        expected = f"gs://{TEST_BUCKET}/v1/{settings.ENV}/{TEST_ORGANIZATION_ID}/browser_sessions/{TEST_BROWSER_SESSION_ID}/videos/2025-01-15/file.webm"
        assert uri == expected

    def test_build_browser_session_uri_without_date(self, gcs_storage: GcsStorageForTests) -> None:
        uri = gcs_storage._build_browser_session_uri(
            organization_id=TEST_ORGANIZATION_ID,
            browser_session_id=TEST_BROWSER_SESSION_ID,
            artifact_type="downloads",
            remote_path="file.pdf",
            date=None,
        )
        expected = f"gs://{TEST_BUCKET}/v1/{settings.ENV}/{TEST_ORGANIZATION_ID}/browser_sessions/{TEST_BROWSER_SESSION_ID}/downloads/file.pdf"
        assert uri == expected


GCS_CONTENT_TYPE_TEST_CASES = [
    ("video.webm", "video/webm"),
    ("data.json", "application/json"),
    ("network.har", "application/json"),
    ("screenshot.png", "image/png"),
    ("output.txt", "text/plain"),
    ("debug.log", "text/plain"),
]


@pytest.mark.asyncio
class TestGcsStorageClientContentType:
    """RealAsyncGcsStorageClient sets the correct content type by extension."""

    @pytest.mark.parametrize("filename,expected_content_type", GCS_CONTENT_TYPE_TEST_CASES)
    async def test_content_type_guessing(self, tmp_path: Path, filename: str, expected_content_type: str) -> None:
        test_file = tmp_path / filename
        test_file.write_bytes(b"test content")

        with patch.object(RealAsyncGcsStorageClient, "_get_client") as mock_get_client:
            mock_blob = MagicMock()
            mock_bucket = MagicMock()
            mock_bucket.blob.return_value = mock_blob
            mock_client = MagicMock()
            mock_client.bucket.return_value = mock_bucket
            mock_get_client.return_value = mock_client

            client = RealAsyncGcsStorageClient(project_id="test")
            await client.upload_file_from_path(uri=f"gs://test-bucket/path/{filename}", file_path=str(test_file))

            call_kwargs = mock_blob.upload_from_filename.call_args.kwargs
            assert call_kwargs["content_type"] == expected_content_type


class TestGcsStorageClientSigning:
    """V4 signed-URL generation and Workload-Identity signBlob routing."""

    def test_create_signed_url_uses_v4_local(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(settings, "GCS_SIGNER_SA_EMAIL", None)
        with patch.object(RealAsyncGcsStorageClient, "_get_blob") as mock_get_blob:
            mock_blob = MagicMock()
            mock_blob.generate_signed_url.return_value = "https://signed?X-Goog-Signature=abc&X-Goog-Algorithm=GOOG4"
            mock_get_blob.return_value = mock_blob

            client = RealAsyncGcsStorageClient(project_id="test")
            url = client._create_signed_url("gs://b/o.txt", expiry_hours=1)

            assert url is not None and "X-Goog-Signature" in url
            kwargs = mock_blob.generate_signed_url.call_args.kwargs
            assert kwargs["version"] == "v4"
            assert kwargs["method"] == "GET"
            # Local key signing — no IAM signBlob params.
            assert "service_account_email" not in kwargs
            assert "access_token" not in kwargs

    def test_signing_under_workload_identity(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(settings, "GCS_SIGNER_SA_EMAIL", "signer@proj.iam.gserviceaccount.com")
        fake_creds = MagicMock()
        fake_creds.token = "tok123"
        monkeypatch.setattr(real_gcp_module, "google_auth_default", lambda scopes=None: (fake_creds, "proj"))

        with patch.object(RealAsyncGcsStorageClient, "_get_blob") as mock_get_blob:
            mock_blob = MagicMock()
            mock_blob.generate_signed_url.return_value = "https://signed"
            mock_get_blob.return_value = mock_blob

            client = RealAsyncGcsStorageClient(project_id="test")
            client._create_signed_url("gs://b/o.txt")

            kwargs = mock_blob.generate_signed_url.call_args.kwargs
            assert kwargs["service_account_email"] == "signer@proj.iam.gserviceaccount.com"
            assert kwargs["access_token"] == "tok123"
            fake_creds.refresh.assert_called_once()

    @pytest.mark.asyncio
    async def test_batch_signing_refreshes_token_once(self, monkeypatch: pytest.MonkeyPatch) -> None:
        # Under Workload Identity, signing a batch must mint the IAM token once,
        # not once per URL.
        monkeypatch.setattr(settings, "GCS_SIGNER_SA_EMAIL", "signer@proj.iam.gserviceaccount.com")
        fake_creds = MagicMock()
        fake_creds.token = "tok123"
        monkeypatch.setattr(real_gcp_module, "google_auth_default", lambda scopes=None: (fake_creds, "proj"))

        with patch.object(RealAsyncGcsStorageClient, "_get_blob") as mock_get_blob:
            mock_blob = MagicMock()
            mock_blob.generate_signed_url.return_value = "https://signed"
            mock_get_blob.return_value = mock_blob

            client = RealAsyncGcsStorageClient(project_id="test")
            urls = await client.create_signed_urls(["gs://b/a.txt", "gs://b/b.txt", "gs://b/c.txt"])

            assert urls is not None and len(urls) == 3
            fake_creds.refresh.assert_called_once()
            assert mock_blob.generate_signed_url.call_count == 3
