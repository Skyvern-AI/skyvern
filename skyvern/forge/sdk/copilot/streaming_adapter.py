"""Map OpenAI Agents SDK stream events to Skyvern SSE payloads."""

from __future__ import annotations

import asyncio
import json
from typing import TYPE_CHECKING, Any

import structlog

# Reuse the HTTP-logging redactor so SSE tool inputs and request-body logs
# share one exact-match sensitive-key policy.
from skyvern.forge.request_logging import redact_sensitive_fields
from skyvern.forge.sdk.copilot.narration import (
    NarratorState,
    TransitionKind,
    cancel_in_flight,
    detect_transitions,
    extract_tool_details,
    handler_available,
    schedule_narration,
    snapshot_ctx,
)
from skyvern.forge.sdk.copilot.output_utils import summarize_tool_result
from skyvern.forge.sdk.schemas.workflow_copilot import (
    WorkflowCopilotStreamMessageType,
    WorkflowCopilotToolCallUpdate,
    WorkflowCopilotToolResultUpdate,
)

if TYPE_CHECKING:
    from agents.result import RunResultStreaming

    from skyvern.forge.sdk.routes.event_source_stream import EventSourceStream

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

    call_id_to_name: dict[str, str] = {}
    # Counts completed tool round-trips (tool_called + tool_output pair), not
    # raw stream events. Both TOOL_CALL and TOOL_RESULT for the same round
    # carry the same iteration value; it advances after the matching result.
    iteration = 0

    # Narrator state persists across enforcement iterations via ctx so
    # cadence (last_emitted_at, min-gap) survives run_with_enforcement retries.
    # Resolve handler availability once so per-event narrator bookkeeping
    # (snapshot, detect, extract) is skipped when no narrator LLM is configured.
    narrator_enabled = handler_available()
    narrator_state: NarratorState = getattr(ctx, "narrator_state", None) or NarratorState()
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

            if event.name == "tool_called":
                raw = event.item.raw_item
                call_id = _get_raw_field(raw, "call_id") or _get_raw_field(raw, "id") or ""
                tool_name = _get_raw_field(raw, "name") or "unknown"
                call_id_to_name[call_id] = tool_name

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
                            tool_input=_sanitize_input(tool_input),
                            iteration=iteration,
                            tool_call_id=call_id,
                        )
                    )

                # First narration lands here (~seconds after submit) rather
                # than waiting for tool_output of a long tool.
                if narrator_enabled:
                    narrator_state.pending_tool_name = tool_name
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
                # Compute summary/success unconditionally: the narrator path
                # below also needs them, and the work is cheap (no I/O).
                summary = summarize_tool_result(tool_name, parsed)
                success = parsed.get("ok", True)

                if not client_gone:
                    await stream.send(
                        WorkflowCopilotToolResultUpdate(
                            type=WorkflowCopilotStreamMessageType.TOOL_RESULT,
                            tool_name=tool_name,
                            success=success,
                            summary=summary,
                            iteration=iteration,
                            tool_call_id=call_id,
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
                    schedule_narration(narrator_state, stream, iteration)
                else:
                    _update_enforcement_from_tool(ctx, tool_name, parsed)
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
