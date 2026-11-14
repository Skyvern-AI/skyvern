import asyncio
import hashlib
import os
import threading
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import google.api_core.exceptions as gcs_exceptions
import pytest

import skyvern.forge.sdk.api.real_gcp as real_gcp_module
import skyvern.forge.sdk.artifact.storage.base as base_module
import skyvern.forge.sdk.artifact.storage.gcs as gcs_module
from skyvern.config import settings
from skyvern.exceptions import DownloadSaveIncompleteError
from skyvern.forge.sdk.api.gcp import STORAGE_CLASS_STANDARD
from skyvern.forge.sdk.api.real_gcp import RealAsyncGcsStorageClient
from skyvern.forge.sdk.artifact.models import Artifact, ArtifactType
from skyvern.forge.sdk.artifact.storage.base import SENSITIVE_SHARE_URL_EXPIRY_HOURS
from skyvern.forge.sdk.artifact.storage.gcs import GcsStorage
from skyvern.forge.sdk.artifact.storage.recording_test_helpers import fake_prepared_recording
from skyvern.forge.sdk.workflow.context_manager import WorkflowContextManager
from skyvern.forge.sdk.workflow.service import WorkflowService
from tests.unit.conftest import FakeWorkflowRunAttemptsRepository
from tests.unit.helpers import PINNED_READ_CASES, make_pdf, unique_stored_pdf_ids

TEST_BUCKET = "test-gcs-bucket"
TEST_ORGANIZATION_ID = "test-org-123"
TEST_BROWSER_SESSION_ID = "bs_test_123"


def make_artifact(
    uri: str, artifact_id: str = "a_1", artifact_type: ArtifactType = ArtifactType.SCREENSHOT
) -> Artifact:
    return Artifact(
        artifact_id=artifact_id,
        artifact_type=artifact_type,
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

    fake_app = MagicMock()
    fake_app.ARTIFACT_MANAGER.create_browser_session_download_artifact = AsyncMock(return_value="a_test")
    fake_app.ARTIFACT_MANAGER.create_browser_session_recording_artifact = AsyncMock(return_value="a_test")
    fake_app.ARTIFACT_MANAGER.create_download_artifact = AsyncMock(return_value="a_test")
    fake_app.WORKFLOW_CONTEXT_MANAGER = WorkflowContextManager()
    fake_app.DATABASE.workflow_run_attempts = FakeWorkflowRunAttemptsRepository()
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
        prepared_file = tmp_path / "recording.mp4"
        prepared_file.write_bytes(b"fake mp4 data")

        with patch(
            "skyvern.forge.sdk.artifact.storage.gcs.prepare_recording_for_upload",
            lambda path: fake_prepared_recording(path, str(prepared_file)),
        ):
            with patch("skyvern.forge.sdk.artifact.storage.gcs.sync_run_recording_clips", new=AsyncMock()):
                uri = await gcs_storage.sync_browser_session_file(
                    organization_id=TEST_ORGANIZATION_ID,
                    browser_session_id=TEST_BROWSER_SESSION_ID,
                    artifact_type="videos",
                    local_file_path=str(test_file),
                    remote_path="recording.webm",
                    date="2025-01-15",
                )

        expected_uri = f"gs://{TEST_BUCKET}/v1/{settings.ENV}/{TEST_ORGANIZATION_ID}/browser_sessions/{TEST_BROWSER_SESSION_ID}/videos/2025-01-15/recording.mp4"
        assert uri == expected_uri
        gcs_storage.async_client.upload_file_from_path.assert_called_once_with(
            expected_uri, str(prepared_file), storage_class=STORAGE_CLASS_STANDARD, tags={}
        )

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
        with pytest.raises(PermissionError, match="No permission to access storage URI"):
            await gcs_storage.read_managed_file_head(uri, TEST_ORGANIZATION_ID, 64)
        with pytest.raises(PermissionError, match="No permission to access storage URI"):
            await gcs_storage.presign_managed_file(
                uri, TEST_ORGANIZATION_ID, expiry_seconds=60, inline_content_type="image/png"
            )

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


@pytest.mark.asyncio
async def test_get_downloaded_files_legacy_listing_attributes_and_scopes_attempt_files(
    gcs_storage: GcsStorageForTests, monkeypatch: pytest.MonkeyPatch
) -> None:
    run_id = "wr_retry"
    attempt_one_modified_at = datetime(2026, 9, 7, 11, 0, tzinfo=UTC)
    attempt_two_started_at = datetime(2026, 9, 7, 12, 0, tzinfo=UTC)
    blob_modified_at = datetime(2026, 9, 7, 12, 1, tzinfo=UTC)
    old_key = f"downloads/local/{settings.ENV}/{TEST_ORGANIZATION_ID}/{run_id}/old.pdf"
    current_key = f"downloads/local/{settings.ENV}/{TEST_ORGANIZATION_ID}/{run_id}/current.pdf"
    old_uri = f"gs://{settings.GCS_BUCKET_UPLOADS}/{old_key}"
    old_artifact = Artifact(
        artifact_id="a_old",
        artifact_type=ArtifactType.DOWNLOAD,
        uri=old_uri,
        organization_id=TEST_ORGANIZATION_ID,
        run_id=run_id,
        workflow_run_id=run_id,
        created_at=attempt_one_modified_at,
        modified_at=attempt_one_modified_at,
    )

    async def get_object_info(uri: str) -> dict[str, Any]:
        return {
            "Metadata": {"sha256_checksum": f"sha-{uri.rsplit('/', 1)[-1]}"},
            "ContentLength": 10,
            "LastModified": blob_modified_at,
        }

    gcs_storage.async_client.list_files = AsyncMock(return_value=[old_key, current_key])
    gcs_storage.async_client.get_object_info = AsyncMock(side_effect=get_object_info)
    gcs_storage.async_client.create_signed_urls = AsyncMock(
        side_effect=lambda uris, **_: [f"https://signed.test/{uri.rsplit('/', 1)[-1]}" for uri in uris]
    )
    rows = AsyncMock(return_value=[old_artifact])
    monkeypatch.setattr(
        "skyvern.forge.sdk.artifact.storage.gcs.app.DATABASE.artifacts.list_artifacts_for_run_by_type", rows
    )

    with patch.object(settings, "ARTIFACT_CONTENT_HMAC_KEYRING", None):
        files = await gcs_storage.get_downloaded_files(TEST_ORGANIZATION_ID, run_id)

    assert len(files) == 2
    old_file, current_file = files
    assert old_file.artifact_id == "a_old"
    assert old_file.modified_at == attempt_one_modified_at
    assert current_file.artifact_id is None
    assert current_file.modified_at == blob_modified_at
    rows.assert_awaited_once_with(
        run_id=run_id,
        organization_id=TEST_ORGANIZATION_ID,
        artifact_type=ArtifactType.DOWNLOAD,
    )

    scoped = WorkflowService()._filter_downloaded_files_to_attempt(
        files,
        attempt_rows=[SimpleNamespace(attempt_number=2, started_at=attempt_two_started_at)],
        attempt_number=2,
        artifact_ids=set(),
    )
    assert [file_info.filename for file_info in scoped] == ["current.pdf"]


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

    @pytest.mark.asyncio
    async def test_download_urls_force_an_octet_stream_attachment(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(settings, "GCS_SIGNER_SA_EMAIL", None)
        with patch.object(RealAsyncGcsStorageClient, "_get_blob") as mock_get_blob:
            mock_blob = MagicMock()
            mock_blob.generate_signed_url.return_value = "https://signed"
            mock_get_blob.return_value = mock_blob
            client = RealAsyncGcsStorageClient(project_id="test")

            await client.create_signed_urls(
                ["gs://b/dir/page.html", "gs://b/dir/x.svg"], attachment_filenames=[None, "logo.svg"]
            )
            forced = [call.kwargs for call in mock_blob.generate_signed_url.call_args_list]
            mock_blob.generate_signed_url.reset_mock()
            await client.create_signed_urls(["gs://b/dir/shot.png"])
            inline = mock_blob.generate_signed_url.call_args.kwargs

        assert [kwargs["response_disposition"] for kwargs in forced] == [
            "attachment; filename=\"page.html\"; filename*=UTF-8''page.html",
            "attachment; filename=\"logo.svg\"; filename*=UTF-8''logo.svg",
        ]
        assert {kwargs["response_type"] for kwargs in forced} == {"application/octet-stream"}
        assert "response_disposition" not in inline and "response_type" not in inline


@pytest.mark.asyncio
class TestGcsShareLinkSensitiveCap:
    """Sensitive artifact types get hour-capped signed URLs (SKY-12527)."""

    async def test_share_links_route_sensitive_types_to_capped_expiry(self, gcs_storage: GcsStorageForTests) -> None:
        screenshot = make_artifact(f"gs://{TEST_BUCKET}/shot.png", artifact_id="a_shot")
        download = make_artifact(
            f"gs://{TEST_BUCKET}/file.pdf", artifact_id="a_file", artifact_type=ArtifactType.DOWNLOAD
        )

        async def fake_sign(
            uris: list[str], expiry_hours: int = 24, attachment_filenames: list[str | None] | None = None
        ) -> list[str]:
            suffix = "&attachment" if attachment_filenames is not None else ""
            return [f"{uri}?h={expiry_hours}{suffix}" for uri in uris]

        gcs_storage.async_client.create_signed_urls = AsyncMock(side_effect=fake_sign)
        urls = await gcs_storage.get_share_links([screenshot, download])
        assert urls == [
            f"{screenshot.uri}?h={SENSITIVE_SHARE_URL_EXPIRY_HOURS}",
            f"{download.uri}?h=24&attachment",
        ]


@pytest.mark.asyncio
async def test_save_downloaded_files_partial_upload_failure_raises_after_saving_the_rest(
    gcs_storage: GcsStorageForTests, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    run_dir = tmp_path / "downloads" / "wr_partial"
    run_dir.mkdir(parents=True)
    (run_dir / "a.pdf").write_bytes(b"first")
    (run_dir / "b.pdf").write_bytes(b"second")
    monkeypatch.setattr("skyvern.forge.sdk.api.files.settings.DOWNLOAD_PATH", str(tmp_path / "downloads"))
    monkeypatch.setattr(gcs_storage, "_get_storage_class_for_org", AsyncMock(return_value="STANDARD"))
    monkeypatch.setattr(gcs_storage, "_get_tags_for_org", AsyncMock(return_value=None))

    uploaded: list[str] = []

    async def _upload(*, uri: str, file_path: str, **kwargs: object) -> None:
        if uri.endswith("/a.pdf"):
            raise RuntimeError("transient 503")
        uploaded.append(uri)

    monkeypatch.setattr(gcs_storage.async_client, "upload_file_from_path", _upload)

    monkeypatch.setattr(base_module.app.DATABASE.workflow_runs, "get_workflow_run", AsyncMock(return_value=None))

    with pytest.raises(DownloadSaveIncompleteError) as raised:
        await gcs_storage.save_downloaded_files(organization_id=TEST_ORGANIZATION_ID, run_id="wr_partial")

    assert raised.value.skipped_files == ["a.pdf"]
    assert [uri.rsplit("/", 1)[-1] for uri in uploaded] == ["b.pdf"]


@pytest.mark.asyncio
async def test_save_downloaded_files_retry_skips_stale_files_and_versions_fresh_redownloads(
    gcs_storage: GcsStorageForTests, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    run_id = "wr_retry"
    run_dir = tmp_path / "downloads" / run_id
    run_dir.mkdir(parents=True)
    stale_file = run_dir / "only-attempt-1.pdf"
    fresh_file = run_dir / "report.pdf"
    stale_file.write_bytes(b"same bytes")
    fresh_file.write_bytes(b"same bytes")
    monkeypatch.setattr("skyvern.forge.sdk.api.files.settings.DOWNLOAD_PATH", str(tmp_path / "downloads"))

    attempt_started_at = datetime.now(UTC) - timedelta(seconds=1)
    os.utime(stale_file, (attempt_started_at.timestamp() - 60, attempt_started_at.timestamp() - 60))
    os.utime(fresh_file, (attempt_started_at.timestamp() + 1, attempt_started_at.timestamp() + 1))
    checksum = hashlib.sha256(b"same bytes").hexdigest()
    base_uri = f"gs://{settings.GCS_BUCKET_UPLOADS}/downloads/{settings.ENV}/{TEST_ORGANIZATION_ID}/{run_id}"
    rows = [
        make_artifact(
            f"{base_uri}/{stale_file.name}", artifact_id="a_stale", artifact_type=ArtifactType.DOWNLOAD
        ).model_copy(update={"checksum": checksum, "modified_at": attempt_started_at - timedelta(seconds=60)}),
        make_artifact(
            f"{base_uri}/{fresh_file.name}", artifact_id="a_fresh", artifact_type=ArtifactType.DOWNLOAD
        ).model_copy(update={"checksum": checksum, "modified_at": attempt_started_at - timedelta(seconds=60)}),
    ]

    async def _list_rows(**kwargs: object) -> list[Artifact]:
        return rows

    async def _save_row(*, uri: str, **kwargs: object) -> str:
        for row_index, row in enumerate(rows):
            if row.uri == uri:
                rows[row_index] = row.model_copy(update={"modified_at": datetime.now(UTC)})
                return row.artifact_id
        rows.append(
            make_artifact(uri, artifact_id="a_retry", artifact_type=ArtifactType.DOWNLOAD).model_copy(
                update={"modified_at": datetime.now(UTC)}
            )
        )
        return rows[-1].artifact_id

    attempts_repository = FakeWorkflowRunAttemptsRepository(
        [SimpleNamespace(attempt_number=2, started_at=attempt_started_at)]
    )
    monkeypatch.setattr(gcs_storage.async_client, "upload_file_from_path", AsyncMock())
    monkeypatch.setattr(gcs_storage, "_get_storage_class_for_org", AsyncMock(return_value="STANDARD"))
    monkeypatch.setattr(gcs_storage, "_get_tags_for_org", AsyncMock(return_value=None))
    monkeypatch.setattr(
        gcs_module,
        "app",
        fake_app := SimpleNamespace(
            ARTIFACT_MANAGER=SimpleNamespace(create_download_artifact=AsyncMock(side_effect=_save_row)),
            WORKFLOW_CONTEXT_MANAGER=WorkflowContextManager(),
            DATABASE=SimpleNamespace(
                artifacts=SimpleNamespace(list_artifacts_for_run_by_type=_list_rows),
                workflow_run_attempts=attempts_repository,
                workflow_runs=SimpleNamespace(get_workflow_run=AsyncMock(return_value=None)),
            ),
        ),
    )
    monkeypatch.setattr(base_module, "app", fake_app)

    await gcs_storage.save_downloaded_files(organization_id=TEST_ORGANIZATION_ID, run_id=run_id)

    assert attempts_repository.requested_workflow_run_ids == [run_id]
    assert gcs_storage.async_client.upload_file_from_path.await_args.kwargs["uri"] == (
        f"{base_uri}/attempts/2/{fresh_file.name}"
    )
    assert len(rows) == 3
    assert all(row.modified_at < attempt_started_at for row in rows[:2])
    registered = [
        call.kwargs["filename"] for call in gcs_module.app.ARTIFACT_MANAGER.create_download_artifact.await_args_list
    ]
    assert registered == [fresh_file.name]
    attempt_two_listing = [row.uri.rsplit("/", 1)[-1] for row in rows if row.modified_at >= attempt_started_at]
    assert attempt_two_listing == [fresh_file.name]


class _GcsObjects:
    """google-cloud-storage fake: object generations, and ranged downloads that fail once the generation changes."""

    def __init__(self, contents: list[bytes]) -> None:
        self.objects = {f"b/o_1/a_{i}.pdf": (data, 100 + i) for i, data in enumerate(contents)}
        self.replaced_after_stat: set[str] = set()
        self.request_policies: list[dict[str, Any]] = []

    def bucket(self, name: str) -> SimpleNamespace:
        return SimpleNamespace(
            get_blob=lambda path, **policy: self._stat(f"{name}/{path}", policy),
            blob=lambda path: self._blob(f"{name}/{path}"),
        )

    def _stat(self, key: str, policy: dict[str, Any]) -> SimpleNamespace | None:
        self.request_policies.append(policy)
        if key not in self.objects:
            return None
        data, generation = self.objects[key]
        if key in self.replaced_after_stat:
            self.objects[key] = (data, generation + 1)
        return SimpleNamespace(size=len(data), generation=generation, metadata={}, updated=None)

    def _blob(self, key: str) -> SimpleNamespace:
        def download_as_bytes(
            *, start: int, end: int, if_generation_match: int, raw_download: bool, **policy: Any
        ) -> bytes:
            self.request_policies.append(policy)
            data, generation = self.objects[key]
            if if_generation_match != generation or not raw_download:
                raise gcs_exceptions.PreconditionFailed("generation changed")
            return data[start : end + 1]

        return SimpleNamespace(download_as_bytes=download_as_bytes)


@pytest.mark.parametrize("case, expected", PINNED_READ_CASES)
@pytest.mark.asyncio
async def test_gcs_downloads_list_each_pdf_content_once_only_from_generation_pinned_reads(
    monkeypatch: pytest.MonkeyPatch, case: str, expected: list[str]
) -> None:
    pdf = make_pdf("Invoice 1001")
    objects = _GcsObjects([pdf, make_pdf("Invoice 1002") if case == "differs" else pdf, make_pdf("Statement 2002")])
    if case == "replaced":
        objects.replaced_after_stat.add("b/o_1/a_1.pdf")
    elif case == "missing":
        del objects.objects["b/o_1/a_1.pdf"]
    elif case == "oversize":
        monkeypatch.setattr(settings, "BROWSER_DOWNLOAD_MAX_FILE_SIZE_BYTES", len(pdf) - 1)
    storage = GcsStorageForTests(bucket="b")
    storage.async_client = RealAsyncGcsStorageClient(project_id="test")
    monkeypatch.setattr(storage.async_client, "_get_client", lambda: objects)
    claimed = [pdf, pdf, make_pdf("Statement 2002")]

    assert await unique_stored_pdf_ids(monkeypatch, storage, "gs://b/o_1", claimed) == expected
    # Each comparison request must end within the comparison budget: bounded waits and no SDK retries.
    assert len(objects.request_policies) >= (0 if case == "oversize" else 2), "stat and ranged read both recorded"
    assert all(
        p.get("retry", "default") is None and max(p.get("timeout", (60,))) < 30 for p in objects.request_policies
    )


@pytest.mark.parametrize(("cancels", "fails"), [(1, False), (2, False), (2, True)])
@pytest.mark.parametrize("held", ["stat", "open", "ranged_read"])
@pytest.mark.asyncio
async def test_cancelling_a_gcs_comparison_waits_for_its_storage_thread_to_finish(
    held: str, cancels: int, fails: bool
) -> None:
    reading, release, finished = threading.Event(), threading.Event(), threading.Event()

    def hold(*_: Any, **__: Any) -> Any:
        reading.set()
        release.wait(5)
        finished.set()
        if fails:
            raise RuntimeError("storage call failed")
        return b"x" if held == "ranged_read" else SimpleNamespace(size=1, generation=100, metadata={}, updated=None)

    storage = GcsStorageForTests(bucket="b")
    storage.async_client = client = RealAsyncGcsStorageClient(project_id="test")
    client._get_client = lambda: SimpleNamespace(bucket=lambda _name: SimpleNamespace(get_blob=hold))  # type: ignore[method-assign]
    client._get_blob = lambda _uri: SimpleNamespace(download_as_bytes=hold)  # type: ignore[method-assign]
    artifact = make_artifact("gs://b/o_1/a_0.pdf")
    work = {
        "stat": lambda: storage.stat_artifact(artifact),
        "open": lambda: storage.open_artifact_stream(artifact, etag="100"),
        "ranged_read": lambda: gcs_module._GcsObjectStream(client, artifact.uri, 1, 100).iter_chunks(1).__anext__(),
    }[held]
    read = asyncio.ensure_future(work())
    try:
        await asyncio.to_thread(reading.wait, 5)
        # The first cancellation is the comparison budget; a later one is the caller's.
        for _ in range(cancels):
            read.cancel()
            for _ in range(5):
                await asyncio.sleep(0)
            assert not read.done(), "the storage thread is still running"
    finally:
        threading.Timer(0.2, release.set).start()
    with pytest.raises(asyncio.CancelledError):
        await read
    assert finished.is_set()


@pytest.mark.asyncio
async def test_managed_file_head_reads_only_the_requested_bytes() -> None:
    data = b"\x89PNG" + b"\x00" * 100_000
    served: list[int] = []

    def download_as_bytes(start: int | None = None, end: int | None = None) -> bytes:
        chunk = data[start or 0 : None if end is None else end + 1]
        served.append(len(chunk))
        return chunk

    storage = GcsStorageForTests(bucket="b")
    storage.async_client = client = RealAsyncGcsStorageClient(project_id="test")
    client._get_blob = lambda _uri: SimpleNamespace(download_as_bytes=download_as_bytes)  # type: ignore[method-assign]
    uri = f"gs://{settings.GCS_BUCKET_UPLOADS}/{settings.ENV}/{TEST_ORGANIZATION_ID}/shot.png"

    head = await storage.read_managed_file_head(uri, TEST_ORGANIZATION_ID, 64)

    assert head == data[:64]
    assert served == [64]
