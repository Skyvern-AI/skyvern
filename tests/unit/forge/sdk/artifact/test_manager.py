import io
import zipfile
from datetime import datetime
from types import SimpleNamespace
from typing import Any

import pytest

from skyvern.forge.sdk.artifact.manager import ArtifactManager
from skyvern.forge.sdk.artifact.models import ArtifactType
from skyvern.forge.sdk.models import Step, StepStatus


def _step(step_id: str, order: int, task_id: str = "tsk_block") -> Step:
    now = datetime.utcnow()
    return Step(
        task_id=task_id,
        step_id=step_id,
        order=order,
        retry_index=0,
        status=StepStatus.running,
        is_last=False,
        organization_id="o_archive",
        created_at=now,
        modified_at=now,
    )


@pytest.mark.asyncio
async def test_a_tasks_step_archives_flush_as_one_zip_with_each_file_on_its_own_step(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # A Task V3 block's turn steps share its ZIP (one storage PUT per block); each file's row stays on its turn.
    manager = ArtifactManager()
    block, turn_1, turn_2 = _step("stp_block", 0), _step("stp_turn_1", 1), _step("stp_turn_2", 2)
    other = _step("stp_other", 1, task_id="tsk_other")
    for step in (turn_1, turn_2, block, other):
        manager.accumulate_llm_call_to_archive(
            step=step,
            workflow_run_id="wr_archive",
            request=f"{step.step_id} request".encode(),
            response=f"{step.step_id} response".encode(),
        )

    stored: list[tuple[Any, bytes]] = []
    rows: list[Any] = []

    async def _store(artifact: Any, data: bytes) -> None:
        stored.append((artifact, data))

    async def _bulk_create(models: list[Any]) -> list[Any]:
        rows.extend(models)
        return models

    monkeypatch.setattr("skyvern.forge.sdk.artifact.manager._bundling_enabled", lambda: True)
    monkeypatch.setattr(
        "skyvern.forge.sdk.artifact.manager.app.STORAGE",
        SimpleNamespace(build_uri=lambda **kwargs: f"s3://artifacts/{kwargs['artifact_id']}", store_artifact=_store),
    )
    monkeypatch.setattr("skyvern.forge.sdk.artifact.manager.app.DATABASE.artifacts.bulk_create_artifacts", _bulk_create)

    await manager.flush_task_step_archives_as_one(block)

    [(archive, zip_bytes)] = stored
    parent, *members = rows
    assert (parent.artifact_type, parent.step_id, parent.uri) == (ArtifactType.STEP_ARCHIVE, "stp_block", archive.uri)
    with zipfile.ZipFile(io.BytesIO(zip_bytes)) as zf:
        assert all(member.uri == archive.uri for member in members)
        assert sorted((member.step_id, zf.read(member.bundle_key).decode()) for member in members) == sorted(
            (step_id, f"{step_id} {kind}")
            for step_id in ("stp_block", "stp_turn_1", "stp_turn_2")
            for kind in ("request", "response")
        )
    # Another task's archive waits for its own flush, and a step flushed alone keeps its own file names.
    assert list(manager._step_archives) == ["stp_other"]
    rows.clear()
    await manager.flush_step_archive("stp_other")
    assert sorted(row.bundle_key for row in rows[1:]) == ["llm_request_0.json", "llm_response_0.json"]
