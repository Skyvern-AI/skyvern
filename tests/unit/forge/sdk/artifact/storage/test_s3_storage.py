from __future__ import annotations

import asyncio
import io
import os
import shutil
import subprocess
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from typing import TYPE_CHECKING
from unittest.mock import AsyncMock, MagicMock

import pytest
import zstandard as zstd
from freezegun import freeze_time

from skyvern.config import settings
from skyvern.exceptions import DownloadSaveIncompleteError
from skyvern.forge.sdk.api.aws import _STREAM_UPLOAD_IO_QUEUE_DEPTH, S3StorageClass, S3Uri
from skyvern.forge.sdk.artifact.manager import ArtifactManager
from skyvern.forge.sdk.artifact.models import Artifact, ArtifactType, LogEntityType
from skyvern.forge.sdk.artifact.signing import SENSITIVE_ARTIFACT_URL_EXPIRY_SECONDS
from skyvern.forge.sdk.artifact.storage.s3 import S3Storage
from skyvern.forge.sdk.db.id import generate_artifact_id
from skyvern.forge.sdk.models import Step
from tests.unit.forge.sdk.artifact.storage.test_helpers import (
    create_fake_for_ai_suggestion,
    create_fake_step,
    create_fake_task_v2,
    create_fake_thought,
    create_fake_workflow_run_block,
)

if TYPE_CHECKING:
    from types_boto3_s3.client import S3Client

# Test constants
TEST_BUCKET = "test-skyvern-bucket"
TEST_ORGANIZATION_ID = "test-org-123"
TEST_TASK_ID = "tsk_123456789"
TEST_STEP_ID = "step_123456789"
TEST_WORKFLOW_RUN_ID = "wfr_123456789"
TEST_BLOCK_ID = "block_123456789"
TEST_AI_SUGGESTION_ID = "ai_sugg_test_123"


class S3StorageForTests(S3Storage):
    async def _get_storage_class_for_org(
        self,
        organization_id: str,
        bucket: str,
        object_size_bytes: int | None = None,
    ) -> S3StorageClass:
        return S3StorageClass.ONEZONE_IA


@pytest.fixture
def s3_storage() -> S3Storage:
    """Construct storage for pure tests without starting an S3 server."""
    return S3StorageForTests(bucket=TEST_BUCKET, endpoint_url="http://127.0.0.1:1")


@pytest.fixture(autouse=True)
def aws_credentials(monkeypatch: pytest.MonkeyPatch) -> None:
    """Mocked AWS Credentials for moto."""
    monkeypatch.setenv("AWS_ACCESS_KEY_ID", "testing")
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "testing")


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


def _assert_object_content(boto3_test_client: S3Client, uri: str, expected_content: bytes) -> None:
    s3uri = S3Uri(uri)
    assert s3uri.bucket == TEST_BUCKET
    obj_response = boto3_test_client.get_object(Bucket=TEST_BUCKET, Key=s3uri.key)
    assert obj_response["Body"].read() == expected_content


@pytest.mark.asyncio
class TestS3StorageStore:
    """Test S3Storage store methods."""

    __test__ = False  # Collected with moto fixtures in test_s3_storage_moto.py.

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

    __test__ = False  # Moto cases are collected in test_s3_storage_moto.py.

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

    async def test_assert_managed_file_access_accepts_org_scoped_uploads(self, s3_storage: S3Storage) -> None:
        legacy_uri = f"s3://{settings.AWS_S3_BUCKET_UPLOADS}/{settings.ENV}/{TEST_ORGANIZATION_ID}/uploaded.pdf"
        downloads_uri = (
            f"s3://{settings.AWS_S3_BUCKET_UPLOADS}/downloads/{settings.ENV}/{TEST_ORGANIZATION_ID}/wr_123/uploaded.pdf"
        )

        s3_storage.assert_managed_file_access(legacy_uri, TEST_ORGANIZATION_ID)
        s3_storage.assert_managed_file_access(downloads_uri, TEST_ORGANIZATION_ID)

    async def test_assert_managed_file_access_accepts_artifact_bucket(self, s3_storage: S3Storage) -> None:
        artifact_uri = (
            f"s3://{settings.AWS_S3_BUCKET_ARTIFACTS}/v1/{settings.ENV}/{TEST_ORGANIZATION_ID}/"
            "workflow_runs/wr_123/wrb_456/2026-03-23T17:57:58.370827_a_789_pdf.pdf"
        )
        s3_storage.assert_managed_file_access(artifact_uri, TEST_ORGANIZATION_ID)

    async def test_assert_managed_file_access_rejects_other_org(self, s3_storage: S3Storage) -> None:
        uri = f"s3://{settings.AWS_S3_BUCKET_UPLOADS}/{settings.ENV}/o_other/uploaded.pdf"
        with pytest.raises(PermissionError, match="No permission to access storage URI"):
            s3_storage.assert_managed_file_access(uri, TEST_ORGANIZATION_ID)

    async def test_assert_managed_file_access_rejects_other_org_artifact_bucket(self, s3_storage: S3Storage) -> None:
        uri = (
            f"s3://{settings.AWS_S3_BUCKET_ARTIFACTS}/v1/{settings.ENV}/o_other/"
            "workflow_runs/wr_123/wrb_456/artifact.pdf"
        )
        with pytest.raises(PermissionError, match="No permission to access storage URI"):
            s3_storage.assert_managed_file_access(uri, TEST_ORGANIZATION_ID)

    async def test_download_managed_file(self, s3_storage: S3Storage) -> None:
        """Test downloading a managed file."""
        test_data = b"uploaded file content"
        saved = await s3_storage.save_legacy_file(
            organization_id=TEST_ORGANIZATION_ID,
            filename="uploaded.pdf",
            fileObj=io.BytesIO(test_data),
        )
        assert saved is not None

        _, uri = saved
        downloaded = await s3_storage.download_managed_file(uri, TEST_ORGANIZATION_ID)
        assert downloaded == test_data

    async def test_download_managed_file_nonexistent(self, s3_storage: S3Storage) -> None:
        """Test downloading a non-existent managed file returns None."""
        uri = f"s3://{settings.AWS_S3_BUCKET_UPLOADS}/{settings.ENV}/{TEST_ORGANIZATION_ID}/nonexistent/file.txt"
        downloaded = await s3_storage.download_managed_file(uri, TEST_ORGANIZATION_ID)
        assert downloaded is None

    async def test_download_managed_file_rejects_other_org(self, s3_storage: S3Storage) -> None:
        uri = f"s3://{settings.AWS_S3_BUCKET_UPLOADS}/{settings.ENV}/o_other/uploaded.pdf"
        with pytest.raises(PermissionError, match="No permission to access storage URI"):
            await s3_storage.download_managed_file(uri, TEST_ORGANIZATION_ID)

    async def test_storage_type_property(self, s3_storage: S3Storage) -> None:
        """Test storage_type returns 's3'."""
        assert s3_storage.storage_type == "s3"

    async def test_get_shared_downloaded_files_returns_all(
        self,
        s3_storage: S3Storage,
        boto3_test_client: S3Client,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Listing many downloaded files returns one FileInfo per object with a presigned URL."""
        monkeypatch.setattr(settings, "AWS_S3_BUCKET_ARTIFACTS", TEST_BUCKET)
        session_id = "bs_returns_all_test"
        for i in range(5):
            test_file = tmp_path / f"invoice_{i}.csv"
            test_file.write_bytes(f"row,{i}\n".encode())
            await s3_storage.sync_browser_session_file(
                organization_id=TEST_ORGANIZATION_ID,
                browser_session_id=session_id,
                artifact_type="downloads",
                local_file_path=str(test_file),
                remote_path=f"invoice_{i}.csv",
                date=None,
            )

        file_infos = await s3_storage.get_shared_downloaded_files_in_browser_session(
            organization_id=TEST_ORGANIZATION_ID,
            browser_session_id=session_id,
        )
        assert len(file_infos) == 5
        assert {fi.filename for fi in file_infos} == {f"invoice_{i}.csv" for i in range(5)}
        for fi in file_infos:
            assert fi.url and "Signature=" in fi.url

    async def test_get_shared_downloaded_files_empty(
        self, s3_storage: S3Storage, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Empty directory returns an empty list, no head_object/presign work."""
        monkeypatch.setattr(settings, "AWS_S3_BUCKET_ARTIFACTS", TEST_BUCKET)
        file_infos = await s3_storage.get_shared_downloaded_files_in_browser_session(
            organization_id=TEST_ORGANIZATION_ID,
            browser_session_id="bs_no_files",
        )
        assert file_infos == []

    async def test_get_shared_downloaded_files_runs_concurrently(
        self,
        s3_storage: S3Storage,
        boto3_test_client: S3Client,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """head_object calls overlap rather than running sequentially."""
        monkeypatch.setattr(settings, "AWS_S3_BUCKET_ARTIFACTS", TEST_BUCKET)
        session_id = "bs_concurrent_test"
        for i in range(8):
            test_file = tmp_path / f"f_{i}.csv"
            test_file.write_bytes(b"x")
            await s3_storage.sync_browser_session_file(
                organization_id=TEST_ORGANIZATION_ID,
                browser_session_id=session_id,
                artifact_type="downloads",
                local_file_path=str(test_file),
                remote_path=f"f_{i}.csv",
                date=None,
            )

        original = s3_storage.async_client.get_object_info
        per_call_delay = 0.1
        in_flight = 0
        max_in_flight = 0

        async def slow_get_object_info(uri: str) -> dict:
            nonlocal in_flight, max_in_flight
            in_flight += 1
            max_in_flight = max(max_in_flight, in_flight)
            try:
                await asyncio.sleep(per_call_delay)
                return await original(uri)
            finally:
                in_flight -= 1

        monkeypatch.setattr(s3_storage.async_client, "get_object_info", slow_get_object_info)

        file_infos = await s3_storage.get_shared_downloaded_files_in_browser_session(
            organization_id=TEST_ORGANIZATION_ID,
            browser_session_id=session_id,
        )

        assert len(file_infos) == 8
        # max_in_flight > 1 directly proves head_object overlapped; wall-clock bounds flake under CI load.
        assert max_in_flight > 1, "expected overlapping head_object calls"

    async def test_get_shared_downloaded_files_caps_concurrency(
        self,
        s3_storage: S3Storage,
        boto3_test_client: S3Client,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """head_object fan-out is bounded by the instance-level semaphore."""
        monkeypatch.setattr(settings, "AWS_S3_BUCKET_ARTIFACTS", TEST_BUCKET)
        # Replace the instance semaphore with a tighter one so the cap is
        # hit with a small file count.
        monkeypatch.setattr(s3_storage, "_head_object_semaphore", asyncio.Semaphore(3))
        session_id = "bs_cap_test"
        file_count = 12
        for i in range(file_count):
            test_file = tmp_path / f"f_{i}.csv"
            test_file.write_bytes(b"x")
            await s3_storage.sync_browser_session_file(
                organization_id=TEST_ORGANIZATION_ID,
                browser_session_id=session_id,
                artifact_type="downloads",
                local_file_path=str(test_file),
                remote_path=f"f_{i}.csv",
                date=None,
            )

        original = s3_storage.async_client.get_object_info
        in_flight = 0
        max_in_flight = 0

        async def tracked_get_object_info(uri: str) -> dict:
            nonlocal in_flight, max_in_flight
            in_flight += 1
            max_in_flight = max(max_in_flight, in_flight)
            try:
                await asyncio.sleep(0.05)
                return await original(uri)
            finally:
                in_flight -= 1

        monkeypatch.setattr(s3_storage.async_client, "get_object_info", tracked_get_object_info)

        file_infos = await s3_storage.get_shared_downloaded_files_in_browser_session(
            organization_id=TEST_ORGANIZATION_ID,
            browser_session_id=session_id,
        )

        assert len(file_infos) == file_count
        assert max_in_flight <= 3, f"head_object exceeded the cap (max_in_flight={max_in_flight})"

    async def test_head_concurrency_cap_is_shared_across_concurrent_calls(
        self,
        s3_storage: S3Storage,
        boto3_test_client: S3Client,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Concurrent listing calls (the /browser_sessions/history fanout) share one cap."""
        monkeypatch.setattr(settings, "AWS_S3_BUCKET_ARTIFACTS", TEST_BUCKET)
        # Tight cap so the test can prove the limit holds across callers.
        monkeypatch.setattr(s3_storage, "_head_object_semaphore", asyncio.Semaphore(3))

        session_ids = ["bs_share_a", "bs_share_b", "bs_share_c"]
        for session_id in session_ids:
            for i in range(4):
                test_file = tmp_path / f"{session_id}_f_{i}.csv"
                test_file.write_bytes(b"x")
                await s3_storage.sync_browser_session_file(
                    organization_id=TEST_ORGANIZATION_ID,
                    browser_session_id=session_id,
                    artifact_type="downloads",
                    local_file_path=str(test_file),
                    remote_path=f"f_{i}.csv",
                    date=None,
                )

        original = s3_storage.async_client.get_object_info
        in_flight = 0
        max_in_flight = 0

        async def tracked_get_object_info(uri: str) -> dict:
            nonlocal in_flight, max_in_flight
            in_flight += 1
            max_in_flight = max(max_in_flight, in_flight)
            try:
                await asyncio.sleep(0.05)
                return await original(uri)
            finally:
                in_flight -= 1

        monkeypatch.setattr(s3_storage.async_client, "get_object_info", tracked_get_object_info)

        results = await asyncio.gather(
            *[
                s3_storage.get_shared_downloaded_files_in_browser_session(
                    organization_id=TEST_ORGANIZATION_ID, browser_session_id=session_id
                )
                for session_id in session_ids
            ]
        )

        for file_infos in results:
            assert len(file_infos) == 4
        # Without a shared semaphore, 3 sessions x 4 files = 12 head_objects could overlap.
        assert max_in_flight <= 3, f"shared semaphore breached the cap (max_in_flight={max_in_flight})"

    async def test_get_shared_downloaded_files_falls_back_when_batch_presign_fails(
        self,
        s3_storage: S3Storage,
        boto3_test_client: S3Client,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """When batch presign returns None, fall back to per-key signing rather than dropping all files."""
        monkeypatch.setattr(settings, "AWS_S3_BUCKET_ARTIFACTS", TEST_BUCKET)
        session_id = "bs_batch_fail_test"
        for i in range(3):
            test_file = tmp_path / f"f_{i}.csv"
            test_file.write_bytes(b"x")
            await s3_storage.sync_browser_session_file(
                organization_id=TEST_ORGANIZATION_ID,
                browser_session_id=session_id,
                artifact_type="downloads",
                local_file_path=str(test_file),
                remote_path=f"f_{i}.csv",
                date=None,
            )

        original_create = s3_storage.async_client.create_presigned_urls

        async def flaky_create(uris: list[str]) -> list[str] | None:
            if len(uris) > 1:
                return None  # simulate batch failure
            return await original_create(uris)

        monkeypatch.setattr(s3_storage.async_client, "create_presigned_urls", flaky_create)

        file_infos = await s3_storage.get_shared_downloaded_files_in_browser_session(
            organization_id=TEST_ORGANIZATION_ID,
            browser_session_id=session_id,
        )
        # Per-key fallback should preserve all 3 URLs.
        assert len(file_infos) == 3
        assert all(fi.url and "Signature=" in fi.url for fi in file_infos)

    async def test_get_shared_downloaded_files_per_key_partial_failure(
        self,
        s3_storage: S3Storage,
        boto3_test_client: S3Client,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Per-key fallback skips only the failing key, preserving the rest."""
        monkeypatch.setattr(settings, "AWS_S3_BUCKET_ARTIFACTS", TEST_BUCKET)
        session_id = "bs_partial_fail_test"
        for i in range(3):
            test_file = tmp_path / f"f_{i}.csv"
            test_file.write_bytes(b"x")
            await s3_storage.sync_browser_session_file(
                organization_id=TEST_ORGANIZATION_ID,
                browser_session_id=session_id,
                artifact_type="downloads",
                local_file_path=str(test_file),
                remote_path=f"f_{i}.csv",
                date=None,
            )

        original_create = s3_storage.async_client.create_presigned_urls

        async def flaky_create(uris: list[str]) -> list[str] | None:
            if len(uris) > 1:
                return None  # force per-key fallback
            if uris[0].endswith("f_1.csv"):
                return None  # one key fails
            return await original_create(uris)

        monkeypatch.setattr(s3_storage.async_client, "create_presigned_urls", flaky_create)

        file_infos = await s3_storage.get_shared_downloaded_files_in_browser_session(
            organization_id=TEST_ORGANIZATION_ID,
            browser_session_id=session_id,
        )
        names = {fi.filename for fi in file_infos}
        assert names == {"f_0.csv", "f_2.csv"}

    async def test_get_shared_recordings_skips_zero_byte_before_presign(
        self,
        s3_storage: S3Storage,
        boto3_test_client: S3Client,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Zero-byte recordings are filtered before they consume a presign slot."""
        monkeypatch.setattr(settings, "AWS_S3_BUCKET_ARTIFACTS", TEST_BUCKET)
        # Force the legacy listing path (DB-first path is exercised in test_browser_session_recording_artifacts).
        monkeypatch.setattr(settings, "ARTIFACT_CONTENT_HMAC_KEYRING", None)
        session_id = "bs_zero_byte_test"

        good_file = tmp_path / "good.webm"
        good_file.write_bytes(b"\x1a\x45\xdf\xa3" + b"\x00" * 32)
        await s3_storage.sync_browser_session_file(
            organization_id=TEST_ORGANIZATION_ID,
            browser_session_id=session_id,
            artifact_type="videos",
            local_file_path=str(good_file),
            remote_path="good.webm",
            date="2025-01-15",
        )

        empty_file = tmp_path / "empty.webm"
        empty_file.write_bytes(b"")
        await s3_storage.sync_browser_session_file(
            organization_id=TEST_ORGANIZATION_ID,
            browser_session_id=session_id,
            artifact_type="videos",
            local_file_path=str(empty_file),
            remote_path="empty.webm",
            date="2025-01-15",
        )

        original_create = s3_storage.async_client.create_presigned_urls
        seen_keys: list[list[str]] = []

        async def tracking_create(uris: list[str]) -> list[str] | None:
            seen_keys.append(list(uris))
            return await original_create(uris)

        monkeypatch.setattr(s3_storage.async_client, "create_presigned_urls", tracking_create)

        file_infos = await s3_storage.get_shared_recordings_in_browser_session(
            organization_id=TEST_ORGANIZATION_ID,
            browser_session_id=session_id,
        )
        assert {fi.filename for fi in file_infos} == {"good.webm"}
        # The empty.webm key must never be passed to create_presigned_urls.
        passed_basenames = [os.path.basename(uri) for batch in seen_keys for uri in batch]
        assert "empty.webm" not in passed_basenames


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

    __test__ = False  # Collected with moto fixtures in test_s3_storage_moto.py.

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


@pytest.mark.asyncio
class TestS3StorageHARCompression:
    """Test S3Storage HAR file compression with zstd."""

    __test__ = False  # Collected with moto fixtures in test_s3_storage_moto.py.

    def _create_har_artifact(self, s3_storage: S3Storage, step_id: str) -> Artifact:
        """Helper method to create a HAR Artifact."""
        artifact_id_val = generate_artifact_id()
        step = create_fake_step(step_id)
        uri = s3_storage.build_uri(
            organization_id=TEST_ORGANIZATION_ID,
            artifact_id=artifact_id_val,
            step=step,
            artifact_type=ArtifactType.HAR,
        )
        return Artifact(
            artifact_id=artifact_id_val,
            artifact_type=ArtifactType.HAR,
            uri=uri,
            organization_id=TEST_ORGANIZATION_ID,
            step_id=step.step_id,
            task_id=step.task_id,
            created_at=datetime.utcnow(),
            modified_at=datetime.utcnow(),
        )

    async def test_store_har_artifact_compresses_with_zstd(
        self, s3_storage: S3Storage, boto3_test_client: S3Client
    ) -> None:
        """Test that HAR artifacts are compressed with zstd and URI is updated."""

        # Create sample HAR JSON data (easily compressible)
        har_data = b'{"log": {"version": "1.2", "entries": [{"request": {}, "response": {}}]}}'
        artifact = self._create_har_artifact(s3_storage, TEST_STEP_ID)
        assert artifact.uri.endswith(".har.zst")

        # Store the artifact
        await s3_storage.store_artifact(artifact, har_data)

        # Verify the stored data is compressed
        s3uri = S3Uri(artifact.uri)
        obj_response = boto3_test_client.get_object(Bucket=TEST_BUCKET, Key=s3uri.key)
        stored_data = obj_response["Body"].read()

        # Stored data should be different from original (compressed)
        assert stored_data != har_data

        # Verify we can decompress it back to original
        dctx = zstd.ZstdDecompressor()
        decompressed = dctx.decompress(stored_data)
        assert decompressed == har_data

    async def test_retrieve_har_artifact_decompresses_zstd(
        self, s3_storage: S3Storage, boto3_test_client: S3Client
    ) -> None:
        """Test that retrieving a .zst HAR artifact auto-decompresses it."""
        # Create and store HAR artifact
        har_data = b'{"log": {"version": "1.2", "creator": {"name": "test"}}}'
        artifact = self._create_har_artifact(s3_storage, TEST_STEP_ID)

        await s3_storage.store_artifact(artifact, har_data)

        # Retrieve should auto-decompress
        retrieved_data = await s3_storage.retrieve_artifact(artifact)
        assert retrieved_data == har_data

    async def test_non_har_artifact_not_compressed(self, s3_storage: S3Storage, boto3_test_client: S3Client) -> None:
        """Test that non-HAR artifacts are NOT compressed."""
        test_data = b"fake screenshot data"
        artifact_id_val = generate_artifact_id()
        step = create_fake_step(TEST_STEP_ID)
        uri = s3_storage.build_uri(
            organization_id=TEST_ORGANIZATION_ID,
            artifact_id=artifact_id_val,
            step=step,
            artifact_type=ArtifactType.SCREENSHOT_LLM,
        )
        artifact = Artifact(
            artifact_id=artifact_id_val,
            artifact_type=ArtifactType.SCREENSHOT_LLM,
            uri=uri,
            organization_id=TEST_ORGANIZATION_ID,
            step_id=step.step_id,
            task_id=step.task_id,
            created_at=datetime.utcnow(),
            modified_at=datetime.utcnow(),
        )

        await s3_storage.store_artifact(artifact, test_data)

        # URI should NOT have .zst extension
        assert not artifact.uri.endswith(".zst")

        # Stored data should be identical to original
        s3uri = S3Uri(artifact.uri)
        obj_response = boto3_test_client.get_object(Bucket=TEST_BUCKET, Key=s3uri.key)
        stored_data = obj_response["Body"].read()
        assert stored_data == test_data


_build_zip = ArtifactManager._build_zip


@pytest.mark.asyncio
class TestS3StorageZIPArchiveRetrieve:
    """Test retrieve_artifact with STEP_ARCHIVE / TASK_ARCHIVE bundle_key extraction."""

    __test__ = False  # Moto cases are collected in test_s3_storage_moto.py.

    def _make_archive_artifact(
        self,
        s3_storage: S3Storage,
        step: Step,
        archive_type: ArtifactType,
        bundle_key: str,
    ) -> Artifact:
        archive_artifact_id = generate_artifact_id()
        uri = s3_storage.build_uri(
            organization_id=TEST_ORGANIZATION_ID,
            artifact_id=archive_artifact_id,
            step=step,
            artifact_type=archive_type,
        )
        member_artifact_id = generate_artifact_id()
        return Artifact(
            artifact_id=member_artifact_id,
            artifact_type=ArtifactType.HTML_SCRAPE,
            uri=uri,
            bundle_key=bundle_key,
            organization_id=TEST_ORGANIZATION_ID,
            step_id=step.step_id,
            task_id=step.task_id,
            created_at=datetime.utcnow(),
            modified_at=datetime.utcnow(),
        )

    async def test_retrieve_text_entry_from_step_archive(
        self, s3_storage: S3Storage, boto3_test_client: S3Client
    ) -> None:
        """Retrieve a text artifact stored inside a STEP_ARCHIVE ZIP."""
        step = create_fake_step(TEST_STEP_ID)
        bundle_key = "scrape.html"
        expected = b"<html>hello world</html>"
        zip_bytes = _build_zip({bundle_key: expected, "element_tree.json": b"[]"})

        artifact = self._make_archive_artifact(s3_storage, step, ArtifactType.STEP_ARCHIVE, bundle_key)

        # Upload the archive directly (simulating what _flush_step_archive does)
        archive_artifact = Artifact(
            artifact_id=generate_artifact_id(),
            artifact_type=ArtifactType.STEP_ARCHIVE,
            uri=artifact.uri,
            organization_id=TEST_ORGANIZATION_ID,
            step_id=step.step_id,
            task_id=step.task_id,
            created_at=datetime.utcnow(),
            modified_at=datetime.utcnow(),
        )
        await s3_storage.store_artifact(archive_artifact, zip_bytes)

        retrieved = await s3_storage.retrieve_artifact(artifact)
        assert retrieved == expected

    async def test_retrieve_screenshot_from_step_archive(
        self, s3_storage: S3Storage, boto3_test_client: S3Client
    ) -> None:
        """Retrieve a PNG screenshot from a STEP_ARCHIVE ZIP."""
        step = create_fake_step(TEST_STEP_ID)
        bundle_key = "screenshot_llm_0.png"
        fake_png = b"\x89PNG\r\n\x1a\n" + b"\x00" * 50
        zip_bytes = _build_zip({bundle_key: fake_png})

        artifact = self._make_archive_artifact(s3_storage, step, ArtifactType.STEP_ARCHIVE, bundle_key)
        archive_artifact = Artifact(
            artifact_id=generate_artifact_id(),
            artifact_type=ArtifactType.STEP_ARCHIVE,
            uri=artifact.uri,
            organization_id=TEST_ORGANIZATION_ID,
            step_id=step.step_id,
            task_id=step.task_id,
            created_at=datetime.utcnow(),
            modified_at=datetime.utcnow(),
        )
        await s3_storage.store_artifact(archive_artifact, zip_bytes)

        retrieved = await s3_storage.retrieve_artifact(artifact)
        assert retrieved == fake_png

    async def test_retrieve_from_task_archive(self, s3_storage: S3Storage, boto3_test_client: S3Client) -> None:
        """Retrieve a browser console log from a TASK_ARCHIVE ZIP."""
        step = create_fake_step(TEST_STEP_ID)
        bundle_key = "browser_console.log"
        log_content = b"[info] page loaded\n[error] fetch failed"
        zip_bytes = _build_zip({bundle_key: log_content, "har.har": b'{"log":{}}'})

        artifact = self._make_archive_artifact(s3_storage, step, ArtifactType.TASK_ARCHIVE, bundle_key)
        archive_artifact = Artifact(
            artifact_id=generate_artifact_id(),
            artifact_type=ArtifactType.TASK_ARCHIVE,
            uri=artifact.uri,
            organization_id=TEST_ORGANIZATION_ID,
            step_id=step.step_id,
            task_id=step.task_id,
            created_at=datetime.utcnow(),
            modified_at=datetime.utcnow(),
        )
        await s3_storage.store_artifact(archive_artifact, zip_bytes)

        retrieved = await s3_storage.retrieve_artifact(artifact)
        assert retrieved == log_content

    async def test_retrieve_missing_bundle_key_returns_none(
        self, s3_storage: S3Storage, boto3_test_client: S3Client
    ) -> None:
        """bundle_key that doesn't exist inside the ZIP should return None."""
        step = create_fake_step(TEST_STEP_ID)
        zip_bytes = _build_zip({"scrape.html": b"content"})

        artifact = self._make_archive_artifact(s3_storage, step, ArtifactType.STEP_ARCHIVE, "nonexistent.txt")
        archive_artifact = Artifact(
            artifact_id=generate_artifact_id(),
            artifact_type=ArtifactType.STEP_ARCHIVE,
            uri=artifact.uri,
            organization_id=TEST_ORGANIZATION_ID,
            step_id=step.step_id,
            task_id=step.task_id,
            created_at=datetime.utcnow(),
            modified_at=datetime.utcnow(),
        )
        await s3_storage.store_artifact(archive_artifact, zip_bytes)

        result = await s3_storage.retrieve_artifact(artifact)
        assert result is None

    async def test_retrieve_corrupt_zip_returns_none(self, s3_storage: S3Storage, boto3_test_client: S3Client) -> None:
        """A corrupt (non-ZIP) payload with a bundle_key should return None gracefully."""
        step = create_fake_step(TEST_STEP_ID)
        artifact = self._make_archive_artifact(s3_storage, step, ArtifactType.STEP_ARCHIVE, "scrape.html")

        # Upload garbage bytes as the archive
        archive_artifact = Artifact(
            artifact_id=generate_artifact_id(),
            artifact_type=ArtifactType.STEP_ARCHIVE,
            uri=artifact.uri,
            organization_id=TEST_ORGANIZATION_ID,
            step_id=step.step_id,
            task_id=step.task_id,
            created_at=datetime.utcnow(),
            modified_at=datetime.utcnow(),
        )
        await s3_storage.store_artifact(archive_artifact, b"this is not a zip file at all")

        result = await s3_storage.retrieve_artifact(artifact)
        assert result is None

    async def test_retrieve_without_bundle_key_returns_raw_bytes(
        self, s3_storage: S3Storage, boto3_test_client: S3Client
    ) -> None:
        """An artifact with no bundle_key (e.g. RECORDING) is returned as-is."""
        step = create_fake_step(TEST_STEP_ID)
        raw_data = b"raw recording bytes"
        artifact_id_val = generate_artifact_id()
        uri = s3_storage.build_uri(
            organization_id=TEST_ORGANIZATION_ID,
            artifact_id=artifact_id_val,
            step=step,
            artifact_type=ArtifactType.RECORDING,
        )
        artifact = Artifact(
            artifact_id=artifact_id_val,
            artifact_type=ArtifactType.RECORDING,
            uri=uri,
            bundle_key=None,
            organization_id=TEST_ORGANIZATION_ID,
            step_id=step.step_id,
            task_id=step.task_id,
            created_at=datetime.utcnow(),
            modified_at=datetime.utcnow(),
        )
        await s3_storage.store_artifact(artifact, raw_data)
        retrieved = await s3_storage.retrieve_artifact(artifact)
        assert retrieved == raw_data

    async def test_build_uri_step_archive_has_zip_extension(self, s3_storage: S3Storage) -> None:
        """STEP_ARCHIVE URIs should end with .zip (not .zst)."""
        step = create_fake_step(TEST_STEP_ID)
        uri = s3_storage.build_uri(
            organization_id=TEST_ORGANIZATION_ID,
            artifact_id=generate_artifact_id(),
            step=step,
            artifact_type=ArtifactType.STEP_ARCHIVE,
        )
        assert uri.endswith(".zip")
        assert not uri.endswith(".zst")

    async def test_build_uri_task_archive_has_zip_extension(self, s3_storage: S3Storage) -> None:
        """TASK_ARCHIVE URIs should end with .zip (not .zst)."""
        step = create_fake_step(TEST_STEP_ID)
        uri = s3_storage.build_uri(
            organization_id=TEST_ORGANIZATION_ID,
            artifact_id=generate_artifact_id(),
            step=step,
            artifact_type=ArtifactType.TASK_ARCHIVE,
        )
        assert uri.endswith(".zip")
        assert not uri.endswith(".zst")


@pytest.mark.asyncio
class TestS3StoragePerRunRecordingClips:
    """Integration test for the SKY-7220 per-run clip path: drives the real
    ``S3Storage.sync_browser_session_file(videos)`` against moto S3 with a real
    ffmpeg-generated recording, asserting a run-scoped clip is cut and uploaded."""

    __test__ = False  # Collected with moto fixtures in test_s3_storage_moto.py.

    async def test_session_close_cuts_and_uploads_run_scoped_clip(
        self,
        s3_storage: S3Storage,
        boto3_test_client: S3Client,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        if shutil.which("ffmpeg") is None or shutil.which("ffprobe") is None:
            pytest.skip("ffmpeg/ffprobe not installed")

        # A real ~12s recording standing in for the finalized session video.
        src = tmp_path / "recording.webm"
        subprocess.check_call(
            [
                "ffmpeg",
                "-y",
                "-hide_banner",
                "-loglevel",
                "error",
                "-f",
                "lavfi",
                "-i",
                "testsrc=size=320x180:rate=10:duration=12",
                "-c:v",
                "libvpx",
                str(src),
            ]
        )

        import skyvern.forge.sdk.artifact.storage.run_recording_clips as rrc

        now = datetime.now(UTC)
        run = SimpleNamespace(
            workflow_run_id="wr_itest",
            started_at=now - timedelta(seconds=10),
            finished_at=now - timedelta(seconds=2),
        )
        create_clip = AsyncMock(return_value="a_clip")
        fake_app = MagicMock()
        fake_app.DATABASE.workflow_runs.get_workflow_runs_for_browser_session = AsyncMock(return_value=[run])
        fake_app.DATABASE.artifacts.list_artifacts_for_run_by_type = AsyncMock(return_value=[])
        fake_app.DATABASE.observer.get_task_v2_by_workflow_run_id = AsyncMock(return_value=None)
        fake_app.ARTIFACT_MANAGER.create_run_recording_artifact = create_clip
        monkeypatch.setattr(rrc, "app", fake_app)

        uri = await s3_storage.sync_browser_session_file(
            organization_id=TEST_ORGANIZATION_ID,
            browser_session_id=TEST_BROWSER_SESSION_ID,
            artifact_type="videos",
            local_file_path=str(src),
            remote_path="recording.webm",
            date="2025-01-15",
        )

        # Full session recording still uploaded under videos/.
        assert f"/browser_sessions/{TEST_BROWSER_SESSION_ID}/videos/2025-01-15/" in uri

        # Exactly one run-scoped clip registered, run_id propagated, stored under run_recordings/.
        create_clip.assert_awaited_once()
        call = create_clip.await_args
        assert call is not None
        kwargs = call.kwargs
        assert kwargs["run_id"] == "wr_itest"
        assert kwargs["workflow_run_id"] == "wr_itest"
        assert kwargs["file_size"] and kwargs["file_size"] > 0
        clip_uri = kwargs["uri"]
        assert f"/browser_sessions/{TEST_BROWSER_SESSION_ID}/run_recordings/2025-01-15/wr_itest/" in clip_uri

        # The clip bytes really landed in (moto) S3 — proves the real ffmpeg cut + upload ran.
        clip_key = S3Uri(clip_uri).key
        head = boto3_test_client.head_object(Bucket=TEST_BUCKET, Key=clip_key)
        assert head["ContentLength"] > 0


@pytest.mark.asyncio
class TestS3StorageBrowserSessionPure:
    """URI authorization checks that do not perform S3 I/O."""

    test_assert_managed_file_access_accepts_org_scoped_uploads = (
        TestS3StorageBrowserSessionFiles.test_assert_managed_file_access_accepts_org_scoped_uploads
    )
    test_assert_managed_file_access_accepts_artifact_bucket = (
        TestS3StorageBrowserSessionFiles.test_assert_managed_file_access_accepts_artifact_bucket
    )
    test_assert_managed_file_access_rejects_other_org = (
        TestS3StorageBrowserSessionFiles.test_assert_managed_file_access_rejects_other_org
    )
    test_assert_managed_file_access_rejects_other_org_artifact_bucket = (
        TestS3StorageBrowserSessionFiles.test_assert_managed_file_access_rejects_other_org_artifact_bucket
    )
    test_download_managed_file_rejects_other_org = (
        TestS3StorageBrowserSessionFiles.test_download_managed_file_rejects_other_org
    )
    test_storage_type_property = TestS3StorageBrowserSessionFiles.test_storage_type_property


@pytest.mark.asyncio
class TestS3StorageZIPArchivePure:
    """Archive URI checks that do not perform S3 I/O."""

    test_build_uri_step_archive_has_zip_extension = (
        TestS3StorageZIPArchiveRetrieve.test_build_uri_step_archive_has_zip_extension
    )
    test_build_uri_task_archive_has_zip_extension = (
        TestS3StorageZIPArchiveRetrieve.test_build_uri_task_archive_has_zip_extension
    )


@pytest.mark.asyncio
async def test_browser_profile_exists_propagates_non_not_found_errors() -> None:
    # Regression: a confirmed not-found returns False, but transient/authz errors must PROPAGATE (not
    # swallow to False) so _managed_browser_profile_has_content's fail-safe treats a flaky read as
    # existing content instead of reseeding a run to fresh and overwriting its saved archive.
    from botocore.exceptions import ClientError

    storage = S3Storage()
    storage.async_client = MagicMock()
    storage.async_client._is_not_found_error = lambda e: e.response["Error"]["Code"] in {"404", "NoSuchKey", "NotFound"}

    storage.async_client.get_object_info = AsyncMock(side_effect=ClientError({"Error": {"Code": "404"}}, "HeadObject"))
    assert await storage.browser_profile_exists("o", "bp") is False

    storage.async_client.get_object_info = AsyncMock(
        side_effect=ClientError({"Error": {"Code": "InternalError"}}, "HeadObject")
    )
    with pytest.raises(ClientError):
        await storage.browser_profile_exists("o", "bp")


@pytest.mark.asyncio
async def test_retrieve_browser_profile_extracts_sub_buffer_size_archive(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Regression: the archive used to be closed only AFTER unzipping, so anything smaller than the io
    # buffer (~8KB) — a cookie-only first bank — was still unflushed and ZipFile hit an empty file.
    zip_bytes = _build_zip({".skyvern_banked_cookies.json": b'[{"name":"session"}]'})
    assert len(zip_bytes) < 1024

    storage = S3Storage()
    storage.async_client = MagicMock()
    storage.async_client.download_file = AsyncMock(return_value=zip_bytes)

    monkeypatch.setattr(settings, "TEMP_PATH", str(tmp_path))
    profile_dir = await storage.retrieve_browser_profile("o", "bp")

    assert profile_dir is not None
    assert (Path(profile_dir) / ".skyvern_banked_cookies.json").read_bytes() == b'[{"name":"session"}]'
    # The extraction directory must be the ONLY thing left in TEMP_PATH — the downloaded archive is
    # written there under a suffixless temp name, so a leak would show up as a second entry.
    assert [entry.name for entry in tmp_path.iterdir()] == [Path(profile_dir).name]


@pytest.mark.asyncio
async def test_delete_browser_profile_hard_raises_soft_swallows() -> None:
    # hard_delete must PROPAGATE an S3 failure (raise_on_error=True) so the reap can't falsely report a
    # cookie-bearing archive erased and silently orphan it; a soft delete stays best-effort.
    storage = S3Storage()
    storage.async_client = MagicMock()

    storage.async_client.delete_file = AsyncMock(side_effect=RuntimeError("s3 down"))
    with pytest.raises(RuntimeError):
        await storage.delete_browser_profile("o", "bp", hard_delete=True)
    assert storage.async_client.delete_file.await_args.kwargs["raise_on_error"] is True

    storage.async_client.delete_file = AsyncMock()
    await storage.delete_browser_profile("o", "bp", hard_delete=False)
    assert storage.async_client.delete_file.await_args.kwargs["raise_on_error"] is False


def _share_artifact(artifact_type: ArtifactType, uri: str) -> Artifact:
    return Artifact(
        artifact_id=generate_artifact_id(),
        artifact_type=artifact_type,
        uri=uri,
        organization_id=TEST_ORGANIZATION_ID,
        created_at=datetime.utcnow(),
        modified_at=datetime.utcnow(),
    )


@pytest.mark.asyncio
class TestS3ShareLinkSensitiveCap:
    """Sensitive artifact types get capped presigned-URL TTLs (SKY-12527)."""

    async def test_screenshot_share_link_uses_capped_expiry(self, s3_storage: S3Storage) -> None:
        s3_storage.async_client.create_presigned_urls = AsyncMock(return_value=["https://s3/shot"])
        artifact = _share_artifact(ArtifactType.SCREENSHOT_ACTION, f"s3://{TEST_BUCKET}/shot.png")
        assert await s3_storage.get_share_link(artifact) == "https://s3/shot"
        s3_storage.async_client.create_presigned_urls.assert_awaited_once_with(
            [artifact.uri], expires_in=SENSITIVE_ARTIFACT_URL_EXPIRY_SECONDS
        )

    async def test_download_share_link_keeps_default_expiry(self, s3_storage: S3Storage) -> None:
        s3_storage.async_client.create_presigned_urls = AsyncMock(return_value=["https://s3/file"])
        artifact = _share_artifact(ArtifactType.DOWNLOAD, f"s3://{TEST_BUCKET}/file.pdf")
        assert await s3_storage.get_share_link(artifact) == "https://s3/file"
        s3_storage.async_client.create_presigned_urls.assert_awaited_once_with([artifact.uri])

    async def test_mixed_batch_preserves_order_and_routes_by_type(self, s3_storage: S3Storage) -> None:
        download = _share_artifact(ArtifactType.DOWNLOAD, f"s3://{TEST_BUCKET}/file.pdf")
        screenshot = _share_artifact(ArtifactType.SCREENSHOT_FINAL, f"s3://{TEST_BUCKET}/shot.png")
        recording = _share_artifact(ArtifactType.RECORDING, f"s3://{TEST_BUCKET}/rec.webm")

        async def fake_presign(uris: list[str], expires_in: int | None = None) -> list[str]:
            suffix = "capped" if expires_in is not None else "default"
            return [f"{uri}?{suffix}" for uri in uris]

        s3_storage.async_client.create_presigned_urls = AsyncMock(side_effect=fake_presign)
        urls = await s3_storage.get_share_links([download, screenshot, recording])
        assert urls == [
            f"{download.uri}?default",
            f"{screenshot.uri}?capped",
            f"{recording.uri}?capped",
        ]

    async def test_failed_sensitive_presign_fails_the_batch(self, s3_storage: S3Storage) -> None:
        screenshot = _share_artifact(ArtifactType.SCREENSHOT_LLM, f"s3://{TEST_BUCKET}/shot.png")
        download = _share_artifact(ArtifactType.DOWNLOAD, f"s3://{TEST_BUCKET}/file.pdf")

        async def fake_presign(uris: list[str], expires_in: int | None = None) -> list[str] | None:
            return None if expires_in is not None else [f"{uri}?ok" for uri in uris]

        s3_storage.async_client.create_presigned_urls = AsyncMock(side_effect=fake_presign)
        assert await s3_storage.get_share_links([screenshot, download]) is None


@pytest.mark.asyncio
class TestS3SaveDownloadedFiles:
    def _seed_run_dir(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        run_dir = tmp_path / "downloads" / "wr_partial"
        run_dir.mkdir(parents=True)
        (run_dir / "a.pdf").write_bytes(b"first")
        (run_dir / "b.pdf").write_bytes(b"second")
        monkeypatch.setattr("skyvern.forge.sdk.api.files.settings.DOWNLOAD_PATH", str(tmp_path / "downloads"))

    async def test_partial_upload_failure_raises_after_saving_the_rest(
        self, s3_storage: S3Storage, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        import skyvern.forge.sdk.artifact.storage.s3 as s3_module

        self._seed_run_dir(tmp_path, monkeypatch)
        uploaded: list[str] = []

        async def _upload(*, uri: str, file_path: str, **kwargs: object) -> None:
            if uri.endswith("/a.pdf"):
                raise RuntimeError("transient 503")
            uploaded.append(uri)

        monkeypatch.setattr(s3_storage.async_client, "upload_file_from_path", _upload)
        create_download_artifact = AsyncMock()
        monkeypatch.setattr(
            s3_module,
            "app",
            SimpleNamespace(ARTIFACT_MANAGER=SimpleNamespace(create_download_artifact=create_download_artifact)),
        )

        with pytest.raises(DownloadSaveIncompleteError) as raised:
            await s3_storage.save_downloaded_files(organization_id=TEST_ORGANIZATION_ID, run_id="wr_partial")

        assert raised.value.skipped_files == ["a.pdf"]
        assert [uri.rsplit("/", 1)[-1] for uri in uploaded] == ["b.pdf"]
        assert create_download_artifact.await_count == 1
        assert create_download_artifact.await_args.kwargs["filename"] == "b.pdf"

    async def test_artifact_row_failure_counts_as_skipped(
        self, s3_storage: S3Storage, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        import skyvern.forge.sdk.artifact.storage.s3 as s3_module

        self._seed_run_dir(tmp_path, monkeypatch)
        monkeypatch.setattr(s3_storage.async_client, "upload_file_from_path", AsyncMock())

        async def _create_row(*, filename: str, **kwargs: object) -> None:
            if filename == "a.pdf":
                raise RuntimeError("db down")

        monkeypatch.setattr(
            s3_module,
            "app",
            SimpleNamespace(
                ARTIFACT_MANAGER=SimpleNamespace(create_download_artifact=AsyncMock(side_effect=_create_row))
            ),
        )

        with pytest.raises(DownloadSaveIncompleteError) as raised:
            await s3_storage.save_downloaded_files(organization_id=TEST_ORGANIZATION_ID, run_id="wr_partial")

        assert raised.value.skipped_files == ["a.pdf"]

    async def test_repeat_save_only_uploads_new_and_changed_files(
        self, s3_storage: S3Storage, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A run's second cleanup must cost only its new files, not the whole download dir (SKY-14752)."""
        import skyvern.forge.sdk.artifact.storage.s3 as s3_module

        self._seed_run_dir(tmp_path, monkeypatch)
        run_dir = tmp_path / "downloads" / "wr_partial"
        uploaded: list[str] = []
        rows: dict[str, Artifact] = {}

        async def _upload(*, uri: str, file_path: str, **kwargs: object) -> None:
            uploaded.append(uri.rsplit("/", 1)[-1])

        async def _create_row(*, uri: str, checksum: str | None = None, **kwargs: object) -> str:
            artifact = (rows.get(uri) or _share_artifact(ArtifactType.DOWNLOAD, uri)).model_copy(
                update={"checksum": checksum}
            )
            rows[uri] = artifact
            return artifact.artifact_id

        async def _list_rows(**kwargs: object) -> list[Artifact]:
            return list(rows.values())

        monkeypatch.setattr(s3_storage.async_client, "upload_file_from_path", _upload)
        monkeypatch.setattr(
            s3_module,
            "app",
            SimpleNamespace(
                ARTIFACT_MANAGER=SimpleNamespace(create_download_artifact=_create_row),
                DATABASE=SimpleNamespace(artifacts=SimpleNamespace(list_artifacts_for_run_by_type=_list_rows)),
            ),
        )

        await s3_storage.save_downloaded_files(organization_id=TEST_ORGANIZATION_ID, run_id="wr_partial")
        assert sorted(uploaded) == ["a.pdf", "b.pdf"]

        uploaded.clear()
        (run_dir / "b.pdf").write_bytes(b"second, edited")
        (run_dir / "c.pdf").write_bytes(b"third")

        await s3_storage.save_downloaded_files(organization_id=TEST_ORGANIZATION_ID, run_id="wr_partial")

        assert sorted(uploaded) == ["b.pdf", "c.pdf"]
        assert len(rows) == 3


async def _settle(iterations: int = 100) -> None:
    """Drain the event loop's ready queue deterministically so create_task'd writes reach their
    chain-reservation/await points before the next reservation. Timing-independent (no wall-clock sleep
    that can flake under load), and it needs no production test-only hook — every write reserves its chain
    slot synchronously at task start, so a bounded set of no-op yields settles the intended interleaving.
    A task parked on an unresolved predecessor future stays parked, preserving the queued-behind ordering.
    """
    for _ in range(iterations):
        await asyncio.sleep(0)


def _recording_artifact(uri: str) -> Artifact:
    now = datetime.now(UTC)
    return Artifact(
        artifact_id="a_rec",
        artifact_type=ArtifactType.RECORDING,
        uri=uri,
        organization_id=TEST_ORGANIZATION_ID,
        task_id=TEST_TASK_ID,
        created_at=now,
        modified_at=now,
    )


@pytest.mark.asyncio
async def test_store_artifact_prefix_from_path_streams_bounded_reader(s3_storage: S3Storage, tmp_path: Path) -> None:
    src = tmp_path / "rec.webm"
    src.write_bytes(b"R" * 500)
    artifact = _recording_artifact(f"s3://{TEST_BUCKET}/k/rec.webm")

    captured: dict = {}

    async def _capture(
        uri: str,
        file_obj: object,
        storage_class: object = None,
        close_file_obj: bool = False,
        serialize_key: str | None = None,
    ) -> str:
        captured["uri"] = uri
        captured["close_file_obj"] = close_file_obj
        captured["serialize_key"] = serialize_key
        captured["data"] = file_obj.read()  # read inside the call; ownership/close is handed to the client
        return uri

    s3_storage.async_client = MagicMock()
    s3_storage.async_client.upload_file_stream = AsyncMock(side_effect=_capture)

    await s3_storage.store_artifact_prefix_from_path(artifact, str(src), 300)

    assert captured["uri"] == artifact.uri
    assert captured["data"] == b"R" * 300  # exactly the snapshot prefix, streamed
    assert captured["close_file_obj"] is True  # reader lifetime handed to the client, not the with-block
    assert captured["serialize_key"] == artifact.uri  # fenced against the terminal write to the same uri


@pytest.mark.asyncio
async def test_store_artifact_prefix_from_path_zst_falls_back_to_buffered(
    s3_storage: S3Storage, tmp_path: Path
) -> None:
    src = tmp_path / "data.zst"
    src.write_bytes(b"D" * 100)
    artifact = _recording_artifact(f"s3://{TEST_BUCKET}/k/data.zst")

    s3_storage.async_client = MagicMock()
    s3_storage.async_client.upload_file = AsyncMock(return_value=artifact.uri)
    s3_storage.async_client.upload_file_stream = AsyncMock()

    await s3_storage.store_artifact_prefix_from_path(artifact, str(src), 50)

    s3_storage.async_client.upload_file_stream.assert_not_awaited()
    s3_storage.async_client.upload_file.assert_awaited_once()  # buffered store_artifact (compressed) path


@pytest.mark.asyncio
async def test_store_artifact_prefix_reader_survives_caller_cancel_until_transfer_done(
    s3_storage: S3Storage, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """AsyncAWSClient shields/detaches the transfer, so a cancelled caller must NOT close the reader
    out from under the still-running transfer. The reader must stay open until the real transfer
    finishes, then close, and the detached transfer must still read the full snapshot."""
    src = tmp_path / "rec.webm"
    src.write_bytes(b"Z" * 400)
    artifact = _recording_artifact(f"s3://{TEST_BUCKET}/k/rec.webm")

    started = asyncio.Event()
    release = asyncio.Event()
    captured: dict = {}

    class _FakeClient:
        async def upload_fileobj(
            self, fileobj: object, bucket: str, key: str, ExtraArgs: dict | None = None, Config: object = None
        ) -> None:
            captured["fileobj"] = fileobj
            started.set()
            await release.wait()  # model an in-flight transfer that outlives the caller's cancel
            captured["read_after_cancel"] = fileobj.read()

    class _FakeCtx:
        async def __aenter__(self) -> _FakeClient:
            return _FakeClient()

        async def __aexit__(self, *exc: object) -> bool:
            return False

    monkeypatch.setattr(s3_storage.async_client, "_s3_client", lambda: _FakeCtx())

    task = asyncio.create_task(s3_storage.store_artifact_prefix_from_path(artifact, str(src), 400))
    await asyncio.wait_for(started.wait(), timeout=5)
    assert captured["fileobj"].closed is False  # open while the transfer is active

    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    # Caller has cancelled, but the shielded transfer is still active — the reader MUST remain open.
    assert captured["fileobj"].closed is False

    release.set()
    for _ in range(1000):
        if captured["fileobj"].closed:
            break
        await asyncio.sleep(0.005)

    assert captured["read_after_cancel"] == b"Z" * 400  # detached transfer streamed the full snapshot
    assert captured["fileobj"].closed is True  # closed only after the real transfer finished


@pytest.mark.asyncio
async def test_stale_prefix_cannot_overwrite_terminal_full_upload(
    s3_storage: S3Storage, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Data-integrity race: a per-step prefix transfer that is cancelled (30s barrier timeout) detaches
    and keeps running. The terminal full-recording upload then writes the SAME uri. The stale detached
    prefix must NOT land afterward and overwrite the finalized object with truncated bytes."""
    src = tmp_path / "rec.webm"
    src.write_bytes(b"P" * 200)  # truncated per-step prefix
    rec = _recording_artifact(f"s3://{TEST_BUCKET}/k/rec.webm")
    full = b"F" * 5000  # finalized full recording

    store: dict[str, bytes] = {}
    prefix_started = asyncio.Event()
    prefix_release = asyncio.Event()

    class _FakeClient:
        async def upload_fileobj(
            self, fileobj: object, bucket: str, key: str, ExtraArgs: dict | None = None, Config: object = None
        ) -> None:
            prefix_started.set()
            await prefix_release.wait()  # in-flight; still reading after the caller cancelled/detached
            store[key] = fileobj.read()

        async def put_object(
            self, Body: bytes = b"", Bucket: str = "", Key: str = "", StorageClass: str = "", **kw: object
        ) -> None:
            store[Key] = Body

    class _FakeCtx:
        async def __aenter__(self) -> _FakeClient:
            return _FakeClient()

        async def __aexit__(self, *exc: object) -> bool:
            return False

    monkeypatch.setattr(s3_storage.async_client, "_s3_client", lambda: _FakeCtx())
    key = S3Uri(rec.uri).key

    prefix_task = asyncio.create_task(s3_storage.store_artifact_prefix_from_path(rec, str(src), 200))
    await asyncio.wait_for(prefix_started.wait(), timeout=5)
    prefix_task.cancel()  # models wait_for_upload_aiotasks' 30s timeout cancelling the tracked task
    with pytest.raises(asyncio.CancelledError):
        await prefix_task

    terminal_task = asyncio.create_task(s3_storage.store_artifact(rec, full))
    await asyncio.sleep(0)  # give the terminal write a turn to attempt/queue
    prefix_release.set()  # let the detached stale prefix finish
    await asyncio.wait_for(terminal_task, timeout=5)
    for _ in range(200):  # let any detached prefix transfer settle
        await asyncio.sleep(0.005)
        if store.get(key) == b"P" * 200:
            break

    assert store[key] == full, "finalized full upload must win; a stale prefix must not overwrite it"
    # No unbounded chain/task registry: the per-key entry is dropped once the key goes idle.
    assert s3_storage.async_client._object_write_chains == {}


@pytest.mark.asyncio
async def test_store_artifact_serializes_only_recording_writes(
    s3_storage: S3Storage, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The terminal write path fences recordings by uri (so any terminal call site is covered) and
    leaves every other artifact type unserialized."""
    seen: dict = {}

    async def _capture(
        uri: str,
        data: bytes,
        storage_class: object = None,
        serialize_key: str | None = None,
        supersede_queued: bool = False,
    ) -> str:
        seen[uri] = (serialize_key, supersede_queued)
        return uri

    s3_storage.async_client = MagicMock()
    s3_storage.async_client.upload_file = AsyncMock(side_effect=_capture)

    rec = _recording_artifact(f"s3://{TEST_BUCKET}/k/rec.webm")
    other = rec.model_copy(update={"uri": f"s3://{TEST_BUCKET}/k/page.html", "artifact_type": ArtifactType.HTML})
    await s3_storage.store_artifact(rec, b"data", supersede_queued_prefixes=True)
    await s3_storage.store_artifact(other, b"data", supersede_queued_prefixes=True)

    # recording terminal write is fenced by uri and (as a finalize) seals queued prefixes;
    # a non-recording write is never fenced (serialize_key=None), so the seal flag is inert.
    assert seen[rec.uri] == (rec.uri, True)
    assert seen[other.uri][0] is None


@pytest.mark.asyncio
async def test_terminal_recording_write_survives_barrier_cancel_and_lands_last(
    s3_storage: S3Storage, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The 30s barrier cancels the QUEUED terminal task too, not just the prefix. The terminal recording
    write must still complete after the older prefix drains — otherwise the persisted recording is left
    truncated (the prefix) because the terminal put_object never ran."""
    src = tmp_path / "rec.webm"
    src.write_bytes(b"P" * 200)
    rec = _recording_artifact(f"s3://{TEST_BUCKET}/k/rec.webm")
    full = b"F" * 5000

    store: dict[str, bytes] = {}
    prefix_started = asyncio.Event()
    prefix_release = asyncio.Event()

    class _FakeClient:
        async def upload_fileobj(
            self, fileobj: object, bucket: str, key: str, ExtraArgs: dict | None = None, Config: object = None
        ) -> None:
            prefix_started.set()
            await prefix_release.wait()
            store[key] = fileobj.read()

        async def put_object(
            self, Body: bytes = b"", Bucket: str = "", Key: str = "", StorageClass: str = "", **kw: object
        ) -> None:
            store[Key] = Body

    class _FakeCtx:
        async def __aenter__(self) -> _FakeClient:
            return _FakeClient()

        async def __aexit__(self, *exc: object) -> bool:
            return False

    monkeypatch.setattr(s3_storage.async_client, "_s3_client", lambda: _FakeCtx())
    key = S3Uri(rec.uri).key

    prefix_task = asyncio.create_task(s3_storage.store_artifact_prefix_from_path(rec, str(src), 200))
    await asyncio.wait_for(prefix_started.wait(), timeout=5)
    prefix_task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await prefix_task

    terminal_task = asyncio.create_task(s3_storage.store_artifact(rec, full))
    await asyncio.sleep(0)  # terminal reaches the chain await, blocked behind the detached prefix
    terminal_task.cancel()  # the barrier cancels the queued terminal task as well
    with pytest.raises(asyncio.CancelledError):
        await terminal_task

    prefix_release.set()
    for _ in range(400):
        await asyncio.sleep(0.005)
        if store.get(key) == full:
            break

    assert store.get(key) == full, "terminal recording write must survive the barrier cancel and land last"
    assert s3_storage.async_client._object_write_chains == {}


@pytest.mark.asyncio
async def test_live_run_prefixes_all_upload_in_issue_order(
    s3_storage: S3Storage, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """No terminal write: every per-step prefix must upload, in issue order (no coalescing)."""
    uploaded: list[bytes] = []

    class _FakeClient:
        async def upload_fileobj(
            self, fileobj: object, bucket: str, key: str, ExtraArgs: dict | None = None, Config: object = None
        ) -> None:
            uploaded.append(fileobj.read())

    class _FakeCtx:
        async def __aenter__(self) -> _FakeClient:
            return _FakeClient()

        async def __aexit__(self, *exc: object) -> bool:
            return False

    monkeypatch.setattr(s3_storage.async_client, "_s3_client", lambda: _FakeCtx())
    rec = _recording_artifact(f"s3://{TEST_BUCKET}/k/rec.webm")

    for content in (b"A" * 10, b"B" * 20, b"C" * 30):
        p = tmp_path / f"{len(content)}.webm"
        p.write_bytes(content)
        await s3_storage.store_artifact_prefix_from_path(rec, str(p), len(content))

    assert uploaded == [b"A" * 10, b"B" * 20, b"C" * 30]
    assert _clean_write_state(s3_storage.async_client)


def _recording_fake_ctx(monkeypatch, s3_storage, client):  # type: ignore[no-untyped-def]
    class _FakeCtx:
        async def __aenter__(self):  # type: ignore[no-untyped-def]
            return client

        async def __aexit__(self, *exc: object) -> bool:
            return False

    monkeypatch.setattr(s3_storage.async_client, "_s3_client", lambda: _FakeCtx())


@pytest.mark.asyncio
async def test_terminal_finalize_suppresses_queued_prefixes_and_waits_only_for_active(
    s3_storage: S3Storage, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """prefix1 active, prefix2/prefix3 queued: the terminal finalize waits only for the active prefix1,
    suppresses the queued prefix2/prefix3, then lands last."""
    store: dict[str, bytes] = {}
    calls = {"fileobj": 0, "put": 0}
    p1_started = asyncio.Event()
    p1_release = asyncio.Event()

    class _FakeClient:
        async def upload_fileobj(
            self, fileobj: object, bucket: str, key: str, ExtraArgs: dict | None = None, Config: object = None
        ) -> None:
            calls["fileobj"] += 1
            p1_started.set()
            await p1_release.wait()
            store[key] = fileobj.read()

        async def put_object(
            self, Body: bytes = b"", Bucket: str = "", Key: str = "", StorageClass: str = "", **kw: object
        ) -> None:
            calls["put"] += 1
            store[Key] = Body

    _recording_fake_ctx(monkeypatch, s3_storage, _FakeClient())
    rec = _recording_artifact(f"s3://{TEST_BUCKET}/k/rec.webm")
    key = S3Uri(rec.uri).key
    for i in (1, 2, 3):
        (tmp_path / f"p{i}.webm").write_bytes(b"P" * (10 * i))

    p1 = asyncio.create_task(s3_storage.store_artifact_prefix_from_path(rec, str(tmp_path / "p1.webm"), 10))
    await asyncio.wait_for(p1_started.wait(), timeout=5)  # prefix1 is the active transfer
    p2 = asyncio.create_task(s3_storage.store_artifact_prefix_from_path(rec, str(tmp_path / "p2.webm"), 20))
    p3 = asyncio.create_task(s3_storage.store_artifact_prefix_from_path(rec, str(tmp_path / "p3.webm"), 30))
    await _settle()  # let prefix2/prefix3 queue behind prefix1
    term = asyncio.create_task(s3_storage.store_artifact(rec, b"F" * 5000, supersede_queued_prefixes=True))
    await _settle()  # terminal reserved + sealed
    p1_release.set()
    await asyncio.gather(p1, p2, p3, term)

    assert calls["fileobj"] == 1  # only the active prefix uploaded; queued prefix2/prefix3 were suppressed
    assert calls["put"] == 1
    assert store[key] == b"F" * 5000  # terminal landed last
    assert _clean_write_state(s3_storage.async_client)


@pytest.mark.asyncio
async def test_prefix_reserved_after_terminal_reserved_cannot_overwrite(
    s3_storage: S3Storage, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A prefix reserved while the terminal finalize is still in flight is superseded and never writes."""
    store: dict[str, bytes] = {}
    calls = {"fileobj": 0, "put": 0}
    term_started = asyncio.Event()
    term_release = asyncio.Event()

    class _FakeClient:
        async def upload_fileobj(
            self, fileobj: object, bucket: str, key: str, ExtraArgs: dict | None = None, Config: object = None
        ) -> None:
            calls["fileobj"] += 1
            store[key] = fileobj.read()

        async def put_object(
            self, Body: bytes = b"", Bucket: str = "", Key: str = "", StorageClass: str = "", **kw: object
        ) -> None:
            calls["put"] += 1
            term_started.set()
            await term_release.wait()
            store[Key] = Body

    _recording_fake_ctx(monkeypatch, s3_storage, _FakeClient())
    rec = _recording_artifact(f"s3://{TEST_BUCKET}/k/rec.webm")
    key = S3Uri(rec.uri).key
    (tmp_path / "late.webm").write_bytes(b"P" * 200)

    term = asyncio.create_task(s3_storage.store_artifact(rec, b"F" * 5000, supersede_queued_prefixes=True))
    await asyncio.wait_for(term_started.wait(), timeout=5)  # terminal in flight, key sealed
    late = asyncio.create_task(s3_storage.store_artifact_prefix_from_path(rec, str(tmp_path / "late.webm"), 200))
    await _settle()
    term_release.set()
    await asyncio.gather(term, late)

    assert calls["fileobj"] == 0  # the late prefix was superseded and never uploaded
    assert store[key] == b"F" * 5000
    assert _clean_write_state(s3_storage.async_client)


@pytest.mark.asyncio
async def test_active_prefix_failure_still_unblocks_terminal(
    s3_storage: S3Storage, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """If the active prefix transfer fails, it must still release the write chain so the terminal lands."""
    store: dict[str, bytes] = {}
    p1_started = asyncio.Event()
    p1_release = asyncio.Event()

    class _FakeClient:
        async def upload_fileobj(
            self, fileobj: object, bucket: str, key: str, ExtraArgs: dict | None = None, Config: object = None
        ) -> None:
            p1_started.set()
            await p1_release.wait()
            raise RuntimeError("prefix transfer boom")

        async def put_object(
            self, Body: bytes = b"", Bucket: str = "", Key: str = "", StorageClass: str = "", **kw: object
        ) -> None:
            store[Key] = Body

    _recording_fake_ctx(monkeypatch, s3_storage, _FakeClient())
    rec = _recording_artifact(f"s3://{TEST_BUCKET}/k/rec.webm")
    key = S3Uri(rec.uri).key
    (tmp_path / "p.webm").write_bytes(b"P" * 200)

    p1 = asyncio.create_task(s3_storage.store_artifact_prefix_from_path(rec, str(tmp_path / "p.webm"), 200))
    await asyncio.wait_for(p1_started.wait(), timeout=5)
    term = asyncio.create_task(s3_storage.store_artifact(rec, b"F" * 5000, supersede_queued_prefixes=True))
    await _settle()
    p1_release.set()  # active prefix now fails
    await asyncio.gather(p1, term)

    assert store[key] == b"F" * 5000  # terminal unblocked and landed despite the prefix failure
    assert _clean_write_state(s3_storage.async_client)


def _clean_write_state(client: object) -> bool:
    return (
        client._object_write_chains == {}
        and client._object_write_sealed == set()
        and client._object_write_fallback == {}
        and client._object_write_fallback_target == {}
    )


@pytest.mark.asyncio
async def test_terminal_failure_uploads_newest_queued_prefix_as_fallback(
    s3_storage: S3Storage, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """active P1 + queued P2/P3 + terminal failure => the newest queued snapshot (P3) lands as a
    fallback, P2 never uploads, and all per-key state drains."""
    store: dict[str, bytes] = {}
    uploaded: list[bytes] = []
    p1_started = asyncio.Event()
    p1_release = asyncio.Event()
    first = {"seen": False}

    class _FakeClient:
        async def upload_fileobj(
            self, fileobj: object, bucket: str, key: str, ExtraArgs: dict | None = None, Config: object = None
        ) -> None:
            if not first["seen"]:  # the active prefix P1
                first["seen"] = True
                p1_started.set()
                await p1_release.wait()
            data = fileobj.read()
            uploaded.append(data)
            store[key] = data

        async def put_object(
            self, Body: bytes = b"", Bucket: str = "", Key: str = "", StorageClass: str = "", **kw: object
        ) -> None:
            raise RuntimeError("terminal put_object failed")  # non-token failure, not retried

    _recording_fake_ctx(monkeypatch, s3_storage, _FakeClient())
    rec = _recording_artifact(f"s3://{TEST_BUCKET}/k/rec.webm")
    key = S3Uri(rec.uri).key
    (tmp_path / "p1.webm").write_bytes(b"1" * 10)
    (tmp_path / "p2.webm").write_bytes(b"2" * 20)
    (tmp_path / "p3.webm").write_bytes(b"3" * 30)

    p1 = asyncio.create_task(s3_storage.store_artifact_prefix_from_path(rec, str(tmp_path / "p1.webm"), 10))
    await asyncio.wait_for(p1_started.wait(), timeout=5)
    p2 = asyncio.create_task(s3_storage.store_artifact_prefix_from_path(rec, str(tmp_path / "p2.webm"), 20))
    p3 = asyncio.create_task(s3_storage.store_artifact_prefix_from_path(rec, str(tmp_path / "p3.webm"), 30))
    await _settle()  # P2 then P3 queue; P3 is the newest pre-terminal snapshot
    term = asyncio.create_task(s3_storage.store_artifact(rec, b"F" * 5000, supersede_queued_prefixes=True))
    await _settle()  # terminal reserved: seals, designates P3 the fallback
    p1_release.set()
    await asyncio.gather(p1, p2, p3, term)

    assert store[key] == b"3" * 30  # newest queued snapshot preserved after the terminal write failed
    assert uploaded == [b"1" * 10, b"3" * 30]  # active P1, then P3 fallback; P2 never uploaded
    assert _clean_write_state(s3_storage.async_client)


@pytest.mark.asyncio
async def test_fallback_upload_failure_still_drains_and_closes(
    s3_storage: S3Storage, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """If both the terminal write and the fallback upload fail, all per-key state still drains and the
    retained reader is closed (no leak, no unhandled exception)."""
    p1_started = asyncio.Event()
    p1_release = asyncio.Event()
    first = {"seen": False}
    readers: list[object] = []

    class _FakeClient:
        async def upload_fileobj(
            self, fileobj: object, bucket: str, key: str, ExtraArgs: dict | None = None, Config: object = None
        ) -> None:
            readers.append(fileobj)
            if not first["seen"]:
                first["seen"] = True
                p1_started.set()
                await p1_release.wait()
                fileobj.read()
                return
            raise RuntimeError("fallback upload failed")

        async def put_object(
            self, Body: bytes = b"", Bucket: str = "", Key: str = "", StorageClass: str = "", **kw: object
        ) -> None:
            raise RuntimeError("terminal put_object failed")

    _recording_fake_ctx(monkeypatch, s3_storage, _FakeClient())
    rec = _recording_artifact(f"s3://{TEST_BUCKET}/k/rec.webm")
    for name, n in (("p1", 10), ("p3", 30)):
        (tmp_path / f"{name}.webm").write_bytes(b"x" * n)

    p1 = asyncio.create_task(s3_storage.store_artifact_prefix_from_path(rec, str(tmp_path / "p1.webm"), 10))
    await asyncio.wait_for(p1_started.wait(), timeout=5)
    p3 = asyncio.create_task(s3_storage.store_artifact_prefix_from_path(rec, str(tmp_path / "p3.webm"), 30))
    await _settle()
    term = asyncio.create_task(s3_storage.store_artifact(rec, b"F" * 5000, supersede_queued_prefixes=True))
    await _settle()
    p1_release.set()
    await asyncio.gather(p1, p3, term)  # no exception escapes

    assert _clean_write_state(s3_storage.async_client)
    # the parked fallback reader (P3) was the second reader handed to the client and must be closed
    assert readers[-1].closed is True


@pytest.mark.asyncio
async def test_renamed_finalize_seals_prefix_key_and_supersedes_queued(
    s3_storage: S3Storage, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The dominant finalize compresses the .webm prefixes into a renamed .mp4 object. The terminal write
    must seal the OLD .webm key the prefixes queued to (so queued prefixes are superseded), while writing
    the new .mp4 object — not seal the .mp4 key that nothing queued to (SKY-15288, thread r3917658240)."""
    store: dict[str, bytes] = {}
    calls = {"fileobj": 0, "put": 0}
    p1_started = asyncio.Event()
    p1_release = asyncio.Event()

    class _FakeClient:
        async def upload_fileobj(
            self, fileobj: object, bucket: str, key: str, ExtraArgs: dict | None = None, Config: object = None
        ) -> None:
            calls["fileobj"] += 1
            p1_started.set()
            await p1_release.wait()
            store[key] = fileobj.read()

        async def put_object(
            self, Body: bytes = b"", Bucket: str = "", Key: str = "", StorageClass: str = "", **kw: object
        ) -> None:
            calls["put"] += 1
            store[Key] = Body

    _recording_fake_ctx(monkeypatch, s3_storage, _FakeClient())
    webm = _recording_artifact(f"s3://{TEST_BUCKET}/k/rec.webm")
    mp4 = webm.model_copy(update={"uri": f"s3://{TEST_BUCKET}/k/rec.mp4"})
    webm_key = S3Uri(webm.uri).key
    mp4_key = S3Uri(mp4.uri).key
    for i in (1, 2, 3):
        (tmp_path / f"p{i}.webm").write_bytes(b"P" * (10 * i))

    p1 = asyncio.create_task(s3_storage.store_artifact_prefix_from_path(webm, str(tmp_path / "p1.webm"), 10))
    await asyncio.wait_for(p1_started.wait(), timeout=5)  # prefix1 active on the .webm key
    p2 = asyncio.create_task(s3_storage.store_artifact_prefix_from_path(webm, str(tmp_path / "p2.webm"), 20))
    p3 = asyncio.create_task(s3_storage.store_artifact_prefix_from_path(webm, str(tmp_path / "p3.webm"), 30))
    await _settle()
    # Terminal finalize: writes the renamed .mp4 object, seals the .webm prefix key.
    term = asyncio.create_task(
        s3_storage.store_artifact(mp4, b"F" * 5000, supersede_queued_prefixes=True, prefix_uri=webm.uri)
    )
    await _settle()
    p1_release.set()
    await asyncio.gather(p1, p2, p3, term)

    assert calls["fileobj"] == 1  # only the active .webm prefix uploaded; queued p2/p3 superseded on the old key
    assert store[mp4_key] == b"F" * 5000  # finalized recording landed under the renamed .mp4 key
    assert store.get(webm_key) == b"P" * 10  # the one active prefix; no queued prefix drained afterward
    assert _clean_write_state(s3_storage.async_client)


@pytest.mark.asyncio
async def test_renamed_finalize_failure_arms_no_fallback(
    s3_storage: S3Storage, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """When the finalize renames the object, a parked .webm prefix could never be served under the .mp4
    row, so the terminal must NOT arm a fallback. On terminal failure the newest queued prefix is
    superseded like the rest (contrast test_terminal_failure_uploads_newest_queued_prefix_as_fallback,
    the same-key case where it IS the fallback)."""
    store: dict[str, bytes] = {}
    uploaded: list[bytes] = []
    p1_started = asyncio.Event()
    p1_release = asyncio.Event()
    first = {"seen": False}

    class _FakeClient:
        async def upload_fileobj(
            self, fileobj: object, bucket: str, key: str, ExtraArgs: dict | None = None, Config: object = None
        ) -> None:
            if not first["seen"]:
                first["seen"] = True
                p1_started.set()
                await p1_release.wait()
            data = fileobj.read()
            uploaded.append(data)
            store[key] = data

        async def put_object(
            self, Body: bytes = b"", Bucket: str = "", Key: str = "", StorageClass: str = "", **kw: object
        ) -> None:
            raise RuntimeError("terminal put_object failed")

    _recording_fake_ctx(monkeypatch, s3_storage, _FakeClient())
    webm = _recording_artifact(f"s3://{TEST_BUCKET}/k/rec.webm")
    mp4 = webm.model_copy(update={"uri": f"s3://{TEST_BUCKET}/k/rec.mp4"})
    (tmp_path / "p1.webm").write_bytes(b"1" * 10)
    (tmp_path / "p3.webm").write_bytes(b"3" * 30)

    p1 = asyncio.create_task(s3_storage.store_artifact_prefix_from_path(webm, str(tmp_path / "p1.webm"), 10))
    await asyncio.wait_for(p1_started.wait(), timeout=5)
    p3 = asyncio.create_task(s3_storage.store_artifact_prefix_from_path(webm, str(tmp_path / "p3.webm"), 30))
    await _settle()
    term = asyncio.create_task(
        s3_storage.store_artifact(mp4, b"F" * 5000, supersede_queued_prefixes=True, prefix_uri=webm.uri)
    )
    await _settle()
    p1_release.set()
    await asyncio.gather(p1, p3, term)

    assert uploaded == [b"1" * 10]  # only the active prefix; no meaningless .webm fallback on rename
    assert _clean_write_state(s3_storage.async_client)


@pytest.mark.asyncio
async def test_stream_upload_bounds_resident_memory_with_transfer_config(
    s3_storage: S3Storage, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """upload_fileobj must receive a TransferConfig that caps s3transfer's io queue, so a streamed
    prefix's resident memory is a fixed queue-depth x chunk-size regardless of prefix size
    (SKY-15288, thread r3918986929)."""
    seen: list[object] = []

    class _FakeClient:
        async def upload_fileobj(
            self, fileobj: object, bucket: str, key: str, ExtraArgs: dict | None = None, Config: object = None
        ) -> None:
            seen.append(Config)
            fileobj.read()

    _recording_fake_ctx(monkeypatch, s3_storage, _FakeClient())
    rec = _recording_artifact(f"s3://{TEST_BUCKET}/k/rec.webm")

    small = tmp_path / "small.webm"
    small.write_bytes(b"s" * 100)
    large = tmp_path / "large.webm"
    large.write_bytes(b"l" * 100_000)
    await s3_storage.store_artifact_prefix_from_path(rec, str(small), 100)
    await s3_storage.store_artifact_prefix_from_path(rec, str(large), 100_000)

    assert len(seen) == 2
    assert all(cfg is not None for cfg in seen)
    # size-independent: the same bounded queue depth for a 100B and a 100KB prefix
    assert {cfg.max_io_queue_size for cfg in seen} == {_STREAM_UPLOAD_IO_QUEUE_DEPTH}
