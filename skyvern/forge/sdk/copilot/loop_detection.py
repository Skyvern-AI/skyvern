"""Shared loop detection utilities for copilot tool dispatch.

Detects consecutive same-tool streaks (e.g., A-A-A). Does not detect
oscillating patterns (e.g., A-B-A-B) — those are left for higher-layer
enforcement to catch.
"""

from __future__ import annotations

MAX_CONSECUTIVE_SAME_TOOL = 3


def detect_tool_loop(
    tracker: list[str],
    tool_name: str,
    threshold: int = MAX_CONSECUTIVE_SAME_TOOL,
) -> str | None:
    """Track tool invocation order and return a loop error message when threshold is hit."""
    tracker.append(tool_name)

    if len(tracker) >= threshold and len(set(tracker[-threshold:])) == 1:
        tracker.clear()
        return (
            f"LOOP DETECTED: '{tool_name}' has been called "
            f"{threshold} times consecutively. "
            "This tool will not run again. Use a DIFFERENT tool "
            "to continue, or produce your final JSON response."
        )

    if len(tracker) >= 2 and tracker[-1] != tracker[-2]:
        tracker.clear()
        tracker.append(tool_name)

    return None
