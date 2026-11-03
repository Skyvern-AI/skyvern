"""Thread-readable action-phase store: ordering through the action entry point, bounds, and cleanup."""

from __future__ import annotations

import asyncio
import contextvars
import gc
from datetime import UTC, datetime
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from structlog.testing import capture_logs

from skyvern.forge.sdk.core import skyvern_context
from skyvern.forge.sdk.core.skyvern_context import SkyvernContext
from skyvern.forge.sdk.models import StepStatus
from skyvern.forge.sdk.schemas.tasks import Task
from skyvern.webeye.actions import action_phase
from skyvern.webeye.actions.action_phase import ActionPhase
from skyvern.webeye.actions.actions import ActionType, ClickAction
from skyvern.webeye.actions.handler import ActionHandler
from skyvern.webeye.actions.responses import ActionSuccess
from tests.unit.helpers import make_organization, make_step, make_task


@pytest.fixture(autouse=True)
def _reset_store() -> Any:
    action_phase._reset_for_tests()
    yield
    action_phase._reset_for_tests()


def _rig() -> tuple:
    now = datetime.now(UTC)
    task = make_task(now, make_organization(now))
    step = make_step(now, task, step_id="step-1", status=StepStatus.created, order=0, output=None)
    return task, step, MagicMock(id_to_element_dict={"el": {"id": "el"}}), MagicMock()


def _app_mock() -> MagicMock:
    app_mock = MagicMock()
    app_mock.AGENT_FUNCTION.wait_for_challenge_solver = AsyncMock()
    return app_mock


def _current_phase() -> str:
    return action_phase.snapshot_action_phases()[0]["phase"]


async def _run_click(handler: Any, setup: Any, teardown: Any) -> list[Any]:
    task, step, scraped_page, page = _rig()
    with (
        patch("skyvern.webeye.actions.handler.app", _app_mock()),
        patch.dict(ActionHandler._handled_action_types, {ActionType.CLICK: handler}),
        patch.dict(ActionHandler._setup_action_types, {ActionType.CLICK: setup}, clear=True),
        patch.dict(ActionHandler._teardown_action_types, {ActionType.CLICK: teardown}, clear=True),
    ):
        return await asyncio.wait_for(
            ActionHandler._handle_action(
                scraped_page=scraped_page, task=task, step=step, page=page, action=ClickAction(element_id="el")
            ),
            timeout=2,
        )


@pytest.mark.asyncio
async def test_phases_follow_the_action_lifecycle_and_end_done() -> None:
    seen: list[str] = []

    async def setup(*_a: Any) -> list[Any]:
        seen.append(_current_phase())
        return []

    async def handler(*_a: Any) -> list[Any]:
        seen.append(_current_phase())
        action_phase.mark_action_phase(ActionPhase.CLICK)
        seen.append(_current_phase())
        return [ActionSuccess()]

    async def teardown(*_a: Any) -> list[Any]:
        seen.append(_current_phase())
        return []

    results = await _run_click(handler, setup, teardown)

    assert results[-1].success
    assert seen == ["setup", "handler", "click", "teardown"]
    [entry] = action_phase.snapshot_action_phases()
    assert entry["phase"] == "action_done"
    assert entry["action_type"] == "click"
    assert entry["phase_seq"] == 6


@pytest.mark.asyncio
async def test_handler_failure_still_reaches_action_done_and_releases_the_scope() -> None:
    async def setup(*_a: Any) -> list[Any]:
        return []

    async def handler(*_a: Any) -> list[Any]:
        action_phase.mark_action_phase(ActionPhase.OBSERVER_START)
        raise RuntimeError("boom")

    results = await _run_click(handler, setup, AsyncMock(return_value=[]))

    assert results[-1].success is False
    assert _current_phase() == "action_done"
    # Outside the action scope a stray marker must not mutate the finished action's record.
    action_phase.mark_action_phase(ActionPhase.CLICK)
    assert _current_phase() == "action_done"


def test_markers_never_log() -> None:
    with capture_logs() as logs:
        action_phase.begin_action("wr_1", "click", page=None)
        for phase in ActionPhase:
            action_phase.mark_action_phase(phase)
    assert logs == []


def test_store_is_bounded_and_cleared_on_run_exit() -> None:
    for i in range(action_phase.MAX_TRACKED_RUNS + 5):
        action_phase.begin_action(f"wr_{i}", "click", page=None)

    assert len(action_phase._slots) == action_phase.MAX_TRACKED_RUNS
    assert "wr_0" not in action_phase._slots

    newest = f"wr_{action_phase.MAX_TRACKED_RUNS + 4}"
    action_phase.clear_action_phases(newest)
    assert newest not in action_phase._slots
    assert len(action_phase.snapshot_action_phases()) == action_phase.SNAPSHOT_LIMIT


def test_stale_records_are_not_reported(monkeypatch: pytest.MonkeyPatch) -> None:
    clock = {"now": 1000.0}
    monkeypatch.setattr(action_phase, "_monotonic", lambda: clock["now"])
    action_phase.end_action(action_phase.begin_action("wr_old", "click", page=None))
    clock["now"] += action_phase.STALE_AFTER_SECONDS + 1

    assert action_phase.snapshot_action_phases() == []
    assert action_phase.page_probe_target() is None


def test_probe_target_holds_pages_weakly() -> None:
    class _Page:
        pass

    page, frame = _Page(), _Page()
    action_phase.begin_action("wr_1", "click", page=page)
    action_phase.mark_action_phase(ActionPhase.OBSERVER_LISTENING, observed_frame=frame)

    assert action_phase.page_probe_target() == (page, frame)
    del page, frame
    gc.collect()
    assert action_phase.page_probe_target() is None


def test_a_live_action_outranks_a_more_recently_finished_one(monkeypatch: pytest.MonkeyPatch) -> None:
    class _Page:
        pass

    clock = {"now": 100.0}
    monkeypatch.setattr(action_phase, "_monotonic", lambda: clock["now"])
    live_page, done_page = _Page(), _Page()
    action_phase.begin_action("wr_live", "click", page=live_page)
    action_phase.mark_action_phase(ActionPhase.OBSERVER_LISTENING)
    clock["now"] += 5
    done_token = action_phase.begin_action("wr_done", "input_text", page=done_page)
    clock["now"] += 5
    action_phase.end_action(done_token)

    snapshot = action_phase.snapshot_action_phases()
    assert [(entry["run_id"], entry["phase"]) for entry in snapshot] == [
        ("wr_live", "observer_listening"),
        ("wr_done", "action_done"),
    ]
    assert action_phase.page_probe_target() == (live_page, None)


@pytest.mark.asyncio
async def test_teardown_under_a_task_v2_identity_clears_the_record_keyed_by_its_workflow_run() -> None:
    skyvern_context.set(
        SkyvernContext(workflow_run_id="wr_v2_run", root_workflow_run_id="wr_v2_run", task_v2_id="tsk_v2_1")
    )
    try:
        await _run_click(
            AsyncMock(return_value=[ActionSuccess()]), AsyncMock(return_value=[]), AsyncMock(return_value=[])
        )
    finally:
        skyvern_context.reset()
    assert [entry["run_id"] for entry in action_phase.snapshot_action_phases()] == ["wr_v2_run"]

    action_phase.clear_action_phases("tsk_v2_1")

    assert action_phase.snapshot_action_phases() == []


async def _run_click_with(task: Any, handler: Any) -> list[Any]:
    _, step, scraped_page, page = _rig()
    with (
        patch("skyvern.webeye.actions.handler.app", _app_mock()),
        patch.dict(ActionHandler._handled_action_types, {ActionType.CLICK: handler}),
        patch.dict(ActionHandler._setup_action_types, {}, clear=True),
        patch.dict(ActionHandler._teardown_action_types, {}, clear=True),
    ):
        return await ActionHandler._handle_action(
            scraped_page=scraped_page, task=task, step=step, page=page, action=ClickAction(element_id="el")
        )


@pytest.mark.asyncio
async def test_task_without_run_fields_still_runs_and_is_tracked_by_its_task_id() -> None:
    task = MagicMock(spec=Task, task_id="tsk_spec", organization_id="o_test", status="running")

    results = await _run_click_with(task, AsyncMock(return_value=[ActionSuccess()]))

    assert results[-1].success
    assert [entry["run_id"] for entry in action_phase.snapshot_action_phases()] == ["tsk_spec"]


def test_in_flight_action_survives_the_stale_cutoff_but_a_finished_one_does_not(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    clock = {"now": 1000.0}
    monkeypatch.setattr(action_phase, "_monotonic", lambda: clock["now"])
    action_phase.end_action(action_phase.begin_action("wr_done", "click", page=None))
    action_phase.begin_action("wr_long_wait", "wait", page=None)
    clock["now"] += action_phase.STALE_AFTER_SECONDS + 300

    assert [(e["run_id"], e["phase"]) for e in action_phase.snapshot_action_phases()] == [
        ("wr_long_wait", "action_start")
    ]


@pytest.mark.asyncio
async def test_sibling_child_runs_under_one_root_keep_separate_records() -> None:
    both_started = asyncio.Barrier(3)
    release = asyncio.Event()

    async def handler(*_a: Any) -> list[Any]:
        await both_started.wait()
        await release.wait()
        return [ActionSuccess()]

    async def run_child(child_run_id: str) -> None:
        skyvern_context.set(SkyvernContext(workflow_run_id=child_run_id, root_workflow_run_id="wr_root"))
        task, step, scraped_page, page = _rig()
        await ActionHandler._handle_action(
            scraped_page=scraped_page, task=task, step=step, page=page, action=ClickAction(element_id="el")
        )

    # Patched once here: entering the same patch in two overlapping tasks restores the wrong original.
    with (
        patch("skyvern.webeye.actions.handler.app", _app_mock()),
        patch.dict(ActionHandler._handled_action_types, {ActionType.CLICK: handler}),
        patch.dict(ActionHandler._setup_action_types, {}, clear=True),
        patch.dict(ActionHandler._teardown_action_types, {}, clear=True),
    ):
        children = [asyncio.create_task(run_child(run_id)) for run_id in ("wr_child_a", "wr_child_b")]
        await asyncio.wait_for(both_started.wait(), timeout=2)
        in_flight = {(e["run_id"], e["phase"]) for e in action_phase.snapshot_action_phases()}
        release.set()
        await asyncio.gather(*children)

    assert in_flight == {("wr_child_a", "handler"), ("wr_child_b", "handler")}
    action_phase.clear_action_phases("wr_root")
    assert action_phase.snapshot_action_phases() == []


def test_concurrent_actions_in_one_run_are_retained_up_to_the_per_run_bound() -> None:
    class _Page:
        pass

    pages = [_Page() for _ in range(action_phase.MAX_RECORDS_PER_RUN + 2)]
    for index, page in enumerate(pages):
        contextvars.copy_context().run(action_phase.begin_action, "wr_shared", f"click_{index}", page)

    snapshot = action_phase.snapshot_action_phases()
    kept = min(action_phase.MAX_RECORDS_PER_RUN, action_phase.SNAPSHOT_LIMIT)
    assert [e["action_type"] for e in snapshot] == [f"click_{index}" for index in reversed(range(len(pages)))][:kept]
    assert len(action_phase._slots["wr_shared"]) == action_phase.MAX_RECORDS_PER_RUN
    assert action_phase.page_probe_target() == (pages[-1], None)


def test_a_new_action_supersedes_the_runs_finished_records() -> None:
    for index in range(10):
        action_phase.end_action(action_phase.begin_action("wr_seq", f"click_{index}", page=None))

    assert [e["action_type"] for e in action_phase.snapshot_action_phases()] == ["click_9"]
    assert len(action_phase._slots["wr_seq"]) == 1


def test_eviction_prefers_runs_with_nothing_in_flight() -> None:
    action_phase.begin_action("wr_live_oldest", "click", page=None)
    for index in range(action_phase.MAX_TRACKED_RUNS):
        action_phase.end_action(action_phase.begin_action(f"wr_done_{index}", "click", page=None))

    assert len(action_phase._slots) == action_phase.MAX_TRACKED_RUNS
    assert "wr_live_oldest" in action_phase._slots
    assert "wr_done_0" not in action_phase._slots
