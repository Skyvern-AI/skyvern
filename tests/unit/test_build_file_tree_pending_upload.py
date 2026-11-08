"""The pending branch of build_file_tree must finish its artifact upload before returning."""

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from skyvern.forge import app
from skyvern.services import script_service


@pytest.mark.asyncio
async def test_pending_file_upload_completes_before_the_tree_is_returned(monkeypatch):
    stored = []

    async def store_artifact(artifact, data):
        stored.append((artifact, data))

    database = SimpleNamespace(
        scripts=SimpleNamespace(
            get_script_file_by_path=AsyncMock(return_value=SimpleNamespace(artifact_id="a1", file_id="f1")),
            update_script_file=AsyncMock(),
            create_script_file=AsyncMock(),
        ),
        artifacts=SimpleNamespace(get_artifact_by_id=AsyncMock(return_value=SimpleNamespace(artifact_id="a1"))),
    )

    monkeypatch.setattr(app, "DATABASE", database)
    monkeypatch.setattr(app, "STORAGE", SimpleNamespace(store_artifact=store_artifact))
    monkeypatch.setattr(app, "ARTIFACT_MANAGER", SimpleNamespace(create_script_file_artifact=AsyncMock()))

    await script_service.build_file_tree(
        files=[SimpleNamespace(path="main.py", mime_type="text/x-python")],
        organization_id="o1",
        script_id="s1",
        script_version=1,
        script_revision_id="r1",
        pending=True,
        file_bytes_by_path={"main.py": b"print('new')"},
    )

    assert len(stored) == 1, "the artifact upload had not run when build_file_tree returned"
