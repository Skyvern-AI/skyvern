"""Pending overwrites in build_file_tree must update ScriptFile.content_hash."""

import hashlib
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from skyvern.schemas.scripts import FileEncoding, ScriptFileCreate
from skyvern.services.script_service import build_file_tree


def _file(path: str, content: bytes) -> ScriptFileCreate:
    import base64

    return ScriptFileCreate(
        path=path,
        content=base64.b64encode(content).decode(),
        encoding=FileEncoding.BASE64,
        mime_type="text/x-python",
    )


@pytest.mark.asyncio
async def test_pending_overwrite_updates_content_hash_after_store() -> None:
    """After saving content A, a pending save of B must record B's hash.

    The storage write is already awaited on this branch. The previous
    ScriptFile row still held A's hash, so a later save of A would compare
    equal in hash-based dedup (generate_script / get_script_file_by_content_hash)
    and skip uploading, leaving B's bytes under A's hash.
    """
    content_a = b"print('A')\n"
    content_b = b"print('B')\n"
    hash_a = f"sha256:{hashlib.sha256(content_a).hexdigest()}"
    hash_b = f"sha256:{hashlib.sha256(content_b).hexdigest()}"

    existing = MagicMock()
    existing.file_id = "sf_existing"
    existing.artifact_id = "artifact_existing"
    existing.content_hash = hash_a

    artifact = MagicMock()

    with patch("skyvern.services.script_service.app") as mock_app:
        mock_app.DATABASE.scripts.get_script_file_by_path = AsyncMock(return_value=existing)
        mock_app.DATABASE.artifacts.get_artifact_by_id = AsyncMock(return_value=artifact)
        mock_app.STORAGE.store_artifact = AsyncMock()
        mock_app.DATABASE.scripts.update_script_file = AsyncMock()
        mock_app.ARTIFACT_MANAGER.create_script_file_artifact = AsyncMock()
        mock_app.DATABASE.scripts.create_script_file = AsyncMock()

        tree = await build_file_tree(
            [_file("main.py", content_b)],
            organization_id="org_1",
            script_id="script_1",
            script_version=1,
            script_revision_id="rev_1",
            pending=True,
            file_bytes_by_path={"main.py": content_b},
        )

        mock_app.STORAGE.store_artifact.assert_awaited_once_with(artifact, content_b)
        mock_app.DATABASE.scripts.update_script_file.assert_awaited_once()
        kwargs = mock_app.DATABASE.scripts.update_script_file.call_args.kwargs
        assert kwargs["script_file_id"] == "sf_existing"
        assert kwargs["organization_id"] == "org_1"
        assert kwargs["content_hash"] == hash_b
        mock_app.ARTIFACT_MANAGER.create_script_file_artifact.assert_not_called()
        assert tree["main.py"].content_hash == hash_b


@pytest.mark.asyncio
async def test_pending_overwrite_does_not_update_hash_if_store_fails() -> None:
    content_a = b"print('A')\n"
    content_b = b"print('B')\n"
    existing = MagicMock()
    existing.file_id = "sf_existing"
    existing.artifact_id = "artifact_existing"
    existing.content_hash = f"sha256:{hashlib.sha256(content_a).hexdigest()}"
    artifact = MagicMock()

    with patch("skyvern.services.script_service.app") as mock_app:
        mock_app.DATABASE.scripts.get_script_file_by_path = AsyncMock(return_value=existing)
        mock_app.DATABASE.artifacts.get_artifact_by_id = AsyncMock(return_value=artifact)
        mock_app.STORAGE.store_artifact = AsyncMock(side_effect=RuntimeError("s3 down"))
        mock_app.DATABASE.scripts.update_script_file = AsyncMock()

        with pytest.raises(RuntimeError, match="s3 down"):
            await build_file_tree(
                [_file("main.py", content_b)],
                organization_id="org_1",
                script_id="script_1",
                script_version=1,
                script_revision_id="rev_1",
                pending=True,
                file_bytes_by_path={"main.py": content_b},
            )

        mock_app.DATABASE.scripts.update_script_file.assert_not_called()
