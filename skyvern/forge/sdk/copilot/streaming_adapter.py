"""Map OpenAI Agents SDK stream events to Skyvern SSE payloads."""

from __future__ import annotations

import asyncio
import json
from typing import TYPE_CHECKING, Any

import structlog

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

    If the client disconnects while the agent is still running, this cancels
    the agent run and raises ``CopilotClientDisconnectedError``.
    """
    from agents.stream_events import RunItemStreamEvent

    from skyvern.forge.sdk.copilot.enforcement import CopilotClientDisconnectedError

    call_id_to_name: dict[str, str] = {}
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

                await stream.send(
                    WorkflowCopilotToolCallUpdate(
                        type=WorkflowCopilotStreamMessageType.TOOL_CALL,
                        tool_name=tool_name,
                        tool_input=_sanitize_input(tool_input),
                        iteration=iteration,
                        tool_call_id=call_id,
                    )
                )

            elif event.name == "tool_output":
                raw = event.item.raw_item
                call_id = _get_raw_field(raw, "call_id") or _get_raw_field(raw, "id") or ""
                tool_name = call_id_to_name.get(call_id, "unknown")

                output = getattr(event.item, "output", None)
                parsed = _parse_tool_output(output)
                summary = summarize_tool_result(tool_name, parsed)
                success = parsed.get("ok", True)

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

                _update_enforcement_from_tool(ctx, tool_name, parsed)
                iteration += 1
    except asyncio.CancelledError:
        result.cancel()
        raise CopilotClientDisconnectedError()


def _get_raw_field(raw: Any, key: str) -> Any:
    if isinstance(raw, dict):
        return raw.get(key)
    return getattr(raw, key, None)


def _parse_tool_output(output: Any) -> dict[str, Any]:
    if output is None:
        return {"ok": True}

    if isinstance(output, list):
        for item in output:
            if isinstance(item, dict) and item.get("type") == "text":
                try:
                    return _to_result_dict(json.loads(item["text"]))
                except (json.JSONDecodeError, TypeError, KeyError):
                    pass
            if hasattr(item, "type") and getattr(item, "type", None) == "text":
                try:
                    return _to_result_dict(json.loads(getattr(item, "text", "")))
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

    if hasattr(output, "type") and hasattr(output, "text"):
        if getattr(output, "type", None) == "text":
            try:
                return _to_result_dict(json.loads(getattr(output, "text", "")))
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

    if tool_name == "update_workflow" and output.get("ok") and has_blocks:
        ctx.update_workflow_called = True
        ctx.test_after_update_done = False
        ctx.post_update_nudge_count = 0
        ctx.premature_completion_nudge_done = False

    if tool_name == "run_blocks_and_collect_debug":
        ctx.test_after_update_done = True

    if tool_name == "navigate_browser" and output.get("ok"):
        ctx.navigate_called = True
        ctx.observation_after_navigate = False

    if tool_name in _OBSERVATION_TOOLS:
        ctx.observation_after_navigate = True


def _sanitize_input(raw_args: dict[str, Any]) -> dict[str, Any]:
    return {k: v for k, v in raw_args.items() if k != "workflow_yaml"}
