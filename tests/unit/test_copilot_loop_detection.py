from __future__ import annotations

import json
from types import SimpleNamespace
from unittest.mock import MagicMock

from skyvern.forge.sdk.copilot.context import CopilotContext
from skyvern.forge.sdk.copilot.loop_detection import (
    clear_failed_step_tracker_for_tools,
    detect_failed_tool_step_loop,
    detect_tool_loop,
    record_consecutive_tool_result_boundary,
    record_consecutive_tool_result_boundary_for_ctx,
    record_tool_step_result,
    record_tool_step_result_for_ctx,
)
from skyvern.forge.sdk.copilot.tools.run_execution import _diagnosis_repair_tool_error


def test_returns_none_below_threshold() -> None:
    tracker: list[str] = []
    assert detect_tool_loop(tracker, "click") is None
    assert detect_tool_loop(tracker, "click") is None
    assert tracker == ["click", "click"]


def test_fires_at_threshold_and_clears_tracker() -> None:
    tracker: list[str] = []
    detect_tool_loop(tracker, "click")
    detect_tool_loop(tracker, "click")
    msg = detect_tool_loop(tracker, "click")

    assert msg is not None
    assert "LOOP DETECTED" in msg
    assert "click" in msg
    assert tracker == []


def test_tool_switch_resets_tracker() -> None:
    tracker: list[str] = []
    detect_tool_loop(tracker, "click")
    detect_tool_loop(tracker, "click")
    assert detect_tool_loop(tracker, "type_text") is None
    assert tracker == ["type_text"]


def test_requires_full_fresh_threshold_after_warning() -> None:
    tracker: list[str] = []
    detect_tool_loop(tracker, "click")
    detect_tool_loop(tracker, "click")
    assert detect_tool_loop(tracker, "click") is not None

    assert detect_tool_loop(tracker, "click") is None
    assert detect_tool_loop(tracker, "click") is None
    assert detect_tool_loop(tracker, "click") is not None


def test_same_tool_result_boundary_does_not_double_count_dispatch() -> None:
    tracker = ["update_workflow"]

    record_consecutive_tool_result_boundary(tracker, "update_workflow")

    assert tracker == ["update_workflow"]


def test_different_tool_result_boundary_resets_to_new_tool() -> None:
    tracker = ["update_workflow", "update_workflow"]

    record_consecutive_tool_result_boundary(tracker, "inspect_page_for_composition")

    assert tracker == ["inspect_page_for_composition"]


def test_result_recording_makes_normal_tool_body_boundaries_visible() -> None:
    ctx = SimpleNamespace(consecutive_tool_tracker=["update_workflow", "update_workflow"])

    record_tool_step_result_for_ctx(
        ctx,
        "inspect_page_for_composition",
        {"target_url": "current_page"},
        {"ok": True, "data": {"summary": "observed"}},
    )

    assert ctx.consecutive_tool_tracker == ["inspect_page_for_composition"]


def test_workflow_progress_result_boundary_clears_tracker() -> None:
    progress_results = [
        ("update_workflow", {"ok": True, "_workflow": object()}),
        ("update_workflow", {"ok": False, "data": {"workflow_updated": True}}),
        ("update_and_run_blocks", {"ok": False, "data": {"workflow_updated": True}}),
        ("run_blocks_and_collect_debug", {"ok": True, "data": {"workflow_run_id": "wr_123"}}),
        ("update_and_run_blocks", {"ok": True, "data": {"workflow_run_id": "wr_123"}}),
    ]

    for tool_name, result in progress_results:
        ctx = SimpleNamespace(consecutive_tool_tracker=["update_workflow", "update_workflow"])
        record_consecutive_tool_result_boundary_for_ctx(ctx, tool_name, result)
        assert ctx.consecutive_tool_tracker == []


def test_get_run_results_workflow_run_id_is_not_run_creation_progress() -> None:
    ctx = SimpleNamespace(consecutive_tool_tracker=["update_workflow", "update_workflow"])

    record_consecutive_tool_result_boundary_for_ctx(
        ctx,
        "get_run_results",
        {"ok": True, "data": {"workflow_run_id": "wr_123"}},
    )

    assert ctx.consecutive_tool_tracker == ["get_run_results"]


def test_telco_mixed_dispatch_stream_does_not_trigger_consecutive_update_halt() -> None:
    ctx = SimpleNamespace(consecutive_tool_tracker=[])

    assert detect_tool_loop(ctx.consecutive_tool_tracker, "update_workflow") is None
    record_consecutive_tool_result_boundary_for_ctx(
        ctx,
        "update_workflow",
        {"ok": False, "error": "workflow yaml rejected"},
    )

    assert detect_tool_loop(ctx.consecutive_tool_tracker, "update_workflow") is None
    record_consecutive_tool_result_boundary_for_ctx(
        ctx,
        "update_workflow",
        {"ok": True, "_workflow": object()},
    )
    assert ctx.consecutive_tool_tracker == []

    record_consecutive_tool_result_boundary_for_ctx(
        ctx,
        "run_blocks_and_collect_debug",
        {"ok": True, "data": {"workflow_run_id": "wr_123"}},
    )
    assert ctx.consecutive_tool_tracker == []

    record_consecutive_tool_result_boundary_for_ctx(
        ctx,
        "update_and_run_blocks",
        {"ok": False, "error": "guardrail blocked the run"},
    )
    assert ctx.consecutive_tool_tracker == ["update_and_run_blocks"]

    record_consecutive_tool_result_boundary_for_ctx(
        ctx,
        "inspect_page_for_composition",
        {"ok": True, "data": {"observation_step": 4}},
    )
    assert ctx.consecutive_tool_tracker == ["inspect_page_for_composition"]

    assert detect_tool_loop(ctx.consecutive_tool_tracker, "update_workflow") is None


def test_strict_get_run_results_three_dispatches_still_halts() -> None:
    tracker: list[str] = []

    assert detect_tool_loop(tracker, "get_run_results") is None
    record_consecutive_tool_result_boundary(tracker, "get_run_results")
    assert detect_tool_loop(tracker, "get_run_results") is None
    record_consecutive_tool_result_boundary(tracker, "get_run_results")

    msg = detect_tool_loop(tracker, "get_run_results")

    assert msg is not None
    assert "LOOP DETECTED" in msg
    assert "get_run_results" in msg


def test_diagnosis_repair_tool_error_records_consecutive_boundary_only() -> None:
    ctx = CopilotContext(
        organization_id="o",
        workflow_id="w",
        workflow_permanent_id="wp",
        workflow_yaml="",
        browser_session_id=None,
        stream=MagicMock(),
    )
    ctx.consecutive_tool_tracker = ["update_workflow", "update_workflow"]
    ctx.failed_tool_step_tracker = {"sentinel": 2}

    payload = json.loads(_diagnosis_repair_tool_error(ctx, "run_blocks_and_collect_debug", "blocked"))

    assert payload == {"ok": False, "error": "blocked"}
    assert ctx.consecutive_tool_tracker == ["run_blocks_and_collect_debug"]
    assert ctx.failed_tool_step_tracker == {"sentinel": 2}


class TestLoopDetection:
    def test_loop_detected_on_third_consecutive_call(self) -> None:
        from skyvern.forge.sdk.copilot.loop_detection import detect_tool_loop

        tracker: list[str] = []
        assert detect_tool_loop(tracker, "update_workflow") is None
        assert detect_tool_loop(tracker, "update_workflow") is None
        error = detect_tool_loop(tracker, "update_workflow")
        assert error is not None
        assert "LOOP DETECTED" in error

    def test_tracker_resets_when_tool_changes(self) -> None:
        from skyvern.forge.sdk.copilot.loop_detection import detect_tool_loop

        tracker: list[str] = []
        assert detect_tool_loop(tracker, "update_workflow") is None
        assert detect_tool_loop(tracker, "list_credentials") is None
        assert tracker == ["list_credentials"]


class TestFailedToolStepLoopDetection:
    def test_interleaved_successful_tool_does_not_reset_failed_step(self) -> None:
        tracker: dict[str, int] = {}

        assert detect_failed_tool_step_loop(tracker, "get_browser_screenshot", {}) is None
        record_tool_step_result(tracker, "get_browser_screenshot", {}, {"ok": False, "error": "screenshot failed"})

        assert detect_failed_tool_step_loop(tracker, "get_run_results", {}) is None
        record_tool_step_result(tracker, "get_run_results", {}, {"ok": True, "data": {"status": "failed"}})

        assert detect_failed_tool_step_loop(tracker, "get_browser_screenshot", {}) is None
        record_tool_step_result(tracker, "get_browser_screenshot", {}, {"ok": False, "error": "screenshot failed"})

        assert detect_failed_tool_step_loop(tracker, "get_run_results", {}) is None
        record_tool_step_result(tracker, "get_run_results", {}, {"ok": True, "data": {"status": "failed"}})

        msg = detect_failed_tool_step_loop(tracker, "get_browser_screenshot", {})

        assert msg is not None
        assert "LOOP DETECTED" in msg
        assert "get_browser_screenshot" in msg

    def test_successful_same_step_resets_failure_streak(self) -> None:
        tracker: dict[str, int] = {}

        record_tool_step_result(tracker, "evaluate", {"script": "document.title"}, {"ok": False, "error": "boom"})
        record_tool_step_result(tracker, "evaluate", {"script": "document.title"}, {"ok": True, "data": "ok"})
        record_tool_step_result(tracker, "evaluate", {"script": "document.title"}, {"ok": False, "error": "boom"})

        assert detect_failed_tool_step_loop(tracker, "evaluate", {"script": "document.title"}) is None

    def test_different_arguments_do_not_share_failure_streak(self) -> None:
        tracker: dict[str, int] = {}

        record_tool_step_result(tracker, "click", {"selector": "#first"}, {"ok": False, "error": "missing"})
        record_tool_step_result(tracker, "click", {"selector": "#first"}, {"ok": False, "error": "missing"})

        assert detect_failed_tool_step_loop(tracker, "click", {"selector": "#second"}) is None
        assert detect_failed_tool_step_loop(tracker, "click", {"selector": "#first"}) is not None

    def test_block_running_credential_errors_share_failure_streak_across_arguments(self) -> None:
        tracker: dict[str, int] = {}

        record_tool_step_result(
            tracker,
            "run_blocks_and_collect_debug",
            {"block_labels": ["draft_a"], "parameters": {}},
            {"ok": False, "error": "Credential username not found by key: first"},
        )
        record_tool_step_result(
            tracker,
            "run_blocks_and_collect_debug",
            {"block_labels": ["draft_b"], "parameters": {}},
            {"ok": False, "error": "Credential username not found by key: second"},
        )

        msg = detect_failed_tool_step_loop(
            tracker,
            "run_blocks_and_collect_debug",
            {"block_labels": ["draft_c"], "parameters": {}},
        )

        assert msg is not None
        assert "LOOP DETECTED" in msg
        assert "CREDENTIAL_ERROR" in msg

    def test_generic_block_running_errors_still_key_by_arguments(self) -> None:
        tracker: dict[str, int] = {}

        record_tool_step_result(
            tracker,
            "run_blocks_and_collect_debug",
            {"block_labels": ["draft_a"], "parameters": {}},
            {"ok": False, "error": "temporary page state mismatch"},
        )
        record_tool_step_result(
            tracker,
            "run_blocks_and_collect_debug",
            {"block_labels": ["draft_b"], "parameters": {}},
            {"ok": False, "error": "temporary page state mismatch"},
        )

        assert (
            detect_failed_tool_step_loop(
                tracker,
                "run_blocks_and_collect_debug",
                {"block_labels": ["draft_c"], "parameters": {}},
            )
            is None
        )

    def test_block_threshold_is_two_failures(self) -> None:
        tracker: dict[str, int] = {}

        record_tool_step_result(tracker, "click", {"selector": "#x"}, {"ok": False, "error": "boom"})
        record_tool_step_result(tracker, "click", {"selector": "#x"}, {"ok": False, "error": "boom"})

        assert detect_failed_tool_step_loop(tracker, "click", {"selector": "#x"}) is not None

        fresh: dict[str, int] = {}
        record_tool_step_result(fresh, "click", {"selector": "#y"}, {"ok": False, "error": "boom"})
        assert detect_failed_tool_step_loop(fresh, "click", {"selector": "#y"}) is None

    def test_set_arguments_produce_stable_identity(self) -> None:
        tracker: dict[str, int] = {}
        args_a = {"keys": {"alpha", "beta", "gamma"}}
        args_b = {"keys": {"gamma", "alpha", "beta"}}

        record_tool_step_result(tracker, "press_keys", args_a, {"ok": False, "error": "boom"})
        record_tool_step_result(tracker, "press_keys", args_b, {"ok": False, "error": "boom"})

        assert detect_failed_tool_step_loop(tracker, "press_keys", args_a) is not None

    def test_clear_failed_step_tracker_for_tools_removes_only_named_tools(self) -> None:
        tracker: dict[str, int] = {}

        record_tool_step_result(tracker, "run_blocks_and_collect_debug", {"x": 1}, {"ok": False, "error": "boom"})
        record_tool_step_result(tracker, "run_blocks_and_collect_debug", {"x": 1}, {"ok": False, "error": "boom"})
        record_tool_step_result(tracker, "update_and_run_blocks", {"y": 2}, {"ok": False, "error": "boom"})
        record_tool_step_result(tracker, "click", {"selector": "#z"}, {"ok": False, "error": "boom"})

        clear_failed_step_tracker_for_tools(tracker, ["run_blocks_and_collect_debug", "update_and_run_blocks"])

        assert detect_failed_tool_step_loop(tracker, "run_blocks_and_collect_debug", {"x": 1}) is None
        assert detect_failed_tool_step_loop(tracker, "update_and_run_blocks", {"y": 2}) is None
        record_tool_step_result(tracker, "click", {"selector": "#z"}, {"ok": False, "error": "boom"})
        assert detect_failed_tool_step_loop(tracker, "click", {"selector": "#z"}) is not None

    def test_workflow_update_clears_block_running_failure_entries(self) -> None:
        from types import SimpleNamespace
        from unittest.mock import MagicMock

        from skyvern.forge.sdk.copilot.context import CopilotContext
        from skyvern.forge.sdk.copilot.tools import _record_workflow_update_result

        ctx = CopilotContext(
            organization_id="o",
            workflow_id="w",
            workflow_permanent_id="wp",
            workflow_yaml="updated yaml",
            browser_session_id=None,
            stream=MagicMock(),
        )
        record_tool_step_result(
            ctx.failed_tool_step_tracker,
            "run_blocks_and_collect_debug",
            {"block_labels": ["A"], "parameters": {}},
            {"ok": False, "error": "boom"},
        )
        record_tool_step_result(
            ctx.failed_tool_step_tracker,
            "run_blocks_and_collect_debug",
            {"block_labels": ["A"], "parameters": {}},
            {"ok": False, "error": "boom"},
        )
        record_tool_step_result(
            ctx.failed_tool_step_tracker,
            "click",
            {"selector": "#x"},
            {"ok": False, "error": "boom"},
        )

        _record_workflow_update_result(
            ctx,
            {
                "ok": True,
                "data": {"block_count": 2},
                "_workflow": SimpleNamespace(workflow_id="wf_new"),
            },
        )

        # A follow-up run after the user's fix must not be blocked.
        assert (
            detect_failed_tool_step_loop(
                ctx.failed_tool_step_tracker,
                "run_blocks_and_collect_debug",
                {"block_labels": ["A"], "parameters": {}},
            )
            is None
        )
        assert (
            detect_failed_tool_step_loop(
                ctx.failed_tool_step_tracker,
                "click",
                {"selector": "#x"},
            )
            is None
        )
        record_tool_step_result(
            ctx.failed_tool_step_tracker,
            "click",
            {"selector": "#x"},
            {"ok": False, "error": "boom"},
        )
        assert (
            detect_failed_tool_step_loop(
                ctx.failed_tool_step_tracker,
                "click",
                {"selector": "#x"},
            )
            is not None
        )
