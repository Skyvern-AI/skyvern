"""Session management for the copilot agent — SQLiteSession + callbacks."""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

import structlog
from agents.memory.sqlite_session import SQLiteSession
from agents.run_config import CallModelData, ModelInputData

from skyvern.forge.sdk.agents.context import (
    compact_agent_messages_for_llm,
    get_agent_message_field,
    replace_agent_message_field,
)
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


def _compact_tool_items(items: list[Any]) -> list[Any]:
    return compact_agent_messages_for_llm(
        items,
        keep_recent_tool_outputs=KEEP_RECENT_TOOL_OUTPUTS,
        max_recent_tool_output_chars=_RECENT_TOOL_OUTPUT_CHAR_CAP,
        summarize_tool_output=_summarize_tool_output,
        summarize_tool_arguments=_summarize_tool_arguments,
        tool_output_truncation_suffix=_TOOL_OUTPUT_HEAD_TRUNCATION_SUFFIX,
    )


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
    #   compaction inside compact_agent_messages_for_llm can still fire on the long tool
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


def make_copilot_call_model_input_filter(token_budget: int) -> Callable[[CallModelData[Any]], ModelInputData]:
    def _filter(data: CallModelData[Any]) -> ModelInputData:
        return _copilot_call_model_input_filter(data, token_budget=token_budget)

    return _filter


def copilot_call_model_input_filter(data: CallModelData[Any]) -> ModelInputData:
    return _copilot_call_model_input_filter(data, token_budget=TOKEN_BUDGET)


def _copilot_call_model_input_filter(data: CallModelData[Any], *, token_budget: int) -> ModelInputData:
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
    if est <= token_budget:
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
    if est <= token_budget:
        LOG.info("Within budget after screenshot drop", tokens=est)
        return ModelInputData(input=items, instructions=model_data.instructions)

    # Layer 3: Truncate ALL tool outputs to 300 chars
    items = [_truncate_tool_output(item, TOOL_OUTPUT_TRUNCATE_EMERGENCY) for item in items]

    est = estimate_tokens(items)
    if est <= token_budget:
        LOG.info("Within budget after emergency truncation", tokens=est)
        return ModelInputData(input=items, instructions=model_data.instructions)

    # Layer 4: Aggressive prune as last resort
    LOG.warning("Aggressive prune needed", tokens=est, budget=token_budget)
    items = aggressive_prune(items)

    est = estimate_tokens(items)
    LOG.info("Final token estimate after aggressive prune", tokens=est)
    return ModelInputData(input=items, instructions=model_data.instructions)


def _truncate_tool_output(item: Any, max_chars: int) -> Any:
    """Truncate a function_call_output item's output if it exceeds max_chars."""
    if get_agent_message_field(item, "type") != "function_call_output":
        return item
    output = get_agent_message_field(item, "output")
    if isinstance(output, str) and len(output) > max_chars:
        return replace_agent_message_field(item, "output", output[:max_chars] + _TOOL_OUTPUT_HEAD_TRUNCATION_SUFFIX)
    return item


def _find_real_user_boundary(items: list[Any], recent_turns: int = 2) -> int:
    """Find the boundary index separating old items from recent ones.

    Counts only real user messages (not screenshots or nudges) when walking
    backward. Returns 0 if fewer than ``recent_turns`` real user messages exist.
    """
    real_count = 0
    for i in range(len(items) - 1, -1, -1):
        item = items[i]
        if get_agent_message_field(item, "role") == "user" and not is_synthetic_user_message(item):
            real_count += 1
            if real_count >= recent_turns:
                return i
    return 0
