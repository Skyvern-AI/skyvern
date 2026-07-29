import asyncio
from pathlib import Path

import pytest

from skyvern.webeye.browser_factory import resolve_artifact_path


class _FakeVideo:
    """Mirrors patchright's Video: path() awaits a single shared artifact future."""

    def __init__(self, artifact_future: asyncio.Future) -> None:
        self._artifact_future = artifact_future

    async def path(self) -> str:
        return await self._artifact_future


class _FakeSharedFutureDownload:
    """Mirrors the hazard resolve_artifact_path future-proofs against: a Download whose path()
    awaits a single shared artifact future (as a pin bump could reintroduce). path() returns a
    pathlib.Path, like the real Download."""

    def __init__(self, artifact_future: asyncio.Future) -> None:
        self._artifact_future = artifact_future

    async def path(self) -> Path:
        return await self._artifact_future


@pytest.mark.asyncio
async def test_returns_path_when_future_resolves_in_time() -> None:
    fut: asyncio.Future = asyncio.get_running_loop().create_future()
    fut.set_result("/videos/a.webm")

    assert await resolve_artifact_path(_FakeVideo(fut), timeout_seconds=1) == "/videos/a.webm"


@pytest.mark.asyncio
async def test_timeout_returns_none_without_cancelling_shared_future() -> None:
    fut: asyncio.Future = asyncio.get_running_loop().create_future()

    result = await resolve_artifact_path(_FakeVideo(fut), timeout_seconds=0.05)

    assert result is None
    assert not fut.cancelled()
    # A later awaiter must still be able to get the value.
    fut.set_result("/videos/b.webm")
    assert await _FakeVideo(fut).path() == "/videos/b.webm"


@pytest.mark.asyncio
async def test_poisoned_future_returns_none_instead_of_cancelling_caller() -> None:
    fut: asyncio.Future = asyncio.get_running_loop().create_future()
    fut.cancel()

    caller = asyncio.ensure_future(resolve_artifact_path(_FakeVideo(fut), timeout_seconds=1))
    result = await asyncio.wait_for(caller, timeout=1)

    assert result is None
    assert not caller.cancelled()


@pytest.mark.asyncio
async def test_concurrent_waiter_timeout_does_not_cancel_other_caller() -> None:
    """The SKY-12852 scenario: the popup-video listener times out on the shared future
    while the activity's discard path is awaiting the same future."""
    fut: asyncio.Future = asyncio.get_running_loop().create_future()
    video = _FakeVideo(fut)

    listener = asyncio.ensure_future(resolve_artifact_path(video, timeout_seconds=0.05))
    caller = asyncio.ensure_future(resolve_artifact_path(video, timeout_seconds=5))

    assert await listener is None
    await asyncio.sleep(0)  # let any stray cancellation propagate
    fut.set_result("/videos/c.webm")

    assert await asyncio.wait_for(caller, timeout=1) == "/videos/c.webm"
    assert not caller.cancelled()


@pytest.mark.asyncio
async def test_poisoned_future_returns_none_with_stale_cancel_count() -> None:
    """A caught-but-not-uncancelled CancelledError leaves current_task().cancelling()
    nonzero; a poisoned-future miss must still degrade to None, not re-raise."""
    fut: asyncio.Future = asyncio.get_running_loop().create_future()
    fut.cancel()

    async def caller_with_stale_cancel_count() -> str | None:
        current = asyncio.current_task()
        assert current is not None
        current.cancel()
        try:
            await asyncio.sleep(0)
        except asyncio.CancelledError:
            pass
        assert current.cancelling() > 0
        return await resolve_artifact_path(_FakeVideo(fut), timeout_seconds=1)

    caller = asyncio.ensure_future(caller_with_stale_cancel_count())
    assert await asyncio.wait_for(caller, timeout=1) is None
    assert not caller.cancelled()


@pytest.mark.asyncio
async def test_real_task_cancellation_still_propagates() -> None:
    fut: asyncio.Future = asyncio.get_running_loop().create_future()

    caller = asyncio.ensure_future(resolve_artifact_path(_FakeVideo(fut), timeout_seconds=5))
    await asyncio.sleep(0.01)
    caller.cancel()

    with pytest.raises(asyncio.CancelledError):
        await caller
    assert caller.cancelled()


@pytest.mark.asyncio
async def test_download_path_object_is_stringified() -> None:
    fut: asyncio.Future = asyncio.get_running_loop().create_future()
    fut.set_result(Path("/downloads/report"))

    assert await resolve_artifact_path(_FakeSharedFutureDownload(fut), timeout_seconds=1) == "/downloads/report"


@pytest.mark.asyncio
async def test_download_concurrent_waiter_timeout_does_not_cancel_other_caller() -> None:
    """Pin-bump regression guard: if Download ever awaits a shared artifact future (as Video did),
    the download listener timing out must not poison the future for _persist_captured_download's
    awaiter (the SKY-12860 orphan-hang class returning via downloads)."""
    fut: asyncio.Future = asyncio.get_running_loop().create_future()
    download = _FakeSharedFutureDownload(fut)

    listener = asyncio.ensure_future(resolve_artifact_path(download, timeout_seconds=0.05))
    caller = asyncio.ensure_future(resolve_artifact_path(download, timeout_seconds=5))

    assert await listener is None
    await asyncio.sleep(0)  # let any stray cancellation propagate
    fut.set_result(Path("/downloads/report.pdf"))

    assert await asyncio.wait_for(caller, timeout=1) == "/downloads/report.pdf"
    assert not caller.cancelled()


@pytest.mark.asyncio
async def test_download_poisoned_future_returns_none_instead_of_cancelling_caller() -> None:
    fut: asyncio.Future = asyncio.get_running_loop().create_future()
    fut.cancel()

    caller = asyncio.ensure_future(resolve_artifact_path(_FakeSharedFutureDownload(fut), timeout_seconds=1))
    result = await asyncio.wait_for(caller, timeout=1)

    assert result is None
    assert not caller.cancelled()
