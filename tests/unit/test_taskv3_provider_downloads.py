"""ProviderDownloadRecovery owns each provider poll it starts: a recover() call never returns, normally or by
cancellation, while that poll is still running its own cleanup."""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from pathlib import Path

import pytest

from skyvern.exceptions import ActionDeadlineExceeded
from skyvern.forge.taskv3 import provider_downloads
from skyvern.forge.taskv3.provider_downloads import ProviderDownloadRecovery
from skyvern.webeye import action_deadline
from skyvern.webeye.action_deadline import under_action_deadline
from tests.unit._download_provider_fakes import _FakeBrowserDownloadMonitor, _GatedCleanupObservation
from tests.unit.scoped_asyncio import ScopedAsyncio


async def _never_cancel() -> bool:
    return False


async def _always_cancel() -> bool:
    return True


def _recovery(tmp_path: Path, observation: object) -> tuple[ProviderDownloadRecovery, _FakeBrowserDownloadMonitor]:
    monitor = _FakeBrowserDownloadMonitor()
    recovery = ProviderDownloadRecovery(observation, tmp_path, monitor)  # type: ignore[arg-type]
    monitor.browser_download_event_count += 1
    return recovery, monitor


def _release_later(gate: asyncio.Event, delay: float) -> None:
    asyncio.get_running_loop().call_later(delay, gate.set)


async def _skip_sleep(delay: float) -> None:
    return None


@pytest.mark.asyncio
@pytest.mark.parametrize("should_cancel", [_never_cancel, _always_cancel], ids=["deadline", "should_cancel"])
async def test_recover_returns_only_after_its_cancelled_poll_finished_cleanup(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, should_cancel: Callable[[], Awaitable[bool]]
) -> None:
    # A real admission grace spends the probe budget before the first poll, so a runner stalled during it reaches
    # the deadline before the poll this test is about ever starts.
    monkeypatch.setattr(provider_downloads, "asyncio", ScopedAsyncio(sleep=_skip_sleep))
    gate = asyncio.Event()
    observation = _GatedCleanupObservation(gate)
    recovery, _monitor = _recovery(tmp_path, observation)
    _release_later(gate, 1.5)

    await recovery.recover(staged=(), timeout=0.6, should_cancel=should_cancel)

    assert observation.started.is_set()
    assert observation.cleanup_finished


@pytest.mark.asyncio
async def test_outer_cancellation_joins_the_poll_even_when_cancelled_again_during_cleanup(tmp_path: Path) -> None:
    gate = asyncio.Event()
    observation = _GatedCleanupObservation(gate)
    recovery, _monitor = _recovery(tmp_path, observation)
    task = asyncio.create_task(recovery.recover(staged=(), timeout=30.0, should_cancel=_never_cancel))
    await asyncio.wait_for(observation.started.wait(), 5)

    task.cancel()
    await asyncio.wait_for(observation.cleanup_started.wait(), 5)
    assert not task.done()
    task.cancel()
    gate.set()

    with pytest.raises(asyncio.CancelledError):
        await task
    assert observation.cleanup_finished and observation.poll_finished


@pytest.mark.asyncio
async def test_dispatch_deadline_bounds_provider_cleanup_and_joins_the_poll(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setattr(provider_downloads, "asyncio", ScopedAsyncio())
    monkeypatch.setattr(provider_downloads, "PROVIDER_POLL_CLEANUP_GRACE_SECONDS", 0.03)
    monkeypatch.setattr(action_deadline, "ACTION_DEADLINE_HEADROOM_MS", 0)
    # Keeps the deadline's re-arm from cancelling the poll, so only the join's own cancel can end it.
    monkeypatch.setattr(action_deadline, "DEADLINE_REARM_INTERVAL_SECONDS", 30.0)
    gate = asyncio.Event()
    # Released far past the grace so a CI runner pause (a late-shard full GC can take ~0.3 s) cannot finish the
    # cleanup inside the grace.
    _release_later(gate, 5.0)
    observation = _GatedCleanupObservation(gate)
    recovery = ProviderDownloadRecovery(observation, tmp_path, None)  # type: ignore[arg-type]

    with pytest.raises(ActionDeadlineExceeded):
        async with under_action_deadline(budget_ms=20):
            await recovery.recover(staged=(), timeout=0.05, should_cancel=_never_cancel)

    assert observation.cleanup_started.is_set()
    assert observation.poll_finished
    assert not observation.cleanup_finished


@pytest.mark.asyncio
async def test_admission_grace_never_outlasts_the_probe_budget(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    sleeps: list[float] = []

    async def _record(delay: float) -> None:
        sleeps.append(delay)

    monkeypatch.setattr(provider_downloads, "asyncio", ScopedAsyncio(sleep=_record))
    gate = asyncio.Event()
    gate.set()
    observation = _GatedCleanupObservation(gate)
    recovery, _monitor = _recovery(tmp_path, observation)

    await recovery.recover(staged=(), timeout=0.05, should_cancel=_never_cancel)

    assert sleeps
    assert all(delay <= 0.05 for delay in sleeps)
    assert observation.cleanup_finished or not observation.started.is_set()
