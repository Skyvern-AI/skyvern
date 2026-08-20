"""Isolation guarantees for the per-call background-artifact tracker introduced in SKY-11698.

The action-execution loop was extracted from ForgeAgent.agent_step into
_execute_step_actions. The in-flight background artifact task, previously held
by a nested closure over a `nonlocal` local, is now a _BackgroundArtifactTaskTracker
instantiated fresh inside agent_step on every call. ForgeAgent is a process-wide
singleton (app.agent), so concurrent agent_step calls share `self`; a tracker stored
on the instance or as a module global would let one call await — or clobber — another
call's background write. These tests pin that the tracker stays per-call.
"""

from __future__ import annotations

import asyncio

import pytest

import skyvern.forge.agent as agent_mod
from skyvern.forge.agent import _BackgroundArtifactTaskTracker
from skyvern.forge.sdk.core import skyvern_context
from skyvern.forge.sdk.models import StepStatus
from tests.unit.test_agent_step_characterization import make_agent_step_rig


class _RecordingTracker(_BackgroundArtifactTaskTracker):
    """Real tracker semantics plus a record of which task results each instance drained."""

    instances: list[_RecordingTracker] = []

    def __init__(self) -> None:
        super().__init__()
        self.drained_results: list[object] = []
        _RecordingTracker.instances.append(self)

    async def drain(self) -> None:
        if self.task is None:
            return
        task = self.task
        self.task = None
        try:
            self.drained_results.append(await task)
        except Exception:
            agent_mod.LOG.warning("Background artifact task failed, continuing", exc_info=True)


def _tag_background_writes(agent: agent_mod.ForgeAgent) -> None:
    """Make the background artifact task resolve to its owning task_id so drains are attributable."""

    async def _record(task, step, browser_state, engine, action) -> str:
        return task.task_id

    agent.record_artifacts_after_action = _record  # type: ignore[method-assign]


# --- _BackgroundArtifactTaskTracker contract (parity with the old closure) ---


@pytest.mark.asyncio
async def test_drain_is_noop_when_no_task() -> None:
    tracker = _BackgroundArtifactTaskTracker()
    assert tracker.task is None
    await tracker.drain()  # must not raise
    assert tracker.task is None


@pytest.mark.asyncio
async def test_drain_awaits_task_and_clears_it() -> None:
    awaited = asyncio.Event()

    async def _work() -> None:
        awaited.set()

    tracker = _BackgroundArtifactTaskTracker()
    tracker.task = asyncio.create_task(_work())
    await tracker.drain()

    assert awaited.is_set()
    assert tracker.task is None


@pytest.mark.asyncio
async def test_drain_swallows_task_exception() -> None:
    async def _boom() -> None:
        raise RuntimeError("artifact write failed")

    tracker = _BackgroundArtifactTaskTracker()
    tracker.task = asyncio.create_task(_boom())

    await tracker.drain()  # closure logged-and-continued; tracker must too

    assert tracker.task is None


# --- Per-call isolation through agent_step ---


@pytest.mark.asyncio
async def test_agent_step_uses_a_fresh_drained_tracker_per_call(monkeypatch: pytest.MonkeyPatch) -> None:
    _RecordingTracker.instances.clear()
    monkeypatch.setattr(agent_mod, "_BackgroundArtifactTaskTracker", _RecordingTracker)

    rig_a = make_agent_step_rig(monkeypatch, task_overrides={"task_id": "task-seq-a"})
    _tag_background_writes(rig_a.agent)
    rig_b = make_agent_step_rig(monkeypatch, task_overrides={"task_id": "task-seq-b"})
    _tag_background_writes(rig_b.agent)

    step_a, _ = await rig_a.run()
    step_b, _ = await rig_b.run()

    assert step_a.status == StepStatus.completed
    assert step_b.status == StepStatus.completed

    assert len(_RecordingTracker.instances) == 2
    first, second = _RecordingTracker.instances
    assert first is not second
    # each call drained (task cleared) and drained only its own background write
    assert first.task is None and second.task is None
    assert first.drained_results == ["task-seq-a"]
    assert second.drained_results == ["task-seq-b"]


@pytest.mark.asyncio
async def test_tracker_is_never_shared_state(monkeypatch: pytest.MonkeyPatch) -> None:
    rig = make_agent_step_rig(monkeypatch)
    _tag_background_writes(rig.agent)

    step, _ = await rig.run()
    assert step.status == StepStatus.completed

    # A stored-on-self tracker would let concurrent calls on the app.agent singleton bleed.
    assert not any(isinstance(v, _BackgroundArtifactTaskTracker) for v in vars(rig.agent).values())
    # A module-global tracker instance would bleed across every call.
    assert not any(isinstance(v, _BackgroundArtifactTaskTracker) for v in vars(agent_mod).values())


@pytest.mark.asyncio
async def test_concurrent_agent_steps_do_not_bleed_tracker_state(monkeypatch: pytest.MonkeyPatch) -> None:
    _RecordingTracker.instances.clear()
    monkeypatch.setattr(agent_mod, "_BackgroundArtifactTaskTracker", _RecordingTracker)

    rig_a = make_agent_step_rig(monkeypatch, task_overrides={"task_id": "task-cc-a"})
    rig_b = make_agent_step_rig(monkeypatch, task_overrides={"task_id": "task-cc-b"})

    # Hold both calls' background writes open until both are scheduled, forcing the
    # window where both trackers are simultaneously live.
    both_scheduled = asyncio.Event()
    started = 0

    async def _blocking_record(task, step, browser_state, engine, action) -> str:
        nonlocal started
        started += 1
        if started >= 2:
            both_scheduled.set()
        await both_scheduled.wait()
        return task.task_id

    rig_a.agent.record_artifacts_after_action = _blocking_record  # type: ignore[method-assign]
    rig_b.agent.record_artifacts_after_action = _blocking_record  # type: ignore[method-assign]

    # Separate asyncio tasks so each call gets its own copy of the skyvern_context ContextVar.
    async def _run(rig) -> StepStatus:
        skyvern_context.set(rig.context)
        try:
            step, _ = await rig.agent.agent_step(
                task=rig.task,
                step=rig.step,
                browser_state=rig.browser_state,
                organization=rig.organization,
            )
            return step.status
        finally:
            skyvern_context.reset()

    status_a, status_b = await asyncio.gather(
        asyncio.create_task(_run(rig_a)),
        asyncio.create_task(_run(rig_b)),
    )

    assert status_a == StepStatus.completed
    assert status_b == StepStatus.completed

    assert len(_RecordingTracker.instances) == 2
    # No tracker drained another call's background write.
    for tracker in _RecordingTracker.instances:
        assert len(set(tracker.drained_results)) == 1
    assert {r for t in _RecordingTracker.instances for r in t.drained_results} == {"task-cc-a", "task-cc-b"}
