from __future__ import annotations

import json
from collections.abc import AsyncIterator
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest
from agents import FunctionTool
from agents.items import RunItem
from agents.stream_events import RunItemStreamEvent
from agents.tool_context import ToolContext
from structlog.testing import capture_logs

from skyvern.forge.sdk.copilot import tools
from skyvern.forge.sdk.copilot.context import CopilotContext
from skyvern.forge.sdk.copilot.hooks import CopilotRunHooks
from skyvern.forge.sdk.copilot.secret_scrub import register_secret_scrub_value
from skyvern.forge.sdk.copilot.streaming_adapter import parse_tool_output, stream_to_sse
from skyvern.forge.sdk.schemas.workflow_copilot import WorkflowCopilotStreamMessageType
from tests.unit.copilot_test_helpers import make_copilot_ctx


@pytest.fixture
def handler_boundary(monkeypatch: pytest.MonkeyPatch) -> SimpleNamespace:
    boundary = SimpleNamespace(
        entered=AsyncMock(),
        update=AsyncMock(return_value={"ok": True, "data": {"block_count": 1}}),
        execute=AsyncMock(return_value=json.dumps({"ok": True, "data": {"overall_status": "completed"}})),
    )
    monkeypatch.setattr(tools, "await_pending_credential_pause", boundary.entered)
    monkeypatch.setattr(tools, "_get_prior_workflow_definition", AsyncMock(return_value=None))
    monkeypatch.setattr(tools, "_update_workflow", boundary.update)
    monkeypatch.setattr(tools, "_run_updated_workflow_blocks", boundary.execute)
    return boundary


async def project_result(ctx: CopilotContext, arguments: str, output: Any) -> dict[str, Any]:
    tool = tools.update_and_run_blocks_tool
    with capture_logs() as logs:
        await CopilotRunHooks(ctx).on_tool_end(MagicMock(), MagicMock(), tool, output)
    completed = [log for log in logs if log["event"] == "copilot tool completed"]
    assert len(completed) == 1

    async def events() -> AsyncIterator[RunItemStreamEvent]:
        call = MagicMock(spec=RunItem)
        call.raw_item = {"call_id": "call_test", "name": tool.name, "arguments": arguments}
        yield RunItemStreamEvent(name="tool_called", item=call)
        result = MagicMock(spec=RunItem)
        result.raw_item = {"call_id": "call_test"}
        result.output = output
        yield RunItemStreamEvent(name="tool_output", item=result)

    sdk_result = MagicMock()
    sdk_result.stream_events = events
    stream = MagicMock()
    stream.is_disconnected = AsyncMock(return_value=False)
    stream.send = AsyncMock(return_value=True)
    await stream_to_sse(sdk_result, stream, ctx)
    results = [
        call.args[0]
        for call in stream.send.call_args_list
        if call.args[0].type == WorkflowCopilotStreamMessageType.TOOL_RESULT
    ]
    assert len(results) == 1
    return {"log": completed[0], "activity": ctx.tool_activity[-1], "sse": results[0].model_dump(mode="json")}


async def invoke(arguments: str, ctx: CopilotContext) -> Any:
    tool = tools.update_and_run_blocks_tool
    return await tool.on_invoke_tool(
        ToolContext(context=ctx, tool_name=tool.name, tool_call_id="call_test", tool_arguments=arguments), arguments
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("metadata", ["<metadata truncated: 11234 chars>", "<metadata truncated: 11615 chars>"])
async def test_rejected_arguments_and_corrected_retry(handler_boundary: SimpleNamespace, metadata: str) -> None:
    ctx = make_copilot_ctx()
    arguments = {"workflow_yaml": "title: Example", "block_labels": ["example"], "code_artifact_metadata": metadata}
    output = await invoke(json.dumps(arguments), ctx)
    parsed = parse_tool_output(output)
    assert parsed["ok"] is False
    assert "code_artifact_metadata" in parsed["error"]
    errors = parsed["data"]["validation_errors"]
    assert errors[0]["loc"] == ["code_artifact_metadata"]
    assert errors[0]["type"] == "list_type"
    assert "list" in errors[0]["msg"]
    assert "input" not in errors[0]
    assert metadata not in output
    handler_boundary.entered.assert_not_awaited()
    handler_boundary.update.assert_not_awaited()
    handler_boundary.execute.assert_not_awaited()
    projection = await project_result(ctx, json.dumps(arguments), output)
    assert projection["log"]["ok"] is False
    assert projection["activity"]["summary"].startswith("Failed:")
    assert projection["sse"]["success"] is False
    assert not ctx.dispatched_run_ids_this_turn
    assert ctx.last_test_ok is not True

    arguments["code_artifact_metadata"] = []
    corrected_output = await invoke(json.dumps(arguments), ctx)
    handler_boundary.entered.assert_awaited_once()
    handler_boundary.update.assert_awaited_once()
    handler_boundary.execute.assert_awaited_once()
    assert corrected_output == handler_boundary.execute.return_value
    projection = await project_result(ctx, json.dumps(arguments), corrected_output)
    assert projection["log"]["ok"] is True
    assert projection["sse"]["success"] is True


@pytest.mark.asyncio
@pytest.mark.parametrize("output", ["ordinary successful text", '{"ok": false, "error": "execution failed"}'])
async def test_handler_results_are_preserved(handler_boundary: SimpleNamespace, output: str) -> None:
    handler_boundary.execute.return_value = output
    ctx = make_copilot_ctx()
    arguments = json.dumps({"workflow_yaml": "title: Example", "block_labels": ["example"]})
    actual = await invoke(arguments, ctx)
    assert actual == output
    handler_boundary.entered.assert_awaited_once()
    handler_boundary.execute.assert_awaited_once()
    projection = await project_result(ctx, arguments, actual)
    assert projection["sse"]["success"] is parse_tool_output(output)["ok"]


@pytest.mark.asyncio
@pytest.mark.parametrize("arguments", ['{"workflow_yaml": "unregistered-sensitive-value",', '{"block_labels": []}'])
async def test_malformed_or_missing_arguments_do_not_echo_input(
    handler_boundary: SimpleNamespace, arguments: str
) -> None:
    output = await invoke(arguments, make_copilot_ctx())
    assert parse_tool_output(output)["ok"] is False
    assert "unregistered-sensitive-value" not in output
    handler_boundary.entered.assert_not_awaited()


@pytest.mark.asyncio
async def test_exception_details_scrub_registered_secrets(handler_boundary: SimpleNamespace) -> None:
    ctx = make_copilot_ctx()
    secret = "private-runtime-value"
    register_secret_scrub_value(ctx, secret)
    handler_boundary.execute.side_effect = RuntimeError(f"Execution rejected {secret}")
    arguments = json.dumps({"workflow_yaml": "title: Example", "block_labels": ["example"]})
    output = await invoke(arguments, ctx)
    assert parse_tool_output(output)["ok"] is False
    assert secret not in output
    projection = await project_result(ctx, arguments, output)
    assert secret not in json.dumps(projection)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "tool",
    [tool for tool in tools.NATIVE_TOOLS if tool is not tools.inspect_locator_matches_tool],
    ids=lambda tool: tool.name,
)
async def test_registered_sdk_tools_report_malformed_arguments(tool: FunctionTool) -> None:
    arguments = "{"
    output = await tool.on_invoke_tool(
        ToolContext(
            context=make_copilot_ctx(), tool_name=tool.name, tool_call_id="call_test", tool_arguments=arguments
        ),
        arguments,
    )
    assert parse_tool_output(output)["ok"] is False
