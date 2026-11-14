"""Session management for the copilot agent — SQLiteSession + callbacks."""

from __future__ import annotations

import json
import os
from collections.abc import Callable
from itertools import count
from pathlib import Path
from typing import Any

import structlog
from agents.items import TResponseInputItem
from agents.memory.sqlite_session import SQLiteSession
from agents.run_config import CallModelData, ModelInputData
from agents.run_context import RunContextWrapper
from pydantic import BaseModel

from skyvern.config import settings
from skyvern.forge import app
from skyvern.forge.sdk.agents.context import (
    get_agent_message_field,
    pair_tool_calls_with_outputs,
    replace_agent_message_field,
)
from skyvern.forge.sdk.copilot.browser_ablation import prompt_sha256
from skyvern.forge.sdk.copilot.cache_envelope import is_replayable_reasoning
from skyvern.forge.sdk.copilot.config import SCREENSHOT_DROPPED_NUDGE
from skyvern.forge.sdk.copilot.enforcement import (
    _TOOL_OUTPUT_HEAD_TRUNCATION_SUFFIX,
    KEEP_RECENT_TOOL_OUTPUTS,
    NUDGE_SENTINEL,
    SCREENSHOT_PLACEHOLDER,
    TOKEN_BUDGET,
    TOOL_WINDOW_SUMMARIZED,
    _cap_tool_outputs,
    _compact_tool_items,
    _strip_input_images,
    aggressive_prune,
    aggressive_prune_whole_pairs,
    estimate_tokens,
    is_attached_image_message,
    is_paired_observation_message,
    is_screenshot_message,
    is_synthetic_user_message,
    pending_screenshot_entry,
    pending_screenshot_message,
    unread_tool_output_indices,
    whole_output_indices,
)
from skyvern.forge.sdk.copilot.model_input_capture import (
    clear_pending_model_input_capture,
    register_pending_model_input_capture,
)
from skyvern.forge.sdk.copilot.runtime import AgentContext
from skyvern.forge.sdk.copilot.screenshot_utils import (
    PendingFrameLease,
    ScreenshotActionRelation,
    model_input_fingerprint,
)
from skyvern.forge.sdk.copilot.tools.banned_blocks import _copilot_authoring_capability

LOG = structlog.get_logger()

RECENT_REAL_TURNS = 2
TOOL_OUTPUT_TRUNCATE_SOFT = 2000
TOOL_OUTPUT_TRUNCATE_EMERGENCY = 300


def _emergency_truncate_all(items: list[Any], cap: int) -> list[Any]:
    kept = whole_output_indices(items)
    truncated_items = [item if i in kept else _truncate_tool_output(item, cap) for i, item in enumerate(items)]
    truncated = {i for i, (old, new) in enumerate(zip(items, truncated_items)) if new is not old}
    if truncated:
        LOG.warning(
            "copilot_tool_output_emergency_truncated",
            truncated_count=len(truncated),
            unread_count=len(truncated & unread_tool_output_indices(items)),
            cap=cap,
        )
    return truncated_items


def create_copilot_session(chat_id: str) -> SQLiteSession:
    """Create an in-memory SQLiteSession scoped to a single copilot request."""
    return SQLiteSession(session_id=chat_id, db_path=":memory:")


_DEFERRED_ARGUMENTS_MAX_CHARS = 2_000


def _defer_unread_outputs_over_budget(items: list[Any], token_budget: int) -> tuple[list[Any], list[str]]:
    """Swap the latest unread outputs for a notice that the call ran until the input fits; one stays whole."""
    calls = {
        get_agent_message_field(item, "call_id"): item
        for item in items
        if get_agent_message_field(item, "type") == "function_call"
    }
    unread = sorted(unread_tool_output_indices(items))
    whole_indices = whole_output_indices(items)
    # A loaded skill or answer is never deferred and already gives the batch one whole result, so every other may go.
    keep_whole = 0 if whole_indices.intersection(unread) else 1
    items = list(items)
    deferred: list[str] = []
    while len(unread) > keep_whole and estimate_tokens(items) > token_budget:
        index = unread.pop()
        if index in whole_indices:
            continue
        call = calls.get(get_agent_message_field(items[index], "call_id"))
        tool_name = get_agent_message_field(call, "name") if call is not None else None
        arguments = get_agent_message_field(call, "arguments") if call is not None else None
        # The call may have changed state, so the notice reports that it ran and never asks for a repeat.
        notice = {
            "not_shown": "This call already ran and finished, but its result did not fit the model input with the "
            "rest of its batch. Calling the tool again runs it again.",
            "already_ran": True,
            "tool_name": tool_name,
            "arguments": arguments[:_DEFERRED_ARGUMENTS_MAX_CHARS] if isinstance(arguments, str) else None,
        }
        replaced = replace_agent_message_field(items[index], "output", json.dumps(notice))
        # A notice that costs as much frees no room, and a result this small can be one saying its call never ran.
        if estimate_tokens([replaced]) >= estimate_tokens([items[index]]):
            continue
        items[index] = replaced
        deferred.append(tool_name or "unknown")
    return items, deferred


def _items_besides_frames(items: list[TResponseInputItem]) -> list[TResponseInputItem]:
    return [item for item in _drop_attached_images(items) if not is_screenshot_message(item)]


def _summarize_older_tool_items(items: list[TResponseInputItem]) -> list[TResponseInputItem]:
    # Summarized before the cap so an over-cap JSON output still parses into a synopsis. The cap then reaches outputs
    # the summarizer keeps whole, unlogged because the caller already logged this call's cuts.
    summarized = _compact_tool_items(items, keep_recent_tool_outputs=KEEP_RECENT_TOOL_OUTPUTS)
    return _compact_tool_items(summarized, keep_recent_tool_outputs=len(summarized))


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
    """Combine session history with new input, keeping the goal and only the newest screenshot of the middle region."""
    if not history_items:
        return new_items

    boundary = _find_real_user_boundary(history_items, recent_turns=RECENT_REAL_TURNS)
    # boundary == 0 means fewer real user turns than RECENT_REAL_TURNS, so everything after the goal is middle and
    # screenshot retention still reaches a first turn's long tool chain.
    if boundary >= 1:
        middle = history_items[1:boundary]
        recent = history_items[boundary:]
    else:
        middle = history_items[1:]
        recent = []

    # The newest frame must survive the merge: with every injected message classified
    # synthetic, the whole post-goal history is middle, and blanket-replacing would
    # blind the agent to the frame it just captured once it rotates into history.
    trailing_items = list(recent) + list(new_items)
    stale_paired_screenshots = {
        i
        for i, item in enumerate(middle)
        if is_paired_observation_message(item)
        and any(not is_synthetic_user_message(following) for following in list(middle[i + 1 :]) + trailing_items)
    }
    middle_screenshots = [
        i for i, item in enumerate(middle) if _screenshot_with_image(item) and i not in stale_paired_screenshots
    ]
    has_newer_screenshot = any(_screenshot_with_image(item) for item in list(recent) + list(new_items))
    keep_screenshot = middle_screenshots[-1] if middle_screenshots and not has_newer_screenshot else None
    middle = [
        {"role": "user", "content": SCREENSHOT_PLACEHOLDER}
        if is_screenshot_message(item) and (i != keep_screenshot or i in stale_paired_screenshots)
        else item
        for i, item in enumerate(middle)
    ]

    return [history_items[0]] + middle + list(recent) + list(new_items)


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


def _is_unsent_reasoning(item: TResponseInputItem) -> bool:
    # Copilot never sets ModelSettings.reasoning, so a reasoning item reaches the request only as replayed summary
    # text or GPT's encrypted reasoning; a signature-only one (Claude's default thinking display) sends nothing and
    # must not use up the budget.
    if get_agent_message_field(item, "type") != "reasoning" or is_replayable_reasoning(item):
        return False
    return not any(get_agent_message_field(part, "text") for part in get_agent_message_field(item, "summary") or [])


def _copilot_call_model_input_filter(data: CallModelData[Any], *, token_budget: int) -> ModelInputData:
    items = [item for item in data.model_data.input if not _is_unsent_reasoning(item)]
    ctx = _run_context(data)
    # A fallback to a non-vision model replays session history that may already hold attached or captured images.
    if not (isinstance(ctx, AgentContext) and ctx.supports_vision):
        items = list(_strip_input_images(items)[0])
    # Read-only peek: the end-of-turn drain in enforcement stays the only clear, so a provider
    # retry or model fallback re-running this filter still carries the frame.
    screenshot_msg = pending_screenshot_message(ctx)
    entry = pending_screenshot_entry(ctx)
    if (
        screenshot_msg is not None
        and entry is not None
        and entry.provenance.action_relation is ScreenshotActionRelation.SAME_PAGE_OBSERVATION
    ):
        fingerprint = model_input_fingerprint(items)
        lease = getattr(ctx, "pending_frame_lease", None)
        if isinstance(lease, PendingFrameLease) and lease.capture_event_id == entry.capture_event_id:
            if lease.input_fingerprint != fingerprint:
                ctx.pending_screenshots.clear()
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
    instructions = data.model_data.instructions
    if screenshot_msg is None:
        budgeted = _filter_to_budget(items, instructions, token_budget=token_budget)
    else:
        budgeted = _filter_to_budget([*items, screenshot_msg], instructions, token_budget=token_budget)
        summarized = TOOL_WINDOW_SUMMARIZED.get()
        sharp_msg = pending_screenshot_message(ctx, sharp=True)
        if sharp_msg is not None:
            sharp_budgeted = _filter_to_budget([*items, sharp_msg], instructions, token_budget=token_budget)
            # The sharp copy may cost older screenshots and attached images, which budget pruning drops first anyway,
            # but nothing else.
            if estimate_tokens(sharp_budgeted.input) <= token_budget and _items_besides_frames(
                sharp_budgeted.input
            ) == _items_besides_frames(budgeted.input):
                budgeted, screenshot_msg = sharp_budgeted, sharp_msg
            else:
                TOOL_WINDOW_SUMMARIZED.set(summarized)
        if screenshot_msg is not sharp_msg:
            LOG.info("copilot_sharp_frame_over_budget")
        LOG.info(
            "Injecting screenshot user message",
            count=len(screenshot_msg["content"]) - 1,
            path="model_input_filter",
            source_tool=entry.provenance.source_tool if entry else None,
            capture_id=entry.capture_id if entry else None,
            capture_event_id=entry.capture_event_id if entry else None,
        )

    # Last thing before the request leaves: every budget rung above reorders nothing, but
    # history assembly upstream can seat a result after a later assistant turn, which the
    # provider rejects outright. Repair here so no path can emit an invalid pairing.
    model_data = ModelInputData(
        input=pair_tool_calls_with_outputs(list(budgeted.input), on_repair=_log_tool_pair_repair),
        instructions=budgeted.instructions,
    )
    _maybe_dump_model_input(data, model_data, token_budget=token_budget)
    return model_data


_MODEL_CALL_SEQ = count()


def _jsonable(item: Any) -> Any:
    if isinstance(item, BaseModel):
        return item.model_dump(mode="json")
    return item


def _captured_authoring_capability(ctx: AgentContext | None) -> dict[str, bool] | None:
    """Which block families the captured turn could author, or None when the turn carried no context.

    Recording the fallback as though it were the turn's own capability would let a replay rebuild a
    surface the live turn never had.
    """
    if ctx is None:
        return None
    capability = _copilot_authoring_capability(ctx)
    return {
        "code_blocks": capability.code_blocks,
        "agent_blocks": capability.agent_blocks,
        "agent_step": capability.agent_step,
    }


def _maybe_dump_model_input(data: CallModelData[Any], model_data: ModelInputData, *, token_budget: int) -> None:
    """Record the exact model input so a prompt or tool-schema change can be replayed offline.

    Written here rather than at the Runner call because this is the only point that sees what the
    model actually receives — after session merge, compaction, and every budget layer above. That
    includes page text and tool results, so writing takes both an explicit path and a local
    environment rather than the path alone.
    """
    clear_pending_model_input_capture()
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
            "eval_mode": getattr(ctx, "eval_mode", None),
            "tool_surface_identity": getattr(ctx, "tool_surface_identity", None),
            "ordered_native_tool_names": list(getattr(ctx, "eval_native_tool_names", ())),
            "ordered_mcp_tool_names": list(getattr(ctx, "eval_mcp_tool_names", ())),
            "prompt_sha256": (
                prompt_sha256(model_data.instructions) if isinstance(model_data.instructions, str) else None
            ),
            "authoring_capability": _captured_authoring_capability(ctx),
            "instructions": model_data.instructions,
            "input": [_jsonable(item) for item in model_data.input],
            "requested_output_paths": requested_output_paths,
            "token_budget": token_budget,
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
        register_pending_model_input_capture(
            path=path,
            payload=payload,
            redaction_parameters=parameters,
        )
    except Exception:
        LOG.warning("Failed to dump copilot model input")


def _filter_to_budget(items: list[Any], instructions: str | None, *, token_budget: int) -> ModelInputData:
    """Under budget, send every tool output whole up to the per-output cap.
    Over budget, summarize all but the newest outputs and arguments, then drop older screenshots and images, defer
    unread outputs, truncate, and prune; loaded skills and user answers stay whole throughout."""
    TOOL_WINDOW_SUMMARIZED.set(False)
    if not items:
        return ModelInputData(input=items, instructions=instructions)

    capped = _cap_tool_outputs(items)
    est = estimate_tokens(capped)
    LOG.info("Token estimate before filtering", tokens=est)
    if est <= token_budget:
        LOG.info("Within budget, outputs whole", tokens=est)
        return ModelInputData(input=capped, instructions=instructions)

    TOOL_WINDOW_SUMMARIZED.set(True)
    items = _summarize_older_tool_items(items)

    est = estimate_tokens(items)
    if est <= token_budget:
        LOG.info("Within budget after tool trim", tokens=est)
        return ModelInputData(input=items, instructions=instructions)

    # Layer 2: Drop all screenshots except the most recent
    items = _drop_older_screenshots(items)

    est = estimate_tokens(items)
    if est <= token_budget:
        LOG.info("Within budget after screenshot drop", tokens=est)
        return ModelInputData(input=items, instructions=instructions)

    stripped = _drop_attached_images(items)
    if stripped != items:
        items = stripped
        est = estimate_tokens(items)
        LOG.warning("copilot_attached_images_dropped_for_budget", tokens=est)
        if est <= token_budget:
            return ModelInputData(input=items, instructions=instructions)

    # Layer 2b: split an unread batch that does not fit. Outputs stay whole in call order; the rest become a
    # notice that the call ran, so nothing the model has not read is summarized or cut.
    items, deferred_tools = _defer_unread_outputs_over_budget(items, token_budget)
    if deferred_tools:
        est = estimate_tokens(items)
        LOG.warning(
            "copilot_unread_tool_outputs_deferred_for_budget",
            deferred_count=len(deferred_tools),
            tool_name=sorted(set(deferred_tools)),
            tokens=est,
        )
        if est <= token_budget:
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
    LOG.warning(
        "Aggressive prune needed",
        tokens=est,
        budget=token_budget,
        unread_count=len(unread_tool_output_indices(items)),
    )
    demonstration_call_ids = {
        call_id
        for item in items
        if isinstance(call_id := get_agent_message_field(item, "call_id"), str)
        and call_id.startswith("call_demo_")
        and get_agent_message_field(item, "type") == "function_call"
    }
    frame = next((item for item in reversed(items[1:]) if _screenshot_with_image(item)), None)
    reinserted_whole_items = {id(item) for item in aggressive_prune_whole_pairs(items)}
    pruned = aggressive_prune(items)
    # The prune drops every screenshot and appends a re-take note, or keeps an earlier note instead of adding one.
    # A frame newer than the oldest kept step goes back in its input position without any note older than it; an
    # older frame stays dropped because the steps taken after it were cut.
    position = {id(item): index for index, item in enumerate(items)}
    oldest_kept = next(
        (position[id(item)] for item in pruned[1:] if id(item) in position and id(item) not in reinserted_whole_items),
        None,
    )
    if frame is not None and (oldest_kept is None or position[id(frame)] > oldest_kept):
        retake_note = NUDGE_SENTINEL + SCREENSHOT_DROPPED_NUDGE
        pruned = sorted(
            (
                item
                for item in [*pruned, frame]
                if id(item) in position
                and (
                    position[id(item)] > position[id(frame)] or get_agent_message_field(item, "content") != retake_note
                )
            ),
            key=lambda item: position[id(item)],
        )
    items = pruned
    retained_demonstration_call_ids = {
        call_id
        for item in items
        if isinstance(call_id := get_agent_message_field(item, "call_id"), str)
        and call_id in demonstration_call_ids
        and get_agent_message_field(item, "type") == "function_call"
    }
    omitted_demonstration_calls = len(demonstration_call_ids - retained_demonstration_call_ids)
    if omitted_demonstration_calls:
        items.append(
            {
                "role": "user",
                "content": (
                    NUDGE_SENTINEL
                    + f"{omitted_demonstration_calls} earlier demonstrated step fact packet(s) were omitted "
                    "because the model input exceeded its token budget."
                ),
            }
        )

    est = estimate_tokens(items)
    LOG.info("Final token estimate after aggressive prune", tokens=est)
    return ModelInputData(input=items, instructions=instructions)


def _drop_older_screenshots(items: list[Any]) -> list[Any]:
    screenshot_indices = [i for i, item in enumerate(items) if is_screenshot_message(item)]
    drop_indices = set(screenshot_indices[:-1])
    return [
        {"role": "user", "content": SCREENSHOT_PLACEHOLDER} if i in drop_indices else item
        for i, item in enumerate(items)
    ]


def _drop_attached_images(items: list[Any]) -> list[Any]:
    return [_strip_input_images([item])[0][0] if is_attached_image_message(item) else item for item in items]


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
