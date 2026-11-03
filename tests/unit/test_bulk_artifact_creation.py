"""ArtifactBatchData must reject an upload that names both bytes and a path."""

import pytest

from skyvern.forge.sdk.artifact.manager import ArtifactBatchData
from skyvern.forge.sdk.artifact.models import ArtifactType
from skyvern.forge.sdk.db.models import ArtifactModel


def test_artifact_batch_data_with_both_raises_error() -> None:
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
