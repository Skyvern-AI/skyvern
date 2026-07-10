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
from skyvern.forge.sdk.api.aws import S3StorageClass, S3Uri
from skyvern.forge.sdk.artifact.manager import ArtifactManager
from skyvern.forge.sdk.artifact.models import Artifact, ArtifactType, LogEntityType
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
