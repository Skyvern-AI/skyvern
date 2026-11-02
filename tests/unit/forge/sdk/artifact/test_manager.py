import asyncio
import io
import threading
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


@pytest.mark.asyncio
async def test_a_background_turn_flush_survives_the_upload_barrier_cancelling_it(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # A turn flushed on its own is one object per turn; a barrier that gives up and cancels the tracked task must not
    # lose an archive the flush already popped, and the block-end flush then finds nothing of those turns left.
    manager = ArtifactManager()
    block, turn_1, turn_2 = _step("stp_block", 0), _step("stp_turn_1", 1), _step("stp_turn_2", 2)
    for step in (block, turn_1, turn_2):
        manager.accumulate_llm_call_to_archive(step=step, request=f"{step.step_id} request".encode())
    manager.accumulate_html_scrape_to_archive(turn_1, b"<p>turn 1</p>")

    release = asyncio.Event()
    stored: list[tuple[str, bytes]] = []

    async def _store(artifact: Any, data: bytes) -> None:
        await release.wait()
        stored.append((artifact.step_id, data))

    async def _bulk_create(models: list[Any]) -> list[Any]:
        return models

    monkeypatch.setattr("skyvern.forge.sdk.artifact.manager._bundling_enabled", lambda: True)
    monkeypatch.setattr(
        "skyvern.forge.sdk.artifact.manager.app.STORAGE",
        SimpleNamespace(build_uri=lambda **kwargs: f"s3://artifacts/{kwargs['artifact_id']}", store_artifact=_store),
    )
    monkeypatch.setattr("skyvern.forge.sdk.artifact.manager.app.DATABASE.artifacts.bulk_create_artifacts", _bulk_create)

    manager.flush_step_archive_in_background(turn_1)
    manager.flush_step_archive_in_background(turn_2)
    await asyncio.sleep(0)
    assert set(manager._step_archives) == {"stp_block"}
    for tracked in list(manager.upload_aiotasks_map["tsk_block"]):
        tracked.cancel()
    await asyncio.sleep(0)
    release.set()
    await asyncio.gather(*manager._background_flushes)

    turn_zips = dict(stored)
    assert sorted(turn_zips) == ["stp_turn_1", "stp_turn_2"]
    with zipfile.ZipFile(io.BytesIO(turn_zips["stp_turn_1"])) as zf:
        assert (sorted(zf.namelist()), zf.read("scrape.html")) == (
            ["llm_request_0.json", "scrape.html"],
            b"<p>turn 1</p>",
        )
    stored.clear()
    await manager.flush_task_step_archives_as_one(block)
    [(owner, zip_bytes)] = stored
    with zipfile.ZipFile(io.BytesIO(zip_bytes)) as zf:
        assert (owner, zf.namelist()) == ("stp_block", ["llm_request_0.json"])


@pytest.mark.asyncio
async def test_a_background_turn_flush_against_a_stalled_store_ends_at_its_deadline(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    manager = ArtifactManager()
    turn = _step("stp_turn_1", 1)
    manager.accumulate_llm_call_to_archive(step=turn, request=b"request")

    async def _never_returns(artifact: Any, data: bytes) -> None:
        await asyncio.Event().wait()

    monkeypatch.setattr("skyvern.forge.sdk.artifact.manager.BACKGROUND_FLUSH_TIMEOUT_SECONDS", 0.05)
    monkeypatch.setattr("skyvern.forge.sdk.artifact.manager._bundling_enabled", lambda: True)
    monkeypatch.setattr(
        "skyvern.forge.sdk.artifact.manager.app.STORAGE",
        SimpleNamespace(
            build_uri=lambda **kwargs: f"s3://artifacts/{kwargs['artifact_id']}", store_artifact=_never_returns
        ),
    )

    manager.flush_step_archive_in_background(turn)
    # The outer bound only keeps a regression from hanging the suite.
    await asyncio.wait_for(asyncio.gather(*manager._background_flushes), timeout=5)

    assert manager._background_flushes == set()


@pytest.mark.asyncio
async def test_archive_zips_are_built_off_the_event_loop_with_unchanged_bytes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    manager = ArtifactManager()
    turn = _step("stp_turn_1", 1)
    manager.accumulate_llm_call_to_archive(step=turn, request=b"request")
    page = b"<html>" + b"<p>row</p>" * 5000 + b"</html>"
    manager.accumulate_html_scrape_to_archive(turn, page)

    stored: list[tuple[ArtifactType, bytes]] = []
    build_threads: list[int] = []
    build_zip = ArtifactManager._build_zip

    def _recording_build_zip(entries: dict[str, bytes]) -> bytes:
        build_threads.append(threading.get_ident())
        return build_zip(entries)

    async def _store(artifact: Any, data: bytes) -> None:
        stored.append((artifact.artifact_type, data))

    async def _bulk_create(models: list[Any]) -> list[Any]:
        return models

    monkeypatch.setattr(ArtifactManager, "_build_zip", staticmethod(_recording_build_zip))
    monkeypatch.setattr("skyvern.forge.sdk.artifact.manager._bundling_enabled", lambda: True)
    monkeypatch.setattr(
        "skyvern.forge.sdk.artifact.manager.app.STORAGE",
        SimpleNamespace(build_uri=lambda **kwargs: f"s3://artifacts/{kwargs['artifact_id']}", store_artifact=_store),
    )
    monkeypatch.setattr("skyvern.forge.sdk.artifact.manager.app.DATABASE.artifacts.bulk_create_artifacts", _bulk_create)

    await manager.flush_step_archive(turn.step_id)
    await manager.create_task_archive(
        step=turn,
        entries={
            "browser_console.log": (ArtifactType.BROWSER_CONSOLE_LOG, b"console line"),
            "trace.zip": (ArtifactType.TRACE, b"PK trace"),
        },
        workflow_run_id="wr_archive",
    )

    loop_thread = threading.get_ident()
    assert [artifact_type for artifact_type, _ in stored] == [ArtifactType.STEP_ARCHIVE, ArtifactType.TASK_ARCHIVE]
    assert len(build_threads) == 2 and loop_thread not in build_threads
    # Same bytes as building on the loop: member names, order and compress types are unchanged.
    assert [data for _, data in stored] == [
        build_zip({"llm_request_0.json": b"request", "scrape.html": page}),
        build_zip({"browser_console.log": b"console line", "trace.zip": b"PK trace"}),
    ]
    with zipfile.ZipFile(io.BytesIO(stored[1][1])) as zf:
        assert [(info.filename, info.compress_type) for info in zf.infolist()] == [
            ("browser_console.log", zipfile.ZIP_DEFLATED),
            ("trace.zip", zipfile.ZIP_STORED),
        ]
