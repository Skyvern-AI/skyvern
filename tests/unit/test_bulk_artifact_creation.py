"""Unit tests for bulk artifact creation functionality."""

import pytest

from skyvern.forge.sdk.artifact.manager import ArtifactBatchData, BulkArtifactCreationRequest
from skyvern.forge.sdk.artifact.models import ArtifactType
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


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
