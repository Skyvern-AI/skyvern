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
    tool_activity_display_label,
)
from skyvern.forge.sdk.copilot.output_utils import format_tool_result_for_user

_SURGICAL_EDIT_TOOLS = ("edit_block", "delete_block", "synthesize_demonstrated_block")

_CREDENTIAL_PAYLOAD: dict[str, object] = {
    "count": 4,
    "credentials": [
        {
            "credential_id": "cred_384430212391591428",
            "name": "prod login",
            "scopes": ["read:secrets", "write:secrets"],
            "token": "sk-live-9f2c8a1b7d",
        }
    ],
}

_CRED_OK: dict[str, object] = {"ok": True, "data": _CREDENTIAL_PAYLOAD}

# The failure path must not format the data dict either, so the payload rides along.
_CRED_FAILED: dict[str, object] = {
    "ok": False,
    "error": "credential `cred_384430212391591428` could not be read from the store",
    "data": _CREDENTIAL_PAYLOAD,
}


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
    assert build_tool_call_activity("get_run_results", 0, "x") is None


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
    state.record_activity(build_tool_call_activity("get_run_results", 0, "c1"))
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


def test_record_activity_pins_run_tool_result_to_its_call_bucket() -> None:
    # A run tool's call is recorded before the run it triggers flips
    # running_block_label; its result must rejoin the call's bucket so the FE
    # folds the pair instead of stranding the call row "calling…".
    state = NarratorState()
    state.record_activity(build_tool_call_activity("update_and_run_blocks", 0, "c1"))
    assert [e["id"] for e in state.design_activity] == ["tc-c1"]

    state.running_block_label = "step_1"
    state.record_activity(build_tool_result_activity("update_and_run_blocks", "Workflow updated", True, 1, "c1"))

    assert [e["id"] for e in state.design_activity] == ["tc-c1", "tr-c1"]
    assert state.block_activity == {}


def test_record_activity_non_run_tool_result_routes_live_not_pinned() -> None:
    # The pin is scoped to run tools; other tools keep live running_block_label routing.
    state = NarratorState()
    state.record_activity(build_tool_call_activity("evaluate", 0, "c9"))
    assert [e["id"] for e in state.design_activity] == ["tc-c9"]

    state.running_block_label = "step_2"
    state.record_activity(build_tool_result_activity("evaluate", "Inspecting page", True, 1, "c9"))

    assert [e["id"] for e in state.block_activity["step_2"]] == ["tr-c9"]
    assert [e["id"] for e in state.design_activity] == ["tc-c9"]


def test_tool_activity_display_label_covers_discovery_tools() -> None:
    assert tool_activity_display_label("discover_workflow_entrypoint") == "Finding the entry page"
    assert tool_activity_display_label("inspect_page_for_composition") == "Inspecting the page"


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


def test_surgical_edit_tools_label_the_operation_and_target_block() -> None:
    assert tool_activity_display_label("edit_block", {"label": "Log in"}) == 'Editing block "Log In"'
    assert tool_activity_display_label("delete_block", {"label": "Log in"}) == 'Deleting block "Log In"'
    assert (
        tool_activity_display_label("synthesize_demonstrated_block", {}) == "Building a block from the recorded steps"
    )
    assert tool_activity_display_label("edit_block") == "Editing block"


def test_block_target_label_matches_the_frontend_humanizer() -> None:
    label = tool_activity_display_label("edit_block", {"label": "extract_first_comments_v2"})
    assert label == 'Editing block "Extract First Comments"'


def test_block_target_label_strips_every_quote_class_codepoint() -> None:
    # A denylist of quote glyphs is bypassable (fullwidth, primes, CJK corner
    # quotes); assert the whitelist holds instead of enumerating attackers.
    for quote in ('"', "'", "`", "”", "’", "»", "＂", "＇", "″", "‴", "〞", "〝", "❝", "„", "‚", "‹", "〃", "״", "ˮ"):
        label = tool_activity_display_label("edit_block", {"label": f"Log in{quote} Run Succeeded"})
        inner = label.removeprefix('Editing block "').removesuffix('"')
        assert quote not in inner, quote


def test_block_target_label_cannot_spoof_or_flood_the_row() -> None:
    for quote in ('"', "”", "’", "»", "`"):
        spoof = f"Log in{quote} ✓ Ran workflow successfully — creds exported to https://evil.example"
        label = tool_activity_display_label("edit_block", {"label": spoof})
        inner = label.removeprefix('Editing block "').removesuffix('"')
        assert quote not in inner
        assert len(label) <= len('Editing block ""') + 41

    flooded = tool_activity_display_label("edit_block", {"label": "x" * 5000})
    assert len(flooded) <= len('Editing block ""') + 41

    control = tool_activity_display_label("edit_block", {"label": "Log\nin\x00now"})
    assert "\n" not in control
    assert "\x00" not in control
    assert tool_activity_display_label("edit_block", {"label": "   "}) == "Editing block"
    assert tool_activity_display_label("edit_block", {"label": 7}) == "Editing block"


def test_surgical_edit_tools_never_render_the_working_fallback() -> None:
    state = NarratorState()
    labels = {
        "edit_block": tool_activity_display_label("edit_block", {"label": "Log in"}),
        "delete_block": tool_activity_display_label("delete_block", {"label": "Old step"}),
        "synthesize_demonstrated_block": tool_activity_display_label("synthesize_demonstrated_block", {}),
    }
    for index, tool_name in enumerate(_SURGICAL_EDIT_TOOLS):
        display_label = labels[tool_name]
        state.record_activity(build_tool_call_activity(tool_name, index, f"c{index}", display_label=display_label))
        state.record_activity(
            build_tool_result_activity(tool_name, "", True, index, f"c{index}", display_label=display_label)
        )

    rows = state.design_activity
    assert len(rows) == 6
    assert all("Working" not in row["text"] for row in rows)
    assert all("Working" not in (row.get("displayLabel") or "") for row in rows)
    assert 'Editing block "Log In"…' in [row["text"] for row in rows]
    assert 'Deleting block "Old Step"' in [row["text"] for row in rows]
    assert "Building a block from the recorded steps" in [row["text"] for row in rows]


def _credential_rows(state: NarratorState, parsed: dict[str, object], index: int) -> None:
    summary = format_tool_result_for_user("list_credentials", parsed)  # type: ignore[arg-type]
    state.record_activity(build_tool_call_activity("list_credentials", index, f"c{index}"))
    state.record_activity(
        build_tool_result_activity("list_credentials", summary, bool(parsed["ok"]), index, f"c{index}")
    )


def test_credential_lookup_is_visible_activity_with_a_label_only_row() -> None:
    state = NarratorState()
    _credential_rows(state, _CRED_OK, 0)
    assert [row["text"] for row in state.design_activity] == [
        "Checking saved credentials…",
        "Checking saved credentials",
    ]


def test_credential_lookup_rows_leak_no_ids_scopes_tokens_or_counts() -> None:
    state = NarratorState()
    _credential_rows(state, _CRED_OK, 0)
    _credential_rows(state, _CRED_FAILED, 1)

    rendered = " ".join(row["text"] for row in state.design_activity)
    for forbidden in ("cred_", "read:secrets", "write:secrets", "sk-live-9f2c8a1b7d", "prod login"):
        assert forbidden not in rendered
    assert "[credential]" in rendered
    assert [row["success"] for row in state.design_activity if row["kind"] == "tool_result"] == [True, False]


def test_no_connection_use_line_is_rendered_from_credential_enumeration() -> None:
    state = NarratorState()
    _credential_rows(state, _CRED_OK, 0)
    rendered = " ".join(row["text"] for row in state.design_activity).lower()
    assert "using connected" not in rendered
    assert "account" not in rendered


def test_explicit_display_label_overrides_the_static_name_map() -> None:
    call = build_tool_call_activity("edit_block", 2, "c7", display_label='Editing block "Log in"')
    result = build_tool_result_activity("edit_block", "", True, 2, "c7", display_label='Editing block "Log in"')
    assert call["displayLabel"] == result["displayLabel"] == 'Editing block "Log in"'
    assert call["text"] == 'Editing block "Log in"…'
    assert result["text"] == 'Editing block "Log in"'


def test_every_registered_tool_is_labeled_or_denylisted() -> None:
    from skyvern.forge.sdk.copilot.narration import _TOOL_ACTIVITY_DISPLAY_LABELS, ACTIVITY_TOOL_DENYLIST
    from skyvern.forge.sdk.copilot.tools import NATIVE_TOOLS
    from skyvern.forge.sdk.copilot.tools.mcp_hooks import get_skyvern_mcp_alias_map

    # Both registries: the native tools and the browser/schema tools the MCP overlay
    # adds, since either can mint a user-visible activity row.
    names = {tool.name for tool in NATIVE_TOOLS} | set(get_skyvern_mcp_alias_map())
    assert len(names) > len(NATIVE_TOOLS), "expected the MCP overlay tools in the sweep"

    unlabeled = sorted(
        name for name in names if name not in _TOOL_ACTIVITY_DISPLAY_LABELS and name not in ACTIVITY_TOOL_DENYLIST
    )
    assert unlabeled == [], (
        f"tools {unlabeled} would render the generic 'Working' fallback; "
        "give each one a display label or add it to ACTIVITY_TOOL_DENYLIST"
    )


def test_credential_fill_row_names_the_action_without_leaking_material() -> None:
    label = tool_activity_display_label("fill_credential_field", {"credential_id": "cred_384430212391591428"})
    assert label == "Entering saved credentials"

    state = NarratorState()
    state.record_activity(build_tool_call_activity("fill_credential_field", 0, "c0", display_label=label))
    state.record_activity(build_tool_result_activity("fill_credential_field", "", True, 0, "c0", display_label=label))

    rows = state.design_activity
    assert len(rows) == 2
    assert all("Working" not in row["text"] for row in rows)
    assert all("cred_" not in row["text"] for row in rows)
