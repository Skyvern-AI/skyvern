import pytest
from freezegun import freeze_time

from skyvern.config import settings
from skyvern.forge.sdk.artifact.models import ArtifactType, LogEntityType
from skyvern.forge.sdk.artifact.storage.s3 import S3Storage
from skyvern.forge.sdk.artifact.storage.test_helpers import (
    create_fake_for_ai_suggestion,
    create_fake_step,
    create_fake_task_v2,
    create_fake_thought,
    create_fake_workflow_run_block,
)

# Test constants
TEST_BUCKET = "test-skyvern-bucket"
TEST_ORGANIZATION_ID = "test-org-123"
TEST_TASK_ID = "tsk_123456789"
TEST_STEP_ID = "step_123456789"
TEST_WORKFLOW_RUN_ID = "wfr_123456789"
TEST_BLOCK_ID = "block_123456789"
TEST_AI_SUGGESTION_ID = "ai_sugg_test_123"


@pytest.fixture
def s3_storage() -> S3Storage:
    return S3Storage(bucket=TEST_BUCKET)


@freeze_time("2025-06-09T12:00:00")
class TestS3StorageBuildURIs:
    def test_build_uri(self, s3_storage: S3Storage) -> None:
        step = create_fake_step(TEST_STEP_ID)
        uri = s3_storage.build_uri("artifact123", step, ArtifactType.LLM_PROMPT)
        assert (
            uri
            == f"s3://{TEST_BUCKET}/{settings.ENV}/{TEST_TASK_ID}/01_0_{TEST_STEP_ID}/2025-06-09T12:00:00_artifact123_llm_prompt.txt"
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
            == f"s3://{TEST_BUCKET}/{settings.ENV}/{TEST_ORGANIZATION_ID}/logs/workflow_run_block/log_id/2025-06-09T12:00:00_skyvern_log.log"
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
            == f"s3://{TEST_BUCKET}/{settings.ENV}/{TEST_ORGANIZATION_ID}/observers/cruise123/thought123/2025-06-09T12:00:00_artifact123_visible_elements_tree.json"
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
            == f"s3://{TEST_BUCKET}/{settings.ENV}/{TEST_ORGANIZATION_ID}/observers/cruise123/2025-06-09T12:00:00_artifact123_html_action.html"
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
            == f"s3://{TEST_BUCKET}/{settings.ENV}/{TEST_ORGANIZATION_ID}/workflow_runs/{TEST_WORKFLOW_RUN_ID}/{TEST_BLOCK_ID}/2025-06-09T12:00:00_artifact123_har.har"
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
            == f"s3://{TEST_BUCKET}/{settings.ENV}/{TEST_ORGANIZATION_ID}/ai_suggestions/{TEST_AI_SUGGESTION_ID}/2025-06-09T12:00:00_artifact123_screenshot_llm.png"
        )
