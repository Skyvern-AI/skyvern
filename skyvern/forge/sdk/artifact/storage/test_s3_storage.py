from datetime import datetime
from pathlib import Path
from typing import Generator

import boto3
import pytest
from freezegun import freeze_time
from moto.server import ThreadedMotoServer
from types_boto3_s3.client import S3Client

from skyvern.config import settings
from skyvern.forge.sdk.api.aws import S3StorageClass, S3Uri, tag_set_to_dict
from skyvern.forge.sdk.artifact.models import Artifact, ArtifactType, LogEntityType
from skyvern.forge.sdk.artifact.storage.s3 import S3Storage
from skyvern.forge.sdk.artifact.storage.test_helpers import (
    create_fake_for_ai_suggestion,
    create_fake_step,
    create_fake_task_v2,
    create_fake_thought,
    create_fake_workflow_run_block,
)
from skyvern.forge.sdk.db.id import generate_artifact_id

# Test constants
TEST_BUCKET = "test-skyvern-bucket"
TEST_ORGANIZATION_ID = "test-org-123"
TEST_TASK_ID = "tsk_123456789"
TEST_STEP_ID = "step_123456789"
TEST_WORKFLOW_RUN_ID = "wfr_123456789"
TEST_BLOCK_ID = "block_123456789"
TEST_AI_SUGGESTION_ID = "ai_sugg_test_123"


class S3StorageForTests(S3Storage):
    async def _get_tags_for_org(self, organization_id: str) -> dict[str, str]:
        return {"dummy": f"org-{organization_id}", "test": "jerry"}

    async def _get_storage_class_for_org(self, organization_id: str) -> S3StorageClass:
        return S3StorageClass.ONEZONE_IA


@pytest.fixture
def s3_storage(moto_server: str) -> S3Storage:
    return S3StorageForTests(bucket=TEST_BUCKET, endpoint_url=moto_server)


@pytest.fixture(autouse=True)
def aws_credentials(monkeypatch: pytest.MonkeyPatch) -> None:
    """Mocked AWS Credentials for moto."""
    monkeypatch.setenv("AWS_ACCESS_KEY_ID", "testing")
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "testing")


@pytest.fixture(scope="module")
def moto_server() -> Generator[str, None, None]:
    # Note: pass `port=0` to get a random free port.
    server = ThreadedMotoServer(port=0)
    server.start()
    host, port = server.get_host_and_port()
    yield f"http://{host}:{port}"
    server.stop()


@pytest.fixture(scope="module", autouse=True)
def boto3_test_client(moto_server: str) -> Generator[S3Client, None, None]:
    client = boto3.client(
        "s3",
        aws_access_key_id="testing",
        aws_secret_access_key="testing",
        region_name=settings.AWS_REGION,
        endpoint_url=moto_server,
    )
    client.create_bucket(Bucket=TEST_BUCKET)  # Ensure the bucket exists for the test
    yield client


@freeze_time("2025-06-09T12:00:00")
class TestS3StorageBuildURIs:
    def test_build_uri(self, s3_storage: S3Storage) -> None:
        step = create_fake_step(TEST_STEP_ID)
        uri = s3_storage.build_uri(
            organization_id=TEST_ORGANIZATION_ID,
            artifact_id="artifact123",
            step=step,
            artifact_type=ArtifactType.LLM_PROMPT,
        )
        assert (
            uri
            == f"s3://{TEST_BUCKET}/v1/{settings.ENV}/{TEST_ORGANIZATION_ID}/{TEST_TASK_ID}/01_0_{TEST_STEP_ID}/2025-06-09T12:00:00_artifact123_llm_prompt.txt"
        )

    def test_build_log_uri(self, s3_storage: S3Storage) -> None:
        uri = s3_storage.build_log_uri(
            organization_id=TEST_ORGANIZATION_ID,
            log_entity_type=LogEntityType.WORKFLOW_RUN_BLOCK,
            log_entity_id="log_id",
            artifact_type=ArtifactType.SKYVERN_LOG,
        )
        assert (
            uri
            == f"s3://{TEST_BUCKET}/v1/{settings.ENV}/{TEST_ORGANIZATION_ID}/logs/workflow_run_block/log_id/2025-06-09T12:00:00_skyvern_log.log"
        )

    def test_build_thought_uri(self, s3_storage: S3Storage) -> None:
        thought = create_fake_thought("cruise123", "thought123")
        uri = s3_storage.build_thought_uri(
            organization_id=TEST_ORGANIZATION_ID,
            artifact_id="artifact123",
            thought=thought,
            artifact_type=ArtifactType.VISIBLE_ELEMENTS_TREE,
        )
        assert (
            uri
            == f"s3://{TEST_BUCKET}/v1/{settings.ENV}/{TEST_ORGANIZATION_ID}/observers/cruise123/thought123/2025-06-09T12:00:00_artifact123_visible_elements_tree.json"
        )

    def test_build_task_v2_uri(self, s3_storage: S3Storage) -> None:
        task_v2 = create_fake_task_v2("cruise123")
        uri = s3_storage.build_task_v2_uri(
            organization_id=TEST_ORGANIZATION_ID,
            artifact_id="artifact123",
            task_v2=task_v2,
            artifact_type=ArtifactType.HTML_ACTION,
        )
        assert (
            uri
            == f"s3://{TEST_BUCKET}/v1/{settings.ENV}/{TEST_ORGANIZATION_ID}/observers/cruise123/2025-06-09T12:00:00_artifact123_html_action.html"
        )

    def test_build_workflow_run_block_uri(self, s3_storage: S3Storage) -> None:
        workflow_run_block = create_fake_workflow_run_block(TEST_WORKFLOW_RUN_ID, TEST_BLOCK_ID)
        uri = s3_storage.build_workflow_run_block_uri(
            organization_id=TEST_ORGANIZATION_ID,
            artifact_id="artifact123",
            workflow_run_block=workflow_run_block,
            artifact_type=ArtifactType.HAR,
        )
        assert (
            uri
            == f"s3://{TEST_BUCKET}/v1/{settings.ENV}/{TEST_ORGANIZATION_ID}/workflow_runs/{TEST_WORKFLOW_RUN_ID}/{TEST_BLOCK_ID}/2025-06-09T12:00:00_artifact123_har.har"
        )

    def test_build_ai_suggestion_uri(self, s3_storage: S3Storage) -> None:
        ai_suggestion = create_fake_for_ai_suggestion(TEST_AI_SUGGESTION_ID)
        uri = s3_storage.build_ai_suggestion_uri(
            organization_id=TEST_ORGANIZATION_ID,
            artifact_id="artifact123",
            ai_suggestion=ai_suggestion,
            artifact_type=ArtifactType.SCREENSHOT_LLM,
        )
        assert (
            uri
            == f"s3://{TEST_BUCKET}/v1/{settings.ENV}/{TEST_ORGANIZATION_ID}/ai_suggestions/{TEST_AI_SUGGESTION_ID}/2025-06-09T12:00:00_artifact123_screenshot_llm.png"
        )


def _assert_object_meta(boto3_test_client: S3Client, uri: str) -> None:
    s3uri = S3Uri(uri)
    assert s3uri.bucket == TEST_BUCKET
    obj_meta = boto3_test_client.head_object(Bucket=TEST_BUCKET, Key=s3uri.key)
    assert obj_meta["StorageClass"] == "ONEZONE_IA"
    s3_tags_resp = boto3_test_client.get_object_tagging(Bucket=TEST_BUCKET, Key=s3uri.key)
    tags_dict = tag_set_to_dict(s3_tags_resp["TagSet"])
    assert tags_dict == {"dummy": f"org-{TEST_ORGANIZATION_ID}", "test": "jerry"}


def _assert_object_content(boto3_test_client: S3Client, uri: str, expected_content: bytes) -> None:
    s3uri = S3Uri(uri)
    assert s3uri.bucket == TEST_BUCKET
    obj_response = boto3_test_client.get_object(Bucket=TEST_BUCKET, Key=s3uri.key)
    assert obj_response["Body"].read() == expected_content


@pytest.mark.asyncio
class TestS3StorageStore:
    """Test S3Storage store methods."""

    def _create_artifact_for_ai_suggestion(
        self,
        s3_storage: S3Storage,
        artifact_type: ArtifactType,
        ai_suggestion_id: str,
    ) -> Artifact:
        """Helper method to create an Artifact for an AI suggestion."""
        artifact_id_val = generate_artifact_id()
        ai_suggestion = create_fake_for_ai_suggestion(ai_suggestion_id)
        uri = s3_storage.build_ai_suggestion_uri(
            organization_id=TEST_ORGANIZATION_ID,
            artifact_id=artifact_id_val,
            ai_suggestion=ai_suggestion,
            artifact_type=artifact_type,
        )
        return Artifact(
            artifact_id=artifact_id_val,
            artifact_type=artifact_type,
            uri=uri,
            organization_id=TEST_ORGANIZATION_ID,
            ai_suggestion_id=ai_suggestion.ai_suggestion_id,
            created_at=datetime.utcnow(),
            modified_at=datetime.utcnow(),
        )

    async def test_store_artifact_from_path(
        self, s3_storage: S3Storage, boto3_test_client: S3Client, tmp_path: Path
    ) -> None:
        test_data = b"fake screenshot data"
        artifact = self._create_artifact_for_ai_suggestion(
            s3_storage, ArtifactType.SCREENSHOT_LLM, TEST_AI_SUGGESTION_ID
        )

        test_file = tmp_path / "test_screenshot.png"
        test_file.write_bytes(test_data)
        await s3_storage.store_artifact_from_path(artifact, str(test_file))
        _assert_object_content(boto3_test_client, artifact.uri, test_data)
        _assert_object_meta(boto3_test_client, artifact.uri)

    async def test_store_artifact(self, s3_storage: S3Storage, boto3_test_client: S3Client) -> None:
        test_data = b"fake artifact data"
        artifact = self._create_artifact_for_ai_suggestion(s3_storage, ArtifactType.LLM_PROMPT, TEST_AI_SUGGESTION_ID)

        await s3_storage.store_artifact(artifact, test_data)
        _assert_object_content(boto3_test_client, artifact.uri, test_data)
        _assert_object_meta(boto3_test_client, artifact.uri)


TEST_BROWSER_SESSION_ID = "bs_test_123"


@pytest.mark.asyncio
class TestS3StorageBrowserSessionFiles:
    """Test S3Storage browser session file methods."""

    async def test_sync_browser_session_file_with_date(
        self, s3_storage: S3Storage, boto3_test_client: S3Client, tmp_path: Path
    ) -> None:
        """Test syncing a file with date in path (videos/har)."""
        test_data = b"fake video data"
        test_file = tmp_path / "recording.webm"
        test_file.write_bytes(test_data)

        uri = await s3_storage.sync_browser_session_file(
            organization_id=TEST_ORGANIZATION_ID,
            browser_session_id=TEST_BROWSER_SESSION_ID,
            artifact_type="videos",
            local_file_path=str(test_file),
            remote_path="recording.webm",
            date="2025-01-15",
        )

        expected_uri = f"s3://{TEST_BUCKET}/v1/{settings.ENV}/{TEST_ORGANIZATION_ID}/browser_sessions/{TEST_BROWSER_SESSION_ID}/videos/2025-01-15/recording.webm"
        assert uri == expected_uri
        _assert_object_content(boto3_test_client, uri, test_data)
        _assert_object_meta(boto3_test_client, uri)

    async def test_sync_browser_session_file_without_date(
        self, s3_storage: S3Storage, boto3_test_client: S3Client, tmp_path: Path
    ) -> None:
        """Test syncing a file without date (downloads category)."""
        test_data = b"fake download data"
        test_file = tmp_path / "document.pdf"
        test_file.write_bytes(test_data)

        uri = await s3_storage.sync_browser_session_file(
            organization_id=TEST_ORGANIZATION_ID,
            browser_session_id=TEST_BROWSER_SESSION_ID,
            artifact_type="downloads",
            local_file_path=str(test_file),
            remote_path="document.pdf",
            date=None,
        )

        expected_uri = f"s3://{TEST_BUCKET}/v1/{settings.ENV}/{TEST_ORGANIZATION_ID}/browser_sessions/{TEST_BROWSER_SESSION_ID}/downloads/document.pdf"
        assert uri == expected_uri
        _assert_object_content(boto3_test_client, uri, test_data)

    async def test_browser_session_file_exists_returns_true(
        self, s3_storage: S3Storage, boto3_test_client: S3Client, tmp_path: Path
    ) -> None:
        """Test browser_session_file_exists returns True for existing file."""
        test_file = tmp_path / "exists.webm"
        test_file.write_bytes(b"test data")

        await s3_storage.sync_browser_session_file(
            organization_id=TEST_ORGANIZATION_ID,
            browser_session_id=TEST_BROWSER_SESSION_ID,
            artifact_type="videos",
            local_file_path=str(test_file),
            remote_path="exists.webm",
            date="2025-01-15",
        )

        exists = await s3_storage.browser_session_file_exists(
            organization_id=TEST_ORGANIZATION_ID,
            browser_session_id=TEST_BROWSER_SESSION_ID,
            artifact_type="videos",
            remote_path="exists.webm",
            date="2025-01-15",
        )
        assert exists is True

    async def test_browser_session_file_exists_returns_false(self, s3_storage: S3Storage) -> None:
        """Test browser_session_file_exists returns False for non-existent file."""
        exists = await s3_storage.browser_session_file_exists(
            organization_id=TEST_ORGANIZATION_ID,
            browser_session_id=TEST_BROWSER_SESSION_ID,
            artifact_type="videos",
            remote_path="nonexistent.webm",
            date="2025-01-15",
        )
        assert exists is False

    async def test_delete_browser_session_file(
        self, s3_storage: S3Storage, boto3_test_client: S3Client, tmp_path: Path
    ) -> None:
        """Test deleting a browser session file."""
        test_file = tmp_path / "to_delete.webm"
        test_file.write_bytes(b"test data")

        await s3_storage.sync_browser_session_file(
            organization_id=TEST_ORGANIZATION_ID,
            browser_session_id=TEST_BROWSER_SESSION_ID,
            artifact_type="videos",
            local_file_path=str(test_file),
            remote_path="to_delete.webm",
            date="2025-01-15",
        )

        exists_before = await s3_storage.browser_session_file_exists(
            organization_id=TEST_ORGANIZATION_ID,
            browser_session_id=TEST_BROWSER_SESSION_ID,
            artifact_type="videos",
            remote_path="to_delete.webm",
            date="2025-01-15",
        )
        assert exists_before is True

        await s3_storage.delete_browser_session_file(
            organization_id=TEST_ORGANIZATION_ID,
            browser_session_id=TEST_BROWSER_SESSION_ID,
            artifact_type="videos",
            remote_path="to_delete.webm",
            date="2025-01-15",
        )

        exists_after = await s3_storage.browser_session_file_exists(
            organization_id=TEST_ORGANIZATION_ID,
            browser_session_id=TEST_BROWSER_SESSION_ID,
            artifact_type="videos",
            remote_path="to_delete.webm",
            date="2025-01-15",
        )
        assert exists_after is False

    async def test_file_exists_returns_true(
        self, s3_storage: S3Storage, boto3_test_client: S3Client, tmp_path: Path
    ) -> None:
        """Test file_exists returns True for existing file."""
        test_file = tmp_path / "test.txt"
        test_file.write_bytes(b"test data")

        uri = await s3_storage.sync_browser_session_file(
            organization_id=TEST_ORGANIZATION_ID,
            browser_session_id=TEST_BROWSER_SESSION_ID,
            artifact_type="downloads",
            local_file_path=str(test_file),
            remote_path="test.txt",
        )

        exists = await s3_storage.file_exists(uri)
        assert exists is True

    async def test_file_exists_returns_false(self, s3_storage: S3Storage) -> None:
        """Test file_exists returns False for non-existent file."""
        uri = f"s3://{TEST_BUCKET}/nonexistent/path/file.txt"
        exists = await s3_storage.file_exists(uri)
        assert exists is False

    async def test_download_uploaded_file(
        self, s3_storage: S3Storage, boto3_test_client: S3Client, tmp_path: Path
    ) -> None:
        """Test downloading an uploaded file."""
        test_data = b"uploaded file content"
        test_file = tmp_path / "uploaded.pdf"
        test_file.write_bytes(test_data)

        uri = await s3_storage.sync_browser_session_file(
            organization_id=TEST_ORGANIZATION_ID,
            browser_session_id=TEST_BROWSER_SESSION_ID,
            artifact_type="downloads",
            local_file_path=str(test_file),
            remote_path="uploaded.pdf",
        )

        downloaded = await s3_storage.download_uploaded_file(uri)
        assert downloaded == test_data

    async def test_download_uploaded_file_nonexistent(self, s3_storage: S3Storage) -> None:
        """Test downloading a non-existent file returns None."""
        uri = f"s3://{TEST_BUCKET}/nonexistent/path/file.txt"
        downloaded = await s3_storage.download_uploaded_file(uri)
        assert downloaded is None

    def test_storage_type_property(self, s3_storage: S3Storage) -> None:
        """Test storage_type returns 's3'."""
        assert s3_storage.storage_type == "s3"


CONTENT_TYPE_TEST_CASES = [
    # (filename, expected_content_type, artifact_type, date)
    ("video.webm", "video/webm", "videos", "2025-01-15"),
    ("data.json", "application/json", "har", "2025-01-15"),
    ("network.har", "application/json", "har", "2025-01-15"),
    ("screenshot.png", "image/png", "downloads", None),
    ("output.txt", "text/plain", "downloads", None),
    ("debug.log", "text/plain", "downloads", None),
]


@pytest.mark.asyncio
class TestS3StorageContentType:
    """Test S3Storage content type guessing."""

    @pytest.mark.parametrize("filename,expected_content_type,artifact_type,date", CONTENT_TYPE_TEST_CASES)
    async def test_content_type_guessing(
        self,
        s3_storage: S3Storage,
        boto3_test_client: S3Client,
        tmp_path: Path,
        filename: str,
        expected_content_type: str,
        artifact_type: str,
        date: str | None,
    ) -> None:
        """Test that files get correct content type based on extension."""
        test_file = tmp_path / filename
        test_file.write_bytes(b"test content")

        uri = await s3_storage.sync_browser_session_file(
            organization_id=TEST_ORGANIZATION_ID,
            browser_session_id=TEST_BROWSER_SESSION_ID,
            artifact_type=artifact_type,
            local_file_path=str(test_file),
            remote_path=filename,
            date=date,
        )

        s3uri = S3Uri(uri)
        obj_meta = boto3_test_client.head_object(Bucket=TEST_BUCKET, Key=s3uri.key)
        assert obj_meta["ContentType"] == expected_content_type
