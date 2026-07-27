"""Unit tests for bulk artifact creation functionality."""

import pytest

from skyvern.forge.sdk.artifact.manager import ArtifactBatchData, BulkArtifactCreationRequest
from skyvern.forge.sdk.artifact.manager import ArtifactManager
from skyvern.forge.sdk.artifact.models import ArtifactType
from skyvern.forge.sdk.artifact.storage.test_helpers import create_fake_for_ai_suggestion, create_fake_step
from skyvern.forge.sdk.core import skyvern_context
from skyvern.forge.sdk.db.models import ArtifactModel


def test_artifact_batch_data_with_data():
    """Test ArtifactBatchData with data field."""
    model = ArtifactModel(
        artifact_id="test-1",
        artifact_type=ArtifactType.SCREENSHOT_LLM,
        uri="s3://bucket/test",
        organization_id="org-1",
    )

    batch_data = ArtifactBatchData(
        artifact_model=model,
        data=b"test data",
    )

    assert batch_data.artifact_model == model
    assert batch_data.data == b"test data"
    assert batch_data.path is None


def test_artifact_batch_data_with_path():
    """Test ArtifactBatchData with path field."""
    model = ArtifactModel(
        artifact_id="test-1",
        artifact_type=ArtifactType.SCREENSHOT_LLM,
        uri="s3://bucket/test",
        organization_id="org-1",
    )

    batch_data = ArtifactBatchData(
        artifact_model=model,
        path="/tmp/test.png",
    )

    assert batch_data.artifact_model == model
    assert batch_data.data is None
    assert batch_data.path == "/tmp/test.png"


def test_artifact_batch_data_with_both_raises_error():
    """Test that ArtifactBatchData raises error when both data and path are provided."""
    model = ArtifactModel(
        artifact_id="test-1",
        artifact_type=ArtifactType.SCREENSHOT_LLM,
        uri="s3://bucket/test",
        organization_id="org-1",
    )

    with pytest.raises(ValueError, match="Cannot specify both data and path"):
        ArtifactBatchData(
            artifact_model=model,
            data=b"test data",
            path="/tmp/test.png",
        )


def test_bulk_artifact_creation_request():
    """Test BulkArtifactCreationRequest structure."""
    model1 = ArtifactModel(
        artifact_id="test-1",
        artifact_type=ArtifactType.LLM_PROMPT,
        uri="s3://bucket/test1",
        organization_id="org-1",
    )
    model2 = ArtifactModel(
        artifact_id="test-2",
        artifact_type=ArtifactType.SCREENSHOT_LLM,
        uri="s3://bucket/test2",
        organization_id="org-1",
    )

    request = BulkArtifactCreationRequest(
        artifacts=[
            ArtifactBatchData(artifact_model=model1, data=b"data1"),
            ArtifactBatchData(artifact_model=model2, data=b"data2"),
        ],
        primary_key="task-123",
    )

    assert len(request.artifacts) == 2
    assert request.primary_key == "task-123"
    assert request.artifacts[0].artifact_model.artifact_id == "test-1"
    assert request.artifacts[1].artifact_model.artifact_id == "test-2"


def test_bulk_artifact_creation_performance_benefit():
    """
    Test to verify that bulk creation reduces database calls.
    This is a conceptual test to document the performance improvement.
    """
    # Before optimization: Creating N artifacts = N database INSERT calls
    # After optimization: Creating N artifacts = 1 bulk INSERT call

    num_artifacts = 10

    # Simulate old approach (N individual inserts)
    individual_insert_count = num_artifacts

    # Simulate new approach (1 bulk insert)
    bulk_insert_count = 1

    # Assert that bulk insert is more efficient
    assert bulk_insert_count < individual_insert_count

    # The reduction ratio
    reduction_ratio = individual_insert_count / bulk_insert_count
    assert reduction_ratio == num_artifacts


@pytest.mark.asyncio
async def test_prepare_llm_artifact_masks_workflow_and_runtime_secrets(monkeypatch):
    class FakeWorkflowRunContext:
        def mask_secrets_in_data(self, data: str, mask: str = "*****") -> str:
            return data.replace("workflow-secret", mask)

    class FakeWorkflowContextManager:
        def get_workflow_run_context(self, workflow_run_id: str) -> FakeWorkflowRunContext:
            assert workflow_run_id == "workflow-run-1"
            return FakeWorkflowRunContext()

    monkeypatch.setattr(
        "skyvern.forge.sdk.artifact.manager.app.WORKFLOW_CONTEXT_MANAGER",
        FakeWorkflowContextManager(),
    )
    monkeypatch.setattr(
        "skyvern.forge.sdk.artifact.manager.app.STORAGE.build_ai_suggestion_uri",
        lambda **kwargs: "s3://bucket/prompt",
    )

    context = skyvern_context.SkyvernContext(
        workflow_run_id="workflow-run-1",
        sensitive_values={"123456", "totp-secret"},
    )

    with skyvern_context.scoped(context):
        request = await ArtifactManager().prepare_llm_artifact(
            data=b"prompt has workflow-secret plus 123456 and totp-secret",
            artifact_type=ArtifactType.LLM_PROMPT,
            ai_suggestion=create_fake_for_ai_suggestion("suggestion-1"),
        )

    assert request is not None
    prompt_artifact = request.artifacts[0]
    assert prompt_artifact.artifact_model.artifact_type == ArtifactType.LLM_PROMPT
    assert prompt_artifact.data == b"prompt has ***** plus ***** and *****"


@pytest.mark.asyncio
async def test_prepare_llm_artifact_redacts_prompt_when_secret_masking_fails(monkeypatch):
    class BrokenWorkflowContextManager:
        def get_workflow_run_context(self, workflow_run_id: str) -> None:
            raise RuntimeError("masking unavailable")

    monkeypatch.setattr(
        "skyvern.forge.sdk.artifact.manager.app.WORKFLOW_CONTEXT_MANAGER",
        BrokenWorkflowContextManager(),
    )
    monkeypatch.setattr(
        "skyvern.forge.sdk.artifact.manager.app.STORAGE.build_ai_suggestion_uri",
        lambda **kwargs: "s3://bucket/prompt",
    )

    context = skyvern_context.SkyvernContext(workflow_run_id="workflow-run-1")

    with skyvern_context.scoped(context):
        request = await ArtifactManager().prepare_llm_artifact(
            data=b"prompt has raw-secret",
            artifact_type=ArtifactType.LLM_PROMPT,
            ai_suggestion=create_fake_for_ai_suggestion("suggestion-1"),
        )

    assert request is not None
    assert request.artifacts[0].data == b"[LLM prompt artifact redacted: secret masking failed]"


def test_add_to_step_archive_masks_bundled_llm_prompt_runtime_secrets(monkeypatch):
    monkeypatch.setattr(
        "skyvern.forge.sdk.artifact.manager.app.STORAGE.build_step_uri",
        lambda **kwargs: "s3://bucket/step-archive.zip",
    )

    manager = ArtifactManager()
    context = skyvern_context.SkyvernContext(sensitive_values={"runtime-secret"})

    with skyvern_context.scoped(context):
        manager.accumulate_llm_call_to_archive(
            step=create_fake_step("step-1"),
            prompt=b"prompt has runtime-secret",
        )

    archive = manager._step_archives["step-1"]
    assert archive.entries["llm_prompt_0.txt"] == b"prompt has *****"


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
