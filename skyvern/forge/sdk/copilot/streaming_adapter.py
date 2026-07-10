"""Map OpenAI Agents SDK stream events to Skyvern SSE payloads."""

from __future__ import annotations

import asyncio
import json
import re
import time
from datetime import datetime, timezone
from typing import TYPE_CHECKING, Any

import structlog

from skyvern.config import settings

# Reuse the HTTP-logging redactor so SSE tool inputs and request-body logs
# share one exact-match sensitive-key policy.
from skyvern.forge.request_logging import redact_sensitive_fields
from skyvern.forge.sdk.copilot.context import InFlightStreamToolCall
from skyvern.forge.sdk.copilot.narration import (
    CODE_REPAIR_PROGRESS_SURFACE_KIND,
    NarratorState,
    TransitionKind,
    build_narration_activity,
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
    WorkflowCopilotCodegenProgressUpdate,
    WorkflowCopilotDesignEndUpdate,
    WorkflowCopilotDesignStartUpdate,
    WorkflowCopilotNarrationUpdate,
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

_AUTHORING_TOOL_NAMES = frozenset({"update_and_run_blocks", "update_workflow"})
# Pure substring heuristic over raw (unparsed) JSON text: a free-text field (e.g. navigation_goal)
# that happens to contain the literal "label:" would also match. Accepted trade-off of not
# json.loads-ing the partial buffer; worst case is a spurious drafted-block entry.
_CODEGEN_LABEL_RE = re.compile(r"label:\s*\\?\"?([A-Za-z0-9_][A-Za-z0-9_ \-]{0,79})")
_CODEGEN_MIN_GAP_SECONDS = 2.0
# Keep enough trailing context that a label split across two argument deltas still matches.
_CODEGEN_TAIL_OVERLAP = 96
_CODEGEN_MAX_LABELS = 50


class _CodegenCallState:
    __slots__ = (
        "tool_name",
        "labels",
        "_seen_labels",
        "chars",
        "n_deltas",
        "tail",
        "last_emit_monotonic",
        "started_monotonic",
    )

    def __init__(self, tool_name: str) -> None:
        self.tool_name = tool_name
        self.labels: list[str] = []
        self._seen_labels: set[str] = set()
        self.chars = 0
        self.n_deltas = 0
        self.tail = ""
        self.last_emit_monotonic = 0.0
        self.started_monotonic = time.monotonic()

    def add_labels(self, text: str) -> bool:
        found_new = False
        for match in _CODEGEN_LABEL_RE.finditer(text):
            if match.end() == len(text):
                # Match runs up to the edge of the currently scanned text: the label may still
                # be mid-stream, split across a delta boundary. Wait for a delimiter to confirm
                # it (re-scanned via the tail overlap on the next delta) instead of committing
                # a truncated fragment permanently.
                continue
            if len(self.labels) >= _CODEGEN_MAX_LABELS:
                break
            label = match.group(1)
            if label in self._seen_labels:
                continue
            self._seen_labels.add(label)
            self.labels.append(label)
            found_new = True
        return found_new


class _CodegenProgressTracker:
    """Tracks streamed authoring-tool-call arguments (keyed by output_index, per the
    FAKE_RESPONSES_ID caveat) and emits throttled CODEGEN_PROGRESS frames."""

    def __init__(self) -> None:
        self._calls: dict[int, _CodegenCallState] = {}

    async def on_raw_event(
        self,
        data: Any,
        stream: EventSourceStream,
        ctx: CopilotContext,
        iteration: int,
    ) -> None:
        event_type = getattr(data, "type", "")

        if event_type == "response.created":
            self._calls.clear()
            return

        if event_type == "response.output_item.added":
            item = getattr(data, "item", None)
            if getattr(item, "type", None) != "function_call":
                return
            name = getattr(item, "name", None)
            if name not in _AUTHORING_TOOL_NAMES:
                return
            output_index = getattr(data, "output_index", None)
            if output_index is None:
                return
            state = _CodegenCallState(name)
            self._calls[output_index] = state
            await self._emit(stream, ctx, iteration, state)
            return

        if event_type == "response.function_call_arguments.delta":
            output_index = getattr(data, "output_index", None)
            tracked_state = self._calls.get(output_index) if output_index is not None else None
            if tracked_state is None:
                return
            delta = getattr(data, "delta", "") or ""
            tracked_state.n_deltas += 1
            tracked_state.chars += len(delta)
            found_new = tracked_state.add_labels(tracked_state.tail + delta)
            tracked_state.tail = (tracked_state.tail + delta)[-_CODEGEN_TAIL_OVERLAP:]
            gap_elapsed = (time.monotonic() - tracked_state.last_emit_monotonic) >= _CODEGEN_MIN_GAP_SECONDS
            if found_new or gap_elapsed:
                await self._emit(stream, ctx, iteration, tracked_state)
            return

        if event_type in ("response.output_item.done", "response.function_call_arguments.done"):
            # Both events fire for one completed call; pop() makes the second a no-op.
            output_index = getattr(data, "output_index", None)
            finished_state = self._calls.pop(output_index, None) if output_index is not None else None
            if finished_state is not None:
                LOG.info(
                    "copilot_codegen_progress_summary",
                    tool_name=finished_state.tool_name,
                    n_deltas=finished_state.n_deltas,
                    chars=finished_state.chars,
                    n_labels=len(finished_state.labels),
                    elapsed=time.monotonic() - finished_state.started_monotonic,
                )
            return

    async def _emit(
        self,
        stream: EventSourceStream,
        ctx: CopilotContext,
        iteration: int,
        state: _CodegenCallState,
    ) -> None:
        try:
            await maybe_emit_design_start(stream, ctx)
        except Exception as emit_err:
            LOG.warning("copilot_narrative_design_start_emit_failed", error=str(emit_err))

        # Advance the throttle gate regardless of connection outcome: otherwise a
        # permanently disconnected client leaves last_emit_monotonic frozen and every
        # subsequent delta re-attempts is_disconnected(), the "never per delta" cost
        # this throttle exists to bound.
        state.last_emit_monotonic = time.monotonic()
        if await stream.is_disconnected():
            return
        try:
            await stream.send(
                WorkflowCopilotCodegenProgressUpdate(
                    tool_name=state.tool_name,
                    blocks_drafted=list(state.labels),
                    chars_streamed=state.chars,
                    iteration=iteration,
                    timestamp=datetime.now(timezone.utc),
                )
            )
        except Exception as emit_err:
            LOG.warning("copilot_codegen_progress_emit_failed", error=str(emit_err))


def _code_repair_progress_text(parsed: dict[str, Any]) -> str | None:
    """Return the classified code-repair progress text carried on a reject's ``data``, else None."""
    data = parsed.get("data")
    if not isinstance(data, dict):
        return None
    if data.get("surface_kind") != CODE_REPAIR_PROGRESS_SURFACE_KIND:
        return None
    progress_text = data.get("progress_text")
    return progress_text if isinstance(progress_text, str) and progress_text else None


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
    from agents.stream_events import RawResponsesStreamEvent, RunItemStreamEvent

    from skyvern.forge.sdk.copilot.enforcement import (
        CopilotUnrecoverableToolError,
        _maybe_raise_unrecoverable_tool_error,
        outcome_fully_verified,
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

    codegen_tracker = _CodegenProgressTracker() if settings.WORKFLOW_COPILOT_CODEGEN_PROGRESS_ENABLED else None

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
            if codegen_tracker is not None and isinstance(event, RawResponsesStreamEvent):
                await codegen_tracker.on_raw_event(event.data, stream, ctx, iteration)
                continue
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
                ctx.in_flight_stream_tool_call = InFlightStreamToolCall(
                    call_id=call_id, tool_name=tool_name, iteration=iteration
                )
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
                ctx.in_flight_stream_tool_call = None
                # Clear pending_tool_name so post-tool transitions describe
                # what the agent just finished, not what it's still doing.
                narrator_state.pending_tool_name = None

                output = getattr(event.item, "output", None)
                parsed = parse_tool_output(output)
                progress_text = _code_repair_progress_text(parsed)
                if progress_text is not None:
                    # Presentation-only: render the reject as quiet de-duplicated progress;
                    # enforcement and the turn-halt below still run.
                    await _emit_code_repair_progress(
                        narrator_state=narrator_state,
                        stream=stream,
                        progress_text=progress_text,
                        iteration=iteration,
                        client_gone=client_gone,
                    )
                    _update_enforcement_from_tool(ctx, tool_name, parsed)
                else:
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
                                workflow_run_id=_tool_result_workflow_run_id(tool_name, parsed),
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
                    raise_if_turn_halt(ctx, verified=outcome_fully_verified(ctx))
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


async def _emit_code_repair_progress(
    *,
    narrator_state: NarratorState,
    stream: EventSourceStream,
    progress_text: str,
    iteration: int,
    client_gone: bool,
) -> None:
    """Surface a code-authoring reject as one quiet progress entry per turn. The persisted activity
    record runs unconditionally (so a disconnected client gets it on rehydration); only the live
    frame is gated on the client."""
    # Advance the narrator iteration on every classified round-trip, including a de-duplicated one,
    # so readers (e.g. the run-outcome frame) don't see a stale value.
    narrator_state.current_iteration = iteration
    if progress_text in narrator_state.emitted_progress_texts:
        return
    narrator_state.emitted_progress_texts.add(progress_text)

    # An LLM tool-start narration scheduled at tool_called would otherwise double
    # up with this progress entry for the same iteration; drop it.
    await cancel_in_flight(narrator_state)

    narration_ts = datetime.now(timezone.utc)
    narrator_state.record_activity(build_narration_activity(progress_text, iteration, narration_ts))
    if client_gone:
        return
    await stream.send(
        WorkflowCopilotNarrationUpdate(
            type=WorkflowCopilotStreamMessageType.NARRATION,
            narration=progress_text,
            iteration=iteration,
            timestamp=narration_ts,
        )
    )


_BLOCK_RUNNING_TOOL_NAMES = frozenset({"update_and_run_blocks", "run_blocks_and_collect_debug"})


def _tool_result_workflow_run_id(tool_name: str, parsed: dict[str, Any]) -> str | None:
    # Only block-running tools create a new run; read-only tools (e.g. get_run_results) echo a prior
    # run id and would misattribute a stale run to the current turn.
    if tool_name not in _BLOCK_RUNNING_TOOL_NAMES:
        return None
    data = parsed.get("data")
    run_id = data.get("workflow_run_id") if isinstance(data, dict) else None
    return run_id if isinstance(run_id, str) else None


async def flush_goal_satisfied_tool_result(stream: EventSourceStream, ctx: CopilotContext) -> None:
    """Emit the TOOL_RESULT frame for the goal-satisfying tool.

    The goal-satisfied stop is raised from ``on_tool_end`` before the SDK
    yields the matching ``tool_output`` event, so the frame the loop above
    would have sent never streams; the exit path calls this instead.
    """
    pending = ctx.in_flight_stream_tool_call
    parsed = ctx.goal_satisfied_tool_output
    tool_name = ctx.goal_satisfied_tool_name
    ctx.in_flight_stream_tool_call = None
    ctx.goal_satisfied_tool_output = None
    ctx.goal_satisfied_tool_name = None
    if pending is None or parsed is None or tool_name != pending.tool_name:
        return
    blocker_signals = _tool_blocker_signal_candidates(ctx)
    summary = format_tool_result_for_user(pending.tool_name, parsed, blocker_signal=blocker_signals)
    success = parsed.get("ok", True)
    narrator_state = ctx.narrator_state
    if narrator_state is not None:
        narrator_state.record_activity(
            build_tool_result_activity(pending.tool_name, summary, success, pending.iteration, pending.call_id)
        )
    if await stream.is_disconnected():
        return
    await stream.send(
        WorkflowCopilotToolResultUpdate(
            type=WorkflowCopilotStreamMessageType.TOOL_RESULT,
            tool_name=pending.tool_name,
            success=success,
            summary=summary,
            iteration=pending.iteration,
            tool_call_id=pending.call_id,
            detail=summarize_tool_result_detail(parsed, tool_name=pending.tool_name, blocker_signal=blocker_signals),
            workflow_run_id=_tool_result_workflow_run_id(pending.tool_name, parsed),
        )
    )


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
    if tool_name in ("update_workflow", "update_and_run_blocks") and has_blocks:
        ctx.synthesized_block_reopened_after_failed_run = False
        ctx.synthesized_block_reopened_for_output_coverage = False
        ctx.uncovered_output_rescout_steer_key = None

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
