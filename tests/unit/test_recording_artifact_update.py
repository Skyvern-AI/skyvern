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
            supersede_queued_prefixes=True,
        )
        await asyncio.gather(*manager.upload_aiotasks_map["task_1"])

    mock_app.DATABASE.artifacts.update_artifact_uri.assert_awaited_once_with(
        artifact_id=original.artifact_id,
        organization_id=original.organization_id,
        uri="s3://bucket/path/recording.mp4",
        file_size=9,
    )
    # The finalize renamed .webm -> .mp4, so the terminal write must seal the OLD .webm key its per-step
    # prefixes queued to (prefix_uri), not the new .mp4 key nothing queued to (SKY-15288, thread r3917658240).
    mock_app.STORAGE.store_artifact.assert_awaited_once_with(
        updated, b"mp4-bytes", supersede_queued_prefixes=True, prefix_uri=original.uri
    )


@pytest.mark.asyncio
async def test_update_recording_artifact_data_defaults_to_not_superseding() -> None:
    """update_artifact_data is generic (also the mid-step byte fallback), so it must default to NOT
    sealing/superseding queued prefixes — only explicit terminal callers pass the flag."""
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

    # No rename (.webm -> .webm), so there is no distinct prefix key to seal: prefix_uri stays None.
    mock_app.STORAGE.store_artifact.assert_awaited_once_with(
        artifact, b"webm-bytes", supersede_queued_prefixes=False, prefix_uri=None
    )


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
            supersede_queued_prefixes=True,
        )
        await asyncio.gather(*manager.upload_aiotasks_map["task_1"])

    mock_app.DATABASE.artifacts.update_artifact_uri.assert_awaited_once_with(
        artifact_id=artifact.artifact_id,
        organization_id=artifact.organization_id,
        uri=artifact.uri,
        file_size=10,
    )
    # Same-extension finalize (.webm -> .webm): the write object and the prefix key coincide, so
    # prefix_uri stays None and store_artifact serializes on its own uri.
    mock_app.STORAGE.store_artifact.assert_awaited_once_with(
        updated, b"webm-bytes", supersede_queued_prefixes=True, prefix_uri=None
    )


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
            supersede_queued_prefixes=True,
        )
        await asyncio.gather(*manager.upload_aiotasks_map["task_1"])

    mock_app.DATABASE.artifacts.update_artifact_uri.assert_not_awaited()
    # No rename happened, so no distinct prefix key to seal: prefix_uri stays None.
    mock_app.STORAGE.store_artifact.assert_awaited_once_with(
        artifact, b"webm-bytes", supersede_queued_prefixes=True, prefix_uri=None
    )


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


@pytest.mark.asyncio
async def test_stream_artifact_prefix_from_path_streams_bounded_length() -> None:
    manager = ArtifactManager()
    artifact = _make_recording_artifact("s3://bucket/path/recording.webm")

    with patch("skyvern.forge.sdk.artifact.manager.app") as mock_app:
        mock_app.DATABASE.artifacts.get_artifact_by_id = AsyncMock(return_value=artifact)
        mock_app.STORAGE.store_artifact_prefix_from_path = AsyncMock()

        key = await manager.stream_artifact_prefix_from_path(
            artifact_id=artifact.artifact_id,
            organization_id=artifact.organization_id,
            path="/tmp/recording.webm",
            length=4096,
        )
        await asyncio.gather(*manager.upload_aiotasks_map["task_1"])

    assert key == "task_1"
    mock_app.STORAGE.store_artifact_prefix_from_path.assert_awaited_once_with(artifact, "/tmp/recording.webm", 4096)


@pytest.mark.asyncio
async def test_stream_artifact_prefix_from_path_noop_without_ids() -> None:
    manager = ArtifactManager()

    with patch("skyvern.forge.sdk.artifact.manager.app") as mock_app:
        mock_app.STORAGE.store_artifact_prefix_from_path = AsyncMock()

        assert await manager.stream_artifact_prefix_from_path(None, "org_1", "/tmp/x.webm", 10) is None
        assert await manager.stream_artifact_prefix_from_path("a_recording", None, "/tmp/x.webm", 10) is None

    mock_app.STORAGE.store_artifact_prefix_from_path.assert_not_awaited()


@pytest.mark.asyncio
async def test_stream_artifact_prefix_from_path_noop_when_artifact_missing() -> None:
    manager = ArtifactManager()

    with patch("skyvern.forge.sdk.artifact.manager.app") as mock_app:
        mock_app.DATABASE.artifacts.get_artifact_by_id = AsyncMock(return_value=None)
        mock_app.STORAGE.store_artifact_prefix_from_path = AsyncMock()

        assert await manager.stream_artifact_prefix_from_path("a_missing", "org_1", "/tmp/x.webm", 10) is None

    mock_app.STORAGE.store_artifact_prefix_from_path.assert_not_awaited()


@pytest.mark.asyncio
async def test_stream_artifact_prefix_swallows_missing_file() -> None:
    """The read now happens in the fire-and-forget task; a concurrently removed recording file must be
    logged and swallowed, not surface unhandled through wait_for_upload_aiotasks' gather."""
    manager = ArtifactManager()
    artifact = _make_recording_artifact("s3://bucket/path/recording.webm")

    with patch("skyvern.forge.sdk.artifact.manager.app") as mock_app:
        mock_app.DATABASE.artifacts.get_artifact_by_id = AsyncMock(return_value=artifact)
        mock_app.STORAGE.store_artifact_prefix_from_path = AsyncMock(side_effect=FileNotFoundError("gone"))

        key = await manager.stream_artifact_prefix_from_path(
            artifact.artifact_id, artifact.organization_id, "/tmp/missing.webm", 10
        )
        # The tracked task must finish cleanly (no exception escaping into the barrier's gather).
        await asyncio.gather(*manager.upload_aiotasks_map["task_1"])

    assert key == "task_1"


@pytest.mark.asyncio
async def test_stream_artifact_prefix_from_path_refuses_unknown_type() -> None:
    manager = ArtifactManager()
    artifact = _make_recording_artifact("s3://bucket/path/recording.webm").model_copy(
        update={"artifact_type": ArtifactType.UNKNOWN}
    )

    with patch("skyvern.forge.sdk.artifact.manager.app") as mock_app:
        mock_app.DATABASE.artifacts.get_artifact_by_id = AsyncMock(return_value=artifact)
        mock_app.STORAGE.store_artifact_prefix_from_path = AsyncMock()

        assert await manager.stream_artifact_prefix_from_path("a_recording", "org_1", "/tmp/x.webm", 10) is None

    mock_app.STORAGE.store_artifact_prefix_from_path.assert_not_awaited()
