"""Tests for artifact archive detection (SKY-9672).

Artifacts older than 90 days in S3 DEEP_ARCHIVE or GLACIER should be marked
as ``archived = True`` so the frontend can display an appropriate message
instead of a generic "unavailable" state.
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock, patch

import pytest
from botocore.exceptions import ClientError

from skyvern.forge.sdk.artifact.manager import ARCHIVE_AGE_THRESHOLD, ArtifactManager
from skyvern.forge.sdk.artifact.models import Artifact, ArtifactType
from skyvern.forge.sdk.artifact.storage.s3 import S3Storage


def _make_s3_storage() -> S3Storage:
    storage = S3Storage.__new__(S3Storage)
    storage.async_client = AsyncMock()
    storage._head_object_semaphore = asyncio.Semaphore(8)
    return storage


def _make_artifact(
    artifact_id: str = "art_1",
    uri: str = "s3://skyvern-artifacts/production/org/recording.webm",
    created_at: datetime | None = None,
    artifact_type: ArtifactType = ArtifactType.RECORDING,
    bundle_key: str | None = None,
) -> Artifact:
    now = datetime.now(UTC)
    return Artifact(
        artifact_id=artifact_id,
        artifact_type=artifact_type,
        uri=uri,
        bundle_key=bundle_key,
        organization_id="o_test",
        created_at=created_at or now,
        modified_at=created_at or now,
    )


# ---------------------------------------------------------------------------
# S3Storage.check_archived_uris
# ---------------------------------------------------------------------------


class TestCheckArchivedUris:
    @pytest.mark.asyncio
    async def test_deep_archive_detected(self) -> None:
        storage = _make_s3_storage()
        storage.async_client.get_object_info = AsyncMock(return_value={"StorageClass": "DEEP_ARCHIVE"})

        result = await storage.check_archived_uris(["s3://bucket/key"])
        assert result == {"s3://bucket/key": True}

    @pytest.mark.asyncio
    async def test_glacier_detected(self) -> None:
        storage = _make_s3_storage()
        storage.async_client.get_object_info = AsyncMock(return_value={"StorageClass": "GLACIER"})

        result = await storage.check_archived_uris(["s3://bucket/key"])
        assert result == {"s3://bucket/key": True}

    @pytest.mark.asyncio
    async def test_glacier_ir_not_archived(self) -> None:
        storage = _make_s3_storage()
        storage.async_client.get_object_info = AsyncMock(return_value={"StorageClass": "GLACIER_IR"})

        result = await storage.check_archived_uris(["s3://bucket/key"])
        assert result == {"s3://bucket/key": False}

    @pytest.mark.asyncio
    async def test_standard_not_archived(self) -> None:
        storage = _make_s3_storage()
        storage.async_client.get_object_info = AsyncMock(return_value={"StorageClass": "STANDARD"})

        result = await storage.check_archived_uris(["s3://bucket/key"])
        assert result == {"s3://bucket/key": False}

    @pytest.mark.asyncio
    async def test_missing_storage_class_defaults_to_standard(self) -> None:
        storage = _make_s3_storage()
        storage.async_client.get_object_info = AsyncMock(return_value={})

        result = await storage.check_archived_uris(["s3://bucket/key"])
        assert result == {"s3://bucket/key": False}

    @pytest.mark.asyncio
    async def test_head_object_client_error_defaults_to_not_archived(self) -> None:
        storage = _make_s3_storage()
        storage.async_client.get_object_info = AsyncMock(
            side_effect=ClientError({"Error": {"Code": "NoSuchKey", "Message": "missing"}}, "HeadObject")
        )

        result = await storage.check_archived_uris(["s3://bucket/key"])
        assert result == {"s3://bucket/key": False}

    @pytest.mark.asyncio
    async def test_unexpected_error_propagates(self) -> None:
        storage = _make_s3_storage()
        storage.async_client.get_object_info = AsyncMock(side_effect=RuntimeError("boom"))

        with pytest.raises(RuntimeError):
            await storage.check_archived_uris(["s3://bucket/key"])

    @pytest.mark.asyncio
    async def test_multiple_uris(self) -> None:
        storage = _make_s3_storage()

        async def _head(uri: str) -> dict:
            if "old" in uri:
                return {"StorageClass": "DEEP_ARCHIVE"}
            return {"StorageClass": "STANDARD"}

        storage.async_client.get_object_info = AsyncMock(side_effect=_head)

        result = await storage.check_archived_uris(["s3://bucket/old.webm", "s3://bucket/new.webm"])
        assert result == {"s3://bucket/old.webm": True, "s3://bucket/new.webm": False}

    @pytest.mark.asyncio
    async def test_empty_input_short_circuits(self) -> None:
        storage = _make_s3_storage()
        result = await storage.check_archived_uris([])
        assert result == {}
        storage.async_client.get_object_info.assert_not_called()


# ---------------------------------------------------------------------------
# ArtifactManager.mark_archived_artifacts
# ---------------------------------------------------------------------------


class TestMarkArchivedArtifacts:
    @pytest.mark.asyncio
    async def test_recent_artifacts_skipped(self) -> None:
        """Artifacts younger than ARCHIVE_AGE_THRESHOLD should not trigger head_object."""
        recent = _make_artifact(created_at=datetime.now(UTC) - timedelta(days=30))
        mock_storage = AsyncMock()
        mock_storage.check_archived_uris = AsyncMock(return_value={})

        manager = ArtifactManager.__new__(ArtifactManager)
        with patch("skyvern.forge.sdk.artifact.manager.app") as mock_app:
            mock_app.STORAGE = mock_storage
            await manager.mark_archived_artifacts([recent])

        mock_storage.check_archived_uris.assert_not_called()
        assert recent.archived is False

    @pytest.mark.asyncio
    async def test_old_archived_artifact_marked(self) -> None:
        old = _make_artifact(created_at=datetime.now(UTC) - timedelta(days=100))
        mock_storage = AsyncMock()
        mock_storage.check_archived_uris = AsyncMock(return_value={old.uri: True})

        manager = ArtifactManager.__new__(ArtifactManager)
        with patch("skyvern.forge.sdk.artifact.manager.app") as mock_app:
            mock_app.STORAGE = mock_storage
            await manager.mark_archived_artifacts([old])

        assert old.archived is True

    @pytest.mark.asyncio
    async def test_old_non_archived_artifact_not_marked(self) -> None:
        old = _make_artifact(created_at=datetime.now(UTC) - timedelta(days=100))
        mock_storage = AsyncMock()
        mock_storage.check_archived_uris = AsyncMock(return_value={old.uri: False})

        manager = ArtifactManager.__new__(ArtifactManager)
        with patch("skyvern.forge.sdk.artifact.manager.app") as mock_app:
            mock_app.STORAGE = mock_storage
            await manager.mark_archived_artifacts([old])

        assert old.archived is False

    @pytest.mark.asyncio
    async def test_uri_dedup_for_bundle_members(self) -> None:
        """Bundle members sharing the same URI should only trigger one head_object."""
        shared_uri = "s3://bucket/archive.zip"
        old_date = datetime.now(UTC) - timedelta(days=100)
        a1 = _make_artifact(artifact_id="a1", uri=shared_uri, created_at=old_date, bundle_key="file1.json")
        a2 = _make_artifact(artifact_id="a2", uri=shared_uri, created_at=old_date, bundle_key="file2.json")

        mock_storage = AsyncMock()
        mock_storage.check_archived_uris = AsyncMock(return_value={shared_uri: True})

        manager = ArtifactManager.__new__(ArtifactManager)
        with patch("skyvern.forge.sdk.artifact.manager.app") as mock_app:
            mock_app.STORAGE = mock_storage
            await manager.mark_archived_artifacts([a1, a2])

        # Only called once with one unique URI
        mock_storage.check_archived_uris.assert_called_once_with([shared_uri])
        assert a1.archived is True
        assert a2.archived is True

    @pytest.mark.asyncio
    async def test_mixed_recent_and_old(self) -> None:
        recent = _make_artifact(artifact_id="recent", created_at=datetime.now(UTC) - timedelta(days=10))
        old = _make_artifact(artifact_id="old", created_at=datetime.now(UTC) - timedelta(days=100))

        mock_storage = AsyncMock()
        mock_storage.check_archived_uris = AsyncMock(return_value={old.uri: True})

        manager = ArtifactManager.__new__(ArtifactManager)
        with patch("skyvern.forge.sdk.artifact.manager.app") as mock_app:
            mock_app.STORAGE = mock_storage
            await manager.mark_archived_artifacts([recent, old])

        assert recent.archived is False
        assert old.archived is True

    @pytest.mark.asyncio
    async def test_empty_list(self) -> None:
        manager = ArtifactManager.__new__(ArtifactManager)
        await manager.mark_archived_artifacts([])

    @pytest.mark.asyncio
    async def test_naive_db_timestamps_normalized(self) -> None:
        naive_old = datetime.utcnow() - timedelta(days=100)
        old = _make_artifact(created_at=naive_old)
        mock_storage = AsyncMock()
        mock_storage.check_archived_uris = AsyncMock(return_value={old.uri: True})

        manager = ArtifactManager.__new__(ArtifactManager)
        with patch("skyvern.forge.sdk.artifact.manager.app") as mock_app:
            mock_app.STORAGE = mock_storage
            await manager.mark_archived_artifacts([old])

        assert old.archived is True


# ---------------------------------------------------------------------------
# ArtifactManager.is_recording_archived
# ---------------------------------------------------------------------------


class TestIsRecordingArchived:
    @pytest.mark.asyncio
    async def test_none_artifact_returns_false(self) -> None:
        manager = ArtifactManager.__new__(ArtifactManager)
        result = await manager.is_recording_archived(None)
        assert result is False

    @pytest.mark.asyncio
    async def test_recent_recording_returns_false(self) -> None:
        recent = _make_artifact(created_at=datetime.now(UTC) - timedelta(days=30))
        manager = ArtifactManager.__new__(ArtifactManager)
        result = await manager.is_recording_archived(recent)
        assert result is False

    @pytest.mark.asyncio
    async def test_old_archived_recording_returns_true(self) -> None:
        old = _make_artifact(created_at=datetime.now(UTC) - timedelta(days=100))
        mock_storage = AsyncMock()
        mock_storage.check_archived_uris = AsyncMock(return_value={old.uri: True})

        manager = ArtifactManager.__new__(ArtifactManager)
        with patch("skyvern.forge.sdk.artifact.manager.app") as mock_app:
            mock_app.STORAGE = mock_storage
            result = await manager.is_recording_archived(old)

        assert result is True

    @pytest.mark.asyncio
    async def test_naive_datetime_age_screen(self) -> None:
        naive_old = datetime.utcnow() - timedelta(days=100)
        old = _make_artifact(created_at=naive_old)
        mock_storage = AsyncMock()
        mock_storage.check_archived_uris = AsyncMock(return_value={old.uri: True})

        manager = ArtifactManager.__new__(ArtifactManager)
        with patch("skyvern.forge.sdk.artifact.manager.app") as mock_app:
            mock_app.STORAGE = mock_storage
            result = await manager.is_recording_archived(old)

        assert result is True


# ---------------------------------------------------------------------------
# ARCHIVE_AGE_THRESHOLD constant
# ---------------------------------------------------------------------------


def test_archive_age_threshold_is_90_days() -> None:
    assert ARCHIVE_AGE_THRESHOLD == timedelta(days=90)
