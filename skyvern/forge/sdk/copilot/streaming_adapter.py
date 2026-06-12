"""Map OpenAI Agents SDK stream events to Skyvern SSE payloads."""

from __future__ import annotations

import asyncio
import json
from datetime import datetime, timezone
from typing import TYPE_CHECKING, Any

import structlog

# Reuse the HTTP-logging redactor so SSE tool inputs and request-body logs
# share one exact-match sensitive-key policy.
from skyvern.forge.request_logging import redact_sensitive_fields
from skyvern.forge.sdk.copilot.narration import (
    NarratorState,
    TransitionKind,
    build_tool_call_activity,
    build_tool_result_activity,
    cancel_in_flight,
    detect_transitions,
    extract_tool_details,
    resolve_narrator_handler,
    schedule_narration,
    snapshot_ctx,
    tool_activity_display_label,
)
from skyvern.forge.sdk.copilot.output_utils import format_tool_result_for_user, summarize_tool_result_detail
from skyvern.forge.sdk.schemas.workflow_copilot import (
    WorkflowCopilotDesignEndUpdate,
    WorkflowCopilotDesignStartUpdate,
    WorkflowCopilotStreamMessageType,
    WorkflowCopilotToolCallUpdate,
    WorkflowCopilotToolResultUpdate,
    WorkflowCopilotTurnMode,
    WorkflowCopilotTurnStartUpdate,
    WorkflowCopilotWorkflowDraftUpdate,
)

if TYPE_CHECKING:
    from agents.result import RunResultStreaming

    from skyvern.forge.sdk.copilot.context import CopilotContext
    from skyvern.forge.sdk.routes.event_source_stream import EventSourceStream
    from skyvern.forge.sdk.workflow.models.workflow import Workflow

LOG = structlog.get_logger()

_OBSERVATION_TOOLS = {
    "evaluate",
    "get_browser_screenshot",
    "click",
    "type_text",
    "run_blocks_and_collect_debug",
    "scroll",
    "console_messages",
    "select_option",
    "press_key",
}


async def stream_to_sse(
    result: RunResultStreaming,
    stream: EventSourceStream,
    ctx: Any,
) -> None:
    """Consume SDK stream events and emit SSE payloads to the client.

    *ctx* is a CopilotContext object with enforcement state attributes such as
    ``update_workflow_called``, ``test_after_update_done``,
    ``post_update_nudge_count``, ``navigate_called``, and
    ``observation_after_navigate``.

    A client disconnect does NOT cancel the agent run: we continue to iterate
    ``result.stream_events()`` so the agent completes whatever work it is
    in the middle of and the caller can persist the reply to the DB. Events
    sent through ``stream.send`` after disconnect are silently dropped by the
    stream, so the queue cannot grow unbounded.

    Real asyncio cancellation (server shutdown, parent task cancelled for
    reasons unrelated to a dropped client) is re-raised unchanged so
    asyncio's cancellation machinery still runs normally.
    """
    from agents.stream_events import RunItemStreamEvent

    from skyvern.forge.sdk.copilot.enforcement import (
        CopilotUnrecoverableToolError,
        _maybe_raise_unrecoverable_tool_error,
    )
    from skyvern.forge.sdk.copilot.turn_halt import (
        CopilotTurnHalt,
        raise_if_turn_halt,
        stash_turn_halt_from_blocker_signal,
    )

    call_id_to_name: dict[str, str] = {}
    # Counts completed tool round-trips (tool_called + tool_output pair), not
    # raw stream events. Both TOOL_CALL and TOOL_RESULT for the same round
    # carry the same iteration value; it advances after the matching result.
    iteration = 0

    # Narrator state persists across enforcement iterations via ctx so
    # cadence (last_emitted_at, min-gap) survives run_with_enforcement retries.
    # Resolve the handler once (PostHog override → env-driven fallback) so
    # per-emission calls don't re-hit PostHog and so per-event bookkeeping
    # (snapshot, detect, extract) is skipped when no narrator LLM is wired.
    narrator_state: NarratorState = getattr(ctx, "narrator_state", None) or NarratorState()
    if narrator_state.resolved_handler is None:
        narrator_state.resolved_handler = await resolve_narrator_handler(
            getattr(ctx, "workflow_permanent_id", None),
            getattr(ctx, "organization_id", None),
        )
    narrator_enabled = narrator_state.resolved_handler is not None
    ctx.narrator_state = narrator_state
    user_message = getattr(ctx, "user_message", "") or ""
    if user_message and not narrator_state.user_goal:
        narrator_state.user_goal = user_message

    try:
        async for event in result.stream_events():
            if not isinstance(event, RunItemStreamEvent):
                continue

            # Skip emission work (serialization, redaction) once the client
            # is gone, but keep draining the SDK stream so the agent can
            # finish. stream.send below would drop the payload anyway.
            client_gone = await stream.is_disconnected()

            # Edge-trigger DESIGN_START on the first user-visible agent event —
            # message_output_created is the canonical signal, tool_called the
            # fallback when the agent goes straight to a tool. Best-effort: a
            # serialization failure here cannot abort the agent run.
            if event.name in ("message_output_created", "tool_called") and not client_gone:
                try:
                    await maybe_emit_design_start(stream, ctx)
                except Exception as emit_err:
                    LOG.warning("copilot_narrative_design_start_emit_failed", error=str(emit_err))

            if event.name == "tool_called":
                raw = event.item.raw_item
                call_id = _get_raw_field(raw, "call_id") or _get_raw_field(raw, "id") or ""
                tool_name = _get_raw_field(raw, "name") or "unknown"
                call_id_to_name[call_id] = tool_name
                narrator_state.record_activity(build_tool_call_activity(tool_name, iteration, call_id))

                if not client_gone:
                    raw_args = _get_raw_field(raw, "arguments")
                    tool_input: dict[str, Any] = {}
                    if isinstance(raw_args, str):
                        try:
                            tool_input = json.loads(raw_args)
                        except (json.JSONDecodeError, TypeError):
                            tool_input = {"raw": raw_args}
                    elif isinstance(raw_args, dict):
                        tool_input = raw_args

                    await stream.send(
                        WorkflowCopilotToolCallUpdate(
                            type=WorkflowCopilotStreamMessageType.TOOL_CALL,
                            tool_name=tool_name,
                            display_label=tool_activity_display_label(tool_name),
                            tool_input=_sanitize_input(tool_input),
                            iteration=iteration,
                            tool_call_id=call_id,
                        )
                    )

                # First narration lands here (~seconds after submit) rather
                # than waiting for tool_output of a long tool.
                if narrator_enabled:
                    narrator_state.pending_tool_name = tool_name
                    narrator_state.current_iteration = iteration
                    narrator_state.record_transition(TransitionKind.TOOL_STARTED)
                    schedule_narration(narrator_state, stream, iteration)

            elif event.name == "tool_output":
                raw = event.item.raw_item
                call_id = _get_raw_field(raw, "call_id") or _get_raw_field(raw, "id") or ""
                tool_name = call_id_to_name.get(call_id, "unknown")
                # Clear pending_tool_name so post-tool transitions describe
                # what the agent just finished, not what it's still doing.
                narrator_state.pending_tool_name = None

                output = getattr(event.item, "output", None)
                parsed = parse_tool_output(output)
                blocker_signals = _tool_blocker_signal_candidates(ctx)
                summary = format_tool_result_for_user(tool_name, parsed, blocker_signal=blocker_signals)
                success = parsed.get("ok", True)
                detail = summarize_tool_result_detail(parsed, tool_name=tool_name, blocker_signal=blocker_signals)
                narrator_state.record_activity(
                    build_tool_result_activity(tool_name, summary, success, iteration, call_id)
                )

                if not client_gone:
                    await stream.send(
                        WorkflowCopilotToolResultUpdate(
                            type=WorkflowCopilotStreamMessageType.TOOL_RESULT,
                            tool_name=tool_name,
                            success=success,
                            summary=summary,
                            iteration=iteration,
                            tool_call_id=call_id,
                            detail=detail,
                        )
                    )

                if narrator_enabled:
                    ctx_before = snapshot_ctx(ctx)
                    _update_enforcement_from_tool(ctx, tool_name, parsed)
                    ctx_after = snapshot_ctx(ctx)

                    prior_tool_name = (
                        narrator_state.pending_activity[-1].tool_name if narrator_state.pending_activity else None
                    )
                    narrator_state.record_tool(
                        tool_name=tool_name,
                        summary=summary,
                        success=success,
                        iteration=iteration,
                        details=extract_tool_details(tool_name, parsed),
                    )
                    for transition in detect_transitions(ctx_before, ctx_after, tool_name, prior_tool_name):
                        narrator_state.record_transition(transition)
                    narrator_state.current_iteration = iteration
                    schedule_narration(narrator_state, stream, iteration)
                else:
                    _update_enforcement_from_tool(ctx, tool_name, parsed)

                try:
                    _maybe_raise_unrecoverable_tool_error(ctx, tool_name, parsed)
                except CopilotUnrecoverableToolError:
                    result.cancel()
                    raise
                stash_turn_halt_from_blocker_signal(
                    ctx,
                    getattr(ctx, "latest_tool_blocker_signal", None) or getattr(ctx, "blocker_signal", None),
                    source="streaming_adapter",
                )
                try:
                    raise_if_turn_halt(ctx)
                except CopilotTurnHalt:
                    result.cancel()
                    raise
                # Keep latest_tool_blocker_signal scoped to the tool result
                # that immediately follows the blocker-producing tool call.
                ctx.latest_tool_blocker_signal = None
                iteration += 1
    except asyncio.CancelledError:
        # Real cancellation (server shutdown, upstream abort). Propagate so
        # asyncio's task machinery sees the cancel; also cancel the SDK
        # run to free provider resources.
        result.cancel()
        raise
    finally:
        # Cancel any in-flight narration before the stream tears down so
        # tasks don't try to send on a disconnected channel.
        await cancel_in_flight(narrator_state)


def _get_raw_field(raw: Any, key: str) -> Any:
    if isinstance(raw, dict):
        return raw.get(key)
    return getattr(raw, key, None)


def _tool_blocker_signal_candidates(ctx: Any) -> list[Any]:
    candidates: list[Any] = []
    latest = getattr(ctx, "latest_tool_blocker_signal", None)
    if latest is not None:
        candidates.append(latest)
    history = getattr(ctx, "tool_blocker_signals", None)
    if isinstance(history, list):
        candidates.extend(reversed(history))
    sticky = getattr(ctx, "blocker_signal", None)
    if sticky is not None:
        candidates.append(sticky)
    deduped: list[Any] = []
    seen_ids: set[int] = set()
    for candidate in candidates:
        candidate_id = id(candidate)
        if candidate_id in seen_ids:
            continue
        seen_ids.add(candidate_id)
        deduped.append(candidate)
    return deduped


def _extract_text_content(item: Any) -> str | None:
    """Extract text from a dict or object with type='text', returning None otherwise."""
    if isinstance(item, dict):
        if item.get("type") == "text":
            return item.get("text")
        return None
    if getattr(item, "type", None) == "text":
        return getattr(item, "text", None)
    return None


def parse_tool_output(output: Any) -> dict[str, Any]:
    if output is None:
        return {"ok": True}

    if isinstance(output, list):
        for item in output:
            text = _extract_text_content(item)
            if text is not None:
                try:
                    return _to_result_dict(json.loads(text))
                except (json.JSONDecodeError, TypeError):
                    pass
        return {"ok": True, "data": str(output)}

    if isinstance(output, dict):
        if output.get("type") == "text":
            try:
                return _to_result_dict(json.loads(output["text"]))
            except (json.JSONDecodeError, TypeError, KeyError):
                return {"ok": True, "data": str(output)}
        if "ok" in output:
            return output
        return {"ok": True, "data": output}

    text = _extract_text_content(output)
    if text is not None:
        try:
            return _to_result_dict(json.loads(text))
        except (json.JSONDecodeError, TypeError):
            pass

    if isinstance(output, str):
        try:
            return _to_result_dict(json.loads(output))
        except (json.JSONDecodeError, TypeError):
            pass

    return {"ok": True, "data": str(output)}


def _to_result_dict(value: Any) -> dict[str, Any]:
    if isinstance(value, dict):
        return value
    return {"ok": True, "data": value}


def _update_enforcement_from_tool(
    ctx: Any,
    tool_name: str,
    output: dict[str, Any],
) -> None:
    data = output.get("data")
    has_blocks = isinstance(data, dict) and data.get("block_count", 0) > 0

    if tool_name in ("update_workflow", "update_and_run_blocks") and output.get("ok") and has_blocks:
        ctx.update_workflow_called = True
        ctx.test_after_update_done = False
        ctx.post_update_nudge_count = 0

    if tool_name in ("run_blocks_and_collect_debug", "update_and_run_blocks"):
        ctx.test_after_update_done = True

    if tool_name == "navigate_browser" and output.get("ok"):
        ctx.navigate_called = True
        ctx.observation_after_navigate = False
        # Re-arm the per-cycle latch so the nudge can fire on the NEXT
        # navigate-without-observe, not only the first one.
        ctx.navigate_enforcement_done = False

    if tool_name in _OBSERVATION_TOOLS:
        ctx.observation_after_navigate = True


def _sanitize_input(raw_args: dict[str, Any]) -> dict[str, Any]:
    # Redacts tool-call args before they hit the SSE payload sent to the UI.
    # Distinct from output_utils.sanitize_tool_result_for_llm, which shapes
    # tool *results* for LLM context consumption.
    # Drop the large workflow YAML blob (it's displayed elsewhere in the UI),
    # then run the remaining args through the shared exact-match redactor to
    # strip values under sensitive key names like `password`, `api_key`,
    # `totp`, etc. Benign identifiers (`credential_id`, `page_token`,
    # `username`) pass through unchanged.
    trimmed = {k: v for k, v in raw_args.items() if k != "workflow_yaml"}
    redacted = redact_sensitive_fields(trimmed)
    if isinstance(redacted, dict):
        return redacted
    return trimmed


async def emit_turn_start(stream: EventSourceStream, ctx: CopilotContext) -> None:
    mode_value = ctx.turn_intent.mode.value if ctx.turn_intent is not None else WorkflowCopilotTurnMode.UNKNOWN.value
    now = datetime.now(timezone.utc)
    if ctx.turn_started_at is None:
        ctx.turn_started_at = now.isoformat()
    await stream.send(
        WorkflowCopilotTurnStartUpdate(
            turn_id=ctx.turn_id,
            turn_index=ctx.turn_index,
            mode=WorkflowCopilotTurnMode(mode_value),
            timestamp=now,
            prior_block_count=ctx.prior_block_count,
        )
    )


async def maybe_emit_design_start(stream: EventSourceStream, ctx: CopilotContext) -> None:
    if ctx.design_start_emitted:
        return
    ctx.design_start_emitted = True
    await stream.send(WorkflowCopilotDesignStartUpdate(timestamp=datetime.now(timezone.utc)))


async def maybe_emit_design_end(stream: EventSourceStream, ctx: CopilotContext) -> None:
    # Guard: never emit DESIGN_END without a matching DESIGN_START. Both flags
    # are turn-scoped, so a turn that exits before the streaming adapter sees
    # its first user-visible event simply skips the design phase entirely.
    if not ctx.design_start_emitted or ctx.design_end_emitted:
        return
    ctx.design_end_emitted = True
    await stream.send(WorkflowCopilotDesignEndUpdate(timestamp=datetime.now(timezone.utc)))


async def emit_workflow_draft(
    stream: EventSourceStream,
    ctx: CopilotContext,
    workflow: Workflow,
    *,
    include_workflow: bool = True,
) -> None:
    """Emit a WORKFLOW_DRAFT envelope; ``include_workflow=False`` suppresses
    the canvas auto-render for untested paths (inline REPLACE_WORKFLOW).
    """
    block_count = 0
    block_labels: list[str] = []
    try:
        for block in workflow.workflow_definition.blocks:
            block_count += 1
            label = getattr(block, "label", None)
            if isinstance(label, str) and label:
                block_labels.append(label)
    except AttributeError:
        pass
    workflow_dump: dict | None = None
    if include_workflow:
        try:
            workflow_dump = workflow.model_dump(mode="json")
        except Exception as dump_err:
            LOG.warning("copilot_workflow_draft_serialization_failed", error=str(dump_err))
    await stream.send(
        WorkflowCopilotWorkflowDraftUpdate(
            block_count=block_count,
            block_labels=block_labels,
            summary=None,
            timestamp=datetime.now(timezone.utc),
            workflow=workflow_dump,
        )
    )
