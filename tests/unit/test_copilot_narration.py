"""Factual Copilot activity survives sidecar retirement."""

import asyncio
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest

from skyvern.forge import app
from skyvern.forge.sdk.copilot.narration import (
    MAX_DESIGN_ACTIVITY_ENTRIES,
    NarratorState,
    build_tool_call_activity,
    build_tool_result_activity,
    narrator_poll_tick,
    tool_activity_display_label,
)
from skyvern.forge.sdk.copilot.tools import run_execution
from tests.unit.copilot_test_helpers import FakeCopilotStream, handback_ctx, terminal_extraction_block


@pytest.mark.asyncio
async def test_pending_block_progress_and_archive_without_narrator() -> None:
    state = NarratorState(current_iteration=7)
    stream = FakeCopilotStream()
    archive = {}
    seen = {}
    stamp = datetime(2026, 1, 1, tzinfo=timezone.utc)
    status = "running"

    async def fetch():
        return [SimpleNamespace(workflow_run_block_id="wrb_1", label="booking", block_type="code", status=status)]

    for index, current in enumerate(("running", "completed")):
        status = current
        await narrator_poll_tick(
            state,
            current_block_ts=stamp,
            prior_block_ts=None,
            last_block_fetch_monotonic=0,
            seen_block_states=seen,
            fetch_block_statuses=fetch,
            stream=stream,
            narrative_block_attempts=archive,
            workflow_run_id="wr_1",
        )
        assert stream.sent[index].status == current
        assert stream.sent[index].iteration == 7
    assert archive["wrb_1"]["rawStatus"] == "completed"
    assert archive["wrb_1"]["startedAt"] is not None
    assert archive["wrb_1"]["endedAt"] is not None
    assert state.running_block_id is None


def test_every_call_keeps_original_container_after_eviction_and_block_transition() -> None:
    state = NarratorState()
    stamp = datetime(2026, 1, 1, tzinfo=timezone.utc)
    state.record_activity(build_tool_call_activity("evaluate", 0, "first", timestamp=stamp))
    for i in range(MAX_DESIGN_ACTIVITY_ENTRIES + 1):
        state.record_activity(build_tool_call_activity("evaluate", i, str(i), timestamp=stamp))
    state.running_block_label = "booking"
    state.running_block_id = "wrb_booking"
    state.record_activity(build_tool_result_activity("evaluate", "Read the page", True, 0, "first", timestamp=stamp))
    assert state.design_activity[-1]["id"] == "tr-first"
    assert state.design_activity[-1]["activityBucket"] == {"kind": "design"}
    state.record_activity(build_tool_call_activity("click", 0, "block-call", timestamp=stamp))
    state.running_block_id = "wrb_other"
    state.record_activity(build_tool_result_activity("click", "Clicked", False, 1, "block-call", timestamp=stamp))
    assert [e["id"] for e in state.block_activity["wrb_booking"]] == ["tc-block-call", "tr-block-call"]


def test_activity_visibility_and_neutral_labels_survive_retirement() -> None:
    stamp = datetime(2026, 1, 1, tzinfo=timezone.utc)
    assert build_tool_call_activity("get_run_results", 0, "hidden", timestamp=stamp) is None
    assert build_tool_result_activity("get_browser_screenshot", "Image", True, 0, "hidden", timestamp=stamp) is None
    assert tool_activity_display_label("set_work_plan") == "Updating its plan"
    assert tool_activity_display_label("edit_block", {"label": "booking"}) == 'Editing block "Booking"'


@pytest.mark.asyncio
async def test_actual_run_polling_bridge_emits_progress_while_action_is_pending(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    ctx = await handback_ctx(monkeypatch, polled_status="running", block_status="running")
    stream = FakeCopilotStream()
    ctx.stream = stream
    ctx.narrator_state = NarratorState()
    stamp = datetime.now(timezone.utc)
    first = asyncio.Event()
    release = asyncio.Event()
    reads = 0

    async def progress(_ctx, _run_id):
        nonlocal reads
        reads += 1
        if reads == 1:
            return SimpleNamespace(status="running", modified_at=stamp), stamp
        if reads == 2:
            return SimpleNamespace(status="running", modified_at=stamp), stamp + timedelta(seconds=1)
        await release.wait()
        return (
            SimpleNamespace(status="completed", modified_at=stamp, failure_reason=None),
            stamp + timedelta(seconds=2),
        )

    async def blocks(**kwargs):
        if release.is_set():
            return [terminal_extraction_block("completed")]
        first.set()
        return [terminal_extraction_block("running")]

    monkeypatch.setattr(run_execution, "_read_progress_sources", progress)
    monkeypatch.setattr(app.DATABASE.observer, "get_workflow_run_blocks", blocks)
    task = asyncio.create_task(
        run_execution._run_blocks_and_collect_debug({"block_labels": ["extract_heading"], "parameters": {}}, ctx)
    )
    waiter = asyncio.create_task(first.wait())
    try:
        done, _ = await asyncio.wait({task, waiter}, timeout=5, return_when=asyncio.FIRST_COMPLETED)
        if task in done:
            raise AssertionError(f"Run returned before polling: {task.result()}")
        assert waiter in done
        for _ in range(50):
            if any(frame.type == "block_progress" and frame.status == "running" for frame in stream.sent):
                break
            await asyncio.sleep(0.01)
        assert not task.done()
        assert any(frame.type == "block_progress" and frame.status == "running" for frame in stream.sent)
        release.set()
        await asyncio.wait_for(task, 5)
        assert ctx.narrative_block_attempts["wrb_extract_heading"]["rawStatus"] == "completed"
    finally:
        release.set()
        if not waiter.done():
            waiter.cancel()
        if not task.done():
            task.cancel()
        await asyncio.gather(task, return_exceptions=True)
