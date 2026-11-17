"""Shared loop detection for agent tool dispatch.

``detect_tool_loop`` fires on strictly consecutive same-identity streaks (A-A-A over one tool_step_identity). It
resets the moment the identity changes, so oscillating and argument-distinct patterns bypass it by design.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping
from typing import Any

MAX_CONSECUTIVE_SAME_TOOL = 3
LOOP_DETECTED_MARKER = "LOOP DETECTED:"


def detect_tool_loop(
    tracker: list[str],
    tool_name: str,
    arguments: Mapping[str, Any] | None = None,
    threshold: int = MAX_CONSECUTIVE_SAME_TOOL,
) -> str | None:
    """Track tool step identities and return a loop error message when threshold is hit."""
    identity = tool_step_identity(tool_name, arguments)
    tracker.append(identity)

    if len(tracker) >= threshold and len(set(tracker[-threshold:])) == 1:
        tracker.clear()
        return (
            f"{LOOP_DETECTED_MARKER} '{tool_name}' has been called "
            f"{threshold} times consecutively. "
            "This tool will not run again. Use a DIFFERENT tool "
            "to continue, or call `reply` with your final response."
        )

    if len(tracker) >= 2 and tracker[-1] != tracker[-2]:
        tracker.clear()
        tracker.append(identity)

    return None


def _normalize_step_argument(value: Any) -> Any:
    if isinstance(value, Mapping):
        return {str(k): _normalize_step_argument(v) for k, v in sorted(value.items(), key=lambda item: str(item[0]))}
    if isinstance(value, list | tuple):
        return [_normalize_step_argument(item) for item in value]
    if isinstance(value, frozenset | set):
        return sorted((_normalize_step_argument(item) for item in value), key=repr)
    if isinstance(value, str | int | float | bool) or value is None:
        return value
    return repr(value)


def tool_step_identity(tool_name: str, arguments: Mapping[str, Any] | None = None) -> str:
    normalized = _normalize_step_argument(arguments or {})
    payload = json.dumps(normalized, sort_keys=True, separators=(",", ":"))
    digest = hashlib.sha256(payload.encode("utf-8")).hexdigest()
    return f"{tool_name}:{digest}"
