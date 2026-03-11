"""Session management for the copilot agent — SQLiteSession + callbacks."""

from __future__ import annotations

from typing import Any

import structlog
from agents.memory.sqlite_session import SQLiteSession
from agents.run_config import CallModelData, ModelInputData

from skyvern.forge.sdk.copilot.enforcement import (
    SCREENSHOT_PLACEHOLDER,
    TOKEN_BUDGET,
    aggressive_prune,
    estimate_tokens,
    is_screenshot_message,
    is_synthetic_user_message,
)

LOG = structlog.get_logger()

RECENT_REAL_TURNS = 2
TOOL_OUTPUT_TRUNCATE_MIDDLE = 1500
TOOL_OUTPUT_TRUNCATE_EMERGENCY = 300


def create_copilot_session(chat_id: str) -> SQLiteSession:
    """Create an in-memory SQLiteSession scoped to a single copilot request."""
    return SQLiteSession(session_id=chat_id, db_path=":memory:")


def copilot_session_input_callback(
    history_items: list[Any],
    new_items: list[Any],
) -> list[Any]:
    """Combine session history with new input, pruning the middle region.

    Keeps the original goal (first item) at full fidelity, applies lighter
    pruning to older turns, and preserves recent turns completely.
    """
    if not history_items:
        return new_items

    original_goal = history_items[0]

    # Find boundary: walk backward counting real user turns (skip index 0 = original goal)
    real_user_count = 0
    boundary = 0
    for i in range(len(history_items) - 1, 0, -1):
        item = history_items[i]
        role = item.get("role") if isinstance(item, dict) else getattr(item, "role", None)
        if role == "user" and not is_synthetic_user_message(item):
            real_user_count += 1
            if real_user_count >= RECENT_REAL_TURNS:
                boundary = i
                break

    middle = history_items[1:boundary] if boundary > 1 else []
    recent = history_items[boundary:] if boundary > 0 else history_items[1:]

    pruned_middle: list[Any] = []
    for item in middle:
        if is_screenshot_message(item):
            pruned_middle.append({"role": "user", "content": SCREENSHOT_PLACEHOLDER})
            continue

        item_type = item.get("type") if isinstance(item, dict) else getattr(item, "type", None)
        if item_type == "function_call_output" and isinstance(item, dict):
            output = item.get("output", "")
            if isinstance(output, str) and len(output) > TOOL_OUTPUT_TRUNCATE_MIDDLE:
                item = {**item, "output": output[:TOOL_OUTPUT_TRUNCATE_MIDDLE] + "\n... [truncated]"}

        pruned_middle.append(item)

    return [original_goal] + pruned_middle + list(recent) + list(new_items)


def copilot_call_model_input_filter(data: CallModelData[Any]) -> ModelInputData:
    """Token-budget enforcement applied just before each model call.

    Graduated pruning:
    1. Trim old tool outputs (via ToolOutputTrimmer-like logic aware of synthetic messages)
    2. If still over budget: drop all screenshots except the most recent
    3. If still over budget: truncate all tool outputs to 300 chars
    4. If still over budget: aggressive prune as last resort
    """
    model_data = data.model_data
    items = list(model_data.input)

    if not items:
        return model_data

    est = estimate_tokens(items)
    LOG.info("Token estimate before filtering", tokens=est)

    # Layer 1: Trim old tool outputs (skip recent real user turns)
    boundary = _find_real_user_boundary(items, recent_turns=2)
    if boundary > 0:
        call_id_to_name = _build_call_id_to_name(items)
        new_items: list[Any] = []
        for i, item in enumerate(items):
            if i < boundary and isinstance(item, dict) and item.get("type") == "function_call_output":
                output = item.get("output", "")
                output_str = output if isinstance(output, str) else str(output)
                if len(output_str) > 500:
                    call_id = str(item.get("call_id", ""))
                    tool_name = call_id_to_name.get(call_id, "unknown_tool")
                    preview = output_str[:200]
                    summary = (
                        f"[Trimmed: {tool_name} output — {len(output_str)} chars → 200 char preview]\n{preview}..."
                    )
                    if len(summary) < len(output_str):
                        new_items.append({**item, "output": summary})
                        continue
            new_items.append(item)
        items = new_items

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
    """Truncate a function_call_output item's output if it exceeds max_chars."""
    if not isinstance(item, dict):
        return item
    if item.get("type") != "function_call_output":
        return item
    output = item.get("output")
    if isinstance(output, str) and len(output) > max_chars:
        return {**item, "output": output[:max_chars] + "\n... [truncated]"}
    return item


def _find_real_user_boundary(items: list[Any], recent_turns: int = 2) -> int:
    """Find the boundary index separating old items from recent ones.

    Counts only real user messages (not screenshots or nudges) when walking
    backward. Returns 0 if fewer than ``recent_turns`` real user messages exist.
    """
    real_count = 0
    for i in range(len(items) - 1, -1, -1):
        item = items[i]
        role = item.get("role") if isinstance(item, dict) else getattr(item, "role", None)
        if role == "user" and not is_synthetic_user_message(item):
            real_count += 1
            if real_count >= recent_turns:
                return i
    return 0


def _build_call_id_to_name(items: list[Any]) -> dict[str, str]:
    """Build a mapping from function call_id to tool name."""
    mapping: dict[str, str] = {}
    for item in items:
        if isinstance(item, dict) and item.get("type") == "function_call":
            call_id = item.get("call_id")
            name = item.get("name")
            if call_id and name:
                mapping[call_id] = name
    return mapping
