from __future__ import annotations

from skyvern.forge.sdk.copilot.loop_detection import (
    detect_failed_tool_step_loop,
    detect_tool_loop,
    record_tool_step_result,
    tool_step_identity,
)


def test_returns_none_below_threshold() -> None:
    tracker: list[str] = []
    assert detect_tool_loop(tracker, "click") is None
    assert detect_tool_loop(tracker, "click") is None
    assert tracker == [tool_step_identity("click"), tool_step_identity("click")]


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
    assert tracker == [tool_step_identity("type_text")]


def test_requires_full_fresh_threshold_after_warning() -> None:
    tracker: list[str] = []
    detect_tool_loop(tracker, "click")
    detect_tool_loop(tracker, "click")
    assert detect_tool_loop(tracker, "click") is not None

    assert detect_tool_loop(tracker, "click") is None
    assert detect_tool_loop(tracker, "click") is None
    assert detect_tool_loop(tracker, "click") is not None


def test_distinct_argument_identities_never_fire_over_repeated_same_tool() -> None:
    tracker: list[str] = []
    for index in range(5):
        assert detect_tool_loop(tracker, "type_text", {"selector": f"#field-{index}", "text": f"v{index}"}) is None
    assert tracker == [tool_step_identity("type_text", {"selector": "#field-4", "text": "v4"})]


def test_third_identical_identity_fires_and_clears() -> None:
    tracker: list[str] = []
    args = {"selector": "#name", "text": "Ada"}
    assert detect_tool_loop(tracker, "type_text", args) is None
    assert detect_tool_loop(tracker, "type_text", args) is None
    msg = detect_tool_loop(tracker, "type_text", args)
    assert msg is not None
    assert "LOOP DETECTED" in msg
    assert "type_text" in msg
    assert tracker == []


class TestFailedToolStepLoopDetection:
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
