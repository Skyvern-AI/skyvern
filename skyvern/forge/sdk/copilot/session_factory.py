"""Session management for the copilot agent — SQLiteSession + callbacks."""

from __future__ import annotations

from typing import Any

import structlog
from agents.memory.sqlite_session import SQLiteSession
from agents.run_config import CallModelData, ModelInputData

from skyvern.forge.sdk.copilot.enforcement import (
    _RECENT_TOOL_OUTPUT_CHAR_CAP,
    _TOOL_OUTPUT_HEAD_TRUNCATION_SUFFIX,
    KEEP_RECENT_TOOL_OUTPUTS,
    SCREENSHOT_PLACEHOLDER,
    TOKEN_BUDGET,
    _summarize_tool_arguments,
    _summarize_tool_output,
    aggressive_prune,
    estimate_tokens,
    is_screenshot_message,
    is_synthetic_user_message,
)

LOG = structlog.get_logger()

RECENT_REAL_TURNS = 2
TOOL_OUTPUT_TRUNCATE_EMERGENCY = 300


def create_copilot_session(chat_id: str) -> SQLiteSession:
    """Create an in-memory SQLiteSession scoped to a single copilot request."""
    return SQLiteSession(session_id=chat_id, db_path=":memory:")


def _item_field(item: Any, name: str) -> Any:
    """Read *name* from an item that is either a dict or attr-style object."""
    if isinstance(item, dict):
        return item.get(name)
    return getattr(item, name, None)


def _replace_item_field(item: Any, name: str, value: Any) -> Any:
    """Return *item* with *name* set to *value*. Dict items get a shallow copy;
    attr-style items are mutated in place. When setattr fails (frozen dataclass
    / __slots__ object), the item is returned unmodified and we log a warning
    so a budget overrun from a silently-unpatched item shows up in the logs
    instead of disappearing."""
    if isinstance(item, dict):
        return {**item, name: value}
    try:
        setattr(item, name, value)
    except (AttributeError, TypeError) as exc:
        LOG.warning(
            "Failed to rewrite session item field; leaving it as-is",
            field=name,
            item_type=type(item).__name__,
            error=str(exc),
        )
    return item


def _compact_tool_items(items: list[Any]) -> list[Any]:
    """Compact older function_call_output and function_call items using the
    same KEEP_RECENT_TOOL_OUTPUTS rule as ``enforcement._prune_input_list``.

    The last ``KEEP_RECENT_TOOL_OUTPUTS`` items in each category stay full
    (head-truncated only if very large). Older items get the JSON synopsis
    compression. This is the session-path mirror of the non-session
    ``_prune_input_list`` behavior, so first-turn transcripts with a long
    tool chain get compacted just like the non-session path."""
    fco_indices = [i for i, it in enumerate(items) if _item_field(it, "type") == "function_call_output"]
    fc_indices = [i for i, it in enumerate(items) if _item_field(it, "type") == "function_call"]
    recent_fco_set = set(fco_indices[-KEEP_RECENT_TOOL_OUTPUTS:]) if fco_indices else set()
    recent_fc_set = set(fc_indices[-KEEP_RECENT_TOOL_OUTPUTS:]) if fc_indices else set()

    result: list[Any] = []
    for i, item in enumerate(items):
        item_type = _item_field(item, "type")
        if item_type == "function_call_output":
            output = _item_field(item, "output")
            if isinstance(output, str):
                if i in recent_fco_set:
                    new_output = (
                        output[:_RECENT_TOOL_OUTPUT_CHAR_CAP] + _TOOL_OUTPUT_HEAD_TRUNCATION_SUFFIX
                        if len(output) > _RECENT_TOOL_OUTPUT_CHAR_CAP
                        else output
                    )
                else:
                    new_output = _summarize_tool_output(output)
                if new_output != output:
                    item = _replace_item_field(item, "output", new_output)
        elif item_type == "function_call" and i not in recent_fc_set:
            args = _item_field(item, "arguments")
            if isinstance(args, str):
                new_args = _summarize_tool_arguments(args)
                if new_args != args:
                    item = _replace_item_field(item, "arguments", new_args)
        result.append(item)
    return result


def copilot_session_input_callback(
    history_items: list[Any],
    new_items: list[Any],
) -> list[Any]:
    """Combine session history with new input, pruning older tool output/call
    payloads in the middle region.

    Keeps the original goal (first item) at full fidelity and preserves the
    last ``RECENT_REAL_TURNS`` real user turns. Within the remaining middle
    region, older ``function_call_output`` / ``function_call`` items are
    compacted using the same ``KEEP_RECENT_TOOL_OUTPUTS`` rule that
    ``enforcement._prune_input_list`` uses in the non-session path, so
    first-turn transcripts with a long tool chain get compacted identically
    regardless of user-turn count.
    """
    if not history_items:
        return new_items

    boundary = _find_real_user_boundary(history_items, recent_turns=RECENT_REAL_TURNS)
    # Partitioning rules:
    # * boundary >= 1 — the helper found ``recent_turns`` real user messages.
    #   items[1:boundary] is the "middle" to compact; items[boundary:] is the
    #   recent region we keep as-is. When boundary == 1 the middle slice is
    #   empty, which is correct (no items between the goal and the recent
    #   region). The prior form also appended history_items[1:] to recent,
    #   which double-emitted every non-goal item — fixed here.
    # * boundary == 0 — first-turn shape (fewer real users than recent_turns).
    #   Treat everything after the goal as "middle" so the KEEP_RECENT_TOOL_OUTPUTS
    #   compaction inside _compact_tool_items can still fire on the long tool
    #   chain. Recent is empty.
    if boundary >= 1:
        middle = history_items[1:boundary]
        recent = history_items[boundary:]
    else:
        middle = history_items[1:]
        recent = []

    pruned_middle = _compact_tool_items(middle)
    pruned_middle = [
        {"role": "user", "content": SCREENSHOT_PLACEHOLDER} if is_screenshot_message(item) else item
        for item in pruned_middle
    ]

    return [history_items[0]] + pruned_middle + list(recent) + list(new_items)


def copilot_call_model_input_filter(data: CallModelData[Any]) -> ModelInputData:
    """Token-budget enforcement applied just before each model call.

    Graduated pruning:
    1. Compact older tool outputs + function-call arguments using the
       KEEP_RECENT_TOOL_OUTPUTS rule (mirrors ``enforcement._prune_input_list``).
    2. If still over budget: drop all screenshots except the most recent.
    3. If still over budget: truncate ALL tool outputs to 300 chars.
    4. If still over budget: aggressive prune as last resort.
    """
    model_data = data.model_data
    items = list(model_data.input)

    if not items:
        return ModelInputData(input=items, instructions=model_data.instructions)

    est = estimate_tokens(items)
    LOG.info("Token estimate before filtering", tokens=est)

    # Re-run compaction here even though ``copilot_session_input_callback``
    # already compacted on session merge. The KEEP_RECENT_TOOL_OUTPUTS window
    # shifts whenever new items get appended — an output that was "recent" on
    # the previous turn may now be old enough to summarize. Cheap to re-run
    # (pure function over the item list), idempotent on already-compact items.
    items = _compact_tool_items(items)

    est = estimate_tokens(items)
    if est <= TOKEN_BUDGET:
        LOG.info("Within budget after tool trim", tokens=est)
        return ModelInputData(input=items, instructions=model_data.instructions)

    # Layer 2: Drop all screenshots except the most recent
    screenshot_indices = [i for i, item in enumerate(items) if is_screenshot_message(item)]
    if len(screenshot_indices) > 1:
        drop_indices = set(screenshot_indices[:-1])
        items = [
            {"role": "user", "content": SCREENSHOT_PLACEHOLDER} if i in drop_indices else item
            for i, item in enumerate(items)
        ]

    est = estimate_tokens(items)
    if est <= TOKEN_BUDGET:
        LOG.info("Within budget after screenshot drop", tokens=est)
        return ModelInputData(input=items, instructions=model_data.instructions)

    # Layer 3: Truncate ALL tool outputs to 300 chars
    items = [_truncate_tool_output(item, TOOL_OUTPUT_TRUNCATE_EMERGENCY) for item in items]

    est = estimate_tokens(items)
    if est <= TOKEN_BUDGET:
        LOG.info("Within budget after emergency truncation", tokens=est)
        return ModelInputData(input=items, instructions=model_data.instructions)

    # Layer 4: Aggressive prune as last resort
    LOG.warning("Aggressive prune needed", tokens=est, budget=TOKEN_BUDGET)
    items = aggressive_prune(items)

    est = estimate_tokens(items)
    LOG.info("Final token estimate after aggressive prune", tokens=est)
    return ModelInputData(input=items, instructions=model_data.instructions)


def _truncate_tool_output(item: Any, max_chars: int) -> Any:
    """Truncate a function_call_output item's output if it exceeds max_chars.

    Handles both dict and attr-style items via the shared ``_item_field`` /
    ``_replace_item_field`` helpers so Layer 3 emergency truncation stays
    consistent with the KEEP_RECENT_TOOL_OUTPUTS path in ``_compact_tool_items``.
    """
    if _item_field(item, "type") != "function_call_output":
        return item
    output = _item_field(item, "output")
    if isinstance(output, str) and len(output) > max_chars:
        return _replace_item_field(item, "output", output[:max_chars] + _TOOL_OUTPUT_HEAD_TRUNCATION_SUFFIX)
    return item


def _find_real_user_boundary(items: list[Any], recent_turns: int = 2) -> int:
    """Find the boundary index separating old items from recent ones.

    Counts only real user messages (not screenshots or nudges) when walking
    backward. Returns 0 if fewer than ``recent_turns`` real user messages exist.
    """
    real_count = 0
    for i in range(len(items) - 1, -1, -1):
        item = items[i]
        if _item_field(item, "role") == "user" and not is_synthetic_user_message(item):
            real_count += 1
            if real_count >= recent_turns:
                return i
    return 0
