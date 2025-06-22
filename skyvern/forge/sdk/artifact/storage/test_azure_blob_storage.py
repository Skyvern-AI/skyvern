from datetime import datetime
from pathlib import Path
from typing import Generator

import pytest
from freezegun import freeze_time

from skyvern.config import settings
from skyvern.forge.sdk.api.azure import AzureBlobTier
from skyvern.forge.sdk.artifact.models import Artifact, ArtifactType, LogEntityType
from skyvern.forge.sdk.artifact.storage.azure_blob import AzureBlobStorage
from skyvern.forge.sdk.artifact.storage.test_helpers import (
    create_fake_for_ai_suggestion,
    create_fake_step,
    create_fake_task_v2,
    create_fake_thought,
    create_fake_workflow_run_block,
)
from skyvern.forge.sdk.db.id import generate_artifact_id

# Test constants
TEST_CONTAINER = "test-skyvern-container"
TEST_ORGANIZATION_ID = "test-org-123"
TEST_TASK_ID = "tsk_123456789"
TEST_STEP_ID = "step_123456789"
TEST_WORKFLOW_RUN_ID = "wfr_123456789"
TEST_BLOCK_ID = "block_123456789"
TEST_AI_SUGGESTION_ID = "ai_sugg_test_123"


class AzureBlobStorageForTests(AzureBlobStorage):
    async def _get_tags_for_org(self, organization_id: str) -> dict[str, str]:
        return {"environment": "test", "org": organization_id}

    async def _get_blob_tier_for_org(self, organization_id: str) -> AzureBlobTier:
        return AzureBlobTier.COOL


@pytest.fixture
def azure_blob_storage() -> AzureBlobStorage:
    # This would need to be configured with test Azure credentials
    # For unit tests, you might want to mock the AsyncAzureClient
    return AzureBlobStorageForTests(
        container_artifacts=TEST_CONTAINER,
        container_screenshots=TEST_CONTAINER,
        container_browser_sessions=TEST_CONTAINER,
        container_uploads=TEST_CONTAINER,
    )


@freeze_time("2025-06-09T12:00:00")
class TestAzureBlobStorageBuildURIs:
    def test_build_uri(self, azure_blob_storage: AzureBlobStorage) -> None:
        step = create_fake_step(TEST_TASK_ID, 2, 1, TEST_STEP_ID, TEST_ORGANIZATION_ID)
        uri = azure_blob_storage.build_uri(
            organization_id=TEST_ORGANIZATION_ID,
            artifact_id="artifact123",
            step=step,
            artifact_type=ArtifactType.SCREENSHOT,
        )
        assert (
            uri
            == f"azure://{TEST_CONTAINER}/v1/{settings.ENV}/{TEST_ORGANIZATION_ID}/{TEST_TASK_ID}/02_1_{TEST_STEP_ID}/2025-06-09T12:00:00_artifact123_screenshot.png"
        )

    def test_build_log_uri(self, azure_blob_storage: AzureBlobStorage) -> None:
        uri = azure_blob_storage.build_log_uri(
            organization_id=TEST_ORGANIZATION_ID,
            log_entity_type=LogEntityType.WORKFLOW_RUN,
            log_entity_id=TEST_WORKFLOW_RUN_ID,
            artifact_type=ArtifactType.WORKFLOW_RUN_LOG,
        )
        assert (
            uri
            == f"azure://{TEST_CONTAINER}/v1/{settings.ENV}/{TEST_ORGANIZATION_ID}/logs/workflow_run/{TEST_WORKFLOW_RUN_ID}/2025-06-09T12:00:00_workflow_run_log.log"
        )

    def test_build_thought_uri(self, azure_blob_storage: AzureBlobStorage) -> None:
        thought = create_fake_thought("cruise123", "thought456")
        uri = azure_blob_storage.build_thought_uri(
            organization_id=TEST_ORGANIZATION_ID,
            artifact_id="artifact123",
            thought=thought,
            artifact_type=ArtifactType.SCREENSHOT_FINAL,
        )
        assert (
            uri
            == f"azure://{TEST_CONTAINER}/v1/{settings.ENV}/{TEST_ORGANIZATION_ID}/observers/cruise123/thought456/2025-06-09T12:00:00_artifact123_screenshot_final.png"
        )

    def test_build_task_v2_uri(self, azure_blob_storage: AzureBlobStorage) -> None:
        task_v2 = create_fake_task_v2("cruise123")
        uri = azure_blob_storage.build_task_v2_uri(
            organization_id=TEST_ORGANIZATION_ID,
            artifact_id="artifact123",
            task_v2=task_v2,
            artifact_type=ArtifactType.HTML_ACTION,
        )
        assert (
            uri
            == f"azure://{TEST_CONTAINER}/v1/{settings.ENV}/{TEST_ORGANIZATION_ID}/observers/cruise123/2025-06-09T12:00:00_artifact123_html_action.html"
        )

    def test_build_workflow_run_block_uri(self, azure_blob_storage: AzureBlobStorage) -> None:
        workflow_run_block = create_fake_workflow_run_block(TEST_WORKFLOW_RUN_ID, TEST_BLOCK_ID)
        uri = azure_blob_storage.build_workflow_run_block_uri(
            organization_id=TEST_ORGANIZATION_ID,
            artifact_id="artifact123",
            workflow_run_block=workflow_run_block,
            artifact_type=ArtifactType.HAR,
        )
        assert (
            uri
            == f"azure://{TEST_CONTAINER}/v1/{settings.ENV}/{TEST_ORGANIZATION_ID}/workflow_runs/{TEST_WORKFLOW_RUN_ID}/{TEST_BLOCK_ID}/2025-06-09T12:00:00_artifact123_har.har"
        )

    def test_build_ai_suggestion_uri(self, azure_blob_storage: AzureBlobStorage) -> None:
        ai_suggestion = create_fake_for_ai_suggestion(TEST_AI_SUGGESTION_ID)
        uri = azure_blob_storage.build_ai_suggestion_uri(
            organization_id=TEST_ORGANIZATION_ID,
            artifact_id="artifact123",
            ai_suggestion=ai_suggestion,
            artifact_type=ArtifactType.SCREENSHOT_LLM,
        )
        assert (
            uri
            == f"azure://{TEST_CONTAINER}/v1/{settings.ENV}/{TEST_ORGANIZATION_ID}/ai_suggestions/{TEST_AI_SUGGESTION_ID}/2025-06-09T12:00:00_artifact123_screenshot_llm.png"
        )


# Note: For actual integration tests with Azure Blob Storage, you would need:
# 1. Azure Storage Emulator or a test Azure account
# 2. Proper mocking of the AsyncAzureClient
# 3. Additional test cases for store/retrieve operations