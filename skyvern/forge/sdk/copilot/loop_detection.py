"""Shared loop detection utilities for copilot tool dispatch.

Two independent guards:

* ``detect_tool_loop`` fires on strictly consecutive same-tool streaks
  (A-A-A). Resets the moment the tool name changes, so oscillating
  patterns (A-B-A-B) bypass it by design.
* ``detect_failed_tool_step_loop`` fires on N repeated failures of the
  same (tool, args) pair, even when other tools dispatch in between.
  Block-running credential/config failures are keyed by failure category
  instead, because draft arguments can change while the init failure does not.
  A successful invocation of the same step resets its counter.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Iterable, Mapping, MutableMapping
from typing import Any

from skyvern.forge.failure_classifier import classify_from_failure_reason
from skyvern.forge.sdk.copilot.blocker_signal import maybe_clear_blocker_signal_on_tool_success

MAX_CONSECUTIVE_SAME_TOOL = 3
MAX_REPEATED_FAILED_STEP = 3
LOOP_DETECTED_MARKER = "LOOP DETECTED:"
ARGUMENT_INSENSITIVE_FAILURE_TOOLS = frozenset({"run_blocks_and_collect_debug", "update_and_run_blocks"})
ARGUMENT_INSENSITIVE_FAILURE_CATEGORIES = frozenset({"CREDENTIAL_ERROR", "PARAMETER_BINDING_ERROR"})


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
            f"{LOOP_DETECTED_MARKER} '{tool_name}' has been called "
            f"{threshold} times consecutively. "
            "This tool will not run again. Use a DIFFERENT tool "
            "to continue, or produce your final JSON response."
        )

    if len(tracker) >= 2 and tracker[-1] != tracker[-2]:
        tracker.clear()
        tracker.append(tool_name)

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


def _failure_category_identity(tool_name: str, category: str) -> str:
    return f"{tool_name}:failure_category:{category}"


def _argument_insensitive_failure_category(result: Mapping[str, Any]) -> str | None:
    data = result.get("data")
    if isinstance(data, Mapping):
        raw_categories = data.get("failure_categories")
        if isinstance(raw_categories, list):
            for raw_category in raw_categories:
                if not isinstance(raw_category, Mapping):
                    continue
                category = raw_category.get("category")
                if isinstance(category, str) and category in ARGUMENT_INSENSITIVE_FAILURE_CATEGORIES:
                    return category

        failure_reason = data.get("failure_reason")
        if isinstance(failure_reason, str):
            categories = classify_from_failure_reason(failure_reason)
            for raw_category in categories or []:
                category = raw_category.get("category")
                if isinstance(category, str) and category in ARGUMENT_INSENSITIVE_FAILURE_CATEGORIES:
                    return category

    error = result.get("error")
    if isinstance(error, str):
        categories = classify_from_failure_reason(error)
        for raw_category in categories or []:
            category = raw_category.get("category")
            if isinstance(category, str) and category in ARGUMENT_INSENSITIVE_FAILURE_CATEGORIES:
                return category

    return None


def _argument_insensitive_failure_identity(tool_name: str, result: Mapping[str, Any]) -> str | None:
    if tool_name not in ARGUMENT_INSENSITIVE_FAILURE_TOOLS:
        return None
    category = _argument_insensitive_failure_category(result)
    return _failure_category_identity(tool_name, category) if category else None


def _clear_argument_insensitive_failure_identities(tracker: MutableMapping[str, int], tool_name: str) -> None:
    prefix = f"{tool_name}:failure_category:"
    for key in list(tracker):
        if key.startswith(prefix):
            del tracker[key]


def _detect_argument_insensitive_failed_tool_loop(
    tracker: MutableMapping[str, int],
    tool_name: str,
    threshold: int,
) -> tuple[str, int] | None:
    if tool_name not in ARGUMENT_INSENSITIVE_FAILURE_TOOLS:
        return None

    prefix = f"{tool_name}:failure_category:"
    for key, failure_count in tracker.items():
        if not key.startswith(prefix):
            continue
        next_attempt = failure_count + 1
        if next_attempt >= threshold:
            return key.removeprefix(prefix), failure_count
    return None


def detect_failed_tool_step_loop(
    tracker: MutableMapping[str, int],
    tool_name: str,
    arguments: Mapping[str, Any] | None = None,
    threshold: int = MAX_REPEATED_FAILED_STEP,
) -> str | None:
    if not tracker:
        return None

    category_failure = _detect_argument_insensitive_failed_tool_loop(tracker, tool_name, threshold)
    if category_failure is not None:
        category, failure_count = category_failure
        next_attempt = failure_count + 1
        return (
            f"{LOOP_DETECTED_MARKER} '{tool_name}' has already failed "
            f"{failure_count} times with {category}; blocking attempt #{next_attempt}. "
            "This failure is not tied to the draft arguments. Fix the credential/configuration, "
            "ask the user, or produce your final JSON response."
        )

    identity = tool_step_identity(tool_name, arguments)
    failure_count = tracker.get(identity, 0)
    next_attempt = failure_count + 1
    if next_attempt < threshold:
        return None

    return (
        f"{LOOP_DETECTED_MARKER} '{tool_name}' has already failed "
        f"{failure_count} consecutive times with these arguments; "
        f"blocking attempt #{next_attempt}. "
        "Use different arguments, a DIFFERENT tool, ask the user, "
        "or produce your final JSON response."
    )


def record_tool_step_result(
    tracker: MutableMapping[str, int],
    tool_name: str,
    arguments: Mapping[str, Any] | None,
    result: Mapping[str, Any],
    threshold: int = MAX_REPEATED_FAILED_STEP,
) -> None:
    identity = tool_step_identity(tool_name, arguments)
    if result.get("ok", True):
        tracker.pop(identity, None)
        _clear_argument_insensitive_failure_identities(tracker, tool_name)
        return

    identity = _argument_insensitive_failure_identity(tool_name, result) or identity
    tracker[identity] = min(tracker.get(identity, 0) + 1, threshold)


def clear_failed_step_tracker_for_tools(
    tracker: MutableMapping[str, int],
    tool_names: Iterable[str],
) -> None:
    prefixes = tuple(f"{name}:" for name in tool_names)
    if not prefixes:
        return
    for key in list(tracker):
        if key.startswith(prefixes):
            del tracker[key]


def _ctx_failed_step_tracker(ctx: Any) -> MutableMapping[str, int] | None:
    tracker = getattr(ctx, "failed_tool_step_tracker", None)
    return tracker if isinstance(tracker, dict) else None


def detect_failed_tool_step_loop_for_ctx(
    ctx: Any,
    tool_name: str,
    arguments: Mapping[str, Any] | None = None,
) -> str | None:
    tracker = _ctx_failed_step_tracker(ctx)
    if tracker is None:
        return None
    return detect_failed_tool_step_loop(tracker, tool_name, arguments)


def record_tool_step_result_for_ctx(
    ctx: Any,
    tool_name: str,
    arguments: Mapping[str, Any] | None,
    result: Mapping[str, Any],
) -> None:
    tracker = _ctx_failed_step_tracker(ctx)
    if tracker is not None:
        record_tool_step_result(tracker, tool_name, arguments, result)
    # Strict ``is True`` check: a malformed result dict missing ``ok`` entirely
    # must not be treated as success and accidentally clear a blocker signal.
    if result.get("ok") is True:
        maybe_clear_blocker_signal_on_tool_success(ctx, tool_name)


def clear_failed_step_tracker_for_tools_in_ctx(ctx: Any, tool_names: Iterable[str]) -> None:
    tracker = _ctx_failed_step_tracker(ctx)
    if tracker is None:
        return
    clear_failed_step_tracker_for_tools(tracker, tool_names)
