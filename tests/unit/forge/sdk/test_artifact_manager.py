import asyncio

import pytest

from skyvern.forge.sdk.artifact.manager import ArtifactManager


async def _noop() -> None:
    return None


async def _boom() -> None:
    raise RuntimeError("upload failed")


async def _drain_loop() -> None:
    for _ in range(10):
        await asyncio.sleep(0)


@pytest.mark.asyncio
async def test_upload_task_self_discards_on_success() -> None:
    manager = ArtifactManager()
    task = asyncio.create_task(_noop())

    manager._track_upload_aiotask("tsk_1", task)
    assert manager.upload_aiotasks_map["tsk_1"] == [task]

    await task
    await _drain_loop()

    assert "tsk_1" not in manager.upload_aiotasks_map


@pytest.mark.asyncio
async def test_upload_task_self_discards_on_failure_and_retrieves_exception() -> None:
    manager = ArtifactManager()
    task = asyncio.create_task(_boom())

    manager._track_upload_aiotask("tsk_1", task)
    with pytest.raises(RuntimeError):
        await task
    await _drain_loop()

    # The failed task must not stay pinned in the map (its traceback retains the artifact bytes).
    assert "tsk_1" not in manager.upload_aiotasks_map


@pytest.mark.asyncio
async def test_upload_task_discard_keeps_other_tasks_for_same_key() -> None:
    manager = ArtifactManager()
    done_task = asyncio.create_task(_noop())
    pending_task = asyncio.create_task(asyncio.sleep(30))

    manager._track_upload_aiotask("tsk_1", done_task)
    manager._track_upload_aiotask("tsk_1", pending_task)
    await done_task
    await _drain_loop()

    assert manager.upload_aiotasks_map["tsk_1"] == [pending_task]

    pending_task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await pending_task
    await _drain_loop()
    assert "tsk_1" not in manager.upload_aiotasks_map


@pytest.mark.asyncio
async def test_wait_for_upload_aiotasks_still_barriers_pending_uploads() -> None:
    manager = ArtifactManager()
    started = asyncio.Event()
    release = asyncio.Event()

    async def _slow_upload() -> None:
        started.set()
        await release.wait()

    task = asyncio.create_task(_slow_upload())
    manager._track_upload_aiotask("tsk_1", task)
    await started.wait()

    waiter = asyncio.create_task(manager.wait_for_upload_aiotasks(["tsk_1"]))
    await _drain_loop()
    assert not waiter.done()

    release.set()
    await waiter

    assert task.done()
    assert "tsk_1" not in manager.upload_aiotasks_map


@pytest.mark.asyncio
async def test_wait_for_upload_aiotasks_does_not_autovivify_keys() -> None:
    manager = ArtifactManager()

    await manager.wait_for_upload_aiotasks(["tsk_never_seen"])

    assert "tsk_never_seen" not in manager.upload_aiotasks_map
