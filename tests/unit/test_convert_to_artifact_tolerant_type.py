import asyncio
from datetime import datetime, timezone
from unittest.mock import AsyncMock, patch

import pytest

from skyvern.forge.sdk.artifact.models import ArtifactType
from skyvern.forge.sdk.db.models import ArtifactModel
from skyvern.forge.sdk.db.utils import convert_to_artifact


def _model(artifact_type: str) -> ArtifactModel:
    now = datetime.now(timezone.utc)
    return ArtifactModel(
        artifact_id="art_1",
        artifact_type=artifact_type,
        uri="s3://bucket/art_1",
        organization_id="org_1",
        created_at=now,
        modified_at=now,
    )


def test_known_type_round_trips() -> None:
    assert convert_to_artifact(_model("html_action")).artifact_type is ArtifactType.HTML_ACTION


def test_type_written_by_a_newer_image_does_not_break_the_read() -> None:
    # A rolling deploy has old pods listing rows written by new pods; the listing must not 500.
    artifact = convert_to_artifact(_model("html_type_from_the_future"))
    assert artifact.artifact_type is ArtifactType.UNKNOWN
    assert artifact.artifact_id == "art_1"


def test_unknown_type_is_treated_as_sensitive_for_signed_urls() -> None:
    from skyvern.forge.sdk.artifact.signing import SENSITIVE_ARTIFACT_TYPES

    assert ArtifactType.UNKNOWN in SENSITIVE_ARTIFACT_TYPES


@pytest.mark.asyncio
async def test_update_artifact_data_refuses_an_unknown_type_row() -> None:
    # The one write path that reads the type back from the row: this image cannot tell whether the
    # row is redactable, so it must write nothing rather than write unredacted bytes.
    from skyvern.forge.sdk.artifact.manager import ArtifactManager
    from skyvern.forge.sdk.artifact.models import Artifact

    now = datetime.now(timezone.utc)
    row = Artifact(
        artifact_id="art_future",
        artifact_type=ArtifactType.UNKNOWN,
        uri="s3://bucket/art_future",
        organization_id="org_1",
        task_id="task_1",
        created_at=now,
        modified_at=now,
    )
    manager = ArtifactManager()
    with patch("skyvern.forge.sdk.artifact.manager.app") as mock_app:
        mock_app.DATABASE.artifacts.get_artifact_by_id = AsyncMock(return_value=row)
        mock_app.DATABASE.artifacts.update_artifact_uri = AsyncMock()
        mock_app.STORAGE.store_artifact = AsyncMock()
        result = await manager.update_artifact_data(
            artifact_id=row.artifact_id, organization_id=row.organization_id, data=b"secret-bearing bytes"
        )
        await asyncio.gather(*manager.upload_aiotasks_map.get("task_1", []))
    assert result is None
    mock_app.STORAGE.store_artifact.assert_not_awaited()
    mock_app.DATABASE.artifacts.update_artifact_uri.assert_not_awaited()
