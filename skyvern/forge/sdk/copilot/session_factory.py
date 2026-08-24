"""Session management for the copilot agent — SQLiteSession + callbacks."""

from __future__ import annotations

import json
import os
from collections.abc import Callable
from itertools import count
from pathlib import Path
from typing import Any

import structlog
from agents.memory.sqlite_session import SQLiteSession
from agents.run_config import CallModelData, ModelInputData
from agents.run_context import RunContextWrapper
from pydantic import BaseModel

from skyvern.config import settings
from skyvern.forge import app
from skyvern.forge.sdk.agents.context import (
    compact_agent_messages_for_llm,
    get_agent_message_field,
    pair_tool_calls_with_outputs,
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
    is_paired_observation_message,
    is_screenshot_message,
    is_synthetic_user_message,
    log_recent_tool_output_truncation,
    pending_screenshot_message,
)
from skyvern.forge.sdk.copilot.screenshot_utils import (
    PendingFrameLease,
    ScreenshotActionRelation,
    model_input_fingerprint,
)

LOG = structlog.get_logger()

RECENT_REAL_TURNS = 2
TOOL_OUTPUT_TRUNCATE_SOFT = 2000
TOOL_OUTPUT_TRUNCATE_EMERGENCY = 300


def _emergency_truncate_all(items: list[Any], cap: int) -> list[Any]:
    truncated_items = [_truncate_tool_output(item, cap) for item in items]
    truncated_count = sum(1 for old, new in zip(items, truncated_items) if new is not old)
    if truncated_count:
        LOG.warning("copilot_tool_output_emergency_truncated", truncated_count=truncated_count, cap=cap)
    return truncated_items


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
        on_recent_truncation=log_recent_tool_output_truncation,
    )


def _screenshot_with_image(item: Any) -> bool:
    """A sentinel-prefixed item only counts as the keepable frame when it actually
    carries an image part; text-only lookalikes and placeholders must not shadow it."""
    if not is_screenshot_message(item):
        return False
    content = get_agent_message_field(item, "content")
    if not isinstance(content, list):
        return False
    return any(get_agent_message_field(block, "type") == "input_image" for block in content)


def copilot_session_input_callback(
    history_items: list[Any],
    new_items: list[Any],
) -> list[Any]:
    """Combine session history with new input, pruning older tool output/call
    payloads in the middle region.

    Keeps the original goal (first item) at full fidelity and preserves the
    last ``RECENT_REAL_TURNS`` real user turns — in production every injected
    copilot message is synthetic, so the whole post-goal history is the middle
    region. Within it, older ``function_call_output`` / ``function_call`` items
    are compacted using the same ``KEEP_RECENT_TOOL_OUTPUTS`` rule that
    ``enforcement._prune_input_list`` uses in the non-session path, and the
    newest screenshot survives unless a newer one rides in recent/new items.
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
    # The newest frame must survive the merge: with every injected message classified
    # synthetic, the whole post-goal history is middle, and blanket-replacing would
    # blind the agent to the frame it just captured once it rotates into history.
    trailing_items = list(recent) + list(new_items)
    stale_paired_screenshots = {
        i
        for i, item in enumerate(pruned_middle)
        if is_paired_observation_message(item)
        and any(not is_synthetic_user_message(following) for following in list(pruned_middle[i + 1 :]) + trailing_items)
    }
    middle_screenshots = [
        i for i, item in enumerate(pruned_middle) if _screenshot_with_image(item) and i not in stale_paired_screenshots
    ]
    has_newer_screenshot = any(_screenshot_with_image(item) for item in list(recent) + list(new_items))
    keep_screenshot = middle_screenshots[-1] if middle_screenshots and not has_newer_screenshot else None
    pruned_middle = [
        {"role": "user", "content": SCREENSHOT_PLACEHOLDER}
        if is_screenshot_message(item) and (i != keep_screenshot or i in stale_paired_screenshots)
        else item
        for i, item in enumerate(pruned_middle)
    ]

    return [history_items[0]] + pruned_middle + list(recent) + list(new_items)


def make_copilot_call_model_input_filter(token_budget: int) -> Callable[[CallModelData[Any]], ModelInputData]:
    def _filter(data: CallModelData[Any]) -> ModelInputData:
        return _copilot_call_model_input_filter(data, token_budget=token_budget)

    return _filter


def copilot_call_model_input_filter(data: CallModelData[Any]) -> ModelInputData:
    return _copilot_call_model_input_filter(data, token_budget=TOKEN_BUDGET)


def _log_tool_pair_repair(moved: int, size_delta: int) -> None:
    LOG.info("copilot_tool_pair_repaired", moved=moved, size_delta=size_delta)


def _run_context(data: CallModelData[Any]) -> Any:
    """CallModelData carries the run context itself; only some entry points hand over a wrapper."""
    return data.context.context if isinstance(data.context, RunContextWrapper) else data.context


def _copilot_call_model_input_filter(data: CallModelData[Any], *, token_budget: int) -> ModelInputData:
    items = list(data.model_data.input)
    ctx = _run_context(data)
    # Read-only peek: the end-of-turn drain in enforcement stays the only clear, so a provider
    # retry or model fallback re-running this filter still carries the frame.
    screenshot_msg = pending_screenshot_message(ctx)
    pending = getattr(ctx, "pending_screenshots", None)
    entry = pending[0] if isinstance(pending, list) and pending else None
    if (
        screenshot_msg is not None
        and entry is not None
        and entry.provenance.action_relation is ScreenshotActionRelation.SAME_PAGE_OBSERVATION
    ):
        fingerprint = model_input_fingerprint(items)
        lease = getattr(ctx, "pending_frame_lease", None)
        if isinstance(lease, PendingFrameLease) and lease.capture_event_id == entry.capture_event_id:
            if lease.input_fingerprint != fingerprint:
                if isinstance(pending, list):
                    pending.clear()
                ctx.pending_frame_lease = None
                screenshot_msg = None
                LOG.info(
                    "copilot_frame_lease_invalidated",
                    capture_id=entry.capture_id,
                    input_fingerprint=fingerprint[:12],
                )
        else:
            ctx.pending_frame_lease = PendingFrameLease(
                capture_event_id=entry.capture_event_id,
                capture_id=entry.capture_id,
                input_fingerprint=fingerprint,
            )
    if screenshot_msg is not None:
        LOG.info(
            "Injecting screenshot user message",
            count=len(screenshot_msg["content"]) - 1,
            path="model_input_filter",
        )
        items.append(screenshot_msg)

    budgeted = _filter_to_budget(items, data.model_data.instructions, token_budget=token_budget)
    # Last thing before the request leaves: every budget rung above reorders nothing, but
    # history assembly upstream can seat a result after a later assistant turn, which the
    # provider rejects outright. Repair here so no path can emit an invalid pairing.
    model_data = ModelInputData(
        input=pair_tool_calls_with_outputs(list(budgeted.input), on_repair=_log_tool_pair_repair),
        instructions=budgeted.instructions,
    )
    _maybe_dump_model_input(data, model_data)
    return model_data


_MODEL_CALL_SEQ = count()


def _jsonable(item: Any) -> Any:
    if isinstance(item, BaseModel):
        return item.model_dump(mode="json")
    return item


def _maybe_dump_model_input(data: CallModelData[Any], model_data: ModelInputData) -> None:
    """Record the exact model input so a prompt or tool-schema change can be replayed offline.

    Written here rather than at the Runner call because this is the only point that sees what the
    model actually receives — after session merge, compaction, and every budget layer above. That
    includes page text and tool results, so writing takes both an explicit path and a local
    environment rather than the path alone.
    """
    dump_dir = os.getenv("COPILOT_DUMP_MODEL_INPUTS")
    if not dump_dir or settings.ENV != "local":
        return
    try:
        from skyvern.forge.sdk.copilot.enforcement import requested_output_paths_for_derivation

        ctx = _run_context(data)
        try:
            requested_output_paths = sorted(requested_output_paths_for_derivation(ctx)) if ctx else []
        except Exception:
            requested_output_paths = []
        target = Path(dump_dir)
        target.mkdir(parents=True, exist_ok=True)
        payload = {
            "capture_case_id": getattr(ctx, "eval_capture_case_id", None),
            "instructions": model_data.instructions,
            "input": [_jsonable(item) for item in model_data.input],
            "requested_output_paths": requested_output_paths,
        }
        parameters = getattr(ctx, "codeblock_redaction_parameters", {})
        if parameters:
            payload = app.AGENT_FUNCTION.redact_codeblock_parameter_values(payload, parameters)
        if not isinstance(payload, dict):
            payload = {}
        path = target / f"call-{next(_MODEL_CALL_SEQ):04d}.json"
        serialized = json.dumps(payload, indent=2, default=str)
        if parameters:
            serialized = app.AGENT_FUNCTION.redact_codeblock_parameter_values(serialized, parameters)
        path.write_text(serialized if isinstance(serialized, str) else "")
    except Exception:
        LOG.warning("Failed to dump copilot model input")


def _filter_to_budget(items: list[Any], instructions: str | None, *, token_budget: int) -> ModelInputData:
    """Token-budget enforcement applied just before each model call.

    Graduated pruning:
    1. Compact older tool outputs + function-call arguments using the
       KEEP_RECENT_TOOL_OUTPUTS rule (mirrors ``enforcement._prune_input_list``).
    2. If still over budget: drop all screenshots except the most recent.
    3. If still over budget: truncate ALL tool outputs — first to 2000 chars,
       then to 300 only if the softer pass was not enough.
    4. If still over budget: aggressive prune as last resort.
    """
    if not items:
        return ModelInputData(input=items, instructions=instructions)

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
        return ModelInputData(input=items, instructions=instructions)

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
        return ModelInputData(input=items, instructions=instructions)

    # Layer 3a: bring every tool output down to the pre-raise bound before resorting
    # to the harsher pass, so a code-bearing recent output degrades gracefully.
    items = _emergency_truncate_all(items, TOOL_OUTPUT_TRUNCATE_SOFT)

    est = estimate_tokens(items)
    if est <= token_budget:
        LOG.info("Within budget after soft emergency truncation", tokens=est)
        return ModelInputData(input=items, instructions=instructions)

    # Layer 3b: Truncate ALL tool outputs to 300 chars
    items = _emergency_truncate_all(items, TOOL_OUTPUT_TRUNCATE_EMERGENCY)

    est = estimate_tokens(items)
    if est <= token_budget:
        LOG.info("Within budget after emergency truncation", tokens=est)
        return ModelInputData(input=items, instructions=instructions)

    # Layer 4: Aggressive prune as last resort
    LOG.warning("Aggressive prune needed", tokens=est, budget=token_budget)
    items = aggressive_prune(items)

    est = estimate_tokens(items)
    LOG.info("Final token estimate after aggressive prune", tokens=est)
    return ModelInputData(input=items, instructions=instructions)


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
