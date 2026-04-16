"""Map OpenAI Agents SDK stream events to Skyvern SSE payloads."""

from __future__ import annotations

import asyncio
import json
from typing import TYPE_CHECKING, Any

import structlog

# Reuse the HTTP-logging redactor so SSE tool inputs and request-body logs
# share one exact-match sensitive-key policy.
from skyvern.forge.request_logging import redact_sensitive_fields
from skyvern.forge.sdk.copilot.exceptions import CopilotClientDisconnectedError
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

    A true client disconnect -- detected by ``stream.is_disconnected()`` or a
    failed ``stream.send()`` -- cancels the agent run and raises
    ``CopilotClientDisconnectedError``. A plain ``asyncio.CancelledError``
    from some other source (task-group cancel, upstream timeout, parent
    abort) is allowed to propagate unchanged so asyncio's cancellation
    machinery runs normally; callers that want the two to share a UX path
    should catch both at the call site.
    """
    from agents.stream_events import RunItemStreamEvent

    call_id_to_name: dict[str, str] = {}
    # Counts completed tool round-trips (tool_called + tool_output pair), not
    # raw stream events. Both TOOL_CALL and TOOL_RESULT for the same round
    # carry the same iteration value; it advances after the matching result.
    iteration = 0

    try:
        async for event in result.stream_events():
            if await stream.is_disconnected():
                result.cancel()
                raise CopilotClientDisconnectedError()
            if not isinstance(event, RunItemStreamEvent):
                continue

            if event.name == "tool_called":
                raw = event.item.raw_item
                call_id = _get_raw_field(raw, "call_id") or _get_raw_field(raw, "id") or ""
                tool_name = _get_raw_field(raw, "name") or "unknown"
                call_id_to_name[call_id] = tool_name

                raw_args = _get_raw_field(raw, "arguments")
                tool_input: dict[str, Any] = {}
                if isinstance(raw_args, str):
                    try:
                        tool_input = json.loads(raw_args)
                    except (json.JSONDecodeError, TypeError):
                        tool_input = {"raw": raw_args}
                elif isinstance(raw_args, dict):
                    tool_input = raw_args

                if not await stream.send(
                    WorkflowCopilotToolCallUpdate(
                        type=WorkflowCopilotStreamMessageType.TOOL_CALL,
                        tool_name=tool_name,
                        tool_input=_sanitize_input(tool_input),
                        iteration=iteration,
                        tool_call_id=call_id,
                    )
                ):
                    result.cancel()
                    raise CopilotClientDisconnectedError()

            elif event.name == "tool_output":
                raw = event.item.raw_item
                call_id = _get_raw_field(raw, "call_id") or _get_raw_field(raw, "id") or ""
                tool_name = call_id_to_name.get(call_id, "unknown")

                output = getattr(event.item, "output", None)
                parsed = parse_tool_output(output)
                summary = summarize_tool_result(tool_name, parsed)
                success = parsed.get("ok", True)

                if not await stream.send(
                    WorkflowCopilotToolResultUpdate(
                        type=WorkflowCopilotStreamMessageType.TOOL_RESULT,
                        tool_name=tool_name,
                        success=success,
                        summary=summary,
                        iteration=iteration,
                        tool_call_id=call_id,
                    )
                ):
                    result.cancel()
                    raise CopilotClientDisconnectedError()

                _update_enforcement_from_tool(ctx, tool_name, parsed)
                iteration += 1
    except asyncio.CancelledError:
        # Don't relabel generic cancellation as a client disconnect -- the
        # inline is_disconnected / send-failure branches above already raise
        # CopilotClientDisconnectedError when there is real evidence of a
        # dropped client. Preserve cancellation semantics by re-raising so
        # asyncio's task machinery sees the cancel and any upstream
        # except Exception does NOT swallow it.
        result.cancel()
        raise


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
