import asyncio
from datetime import UTC, datetime
from unittest.mock import AsyncMock, patch

import pytest

from skyvern.forge.sdk.artifact.manager import ArtifactManager
from skyvern.forge.sdk.artifact.models import Artifact, ArtifactType


def _make_recording_artifact(uri: str) -> Artifact:
    now = datetime.now(UTC)
    return Artifact(
        artifact_id="a_recording",
        artifact_type=ArtifactType.RECORDING,
        uri=uri,
        organization_id="org_1",
        task_id="task_1",
        created_at=now,
        modified_at=now,
    )


@pytest.mark.asyncio
async def test_update_recording_artifact_data_rewrites_uri_for_prepared_extension() -> None:
    manager = ArtifactManager()
    original = _make_recording_artifact("s3://bucket/path/recording.webm")
    updated = original.model_copy(update={"uri": "s3://bucket/path/recording.mp4", "file_size": 9})

    with patch("skyvern.forge.sdk.artifact.manager.app") as mock_app:
        mock_app.DATABASE.artifacts.get_artifact_by_id = AsyncMock(return_value=original)
        mock_app.DATABASE.artifacts.update_artifact_uri = AsyncMock(return_value=updated)
        mock_app.STORAGE.store_artifact = AsyncMock()

        await manager.update_artifact_data(
            artifact_id=original.artifact_id,
            organization_id=original.organization_id,
            data=b"mp4-bytes",
            file_extension="mp4",
        )
        await asyncio.gather(*manager.upload_aiotasks_map["task_1"])

    mock_app.DATABASE.artifacts.update_artifact_uri.assert_awaited_once_with(
        artifact_id=original.artifact_id,
        organization_id=original.organization_id,
        uri="s3://bucket/path/recording.mp4",
        file_size=9,
    )
    mock_app.STORAGE.store_artifact.assert_awaited_once_with(updated, b"mp4-bytes")


@pytest.mark.asyncio
async def test_update_recording_artifact_data_same_extension_updates_file_size() -> None:
    manager = ArtifactManager()
    artifact = _make_recording_artifact("s3://bucket/path/recording.webm")
    updated = artifact.model_copy(update={"file_size": 10})

    with patch("skyvern.forge.sdk.artifact.manager.app") as mock_app:
        mock_app.DATABASE.artifacts.get_artifact_by_id = AsyncMock(return_value=artifact)
        mock_app.DATABASE.artifacts.update_artifact_uri = AsyncMock(return_value=updated)
        mock_app.STORAGE.store_artifact = AsyncMock()

        await manager.update_artifact_data(
            artifact_id=artifact.artifact_id,
            organization_id=artifact.organization_id,
            data=b"webm-bytes",
            file_extension="webm",
        )
        await asyncio.gather(*manager.upload_aiotasks_map["task_1"])

    mock_app.DATABASE.artifacts.update_artifact_uri.assert_awaited_once_with(
        artifact_id=artifact.artifact_id,
        organization_id=artifact.organization_id,
        uri=artifact.uri,
        file_size=10,
    )
    mock_app.STORAGE.store_artifact.assert_awaited_once_with(updated, b"webm-bytes")


@pytest.mark.asyncio
async def test_update_recording_artifact_data_skips_db_update_when_uri_and_size_unchanged() -> None:
    manager = ArtifactManager()
    artifact = _make_recording_artifact("s3://bucket/path/recording.webm").model_copy(update={"file_size": 10})

    with patch("skyvern.forge.sdk.artifact.manager.app") as mock_app:
        mock_app.DATABASE.artifacts.get_artifact_by_id = AsyncMock(return_value=artifact)
        mock_app.DATABASE.artifacts.update_artifact_uri = AsyncMock()
        mock_app.STORAGE.store_artifact = AsyncMock()

        await manager.update_artifact_data(
            artifact_id=artifact.artifact_id,
            organization_id=artifact.organization_id,
            data=b"webm-bytes",
            file_extension="webm",
        )
        await asyncio.gather(*manager.upload_aiotasks_map["task_1"])

    mock_app.DATABASE.artifacts.update_artifact_uri.assert_not_awaited()
    mock_app.STORAGE.store_artifact.assert_awaited_once_with(artifact, b"webm-bytes")


@pytest.mark.asyncio
async def test_update_recording_artifact_data_fails_when_metadata_update_returns_none() -> None:
    manager = ArtifactManager()
    artifact = _make_recording_artifact("s3://bucket/path/recording.webm")

    with patch("skyvern.forge.sdk.artifact.manager.app") as mock_app:
        mock_app.DATABASE.artifacts.get_artifact_by_id = AsyncMock(return_value=artifact)
        mock_app.DATABASE.artifacts.update_artifact_uri = AsyncMock(return_value=None)
        mock_app.STORAGE.store_artifact = AsyncMock()

        with pytest.raises(RuntimeError, match="Failed to update recording artifact metadata"):
            await manager.update_artifact_data(
                artifact_id=artifact.artifact_id,
                organization_id=artifact.organization_id,
                data=b"mp4-bytes",
                file_extension="mp4",
            )

    mock_app.STORAGE.store_artifact.assert_not_awaited()


@pytest.mark.asyncio
async def test_update_recording_artifact_data_logs_store_task_failure() -> None:
    manager = ArtifactManager()
    artifact = _make_recording_artifact("s3://bucket/path/recording.webm")
    updated = artifact.model_copy(update={"uri": "s3://bucket/path/recording.mp4", "file_size": 9})

    with (
        patch("skyvern.forge.sdk.artifact.manager.app") as mock_app,
        patch("skyvern.forge.sdk.artifact.manager.LOG") as mock_log,
    ):
        mock_app.DATABASE.artifacts.get_artifact_by_id = AsyncMock(return_value=artifact)
        mock_app.DATABASE.artifacts.update_artifact_uri = AsyncMock(return_value=updated)
        mock_app.STORAGE.store_artifact = AsyncMock(side_effect=RuntimeError("upload failed"))

        await manager.update_artifact_data(
            artifact_id=artifact.artifact_id,
            organization_id=artifact.organization_id,
            data=b"mp4-bytes",
            file_extension="mp4",
        )
        await asyncio.gather(*manager.upload_aiotasks_map["task_1"], return_exceptions=True)

    assert any(
        call.args == ("Artifact store task failed",)
        and call.kwargs["artifact_id"] == updated.artifact_id
        and call.kwargs["uri"] == updated.uri
        for call in mock_log.warning.call_args_list
    )
