"""Regression: concurrent ``_start_frame_publisher`` for one key must not
orphan a publisher loop. The lock around check/create/start/store serializes
starts so exactly one publisher is created per stream key.
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from typing import Any

import pytest

from skyvern.webeye import real_browser_manager as manager_module
from skyvern.webeye.real_browser_manager import RealBrowserManager


def _marked_state() -> SimpleNamespace:
    return SimpleNamespace(
        browser_artifacts=SimpleNamespace(needs_cdp_frame_publisher=True),
        add_on_close=lambda _cb: None,
    )


@pytest.mark.asyncio
async def test_start_frame_publisher_serializes_concurrent_starts(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    instances: list[Any] = []
    barrier = asyncio.Event()
    first_start_entered = asyncio.Event()

    class _BlockingPublisher:
        def __init__(
            self,
            *,
            browser_state: Any,
            stream_key: str,
            organization_id: str,
        ) -> None:
            self.stream_key = stream_key
            self.organization_id = organization_id
            self.start_calls = 0
            self.stop_calls = 0
            instances.append(self)

        async def start(self) -> None:
            self.start_calls += 1
            first_start_entered.set()
            # Block here so a second concurrent call gets a chance to race the
            # registry check before this publisher is stored.
            await barrier.wait()

        async def stop(self) -> None:
            self.stop_calls += 1

    monkeypatch.setattr(manager_module, "CDPFramePublisher", _BlockingPublisher)

    manager = RealBrowserManager()

    task_a = asyncio.create_task(
        manager._start_frame_publisher(
            browser_state=_marked_state(),
            workflow_run_id="wr_race",
            organization_id="o_1",
        )
    )
    # Wait until publisher A is suspended inside start(); without the lock fix
    # this is exactly the window where publisher B can slip past the registry
    # check.
    await first_start_entered.wait()

    task_b = asyncio.create_task(
        manager._start_frame_publisher(
            browser_state=_marked_state(),
            workflow_run_id="wr_race",
            organization_id="o_1",
        )
    )
    # Give task B a turn so it advances as far as it can (either to the lock
    # under the fix, or into the factory under the bug).
    for _ in range(5):
        await asyncio.sleep(0)

    barrier.set()
    await asyncio.gather(task_a, task_b)

    assert len(instances) == 1, (
        f"expected exactly one publisher under concurrent start, got {len(instances)} — "
        "the check-then-await-then-store race lets a second publisher slip past the "
        "registry check before the first is stored, then orphans the loser."
    )
    assert instances[0].start_calls == 1
    assert manager._frame_publishers["wr_race.png"] is instances[0]
    # No orphan loop: the single publisher we created can be stopped through
    # the registry, with no second instance lingering.
    await manager._stop_frame_publisher(workflow_run_id="wr_race")
    assert instances[0].stop_calls == 1
    assert manager._frame_publishers == {}


@pytest.mark.asyncio
async def test_on_close_callback_pops_under_publisher_lock(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The ``add_on_close`` callback registered in ``_start_frame_publisher``
    must pop ``_frame_publishers`` under ``_publisher_lock`` so a concurrent
    re-start cannot read a stale registry between the pop and the publisher's
    actual stop."""
    callbacks: list[Any] = []

    class _FakePublisher:
        def __init__(self, *, browser_state: Any, stream_key: str, organization_id: str) -> None:
            self.stop_calls = 0

        async def start(self) -> None:
            return None

        async def stop(self) -> None:
            self.stop_calls += 1

    monkeypatch.setattr(manager_module, "CDPFramePublisher", _FakePublisher)

    state = SimpleNamespace(
        browser_artifacts=SimpleNamespace(needs_cdp_frame_publisher=True),
        add_on_close=lambda cb: callbacks.append(cb),
    )

    manager = RealBrowserManager()
    await manager._start_frame_publisher(
        browser_state=state,
        workflow_run_id="wr_lock",
        organization_id="o_1",
    )
    assert "wr_lock.png" in manager._frame_publishers
    assert len(callbacks) == 1

    # Hold the lock from outside, then fire the on-close callback. It must wait
    # on the lock before popping rather than racing the start path.
    async with manager._publisher_lock:
        cb_task = asyncio.create_task(callbacks[0]())
        for _ in range(5):
            await asyncio.sleep(0)
        # Lock still held by us, so the callback should not have popped yet.
        assert "wr_lock.png" in manager._frame_publishers
        assert not cb_task.done()

    await cb_task
    assert "wr_lock.png" not in manager._frame_publishers
