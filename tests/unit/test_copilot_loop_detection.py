from __future__ import annotations

from skyvern.forge.sdk.copilot.loop_detection import detect_tool_loop


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
