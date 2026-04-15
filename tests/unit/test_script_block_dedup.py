"""
Tests for script block content deduplication (SKY-8684).

Verifies that:
1. create_or_update_script_block skips S3 upload when content matches (dedup hit)
2. create_or_update_script_block uploads to S3 when content differs (dedup miss)
3. create_or_update_script_block uploads to S3 when no previous block exists
4. Edge case: matching hash but no artifact_id falls through to full upload
"""

import hashlib
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from skyvern.core.script_generations.generate_script import create_or_update_script_block


class TestScriptBlockDedup:
    """Test content deduplication in create_or_update_script_block."""

    @pytest.mark.asyncio
    async def test_create_new_block_no_previous_file_uploads_to_s3(self) -> None:
        """When no ScriptFile with matching content_hash exists, full S3 upload occurs."""
        mock_script_block = MagicMock()
        mock_script_block.script_block_id = "sb_new"
        mock_script_block.script_file_id = None

        mock_script_file = MagicMock()
        mock_script_file.file_id = "sf_new"

        with patch("skyvern.core.script_generations.generate_script.app") as mock_app:
            mock_app.DATABASE.scripts.get_script_block_by_label = AsyncMock(return_value=None)
            mock_app.DATABASE.scripts.create_script_block = AsyncMock(return_value=mock_script_block)
            mock_app.DATABASE.scripts.get_script_file_by_content_hash = AsyncMock(return_value=None)
            mock_app.ARTIFACT_MANAGER.create_script_file_artifact = AsyncMock(return_value="artifact_new")
            mock_app.DATABASE.scripts.create_script_file = AsyncMock(return_value=mock_script_file)
            mock_app.DATABASE.scripts.update_script_block = AsyncMock(return_value=mock_script_block)

            result = await create_or_update_script_block(
                block_code="async def task_1(): pass",
                script_revision_id="rev_new",
                script_id="script_1",
                organization_id="org_1",
                block_label="task_1",
            )

            assert result is True
            # S3 upload SHOULD happen — no dedup match
            mock_app.ARTIFACT_MANAGER.create_script_file_artifact.assert_called_once()
            mock_app.DATABASE.scripts.create_script_file.assert_called_once()
            # Verify content_hash was passed to create_script_file
            call_kwargs = mock_app.DATABASE.scripts.create_script_file.call_args.kwargs
            assert call_kwargs["content_hash"].startswith("sha256:")

    @pytest.mark.asyncio
    async def test_create_new_block_matching_hash_skips_s3_upload(self) -> None:
        """When a ScriptFile with matching content_hash exists, S3 upload is skipped (cross-revision dedup)."""
        mock_script_block = MagicMock()
        mock_script_block.script_block_id = "sb_dedup"
        mock_script_block.script_file_id = None

        existing_script_file = MagicMock()
        existing_script_file.artifact_id = "artifact_existing"

        new_script_file = MagicMock()
        new_script_file.file_id = "sf_dedup"

        with patch("skyvern.core.script_generations.generate_script.app") as mock_app:
            mock_app.DATABASE.scripts.get_script_block_by_label = AsyncMock(return_value=None)
            mock_app.DATABASE.scripts.create_script_block = AsyncMock(return_value=mock_script_block)
            mock_app.DATABASE.scripts.get_script_file_by_content_hash = AsyncMock(return_value=existing_script_file)
            mock_app.DATABASE.scripts.create_script_file = AsyncMock(return_value=new_script_file)
            mock_app.DATABASE.scripts.update_script_block = AsyncMock(return_value=mock_script_block)

            result = await create_or_update_script_block(
                block_code="async def task_1(): pass",
                script_revision_id="rev_new",
                script_id="script_1",
                organization_id="org_1",
                block_label="task_1",
            )

            assert result is True
            # S3 upload should NOT happen — dedup hit
            mock_app.ARTIFACT_MANAGER.create_script_file_artifact.assert_not_called()
            # ScriptFile should still be created (for the new revision) with reused artifact_id
            mock_app.DATABASE.scripts.create_script_file.assert_called_once()
            call_kwargs = mock_app.DATABASE.scripts.create_script_file.call_args.kwargs
            assert call_kwargs["artifact_id"] == "artifact_existing"

    @pytest.mark.asyncio
    async def test_update_existing_block_same_content_skips_s3(self) -> None:
        """When updating a block and content_hash matches, S3 upload is skipped entirely."""
        block_code = "async def task_1(): pass"
        content_hash = f"sha256:{hashlib.sha256(block_code.encode('utf-8')).hexdigest()}"

        mock_script_block = MagicMock()
        mock_script_block.script_block_id = "sb_existing"
        mock_script_block.script_file_id = "sf_existing"

        mock_script_file = MagicMock()
        mock_script_file.content_hash = content_hash
        mock_script_file.artifact_id = "artifact_existing"

        with patch("skyvern.core.script_generations.generate_script.app") as mock_app:
            mock_app.DATABASE.scripts.get_script_block_by_label = AsyncMock(return_value=mock_script_block)
            mock_app.DATABASE.scripts.get_script_file_by_id = AsyncMock(return_value=mock_script_file)

            result = await create_or_update_script_block(
                block_code=block_code,
                script_revision_id="rev_1",
                script_id="script_1",
                organization_id="org_1",
                block_label="task_1",
                update=True,
            )

            assert result is True
            # S3 upload should NOT happen — content unchanged
            mock_app.STORAGE.store_artifact.assert_not_called()
            mock_app.DATABASE.artifacts.get_artifact_by_id.assert_not_called()

    @pytest.mark.asyncio
    async def test_update_existing_block_different_content_uploads_to_s3(self) -> None:
        """When updating a block and content_hash differs, S3 upload proceeds."""
        mock_script_block = MagicMock()
        mock_script_block.script_block_id = "sb_existing"
        mock_script_block.script_file_id = "sf_existing"

        mock_script_file = MagicMock()
        mock_script_file.content_hash = "sha256:old_hash_value"
        mock_script_file.artifact_id = "artifact_existing"

        mock_artifact = MagicMock()

        with patch("skyvern.core.script_generations.generate_script.app") as mock_app:
            mock_app.DATABASE.scripts.get_script_block_by_label = AsyncMock(return_value=mock_script_block)
            mock_app.DATABASE.scripts.get_script_file_by_id = AsyncMock(return_value=mock_script_file)
            mock_app.DATABASE.artifacts.get_artifact_by_id = AsyncMock(return_value=mock_artifact)
            mock_app.DATABASE.scripts.update_script_file = AsyncMock()
            mock_app.STORAGE.store_artifact = AsyncMock()

            result = await create_or_update_script_block(
                block_code="async def task_1_v2(): pass",
                script_revision_id="rev_1",
                script_id="script_1",
                organization_id="org_1",
                block_label="task_1",
                update=True,
            )

            assert result is True
            # S3 upload SHOULD happen — content changed
            mock_app.DATABASE.artifacts.get_artifact_by_id.assert_called_once()
            mock_app.STORAGE.store_artifact.assert_called_once()
            # content_hash should be updated on the ScriptFile record AFTER S3 upload
            mock_app.DATABASE.scripts.update_script_file.assert_called_once()
            assert "content_hash" in mock_app.DATABASE.scripts.update_script_file.call_args.kwargs

    @pytest.mark.asyncio
    async def test_create_block_matching_hash_but_no_artifact_falls_through(self) -> None:
        """When ScriptFile has matching hash but artifact_id is None, fall through to full upload."""
        mock_script_block = MagicMock()
        mock_script_block.script_block_id = "sb_edge"
        mock_script_block.script_file_id = None

        orphan_script_file = MagicMock()
        orphan_script_file.artifact_id = None  # No artifact — can't reuse

        new_script_file = MagicMock()
        new_script_file.file_id = "sf_edge"

        with patch("skyvern.core.script_generations.generate_script.app") as mock_app:
            mock_app.DATABASE.scripts.get_script_block_by_label = AsyncMock(return_value=None)
            mock_app.DATABASE.scripts.create_script_block = AsyncMock(return_value=mock_script_block)
            mock_app.DATABASE.scripts.get_script_file_by_content_hash = AsyncMock(return_value=orphan_script_file)
            mock_app.ARTIFACT_MANAGER.create_script_file_artifact = AsyncMock(return_value="artifact_new")
            mock_app.DATABASE.scripts.create_script_file = AsyncMock(return_value=new_script_file)
            mock_app.DATABASE.scripts.update_script_block = AsyncMock(return_value=mock_script_block)

            result = await create_or_update_script_block(
                block_code="async def task_edge(): pass",
                script_revision_id="rev_edge",
                script_id="script_1",
                organization_id="org_1",
                block_label="task_edge",
            )

            assert result is True
            # Should fall through to full upload since artifact_id is None
            mock_app.ARTIFACT_MANAGER.create_script_file_artifact.assert_called_once()
