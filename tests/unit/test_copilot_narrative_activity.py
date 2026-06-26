from __future__ import annotations

from datetime import datetime, timezone
from types import SimpleNamespace

from skyvern.forge.sdk.copilot.agent import _build_narrative_payload
from skyvern.forge.sdk.copilot.context import CopilotContext
from skyvern.forge.sdk.copilot.narration import (
    MAX_BLOCK_ACTIVITY_ENTRIES,
    MAX_DESIGN_ACTIVITY_ENTRIES,
    NarratorState,
    build_narration_activity,
    build_tool_call_activity,
    build_tool_result_activity,
)


def _ctx() -> CopilotContext:
    return CopilotContext(
        organization_id="org",
        workflow_id="wf",
        workflow_permanent_id="wfp",
        workflow_yaml="",
        browser_session_id=None,
        stream=SimpleNamespace(),  # type: ignore[arg-type]
    )


def _staged(*labels: str) -> SimpleNamespace:
    return SimpleNamespace(
        workflow_definition=SimpleNamespace(
            blocks=[SimpleNamespace(label=label, block_type="task") for label in labels]
        )
    )


def test_tool_call_activity_shape_and_denylist() -> None:
    entry = build_tool_call_activity("update_workflow", 3, "abc")
    assert entry == {
        "kind": "tool_call",
        "text": "Updating workflow…",
        "iteration": 3,
        "toolName": "update_workflow",
        "displayLabel": "Updating workflow",
        "id": "tc-abc",
    }
    assert "success" not in entry
    assert build_tool_call_activity("list_credentials", 0, "x") is None


def test_tool_result_activity_shape_falls_back_to_tool_name_and_denylist() -> None:
    entry = build_tool_result_activity("update_workflow", "Updated 2 blocks", True, 4, "abc")
    assert entry == {
        "kind": "tool_result",
        "text": "Updated 2 blocks",
        "iteration": 4,
        "toolName": "update_workflow",
        "displayLabel": "Updating workflow",
        "success": True,
        "id": "tr-abc",
    }
    assert build_tool_result_activity("update_workflow", "", False, 4, "abc")["text"] == "Updating workflow"
    assert build_tool_result_activity("get_browser_screenshot", "s", True, 0, "x") is None
    assert build_tool_result_activity("get_run_results", "s", True, 0, "x") is None


def test_narration_activity_shape() -> None:
    entry = build_narration_activity("Doing the thing", 5, datetime(2026, 1, 1, tzinfo=timezone.utc))
    assert entry == {
        "kind": "narration",
        "text": "Doing the thing",
        "iteration": 5,
        "id": "n-5-2026-01-01T00:00:00+00:00",
    }
    assert "toolName" not in entry


def test_emitted_progress_texts_is_a_fresh_per_state_set() -> None:
    # NarratorState is born and dies with one turn's CopilotContext, so the
    # set is per-turn by construction (no cross-turn leakage between states).
    first = NarratorState()
    first.emitted_progress_texts.add("Refining the workflow's code")
    second = NarratorState()
    assert second.emitted_progress_texts == set()


def test_record_activity_routes_to_design_when_no_block_running() -> None:
    state = NarratorState()
    state.record_activity(build_tool_call_activity("update_workflow", 0, "c1"))
    assert [e["id"] for e in state.design_activity] == ["tc-c1"]
    assert state.block_activity == {}


def test_record_activity_routes_to_running_block() -> None:
    state = NarratorState()
    state.running_block_label = "step_1"
    state.record_activity(build_tool_result_activity("run_blocks_and_collect_debug", "ran", True, 1, "c2"))
    assert [e["id"] for e in state.block_activity["step_1"]] == ["tr-c2"]
    assert state.design_activity == []


def test_record_activity_drops_denylisted_entries() -> None:
    state = NarratorState()
    state.running_block_label = "step_1"
    state.record_activity(build_tool_call_activity("list_credentials", 0, "c1"))
    state.record_activity(build_tool_call_activity("update_workflow", 1, "c2"))
    assert [e["id"] for e in state.block_activity["step_1"]] == ["tc-c2"]


def test_record_activity_caps_keep_most_recent() -> None:
    state = NarratorState()
    state.running_block_label = "b"
    for i in range(MAX_BLOCK_ACTIVITY_ENTRIES + 10):
        state.record_activity(build_tool_call_activity("t", i, f"c{i}"))
    bucket = state.block_activity["b"]
    assert len(bucket) == MAX_BLOCK_ACTIVITY_ENTRIES
    assert bucket[0]["iteration"] == 10
    assert bucket[-1]["iteration"] == MAX_BLOCK_ACTIVITY_ENTRIES + 9

    design_state = NarratorState()
    for i in range(MAX_DESIGN_ACTIVITY_ENTRIES + 5):
        design_state.record_activity(build_narration_activity(f"n{i}", i, datetime(2026, 1, 1, tzinfo=timezone.utc)))
    assert len(design_state.design_activity) == MAX_DESIGN_ACTIVITY_ENTRIES
    assert design_state.design_activity[0]["text"] == "n5"


def test_build_narrative_payload_serializes_block_and_design_activity() -> None:
    ctx = _ctx()
    ctx.staged_workflow = _staged("step_1", "step_2")  # type: ignore[assignment]
    ctx.has_staged_proposal = True
    ctx.block_state_map = {"step_1": "completed", "step_2": "running"}
    ctx.turn_id = "turn-1"
    ctx.turn_index = 2

    state = NarratorState()
    state.design_activity = [
        build_narration_activity("Planning the build", 0, datetime(2026, 1, 1, tzinfo=timezone.utc))
    ]
    state.block_activity = {
        "step_1": [build_tool_result_activity("run_blocks_and_collect_debug", "ran step_1", True, 1, "c1")]
    }
    ctx.narrator_state = state

    payload = _build_narrative_payload(ctx, terminal="response", terminal_message="done", narrative_summary="summary")

    assert payload["designActivity"] == [
        {"kind": "narration", "text": "Planning the build", "iteration": 0, "id": "n-0-2026-01-01T00:00:00+00:00"}
    ]
    blocks_by_label = {b["label"]: b for b in payload["blocks"]}
    assert blocks_by_label["step_1"]["activity"] == [
        {
            "kind": "tool_result",
            "text": "ran step_1",
            "iteration": 1,
            "toolName": "run_blocks_and_collect_debug",
            "displayLabel": "Testing workflow",
            "success": True,
            "id": "tr-c1",
        }
    ]
    assert blocks_by_label["step_2"]["activity"] == []


def test_build_narrative_payload_empty_when_no_narrator_state() -> None:
    ctx = _ctx()
    ctx.staged_workflow = _staged("step_1")  # type: ignore[assignment]
    ctx.has_staged_proposal = True
    ctx.narrator_state = None

    payload = _build_narrative_payload(ctx, terminal="response", terminal_message="done", narrative_summary=None)

    assert payload["designActivity"] == []
    assert payload["blocks"][0]["activity"] == []
