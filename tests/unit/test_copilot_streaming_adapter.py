from __future__ import annotations

import asyncio
import base64
import hashlib
import io
import json
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock
from urllib.parse import quote

import pytest
from agents import (
    Agent,
    RunConfig,
    Runner,
    function_tool,
)
from agents.items import RunItem
from agents.mcp.util import MCPUtil
from agents.stream_events import RawResponsesStreamEvent, RunItemStreamEvent
from agents.tool_context import ToolContext
from fastmcp import FastMCP
from openai.types.responses import (
    ResponseCreatedEvent,
    ResponseFunctionCallArgumentsDeltaEvent,
    ResponseFunctionCallArgumentsDoneEvent,
    ResponseOutputItemAddedEvent,
    ResponseOutputItemDoneEvent,
)
from openai.types.responses.response import Response
from openai.types.responses.response_function_tool_call import ResponseFunctionToolCall
from PIL import Image

from skyvern.forge import app
from skyvern.forge.sdk.api.files import parse_uri_to_path
from skyvern.forge.sdk.artifact.manager import ArtifactManager
from skyvern.forge.sdk.artifact.models import Artifact, ArtifactType
from skyvern.forge.sdk.artifact.storage.local import LocalStorage
from skyvern.forge.sdk.copilot import streaming_adapter as streaming_adapter_module
from skyvern.forge.sdk.copilot.agent import (
    _build_narrative_payload,
    _build_system_prompt,
)
from skyvern.forge.sdk.copilot.context import (
    USER_FACING_REASON_SCHEMA,
    CopilotContext,
    InFlightStreamToolCall,
)
from skyvern.forge.sdk.copilot.hooks import CopilotRunHooks
from skyvern.forge.sdk.copilot.mcp_adapter import SchemaOverlay, SkyvernOverlayMCPServer
from skyvern.forge.sdk.copilot.model_input_capture import serialize_tool_surface
from skyvern.forge.sdk.copilot.narration import (
    MAX_DESIGN_ACTIVITY_ENTRIES,
    NarratorState,
)
from skyvern.forge.sdk.copilot.screenshot_utils import ScreenshotProvenance, capturing_tool_call, enqueue_screenshot
from skyvern.forge.sdk.copilot.secret_scrub import register_secret_scrub_value
from skyvern.forge.sdk.copilot.streaming_adapter import (
    _sanitize_input,
    _update_enforcement_from_tool,
    flush_goal_satisfied_tool_result,
    stream_to_sse,
)
from skyvern.forge.sdk.copilot.tools import _with_action_reason, copilot_native_tools
from skyvern.forge.sdk.core import skyvern_context
from skyvern.forge.sdk.core.skyvern_context import SkyvernContext
from skyvern.forge.sdk.schemas.workflow_copilot import (
    WorkflowCopilotChatHistoryMessage,
    WorkflowCopilotChatSender,
    WorkflowCopilotStreamMessageType,
)
from tests.unit.copilot_test_helpers import FakeCopilotStream, ScriptedModel, scripted_call, scripted_text
from tests.unit.scoped_asyncio import ScopedAsyncio


@pytest.mark.parametrize(
    ("key", "definition"),
    [("workflow", {"title": "x"}), ("block", {"block_type": "code", "label": "a"}), ("workflow_yaml", "title: x")],
)
def test_strips_submitted_definition(key: str, definition: dict[str, str] | str) -> None:
    result = _sanitize_input({key: definition, "block_labels": ["a"]})
    assert result == {"block_labels": ["a"]}


def test_redacts_password_in_parameters() -> None:
    result = _sanitize_input(
        {
            "workflow_yaml": "...",
            "parameters": {"username": "u", "password": "p"},
        }
    )
    params = result["parameters"]
    assert params["username"] == "u"
    assert params["password"] == "****"


def test_redacts_totp_and_api_key() -> None:
    result = _sanitize_input(
        {
            "parameters": {
                "totp": "123456",
                "api_key": "sk-abc",
                "mfa_code": "999",
            }
        }
    )
    params = result["parameters"]
    assert params["totp"] == "****"
    assert params["api_key"] == "****"
    assert params["mfa_code"] == "****"


def test_does_not_redact_benign_identifiers() -> None:
    result = _sanitize_input(
        {
            "parameters": {
                "credential_id": "cred_abc",
                "page_token": "pt_xyz",
                "username": "user1",
                "search_term": "apple",
            }
        }
    )
    params = result["parameters"]
    assert params["credential_id"] == "cred_abc"
    assert params["page_token"] == "pt_xyz"
    assert params["username"] == "user1"
    assert params["search_term"] == "apple"


def test_redacts_nested_dict() -> None:
    result = _sanitize_input(
        {
            "parameters": {
                "outer": {"password": "p", "label": "ok"},
            }
        }
    )
    assert result["parameters"]["outer"]["password"] == "****"
    assert result["parameters"]["outer"]["label"] == "ok"


def test_empty_input() -> None:
    assert _sanitize_input({}) == {}


async def _stream_events_from(*events: Any) -> Any:
    for event in events:
        yield event


def _minimal_response() -> Response:
    return Response(
        id="resp_1",
        created_at=0.0,
        model="gpt-4o",
        object="response",
        output=[],
        parallel_tool_calls=True,
        tool_choice="auto",
        tools=[],
    )


def _response_created_event() -> RawResponsesStreamEvent:
    return RawResponsesStreamEvent(
        data=ResponseCreatedEvent(response=_minimal_response(), sequence_number=1, type="response.created")
    )


def _item_added_event(output_index: int, name: str, call_id: str = "__fake_responses_id__") -> RawResponsesStreamEvent:
    item = ResponseFunctionToolCall(arguments="", call_id=call_id, name=name, type="function_call")
    return RawResponsesStreamEvent(
        data=ResponseOutputItemAddedEvent(
            item=item, output_index=output_index, sequence_number=1, type="response.output_item.added"
        )
    )


def _args_delta_event(output_index: int, delta: str, item_id: str = "__fake_responses_id__") -> RawResponsesStreamEvent:
    return RawResponsesStreamEvent(
        data=ResponseFunctionCallArgumentsDeltaEvent(
            delta=delta,
            item_id=item_id,
            output_index=output_index,
            sequence_number=1,
            type="response.function_call_arguments.delta",
        )
    )


def _args_done_event(
    output_index: int, arguments: str, name: str, item_id: str = "__fake_responses_id__"
) -> RawResponsesStreamEvent:
    return RawResponsesStreamEvent(
        data=ResponseFunctionCallArgumentsDoneEvent(
            arguments=arguments,
            item_id=item_id,
            name=name,
            output_index=output_index,
            sequence_number=1,
            type="response.function_call_arguments.done",
        )
    )


def _item_done_event(output_index: int, name: str, arguments: str, item_id: str = "__fake_responses_id__") -> Any:
    item = ResponseFunctionToolCall(arguments=arguments, call_id=item_id, name=name, type="function_call")
    return RawResponsesStreamEvent(
        data=ResponseOutputItemDoneEvent(
            item=item, output_index=output_index, sequence_number=1, type="response.output_item.done"
        )
    )


def _tool_called_event(
    call_id: str, name: str, arguments: str = "{}", agent: Agent[Any] | None = None
) -> RunItemStreamEvent:
    call_item = MagicMock(spec=RunItem)
    call_item.raw_item = {"call_id": call_id, "name": name, "arguments": arguments}
    call_item.agent = agent or Agent(name="no-tools")
    return RunItemStreamEvent(name="tool_called", item=call_item)


def _tool_output_event(call_id: str, ok: bool = True, block_count: int = 2) -> RunItemStreamEvent:
    out_item = MagicMock(spec=RunItem)
    out_item.raw_item = {"call_id": call_id}
    out_item.output = [{"type": "text", "text": json.dumps({"ok": ok, "data": {"block_count": block_count}})}]
    return RunItemStreamEvent(name="tool_output", item=out_item)


def _new_ctx() -> CopilotContext:
    return CopilotContext(
        organization_id="org_test",
        workflow_permanent_id="wpid_test",
        workflow_id=None,
        workflow_yaml=None,
        browser_session_id=None,
        stream=None,
    )


def _test_copilot_context(**overrides: Any) -> CopilotContext:
    ctx = _new_ctx()
    for name, value in overrides.items():
        setattr(ctx, name, value)
    return ctx


def _fixture_pending_identity(ctx: Any) -> None:
    pending = ctx.in_flight_stream_tool_call
    ctx.stream_tool_calls = {pending.call_id: pending} if pending else {}
    ctx.pending_stream_tool_call_ids = {pending.call_id} if pending else set()
    ctx.goal_satisfied_tool_call_id = pending.call_id if pending else None


@pytest.mark.asyncio
async def test_tool_activity_entries_share_the_clock_read_of_their_sse_update() -> None:
    result = MagicMock()
    result.stream_events = lambda: _stream_events_from(
        _tool_called_event("c1", "update_workflow"),
        _tool_output_event("c1"),
    )
    result.cancel = MagicMock()

    sent: list[Any] = []

    async def _send(payload: Any) -> bool:
        sent.append(payload)
        return True

    stream = MagicMock()
    stream.is_disconnected = AsyncMock(return_value=False)
    stream.send = _send
    ctx = _new_ctx()

    await stream_to_sse(result, stream, ctx)

    live_by_id = {
        f"tc-{p.tool_call_id}" if p.type == WorkflowCopilotStreamMessageType.TOOL_CALL else f"tr-{p.tool_call_id}": p
        for p in sent
        if p.type in (WorkflowCopilotStreamMessageType.TOOL_CALL, WorkflowCopilotStreamMessageType.TOOL_RESULT)
    }
    entries = ctx.narrator_state.design_activity
    assert {e["id"] for e in entries} == {"tc-c1", "tr-c1"}
    for entry in entries:
        assert entry["timestamp"] == live_by_id[entry["id"]].timestamp.isoformat()


@pytest.mark.asyncio
async def test_goal_satisfied_flush_entry_shares_the_clock_read_of_its_sse_update() -> None:
    sent: list[Any] = []

    async def _send(payload: Any) -> bool:
        sent.append(payload)
        return True

    stream = MagicMock()
    stream.is_disconnected = AsyncMock(return_value=False)
    stream.send = _send

    narrator_state = NarratorState()
    ctx = _test_copilot_context(
        narrator_state=narrator_state,
        in_flight_stream_tool_call=InFlightStreamToolCall(
            call_id="c9", tool_name="update_workflow", iteration=2, display_label=None
        ),
        goal_satisfied_tool_name="update_workflow",
        goal_satisfied_tool_output={"ok": True, "data": {"block_count": 1}},
        last_artifact_health_blocker_reason=None,
        completion_verification_result=None,
        pending_code_write_diffs={},
    )

    _fixture_pending_identity(ctx)
    await flush_goal_satisfied_tool_result(stream, ctx)  # type: ignore[arg-type]

    entry = narrator_state.design_activity[0]
    live = next(p for p in sent if p.type == WorkflowCopilotStreamMessageType.TOOL_RESULT)
    assert entry["id"] == "tr-c9"
    assert entry["timestamp"] == live.timestamp.isoformat()


@pytest.mark.asyncio
async def test_stream_to_sse_keeps_running_after_client_disconnect() -> None:
    """SKY-8986 regression: a dropped SSE client must NOT cancel the agent run.

    The handler task outlives the SSE response so the agent's reply can be
    persisted to the chat history. stream_to_sse keeps draining the SDK's
    event stream; emissions turn into no-ops when is_disconnected() returns
    True, but result.cancel() is never called and no exception escapes.
    """
    from agents.items import RunItem
    from agents.stream_events import RunItemStreamEvent

    raw_call = {"call_id": "c1", "name": "click", "arguments": "{}"}
    call_item = MagicMock(spec=RunItem)
    call_item.raw_item = raw_call
    tool_call = RunItemStreamEvent(name="tool_called", item=call_item)

    raw_output = {"call_id": "c1"}
    output_item = MagicMock(spec=RunItem)
    output_item.raw_item = raw_output
    output_item.output = None
    tool_output = RunItemStreamEvent(name="tool_output", item=output_item)

    result = MagicMock()
    result.stream_events = lambda: _stream_events_from(tool_call, tool_output)
    result.cancel = MagicMock()

    stream = MagicMock()
    stream.is_disconnected = AsyncMock(return_value=True)
    stream.send = AsyncMock(return_value=True)

    ctx = _test_copilot_context(
        last_artifact_health_blocker_reason=None,
        pending_code_write_diffs={},
        completion_verification_result=None,
    )

    await stream_to_sse(result, stream, ctx)

    result.cancel.assert_not_called()
    stream.send.assert_not_called()


@pytest.mark.asyncio
async def test_tool_call_sse_uses_product_safe_activity_label() -> None:
    from agents.items import RunItem
    from agents.stream_events import RunItemStreamEvent

    from skyvern.forge.sdk.schemas.workflow_copilot import WorkflowCopilotStreamMessageType

    call_item = MagicMock(spec=RunItem)
    call_item.raw_item = {"call_id": "c1", "name": "update_and_run_blocks", "arguments": "{}"}
    tool_call_event = RunItemStreamEvent(name="tool_called", item=call_item)

    result = MagicMock()
    result.stream_events = lambda: _stream_events_from(tool_call_event)
    result.cancel = MagicMock()

    sent: list[Any] = []

    async def _send(payload: Any) -> bool:
        sent.append(payload)
        return True

    stream = MagicMock()
    stream.is_disconnected = AsyncMock(return_value=False)
    stream.send = _send
    ctx = _test_copilot_context(
        last_artifact_health_blocker_reason=None,
        pending_code_write_diffs={},
        completion_verification_result=None,
    )

    await stream_to_sse(result, stream, ctx)

    tool_calls = [p for p in sent if getattr(p, "type", None) == WorkflowCopilotStreamMessageType.TOOL_CALL]
    assert len(tool_calls) == 1
    assert tool_calls[0].tool_name == "update_and_run_blocks"
    assert tool_calls[0].display_label == "Testing workflow"

    activity = ctx.narrator_state.design_activity
    assert len(activity) == 1
    assert activity[0]["kind"] == "tool_call"
    assert activity[0]["toolName"] == "update_and_run_blocks"
    assert activity[0]["displayLabel"] == "Testing workflow"
    assert activity[0]["text"] == "Testing workflow…"
    assert "update_and_run_blocks" not in activity[0]["text"]


@pytest.mark.asyncio
async def test_stream_to_sse_propagates_cancelled_error() -> None:
    """A generic asyncio.CancelledError must propagate up from stream_to_sse so
    the event loop's cancellation machinery still works for task-group cancel,
    upstream timeout, or parent abort. The adapter must not catch it and turn
    it into a normal return.
    """

    async def _raises_cancelled() -> Any:
        raise asyncio.CancelledError()
        yield  # make it an async generator

    result = MagicMock()
    result.stream_events = _raises_cancelled
    result.cancel = MagicMock()

    stream = MagicMock()
    stream.is_disconnected = AsyncMock(return_value=False)
    stream.send = AsyncMock(return_value=True)

    ctx = _test_copilot_context(
        last_artifact_health_blocker_reason=None,
        pending_code_write_diffs={},
        completion_verification_result=None,
    )

    with pytest.raises(asyncio.CancelledError):
        await stream_to_sse(result, stream, ctx)

    result.cancel.assert_called_once()


@pytest.mark.asyncio
async def test_tool_result_sse_uses_latest_blocker_signal_for_activity_surface() -> None:
    from agents.items import RunItem
    from agents.stream_events import RunItemStreamEvent

    from skyvern.forge.sdk.copilot.blocker_signal import CopilotToolBlockerSignal
    from skyvern.forge.sdk.schemas.workflow_copilot import WorkflowCopilotStreamMessageType

    signal = CopilotToolBlockerSignal(
        blocker_kind="tool_error",
        agent_steering_text=(
            "Less than 90 seconds remain in this Copilot turn. "
            "Do NOT start another block-running tool call; reply from gathered progress."
        ),
        user_facing_reason="I'm running out of time on this turn. I'll wrap up with what I have so far.",
        recovery_hint="stop",
        renders_final_reply=False,
        internal_reason_code="tool_error_late_block_running",
        blocked_tool="update_and_run_blocks",
    )

    call_item = MagicMock(spec=RunItem)
    call_item.raw_item = {"call_id": "c1", "name": "update_and_run_blocks", "arguments": "{}"}
    tool_call_event = RunItemStreamEvent(name="tool_called", item=call_item)

    out_item = MagicMock(spec=RunItem)
    out_item.raw_item = {"call_id": "c1", "name": "update_and_run_blocks"}
    out_item.output = [{"type": "text", "text": json.dumps({"ok": False, "error": signal.agent_steering_text})}]
    tool_output_event = RunItemStreamEvent(name="tool_output", item=out_item)

    result = MagicMock()
    result.stream_events = lambda: _stream_events_from(tool_call_event, tool_output_event)
    result.cancel = MagicMock()

    sent: list[Any] = []

    async def _send(payload: Any) -> bool:
        sent.append(payload)
        return True

    stream = MagicMock()
    stream.is_disconnected = AsyncMock(return_value=False)
    stream.send = _send
    ctx = _test_copilot_context(
        latest_tool_blocker_signal=signal,
        tool_blocker_signals=[signal],
        last_artifact_health_blocker_reason=None,
        pending_code_write_diffs={},
        completion_verification_result=None,
    )

    await stream_to_sse(result, stream, ctx)

    tool_results = [p for p in sent if getattr(p, "type", None) == WorkflowCopilotStreamMessageType.TOOL_RESULT]
    assert len(tool_results) == 1
    assert tool_results[0].summary == signal.user_facing_reason
    assert tool_results[0].detail == signal.user_facing_reason
    assert "Do NOT" not in tool_results[0].summary
    # tool_error blocker signals are genuine failures (something ran out of budget/broke,
    # not a precondition redirect) and must keep their failure affect.
    assert tool_results[0].success is False

    activity = ctx.narrator_state.design_activity
    assert len(activity) == 2
    assert activity[-1]["kind"] == "tool_result"
    assert activity[-1]["text"] == signal.user_facing_reason
    assert "Do NOT" not in activity[-1]["text"]
    assert activity[-1]["success"] is False


@pytest.mark.asyncio
@pytest.mark.parametrize("tool_name", ["get_browser_screenshot", "run_browser_code"])
@pytest.mark.parametrize(
    "error_json",
    [
        '{"ok": false, "error": "Browser session pbs_123 not found while taking screenshot (404)."}',
        # run_browser_code names a lost browser by its typed code, in prose with no status or "not found".
        '{"ok": false, "error": "The chat\'s browser session is no longer available.", '
        '"error_code": "browser_session_unavailable"}',
    ],
    ids=["prose", "typed"],
)
async def test_stream_to_sse_raises_and_cancels_on_repeated_unrecoverable_tool_error(
    tool_name: str, error_json: str
) -> None:
    from agents.items import RunItem
    from agents.stream_events import RunItemStreamEvent

    from skyvern.forge.sdk.copilot.enforcement import CopilotUnrecoverableToolError

    events = []
    for call_id in ("c1", "c2"):
        call_item = MagicMock(spec=RunItem)
        call_item.raw_item = {"call_id": call_id, "name": tool_name, "arguments": "{}"}
        events.append(RunItemStreamEvent(name="tool_called", item=call_item))

        output_item = MagicMock(spec=RunItem)
        output_item.raw_item = {"call_id": call_id, "name": tool_name}
        output_item.output = [{"type": "text", "text": error_json}]
        events.append(RunItemStreamEvent(name="tool_output", item=output_item))

    result = MagicMock()
    result.stream_events = lambda: _stream_events_from(*events)
    result.cancel = MagicMock()

    stream = MagicMock()
    stream.is_disconnected = AsyncMock(return_value=False)
    stream.send = AsyncMock(return_value=True)
    ctx = _test_copilot_context(
        last_artifact_health_blocker_reason=None,
        pending_code_write_diffs={},
        completion_verification_result=None,
        composition_page_evidence=None,
        last_test_anti_bot=None,
        user_message="",
    )

    with pytest.raises(CopilotUnrecoverableToolError):
        await stream_to_sse(result, stream, ctx)

    result.cancel.assert_called_once()
    assert ctx.latest_diagnosis_repair_contract.repair_decision.next_action == "stop"
    assert "pbs_" not in ctx.latest_diagnosis_repair_contract.verification_result.remaining_blocker


@pytest.mark.asyncio
async def test_tool_result_sse_summary_drops_click_selector_on_success() -> None:
    """A successful click emits an empty SSE summary; the call_id → tool_name
    lookup must resolve to 'click' for the success-redaction branch to fire."""
    from agents.items import RunItem
    from agents.stream_events import RunItemStreamEvent

    from skyvern.forge.sdk.schemas.workflow_copilot import WorkflowCopilotStreamMessageType

    raw_call = {"call_id": "c-click", "name": "click", "arguments": '{"selector": "#btn-submit"}'}
    call_item = MagicMock(spec=RunItem)
    call_item.raw_item = raw_call
    tool_call_event = RunItemStreamEvent(name="tool_called", item=call_item)

    output_text = '{"ok": true, "data": {"selector": "#btn-submit"}}'
    out_item = MagicMock(spec=RunItem)
    out_item.raw_item = {"call_id": "c-click", "name": "click"}
    out_item.output = [{"type": "text", "text": output_text}]
    tool_output_event = RunItemStreamEvent(name="tool_output", item=out_item)

    result = MagicMock()
    result.stream_events = lambda: _stream_events_from(tool_call_event, tool_output_event)
    result.cancel = MagicMock()

    sent: list[Any] = []

    async def _send(payload: Any) -> bool:
        sent.append(payload)
        return True

    stream = MagicMock()
    stream.is_disconnected = AsyncMock(return_value=False)
    stream.send = _send

    ctx = _test_copilot_context(
        last_artifact_health_blocker_reason=None,
        pending_code_write_diffs={},
        completion_verification_result=None,
    )

    await stream_to_sse(result, stream, ctx)

    tool_results = [p for p in sent if getattr(p, "type", None) == WorkflowCopilotStreamMessageType.TOOL_RESULT]
    assert len(tool_results) == 1
    assert tool_results[0].tool_name == "click"
    assert tool_results[0].success is True
    assert tool_results[0].summary == ""


class TestParseToolOutput:
    @staticmethod
    def _parse(output: Any) -> dict[str, Any]:
        from skyvern.forge.sdk.copilot.streaming_adapter import parse_tool_output

        return parse_tool_output(output)

    def test_parse_none(self) -> None:
        assert self._parse(None) == {"ok": True}

    def test_parse_plain_json_string(self) -> None:
        assert self._parse('{"ok": true}') == {"ok": True}

    def test_parse_json_string_with_data(self) -> None:
        result = self._parse('{"ok": true, "data": {"count": 5}}')
        assert result["ok"] is True
        assert result["data"]["count"] == 5

    def test_parse_error_json_string(self) -> None:
        result = self._parse('{"ok": false, "error": "something broke"}')
        assert result["ok"] is False
        assert result["error"] == "something broke"

    def test_parse_non_json_string(self) -> None:
        result = self._parse("just plain text")
        assert result["ok"] is True
        assert result["data"] == "just plain text"

    def test_parse_list_with_text_dict(self) -> None:
        output = [{"type": "text", "text": '{"ok": false, "error": "fail"}'}]
        result = self._parse(output)
        assert result == {"ok": False, "error": "fail"}

    def test_parse_list_with_text_object(self) -> None:
        """SDK may return ToolOutputText objects, not dicts."""

        class FakeTextOutput:
            type = "text"
            text = '{"ok": true, "data": "hello"}'

        result = self._parse([FakeTextOutput()])
        assert result == {"ok": True, "data": "hello"}

    def test_parse_list_skips_image_items(self) -> None:
        output = [
            {"type": "text", "text": '{"ok": true}'},
            {"type": "image", "image_url": "data:image/png;base64,abc"},
        ]
        result = self._parse(output)
        assert result == {"ok": True}

    def test_parse_wrapped_text_dict(self) -> None:
        output = {"type": "text", "text": '{"ok": true}'}
        result = self._parse(output)
        assert result == {"ok": True}

    def test_parse_direct_copilot_dict(self) -> None:
        output = {"ok": True, "data": {"workflow_id": "wf_1"}}
        result = self._parse(output)
        assert result == output

    def test_parse_dict_without_ok_or_type(self) -> None:
        output = {"some_key": "some_value"}
        result = self._parse(output)
        assert result["ok"] is True
        assert result["data"] == output

    def test_parse_object_with_text_attr(self) -> None:
        class FakeOutput:
            type = "text"
            text = '{"ok": true, "data": 42}'

        result = self._parse(FakeOutput())
        assert result == {"ok": True, "data": 42}

    def test_parse_empty_list(self) -> None:
        result = self._parse([])
        assert result["ok"] is True

    def test_run_blocks_summary_handles_non_dict_data(self) -> None:
        from skyvern.forge.sdk.copilot.output_utils import summarize_tool_result

        summary = summarize_tool_result(
            "run_blocks_and_collect_debug",
            {"ok": True, "data": [{"type": "text", "text": '{"ok": true}'}]},
        )
        assert summary == "Run debug completed"


class TestEnforcementStateUpdates:
    def _make_ctx(self) -> Any:
        ctx = MagicMock()
        ctx.update_workflow_called = False
        ctx.test_after_update_done = False
        ctx.post_update_nudge_count = 0
        ctx.navigate_called = False
        ctx.observation_after_navigate = False
        ctx.synthesized_block_reopened_after_failed_run = False
        return ctx

    def test_update_workflow_sets_flags(self) -> None:
        from skyvern.forge.sdk.copilot.streaming_adapter import _update_enforcement_from_tool

        ctx = self._make_ctx()
        _update_enforcement_from_tool(
            ctx,
            "update_workflow",
            {
                "ok": True,
                "data": {"block_count": 2},
            },
        )
        assert ctx.update_workflow_called is True
        assert ctx.test_after_update_done is False
        assert ctx.post_update_nudge_count == 0

    def test_run_blocks_sets_test_done(self) -> None:
        from skyvern.forge.sdk.copilot.streaming_adapter import _update_enforcement_from_tool

        ctx = self._make_ctx()
        _update_enforcement_from_tool(ctx, "run_blocks_and_collect_debug", {"ok": True})
        assert ctx.test_after_update_done is True

    def test_update_and_run_blocks_sets_both_flags(self) -> None:
        """update_and_run_blocks is a composite tool — it must set update AND test flags."""
        from skyvern.forge.sdk.copilot.streaming_adapter import _update_enforcement_from_tool

        ctx = self._make_ctx()
        _update_enforcement_from_tool(
            ctx,
            "update_and_run_blocks",
            {"ok": True, "data": {"block_count": 2}},
        )
        assert ctx.update_workflow_called is True
        assert ctx.test_after_update_done is True

    def test_navigate_sets_flags(self) -> None:
        from skyvern.forge.sdk.copilot.streaming_adapter import _update_enforcement_from_tool

        ctx = self._make_ctx()
        _update_enforcement_from_tool(ctx, "navigate_browser", {"ok": True})
        assert ctx.navigate_called is True
        assert ctx.observation_after_navigate is False

    def test_observation_tool_sets_flag(self) -> None:
        from skyvern.forge.sdk.copilot.streaming_adapter import _update_enforcement_from_tool

        ctx = self._make_ctx()
        ctx.observation_after_navigate = False
        _update_enforcement_from_tool(ctx, "get_browser_screenshot", {"ok": True})
        assert ctx.observation_after_navigate is True

    def test_update_without_blocks_does_not_set_flag(self) -> None:
        from skyvern.forge.sdk.copilot.streaming_adapter import _update_enforcement_from_tool

        ctx = self._make_ctx()
        _update_enforcement_from_tool(
            ctx,
            "update_workflow",
            {
                "ok": True,
                "data": {"block_count": 0},
            },
        )
        assert ctx.update_workflow_called is False


def test_tool_result_workflow_run_id_only_for_block_running_tools() -> None:
    from skyvern.forge.sdk.copilot.streaming_adapter import _tool_result_workflow_run_id

    payload = {"ok": True, "data": {"workflow_run_id": "wr_42"}}
    # Block-running tools created the run -> surface it (pass or fail).
    assert _tool_result_workflow_run_id("update_and_run_blocks", payload) == "wr_42"
    assert _tool_result_workflow_run_id("run_blocks_and_collect_debug", payload) == "wr_42"
    # get_run_results echoes a prior run id; attributing it to this turn would grade a stale run.
    assert _tool_result_workflow_run_id("get_run_results", payload) is None
    assert _tool_result_workflow_run_id("update_workflow", payload) is None
    # Malformed / missing data payloads yield None rather than raising.
    assert _tool_result_workflow_run_id("update_and_run_blocks", {}) is None
    assert _tool_result_workflow_run_id("update_and_run_blocks", {"data": "string"}) is None
    assert _tool_result_workflow_run_id("update_and_run_blocks", {"data": {"workflow_run_id": 42}}) is None


def test_tool_result_executed_source_reference_only_for_browser_code() -> None:
    from skyvern.forge.sdk.copilot.streaming_adapter import _tool_result_executed_source_reference

    payload = {"ok": True, "executed_source_reference": "browser-code-source:opaque"}
    assert _tool_result_executed_source_reference("run_browser_code", payload) == "browser-code-source:opaque"
    assert _tool_result_executed_source_reference("edit_block_and_run", payload) is None
    assert _tool_result_executed_source_reference("run_browser_code", {"executed_source_reference": 42}) is None


@pytest.mark.asyncio
async def test_a_stored_work_plan_outlives_its_row_being_capped_away() -> None:
    plan = ["Open the admin page", "Create the user"]

    def tool_output(call_id: str, payload: dict[str, Any]) -> RunItemStreamEvent:
        out_item = MagicMock(spec=RunItem)
        out_item.raw_item = {"call_id": call_id}
        out_item.output = [{"type": "text", "text": json.dumps(payload)}]
        return RunItemStreamEvent(name="tool_output", item=out_item)

    # Enough later calls that the design activity cap trims the plan's own rows.
    filler = [
        event
        for index in range(MAX_DESIGN_ACTIVITY_ENTRIES)
        for event in (
            _tool_called_event(f"f{index}", "evaluate"),
            tool_output(f"f{index}", {"ok": True}),
        )
    ]
    result = MagicMock()
    result.stream_events = lambda: _stream_events_from(
        _tool_called_event("p1", "set_work_plan"),
        tool_output("p1", {"ok": True, "items": plan}),
        # A refused write echoes the plan still in force; it is not a new plan.
        _tool_called_event("p2", "set_work_plan"),
        tool_output("p2", {"ok": False, "error": "too long", "items": ["something else"]}),
        *filler,
    )
    result.cancel = MagicMock()

    sent: list[Any] = []

    async def _send(payload: Any) -> bool:
        sent.append(payload)
        return True

    stream = MagicMock()
    stream.is_disconnected = AsyncMock(return_value=False)
    stream.send = _send
    ctx = _new_ctx()

    await stream_to_sse(result, stream, ctx)

    plan_results = [
        p
        for p in sent
        if getattr(p, "type", None) == WorkflowCopilotStreamMessageType.TOOL_RESULT and p.tool_name == "set_work_plan"
    ]
    assert [p.work_plan for p in plan_results] == [plan, None]
    assert "tr-p1" not in {row["id"] for row in ctx.narrator_state.design_activity}
    assert ctx.narrator_state.work_plan == {"toolCallId": "p1", "items": plan}


@pytest.mark.asyncio
async def test_browser_code_steps_ride_the_result_event_and_its_saved_row() -> None:
    def tool_output(call_id: str, payload: dict[str, Any]) -> RunItemStreamEvent:
        out_item = MagicMock(spec=RunItem)
        out_item.raw_item = {"call_id": call_id}
        out_item.output = [{"type": "text", "text": json.dumps(payload)}]
        return RunItemStreamEvent(name="tool_output", item=out_item)

    operations = [{"operation": "goto", "status": "ok"}, {"operation": "click", "status": "ok", "selector": "#next"}]
    result = MagicMock()
    result.stream_events = lambda: _stream_events_from(
        _tool_called_event("b1", "run_browser_code"),
        tool_output("b1", {"ok": True, "operations": operations}),
        _tool_called_event("b2", "run_browser_code"),
        tool_output("b2", {"ok": False, "error": "boom", "operations": operations}),
    )
    result.cancel = MagicMock()

    sent: list[Any] = []

    async def _send(payload: Any) -> bool:
        sent.append(payload)
        return True

    stream = MagicMock()
    stream.is_disconnected = AsyncMock(return_value=False)
    stream.send = _send
    ctx = _new_ctx()

    await stream_to_sse(result, stream, ctx)

    steps = ["Opened a page", "clicked '#next'"]
    tool_results = [p for p in sent if getattr(p, "type", None) == WorkflowCopilotStreamMessageType.TOOL_RESULT]
    assert [p.browser_steps for p in tool_results] == [steps, None]
    rows = {row["id"]: row for row in ctx.narrator_state.design_activity}
    assert rows["tr-b1"]["browserSteps"] == steps
    assert "browserSteps" not in rows["tr-b2"]


@pytest.mark.asyncio
async def test_a_stashed_write_diff_rides_one_result_and_no_later_foreign_one() -> None:
    diffs = [{"label": "download_step", "added": 3, "removed": 1, "patch": "@@\n-old\n+new"}]

    result = MagicMock()
    result.stream_events = lambda: _stream_events_from(
        _tool_called_event("c1", "edit_block", '{"label": "download_step"}'),
        _tool_output_event("c1"),
        _tool_called_event("c2", "inspect_page_for_composition"),
        _tool_output_event("c2"),
    )
    result.cancel = MagicMock()

    sent: list[Any] = []

    async def _send(payload: Any) -> bool:
        sent.append(payload)
        return True

    stream = MagicMock()
    stream.is_disconnected = AsyncMock(return_value=False)
    stream.send = _send
    ctx = _test_copilot_context(
        last_artifact_health_blocker_reason=None,
        pending_code_write_diffs={"c1": diffs},
        completion_verification_result=None,
    )

    await stream_to_sse(result, stream, ctx)

    tool_results = [p for p in sent if getattr(p, "type", None) == WorkflowCopilotStreamMessageType.TOOL_RESULT]
    assert [p.code_diffs for p in tool_results] == [diffs, None]
    assert ctx.pending_code_write_diffs == {}

    write_row = [row for row in ctx.narrator_state.design_activity if row["id"] == "tr-c1"]
    assert write_row[0]["codeDiffs"] == diffs
    assert all("codeDiffs" not in row for row in ctx.narrator_state.design_activity if row["id"] != "tr-c1")


@pytest.mark.asyncio
async def test_two_writes_resolving_out_of_order_each_keep_their_own_diff() -> None:
    """Parallel tool calls are the provider default and results are not ordered by call. Draining
    by arrival handed the first result both writes' diffs, so a patch rendered — and persisted —
    against a write that did not produce it."""
    first = [{"label": "download_step", "added": 3, "removed": 1, "patch": "@@\n+first"}]
    second = [{"label": "parse_step", "added": 9, "removed": 0, "patch": "@@\n+second"}]

    result = MagicMock()
    result.stream_events = lambda: _stream_events_from(
        _tool_called_event("c1", "edit_block", '{"label": "download_step"}'),
        _tool_called_event("c2", "add_block", '{"label": "parse_step"}'),
        # c2 answers first: the stash must not hand c2 the diff c1 produced.
        _tool_output_event("c2"),
        _tool_output_event("c1"),
    )
    result.cancel = MagicMock()

    sent: list[Any] = []

    async def _send(payload: Any) -> bool:
        sent.append(payload)
        return True

    stream = MagicMock()
    stream.is_disconnected = AsyncMock(return_value=False)
    stream.send = _send
    ctx = _test_copilot_context(
        last_artifact_health_blocker_reason=None,
        pending_code_write_diffs={"c1": first, "c2": second},
        completion_verification_result=None,
    )

    await stream_to_sse(result, stream, ctx)

    rows = {row["id"]: row for row in ctx.narrator_state.design_activity if row["id"].startswith("tr-")}
    assert rows["tr-c1"]["codeDiffs"] == first
    assert rows["tr-c2"]["codeDiffs"] == second
    assert ctx.pending_code_write_diffs == {}


@pytest.mark.asyncio
async def test_a_write_that_failed_does_not_display_a_sibling_write_diff() -> None:
    """The failed row is the one a user inspects to see what went wrong; showing it a concurrent
    write's patch is worse than showing none."""
    healthy = [{"label": "download_step", "added": 4, "removed": 0, "patch": "@@\n+ok"}]

    result = MagicMock()
    result.stream_events = lambda: _stream_events_from(
        _tool_called_event("c1", "edit_block", '{"label": "download_step"}'),
        _tool_called_event("c2", "add_block", '{"label": "broken_step"}'),
        _tool_output_event("c2"),
        _tool_output_event("c1"),
    )
    result.cancel = MagicMock()

    sent: list[Any] = []

    async def _send(payload: Any) -> bool:
        sent.append(payload)
        return True

    stream = MagicMock()
    stream.is_disconnected = AsyncMock(return_value=False)
    stream.send = _send
    # c2 stashed nothing: its write raised before a diff could be built.
    ctx = _test_copilot_context(
        last_artifact_health_blocker_reason=None,
        pending_code_write_diffs={"c1": healthy},
        completion_verification_result=None,
    )

    await stream_to_sse(result, stream, ctx)

    rows = {row["id"]: row for row in ctx.narrator_state.design_activity if row["id"].startswith("tr-")}
    assert "codeDiffs" not in rows["tr-c2"]
    assert rows["tr-c1"]["codeDiffs"] == healthy


@pytest.mark.asyncio
async def test_a_foreign_result_arriving_first_does_not_cost_the_write_its_counts() -> None:
    """Parallel tool calls are the provider default, so the write's own result is not
    guaranteed to drain first. Consuming the stash on the foreign result would leave the
    write row with no counts at all, which the narrative-stream contract forbids."""
    diffs = [{"label": "download_step", "added": 3, "removed": 1, "patch": "@@\n-old\n+new"}]

    result = MagicMock()
    result.stream_events = lambda: _stream_events_from(
        _tool_called_event("c2", "inspect_page_for_composition"),
        _tool_output_event("c2"),
        _tool_called_event("c1", "edit_block", '{"label": "download_step"}'),
        _tool_output_event("c1"),
    )
    result.cancel = MagicMock()

    sent: list[Any] = []

    async def _send(payload: Any) -> bool:
        sent.append(payload)
        return True

    stream = MagicMock()
    stream.is_disconnected = AsyncMock(return_value=False)
    stream.send = _send
    ctx = _test_copilot_context(
        last_artifact_health_blocker_reason=None,
        pending_code_write_diffs={"c1": diffs},
        completion_verification_result=None,
    )

    await stream_to_sse(result, stream, ctx)

    tool_results = [p for p in sent if getattr(p, "type", None) == WorkflowCopilotStreamMessageType.TOOL_RESULT]
    assert [p.code_diffs for p in tool_results] == [None, diffs]
    assert ctx.pending_code_write_diffs == {}

    write_row = [row for row in ctx.narrator_state.design_activity if row["id"] == "tr-c1"]
    assert write_row[0]["codeDiffs"] == diffs
    foreign_row = [row for row in ctx.narrator_state.design_activity if row["id"] == "tr-c2"]
    assert "codeDiffs" not in foreign_row[0]


class TestFlushGoalSatisfiedToolResult:
    @staticmethod
    def _ctx(**overrides: Any) -> SimpleNamespace:
        from skyvern.forge.sdk.copilot.context import InFlightStreamToolCall

        defaults: dict[str, Any] = dict(
            in_flight_stream_tool_call=InFlightStreamToolCall(
                call_id="c9", tool_name="update_and_run_blocks", iteration=3
            ),
            goal_satisfied_tool_name="update_and_run_blocks",
            goal_satisfied_tool_output={"ok": True, "data": {"workflow_run_id": "wr_1"}},
            pending_code_write_diffs={},
            pending_chat_screenshots=[],
            narrator_state=None,
        )
        defaults.update(overrides)
        return SimpleNamespace(**defaults)

    @staticmethod
    def _stream(sent: list[Any], *, disconnected: bool = False) -> MagicMock:
        async def _send(payload: Any) -> bool:
            sent.append(payload)
            return True

        stream = MagicMock()
        stream.is_disconnected = AsyncMock(return_value=disconnected)
        stream.send = _send
        return stream

    @pytest.mark.asyncio
    async def test_emits_tool_result_for_goal_satisfying_call(self) -> None:
        from skyvern.forge.sdk.copilot.streaming_adapter import flush_goal_satisfied_tool_result
        from skyvern.forge.sdk.schemas.workflow_copilot import WorkflowCopilotStreamMessageType

        sent: list[Any] = []
        ctx = self._ctx()

        _fixture_pending_identity(ctx)
        await flush_goal_satisfied_tool_result(self._stream(sent), ctx)

        assert len(sent) == 1
        frame = sent[0]
        assert frame.type == WorkflowCopilotStreamMessageType.TOOL_RESULT
        assert frame.tool_call_id == "c9"
        assert frame.tool_name == "update_and_run_blocks"
        assert frame.success is True
        assert frame.iteration == 3
        assert ctx.in_flight_stream_tool_call is None
        assert ctx.goal_satisfied_tool_output is None
        assert ctx.goal_satisfied_tool_name is None

    @pytest.mark.asyncio
    async def test_flush_seam_drains_the_stashed_write_diff(self) -> None:
        diffs = [{"label": "download_step", "added": 2, "removed": 0}]
        sent: list[Any] = []
        ctx = self._ctx(pending_code_write_diffs={"c9": diffs})

        _fixture_pending_identity(ctx)
        await flush_goal_satisfied_tool_result(self._stream(sent), ctx)

        assert sent[0].code_diffs == diffs
        assert ctx.pending_code_write_diffs == {}

    @pytest.mark.asyncio
    async def test_flush_seam_withholds_a_write_diff_from_a_non_write_tool(self) -> None:
        sent: list[Any] = []
        diffs = [{"label": "download_step", "added": 2, "removed": 0}]
        ctx = self._ctx(
            in_flight_stream_tool_call=InFlightStreamToolCall(
                call_id="c9", tool_name="run_blocks_and_collect_debug", iteration=3
            ),
            goal_satisfied_tool_name="run_blocks_and_collect_debug",
            pending_code_write_diffs={"c1": diffs},
        )

        _fixture_pending_identity(ctx)
        await flush_goal_satisfied_tool_result(self._stream(sent), ctx)

        assert sent[0].code_diffs is None
        # This seam is a terminal exit, so withheld and discarded look the same to the user;
        # the assertion pins the drain rule itself, which the mid-turn seam does depend on.
        # The write's own entry is still keyed under c1 — a foreign call cannot consume it.
        assert ctx.pending_code_write_diffs == {"c1": diffs}

    @pytest.mark.asyncio
    async def test_noops_when_no_call_is_pending(self) -> None:
        from skyvern.forge.sdk.copilot.streaming_adapter import flush_goal_satisfied_tool_result

        sent: list[Any] = []
        ctx = self._ctx(in_flight_stream_tool_call=None)

        _fixture_pending_identity(ctx)
        await flush_goal_satisfied_tool_result(self._stream(sent), ctx)

        assert sent == []

    @pytest.mark.asyncio
    async def test_noops_when_pending_call_is_a_different_tool(self) -> None:
        from skyvern.forge.sdk.copilot.streaming_adapter import flush_goal_satisfied_tool_result

        sent: list[Any] = []
        ctx = self._ctx(goal_satisfied_tool_name="get_run_results")

        _fixture_pending_identity(ctx)
        ctx.goal_satisfied_tool_call_id = "unobserved-call"
        await flush_goal_satisfied_tool_result(self._stream(sent), ctx)

        assert sent == []
        assert ctx.in_flight_stream_tool_call is not None

    @pytest.mark.asyncio
    async def test_records_narrator_activity_but_skips_send_when_disconnected(self) -> None:
        from skyvern.forge.sdk.copilot.narration import NarratorState
        from skyvern.forge.sdk.copilot.streaming_adapter import flush_goal_satisfied_tool_result

        sent: list[Any] = []
        narrator_state = NarratorState()
        ctx = self._ctx(narrator_state=narrator_state)

        _fixture_pending_identity(ctx)
        await flush_goal_satisfied_tool_result(self._stream(sent, disconnected=True), ctx)

        assert sent == []
        assert narrator_state.design_activity
        assert narrator_state.design_activity[-1]["kind"] == "tool_result"


_PROGRESS_TEXT = "Refining the workflow's code"


def _code_repair_reject_payload() -> list[dict[str, str]]:
    body = json.dumps(
        {
            "ok": False,
            "error": "Insecure code detected: Not allowed to import modules.",
            "user_facing_summary": "I need to adjust the workflow's code so it can run safely before testing.",
            "data": {"surface_kind": "code_repair_progress", "progress_text": _PROGRESS_TEXT},
        }
    )
    return [{"type": "text", "text": body}]


def _round_trip_events(call_id: str, tool_name: str, output_payload: Any) -> list[Any]:
    call_item = MagicMock(spec=RunItem)
    call_item.raw_item = {"call_id": call_id, "name": tool_name, "arguments": "{}"}
    out_item = MagicMock(spec=RunItem)
    out_item.raw_item = {"call_id": call_id, "name": tool_name}
    out_item.output = output_payload
    return [
        RunItemStreamEvent(name="tool_called", item=call_item),
        RunItemStreamEvent(name="tool_output", item=out_item),
    ]


def _sink_stream(sent: list[Any], *, disconnected: bool = False) -> MagicMock:
    async def _send(payload: Any) -> bool:
        sent.append(payload)
        return True

    stream = MagicMock()
    stream.is_disconnected = AsyncMock(return_value=disconnected)
    stream.send = _send
    return stream


def _copilot_ctx() -> Any:
    return CopilotContext(
        organization_id="org_test",
        workflow_id="wf_test",
        workflow_permanent_id="wpid_test",
        workflow_yaml="",
        browser_session_id=None,
        stream=None,  # type: ignore[arg-type]
        api_key=None,
        user_message="",
    )


class TestGenuineAttemptScoutStamp:
    def test_run_blocks_scout_stamp_does_not_count_as_genuine_attempt(self) -> None:
        ctx = _copilot_ctx()
        _update_enforcement_from_tool(ctx, "run_blocks_and_collect_debug", {"ok": True})
        assert ctx.test_after_update_done is True
        assert ctx.has_genuine_workflow_attempt() is False

    def test_failed_run_blocks_scout_stamp_does_not_count_as_genuine_attempt(self) -> None:
        ctx = _copilot_ctx()
        _update_enforcement_from_tool(ctx, "run_blocks_and_collect_debug", {"ok": False})
        assert ctx.test_after_update_done is True
        assert ctx.has_genuine_workflow_attempt() is False

    def test_persisted_update_counts_as_genuine_attempt(self) -> None:
        ctx = _copilot_ctx()
        _update_enforcement_from_tool(ctx, "update_workflow", {"ok": True, "data": {"block_count": 2}})
        assert ctx.update_workflow_called is True
        assert ctx.has_genuine_workflow_attempt() is True

    def test_update_and_run_blocks_counts_as_genuine_attempt(self) -> None:
        ctx = _copilot_ctx()
        _update_enforcement_from_tool(ctx, "update_and_run_blocks", {"ok": True, "data": {"block_count": 2}})
        assert ctx.has_genuine_workflow_attempt() is True

    def test_update_without_blocks_is_not_a_genuine_attempt(self) -> None:
        ctx = _copilot_ctx()
        _update_enforcement_from_tool(ctx, "update_workflow", {"ok": True, "data": {"block_count": 0}})
        assert ctx.update_workflow_called is False
        assert ctx.has_genuine_workflow_attempt() is False


class TestCodeRepairProgressStreaming:
    @pytest.mark.asyncio
    async def test_repeated_classified_rejects_collapse_to_one_progress_frame(self) -> None:
        """Repeated identical classified rejects emit one progress narration and no failure rows."""
        events: list[Any] = []
        for idx in range(3):
            events.extend(_round_trip_events(f"c{idx}", "update_and_run_blocks", _code_repair_reject_payload()))

        result = MagicMock()
        result.stream_events = lambda: _stream_events_from(*events)
        result.cancel = MagicMock()

        sent: list[Any] = []
        ctx = _copilot_ctx()

        await stream_to_sse(result, _sink_stream(sent), ctx)

        tool_results = [p for p in sent if getattr(p, "type", None) == WorkflowCopilotStreamMessageType.TOOL_RESULT]
        assert tool_results == []

        narrations = [p for p in sent if getattr(p, "type", None) == WorkflowCopilotStreamMessageType.NARRATION]
        assert len(narrations) == 1
        assert narrations[0].narration == _PROGRESS_TEXT

        narration_entries = [e for e in ctx.narrator_state.design_activity if e["kind"] == "narration"]
        assert len(narration_entries) == 1
        assert narration_entries[0]["text"] == _PROGRESS_TEXT
        assert narration_entries[0]["timestamp"] == narrations[0].timestamp.isoformat()
        assert all(e["kind"] != "tool_result" for e in ctx.narrator_state.design_activity)
        # De-duplicated rejects still advance the narrator iteration (read by the run-outcome frame).
        assert ctx.narrator_state.current_iteration == 2

    @pytest.mark.asyncio
    async def test_classified_reject_persists_one_entry_on_disconnect(self) -> None:
        """A disconnected client still gets the persisted progress entry; no live frame is sent."""
        events = _round_trip_events("c1", "update_and_run_blocks", _code_repair_reject_payload())

        result = MagicMock()
        result.stream_events = lambda: _stream_events_from(*events)
        result.cancel = MagicMock()

        sent: list[Any] = []
        ctx = _copilot_ctx()

        await stream_to_sse(result, _sink_stream(sent, disconnected=True), ctx)

        assert sent == []
        narration_entries = [e for e in ctx.narrator_state.design_activity if e["kind"] == "narration"]
        assert len(narration_entries) == 1
        assert narration_entries[0]["text"] == _PROGRESS_TEXT

    @pytest.mark.asyncio
    async def test_classified_reject_still_runs_enforcement_and_increments_iteration(self) -> None:
        """A classified reject still runs the enforcement update and advances the iteration."""
        first = _round_trip_events("c1", "update_and_run_blocks", _code_repair_reject_payload())
        second_call_item = MagicMock()
        second_call_item.raw_item = {"call_id": "c2", "name": "navigate_browser", "arguments": "{}"}
        second_call = RunItemStreamEvent(name="tool_called", item=second_call_item)

        result = MagicMock()
        result.stream_events = lambda: _stream_events_from(*first, second_call)
        result.cancel = MagicMock()

        sent: list[Any] = []
        ctx = _copilot_ctx()

        await stream_to_sse(result, _sink_stream(sent), ctx)

        # update_and_run_blocks flips test_after_update_done even on an ok:False reject; skipping
        # enforcement on the classified path would change agent behavior.
        assert ctx.test_after_update_done is True
        tool_calls = [p for p in sent if getattr(p, "type", None) == WorkflowCopilotStreamMessageType.TOOL_CALL]
        assert tool_calls[-1].iteration == 1

    @pytest.mark.asyncio
    async def test_unclassified_reject_still_renders_failure_tool_result(self) -> None:
        """An unclassified ok:False reject still emits a success=false TOOL_RESULT and activity row."""
        payload = [{"type": "text", "text": json.dumps({"ok": False, "error": "plain failure"})}]
        events = _round_trip_events("c1", "update_workflow", payload)

        result = MagicMock()
        result.stream_events = lambda: _stream_events_from(*events)
        result.cancel = MagicMock()

        sent: list[Any] = []
        ctx = _copilot_ctx()

        await stream_to_sse(result, _sink_stream(sent), ctx)

        tool_results = [p for p in sent if getattr(p, "type", None) == WorkflowCopilotStreamMessageType.TOOL_RESULT]
        assert len(tool_results) == 1
        assert tool_results[0].success is False
        narrations = [p for p in sent if getattr(p, "type", None) == WorkflowCopilotStreamMessageType.NARRATION]
        assert narrations == []


async def _capture_tool_result(tool_name: str, parsed_output: dict[str, Any]) -> Any:
    """Drive `stream_to_sse` over a single tool round-trip and return the
    emitted ``WorkflowCopilotToolResultUpdate``."""
    call_item = MagicMock(spec=RunItem)
    call_item.raw_item = {"call_id": "c1", "name": tool_name, "arguments": "{}"}
    tool_call = RunItemStreamEvent(name="tool_called", item=call_item)

    out_item = MagicMock(spec=RunItem)
    out_item.raw_item = {"call_id": "c1", "name": tool_name}
    out_item.output = [{"type": "text", "text": json.dumps(parsed_output)}]
    tool_output = RunItemStreamEvent(name="tool_output", item=out_item)

    async def _events() -> Any:
        yield tool_call
        yield tool_output

    result = MagicMock()
    result.stream_events = lambda: _events()
    result.cancel = MagicMock()

    sent: list[Any] = []

    async def _send(payload: Any) -> bool:
        sent.append(payload)
        return True

    stream = MagicMock()
    stream.is_disconnected = AsyncMock(return_value=False)
    stream.send = _send

    await stream_to_sse(
        result,
        stream,
        _test_copilot_context(
            last_artifact_health_blocker_reason=None, pending_code_write_diffs={}, completion_verification_result=None
        ),
    )

    tool_results = [p for p in sent if getattr(p, "type", None) == WorkflowCopilotStreamMessageType.TOOL_RESULT]
    assert len(tool_results) == 1
    return tool_results[0]


@pytest.mark.asyncio
async def test_stream_emits_detail_for_failure() -> None:
    long_error = "blocks.0.task expects 'navigation_goal' but the emitted YAML omitted it. " * 4
    payload = await _capture_tool_result(
        "update_workflow",
        {"ok": False, "error": long_error},
    )
    assert payload.success is False
    assert payload.detail is not None
    assert len(payload.detail) > 120
    # `summary` is the visible bullet, capped tighter than `detail` (the
    # tooltip-grade text) — strictly longer detail is the contract.
    assert len(payload.detail) > len(payload.summary)


@pytest.mark.asyncio
async def test_stream_emits_generic_detail_for_internal_validation_failure() -> None:
    """The tooltip-grade detail must not leak raw internal validator text either —
    only the visible summary was sanitized before this regression test was added."""
    long_error = "Workflow validation failed: " + (
        "blocks.0.task expects 'navigation_goal' but the emitted YAML omitted it. " * 4
    )
    payload = await _capture_tool_result(
        "update_workflow",
        {"ok": False, "error": long_error},
    )
    assert payload.detail == "Couldn't complete that step."
    assert "navigation_goal" not in payload.detail


@pytest.mark.asyncio
async def test_stream_emits_no_detail_for_success() -> None:
    payload = await _capture_tool_result(
        "update_workflow",
        {"ok": True, "data": {"block_count": 3}},
    )
    assert payload.success is True
    assert payload.detail is None


def _codegen_payloads(sent: list[Any]) -> list[Any]:
    return [p for p in sent if getattr(p, "type", None) == WorkflowCopilotStreamMessageType.CODEGEN_PROGRESS]


@pytest.mark.asyncio
async def test_codegen_progress_pins_incremental_label_extraction_on_raw_deltas() -> None:
    """Regression pin: fails on old code because RawResponsesStreamEvent is dropped at the
    ``isinstance(event, RunItemStreamEvent)`` skip, so zero CODEGEN_PROGRESS frames are ever sent."""
    delta1 = (
        '{"workflow": {"workflow_definition": {"blocks": [{"block_type": "navigation", "label": "open_page", '
        '"next_block_label": "branch_target", '
    )
    delta2 = (
        '"navigation_goal": "Navigate to the page"}, {"block_type": "task", "label": "fill_form", '
        '"next_block_label": null, '
    )
    delta3 = '"navigation_goal": "Fill out the form"}]}}'
    delta4 = ', "block_labels": ["open_page", "fill_form"]}'
    full_args = delta1 + delta2 + delta3 + delta4

    events = [
        _item_added_event(0, "update_and_run_blocks"),
        _args_delta_event(0, delta1),
        _args_delta_event(0, delta2),
        _args_delta_event(0, delta3),
        _args_delta_event(0, delta4),
        _args_done_event(0, full_args, "update_and_run_blocks"),
        _item_done_event(0, "update_and_run_blocks", full_args),
        _tool_called_event("c1", "update_and_run_blocks"),
        _tool_output_event("c1"),
    ]

    result = MagicMock()
    result.stream_events = lambda: _stream_events_from(*events)
    result.cancel = MagicMock()

    sent: list[Any] = []

    async def _send(payload: Any) -> bool:
        sent.append(payload)
        return True

    stream = MagicMock()
    stream.is_disconnected = AsyncMock(return_value=False)
    stream.send = _send

    await stream_to_sse(result, stream, _new_ctx())

    codegen_payloads = _codegen_payloads(sent)
    assert codegen_payloads, "expected at least one CODEGEN_PROGRESS frame"
    assert any(p.blocks_drafted == ["open_page", "fill_form"] for p in codegen_payloads)
    assert codegen_payloads[0].blocks_drafted == []
    assert codegen_payloads[0].chars_streamed == 0


@pytest.mark.asyncio
async def test_codegen_progress_does_not_leak_truncated_label_split_across_deltas() -> None:
    """A label that happens to end exactly at a delta boundary (real token-by-token
    streaming can split anywhere) must never surface a truncated fragment like "open_pa"
    in blocks_drafted -- only the completed "open_page" once the boundary resolves."""
    events = [
        _item_added_event(0, "update_and_run_blocks"),
        _args_delta_event(0, '{"workflow_yaml": "blocks:\\n  label: open_pa'),
        _args_delta_event(0, 'ge\\n  navigation_goal: x\\n"}'),
    ]

    result = MagicMock()
    result.stream_events = lambda: _stream_events_from(*events)
    result.cancel = MagicMock()

    sent: list[Any] = []

    async def _send(payload: Any) -> bool:
        sent.append(payload)
        return True

    stream = MagicMock()
    stream.is_disconnected = AsyncMock(return_value=False)
    stream.send = _send

    await stream_to_sse(result, stream, _new_ctx())

    codegen_payloads = _codegen_payloads(sent)
    all_labels = {label for p in codegen_payloads for label in p.blocks_drafted}
    assert "open_pa" not in all_labels
    assert any(p.blocks_drafted == ["open_page"] for p in codegen_payloads)


@pytest.mark.asyncio
async def test_codegen_progress_does_not_report_the_block_edit_block_and_run_targets() -> None:
    args = '{"label": "extract_totals", "code": "rows = []\\nreturn rows"}'
    events = [
        _item_added_event(0, "edit_block_and_run"),
        _args_delta_event(0, args),
        _args_done_event(0, args, "edit_block_and_run"),
        _item_done_event(0, "edit_block_and_run", args),
    ]

    result = MagicMock()
    result.stream_events = lambda: _stream_events_from(*events)
    result.cancel = MagicMock()

    sent: list[Any] = []

    async def _send(payload: Any) -> bool:
        sent.append(payload)
        return True

    stream = MagicMock()
    stream.is_disconnected = AsyncMock(return_value=False)
    stream.send = _send

    await stream_to_sse(result, stream, _new_ctx())

    codegen_payloads = _codegen_payloads(sent)
    assert codegen_payloads
    assert all(p.blocks_drafted == [] for p in codegen_payloads)


@pytest.mark.asyncio
async def test_codegen_progress_throttles_no_new_label_deltas_then_emits_keepalive(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class _FakeClock:
        def __init__(self) -> None:
            self.value = 0.0

        def monotonic(self) -> float:
            return self.value

    clock = _FakeClock()
    monkeypatch.setattr(streaming_adapter_module, "time", clock)

    async def _events() -> Any:
        yield _item_added_event(0, "update_workflow")
        yield _args_delta_event(0, "no label content here, ")
        yield _args_delta_event(0, "still nothing new, ")
        clock.value = 2.5
        yield _args_delta_event(0, "and more with no label, ")

    result = MagicMock()
    result.stream_events = _events
    result.cancel = MagicMock()

    sent: list[Any] = []

    async def _send(payload: Any) -> bool:
        sent.append(payload)
        return True

    stream = MagicMock()
    stream.is_disconnected = AsyncMock(return_value=False)
    stream.send = _send

    await stream_to_sse(result, stream, _new_ctx())

    codegen_payloads = _codegen_payloads(sent)
    # ItemAdded emits immediately; the two in-gap deltas emit nothing; the
    # post-gap delta emits a throttled keepalive with more chars, no new labels.
    assert len(codegen_payloads) == 2
    assert codegen_payloads[0].chars_streamed == 0
    assert codegen_payloads[1].chars_streamed > codegen_payloads[0].chars_streamed
    assert codegen_payloads[1].blocks_drafted == []


@pytest.mark.asyncio
async def test_codegen_progress_disabled_by_kill_switch(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(streaming_adapter_module.settings, "WORKFLOW_COPILOT_CODEGEN_PROGRESS_ENABLED", False)

    events = [
        _item_added_event(0, "update_and_run_blocks"),
        _args_delta_event(0, '{"workflow_yaml": "blocks:\\n  label: open_page\\n"}'),
        _tool_called_event("c1", "update_and_run_blocks"),
        _tool_output_event("c1"),
    ]

    result = MagicMock()
    result.stream_events = lambda: _stream_events_from(*events)
    result.cancel = MagicMock()

    sent: list[Any] = []

    async def _send(payload: Any) -> bool:
        sent.append(payload)
        return True

    stream = MagicMock()
    stream.is_disconnected = AsyncMock(return_value=False)
    stream.send = _send

    await stream_to_sse(result, stream, _new_ctx())

    assert _codegen_payloads(sent) == []
    tool_call_payloads = [p for p in sent if getattr(p, "type", None) == WorkflowCopilotStreamMessageType.TOOL_CALL]
    assert len(tool_call_payloads) == 1


@pytest.mark.asyncio
async def test_codegen_progress_ignores_non_authoring_tool() -> None:
    events = [
        _item_added_event(0, "evaluate"),
        _args_delta_event(0, "label: should_not_appear\\n"),
    ]

    result = MagicMock()
    result.stream_events = lambda: _stream_events_from(*events)
    result.cancel = MagicMock()

    sent: list[Any] = []

    async def _send(payload: Any) -> bool:
        sent.append(payload)
        return True

    stream = MagicMock()
    stream.is_disconnected = AsyncMock(return_value=False)
    stream.send = _send

    await stream_to_sse(result, stream, _new_ctx())

    assert _codegen_payloads(sent) == []


@pytest.mark.asyncio
async def test_codegen_progress_resets_on_response_created() -> None:
    events = [
        _item_added_event(0, "update_and_run_blocks"),
        _args_delta_event(0, '{"workflow_yaml": "blocks:\\n  label: block_one\\n"}'),
        _item_added_event(1, "update_workflow"),
        _args_delta_event(1, '{"workflow_yaml": "blocks:\\n  label: block_extra\\n"}'),
        _response_created_event(),
        _item_added_event(0, "update_and_run_blocks"),
        _args_delta_event(0, '{"workflow_yaml": "blocks:\\n  label: block_two\\n"}'),
    ]

    result = MagicMock()
    result.stream_events = lambda: _stream_events_from(*events)
    result.cancel = MagicMock()

    sent: list[Any] = []

    async def _send(payload: Any) -> bool:
        sent.append(payload)
        return True

    stream = MagicMock()
    stream.is_disconnected = AsyncMock(return_value=False)
    stream.send = _send

    await stream_to_sse(result, stream, _new_ctx())

    codegen_payloads = _codegen_payloads(sent)
    assert any(p.blocks_drafted == ["block_two"] for p in codegen_payloads)
    assert not any("block_one" in p.blocks_drafted for p in codegen_payloads if p.blocks_drafted == ["block_two"])

    # The restarted response opens exactly like a second authoring call, so only the id separates them.
    ids = [p.generation_id for p in codegen_payloads]
    assert len(ids) == 6 and None not in ids
    assert len(set(ids[:4])) == 1 and len(set(ids[4:])) == 1 and ids[0] != ids[4]

    # Each enforcement pass streams through a fresh tracker; its ids must not repeat the last pass's.
    result.stream_events = lambda: _stream_events_from(_item_added_event(0, "update_and_run_blocks"))
    await stream_to_sse(result, stream, _new_ctx())
    assert _codegen_payloads(sent)[-1].generation_id not in ids


@pytest.mark.asyncio
async def test_codegen_progress_first_frame_preceded_by_design_start() -> None:
    events = [
        _item_added_event(0, "update_and_run_blocks"),
    ]

    result = MagicMock()
    result.stream_events = lambda: _stream_events_from(*events)
    result.cancel = MagicMock()

    sent: list[Any] = []

    async def _send(payload: Any) -> bool:
        sent.append(payload)
        return True

    stream = MagicMock()
    stream.is_disconnected = AsyncMock(return_value=False)
    stream.send = _send

    ctx = _new_ctx()
    assert ctx.design_start_emitted is False

    await stream_to_sse(result, stream, ctx)

    types_in_order = [getattr(p, "type", None) for p in sent]
    assert WorkflowCopilotStreamMessageType.DESIGN_START in types_in_order
    design_start_index = types_in_order.index(WorkflowCopilotStreamMessageType.DESIGN_START)
    codegen_index = types_in_order.index(WorkflowCopilotStreamMessageType.CODEGEN_PROGRESS)
    assert design_start_index < codegen_index
    assert ctx.design_start_emitted is True


@pytest.mark.asyncio
async def test_codegen_progress_is_disconnected_bounded_when_client_gone(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """SKY-8986-adjacent: many argument deltas with a disconnected client must not call
    is_disconnected() once per delta -- only at throttled emit attempts. Clock is pinned
    (not real wall-clock) so the 2s gap deterministically never elapses within the 20 deltas,
    proving the throttle -- not fast test execution -- bounds the call count."""

    class _FakeClock:
        def __init__(self) -> None:
            self.value = 0.0

        def monotonic(self) -> float:
            return self.value

    monkeypatch.setattr(streaming_adapter_module, "time", _FakeClock())

    events = [
        _item_added_event(0, "update_and_run_blocks"),
    ] + [_args_delta_event(0, "repeated_label_chunk ") for _ in range(20)]

    result = MagicMock()
    result.stream_events = lambda: _stream_events_from(*events)
    result.cancel = MagicMock()

    stream = MagicMock()
    stream.is_disconnected = AsyncMock(return_value=True)
    stream.send = AsyncMock(return_value=True)

    # Pre-mark design_start emitted so the unconditional (not disconnect-gated)
    # design_start send doesn't muddy the codegen-frame-specific assertion below.
    ctx = _new_ctx()
    ctx.design_start_emitted = True

    await stream_to_sse(result, stream, ctx)

    stream.send.assert_not_called()
    # 20 deltas were pumped with a clock that never advances (gap never elapses) and no
    # new labels; is_disconnected must be checked only at the initial ItemAdded emit.
    assert stream.is_disconnected.call_count == 1


def _label_probe_ctx() -> SimpleNamespace:
    return _test_copilot_context(
        last_artifact_health_blocker_reason=None,
        pending_code_write_diffs={},
        completion_verification_result=None,
    )


def _tool_round_trip(tool_name: str, arguments: str, output: str) -> list[RunItemStreamEvent]:
    call_item = MagicMock(spec=RunItem)
    call_item.raw_item = {"call_id": "c1", "name": tool_name, "arguments": arguments}
    output_item = MagicMock(spec=RunItem)
    output_item.raw_item = {"call_id": "c1"}
    output_item.output = output
    return [
        RunItemStreamEvent(name="tool_called", item=call_item),
        RunItemStreamEvent(name="tool_output", item=output_item),
    ]


async def _drive(events: list[RunItemStreamEvent], ctx: SimpleNamespace) -> list[Any]:
    sent: list[Any] = []

    async def _send(payload: Any) -> bool:
        sent.append(payload)
        return True

    result = MagicMock()
    result.stream_events = lambda: _stream_events_from(*events)
    result.cancel = MagicMock()
    stream = MagicMock()
    stream.is_disconnected = AsyncMock(return_value=False)
    stream.send = _send
    await stream_to_sse(result, stream, ctx)
    return sent


@pytest.mark.asyncio
async def test_tool_result_reuses_the_tool_calls_target_block_label() -> None:
    ctx = _label_probe_ctx()
    events = _tool_round_trip("edit_block", '{"label": "Log in"}', json.dumps({"ok": True, "data": {}}))

    sent = await _drive(events, ctx)

    calls = [p for p in sent if getattr(p, "type", None) == WorkflowCopilotStreamMessageType.TOOL_CALL]
    results = [p for p in sent if getattr(p, "type", None) == WorkflowCopilotStreamMessageType.TOOL_RESULT]
    assert [p.display_label for p in calls] == ['Editing block "Log In"']
    assert [p.display_label for p in results] == ['Editing block "Log In"']
    assert calls[0].tool_call_id == results[0].tool_call_id == "c1"

    activity = ctx.narrator_state.design_activity
    assert [e["displayLabel"] for e in activity] == ['Editing block "Log In"', 'Editing block "Log In"']
    assert all("Working" not in e["text"] for e in activity)


@pytest.mark.asyncio
async def test_non_dict_tool_arguments_degrade_to_the_generic_label_and_keep_draining() -> None:
    ctx = _label_probe_ctx()
    events = _tool_round_trip("edit_block", "[1, 2]", json.dumps({"ok": True, "data": {}}))

    sent = await _drive(events, ctx)

    calls = [p for p in sent if getattr(p, "type", None) == WorkflowCopilotStreamMessageType.TOOL_CALL]
    results = [p for p in sent if getattr(p, "type", None) == WorkflowCopilotStreamMessageType.TOOL_RESULT]
    assert [p.display_label for p in calls] == ["Editing block"]
    assert [p.display_label for p in results] == ["Editing block"]


@pytest.mark.asyncio
async def test_goal_satisfied_flush_reuses_the_pending_calls_label() -> None:
    sent: list[Any] = []

    async def _send(payload: Any) -> bool:
        sent.append(payload)
        return True

    stream = MagicMock()
    stream.is_disconnected = AsyncMock(return_value=False)
    stream.send = _send
    narrator_state = NarratorState()
    ctx = _test_copilot_context(
        in_flight_stream_tool_call=InFlightStreamToolCall(
            call_id="c9", tool_name="edit_block", iteration=3, display_label='Editing block "Log In"'
        ),
        goal_satisfied_tool_name="edit_block",
        goal_satisfied_tool_output={"ok": True, "data": {}},
        pending_code_write_diffs={},
        narrator_state=narrator_state,
    )

    _fixture_pending_identity(ctx)
    await flush_goal_satisfied_tool_result(stream, ctx)

    assert [p.display_label for p in sent] == ['Editing block "Log In"']
    assert narrator_state.design_activity[-1]["displayLabel"] == 'Editing block "Log In"'


@pytest.mark.asyncio
async def test_design_start_narrates_for_a_plain_agent_context() -> None:
    from skyvern.forge.sdk.copilot.runtime import AgentContext
    from skyvern.forge.sdk.copilot.streaming_adapter import maybe_emit_design_start

    sent: list[Any] = []

    class _Stream:
        async def send(self, update: Any) -> None:
            sent.append(update)

    stream = _Stream()
    ctx = AgentContext(
        organization_id="o_1",
        workflow_id="w_1",
        workflow_permanent_id="wpid_1",
        workflow_yaml="",
        browser_session_id=None,
        stream=stream,  # type: ignore[arg-type]
    )

    await maybe_emit_design_start(stream, ctx)  # type: ignore[arg-type]

    assert len(sent) == 1
    assert ctx.design_start_emitted is True


@pytest.mark.asyncio
async def test_actor_reason_native_dispatch_preserves_ordinary_validation() -> None:
    ctx = CopilotContext(
        organization_id="org_test",
        workflow_permanent_id="wpid_test",
        workflow_id=None,
        workflow_yaml=None,
        browser_session_id=None,
        stream=None,
    )
    tool = next(
        tool
        for tool in copilot_native_tools(
            supports_question_tool=True, browser_code_available=False, run_tools_available=True
        )
        if tool.name == "set_work_plan"
    )
    schema = tool.params_json_schema
    assert "user_facing_reason" in schema["properties"]
    assert "user_facing_reason" in schema["required"]
    assert tool.strict_json_schema is False
    tc = ToolContext(context=ctx, tool_name=tool.name, tool_call_id="plan-call", tool_arguments="{}")
    for reason in ("I will plan the booking steps.", None, "", "   ", 17, {"bad": True}):
        raw = {"items": ["Inspect availability"], "user_facing_reason": reason}
        result = json.loads(await tool.on_invoke_tool(tc, json.dumps(raw)))
        assert result["ok"] is True
        assert ctx.work_plan == ["Inspect availability"]
    omitted = json.loads(await tool.on_invoke_tool(tc, json.dumps({"items": ["Read the page"]})))
    assert omitted["ok"] is True
    assert ctx.work_plan == ["Read the page"]
    result = await tool.on_invoke_tool(tc, json.dumps({"user_facing_reason": "Explain", "items": 17}))
    assert "error" in result.lower()


def test_every_native_tool_but_reply_requires_a_nullable_reason() -> None:
    tools = copilot_native_tools(supports_question_tool=False, browser_code_available=False, run_tools_available=False)
    by_name = {tool.name: tool.params_json_schema for tool in tools}
    reply_schema = by_name.pop("reply")
    assert "user_facing_reason" not in reply_schema["properties"]
    assert reply_schema["required"] == ["user_response", "global_llm_context"]
    assert by_name
    for name, schema in by_name.items():
        assert "user_facing_reason" in schema["required"], name
        assert schema["properties"]["user_facing_reason"]["type"] == ["string", "null"], name


@pytest.mark.asyncio
@pytest.mark.parametrize("transport", ["native", "mcp"])
async def test_real_runner_streams_actor_reason_before_held_action_returns(
    transport: str,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    release = asyncio.Event()
    entered = asyncio.Event()
    seen: list[str] = []
    hook_call_ids: list[str] = []
    pre_hook_inputs: list[dict[str, Any]] = []

    class ObservedHooks(CopilotRunHooks):
        async def on_tool_end(self, context, agent, tool, result):
            hook_call_ids.append(context.tool_call_id)
            await super().on_tool_end(context, agent, tool, result)

    async def pre_hook(arguments, _ctx):
        pre_hook_inputs.append(dict(arguments))
        return None

    async def leaf(value: str) -> str:
        entered.set()
        await release.wait()
        seen.append(value)
        return json.dumps({"ok": True, "data": {"value": value}})

    ctx = _new_ctx()
    ctx.api_key = "test-in-process-key"
    monkeypatch.setattr(streaming_adapter_module.settings, "ENV", "local")
    monkeypatch.setenv("COPILOT_DUMP_MODEL_INPUTS", str(tmp_path))
    stream = FakeCopilotStream()
    ctx.stream = stream
    server = None
    if transport == "native":
        tool = _with_action_reason(function_tool(leaf, name_override="probe"))
    else:
        probe = FastMCP("actor-reason-held")
        probe.tool(name="skyvern_probe")(leaf)
        server = SkyvernOverlayMCPServer(
            transport=probe,
            overlays={
                "probe": SchemaOverlay(
                    copilot_params={"user_facing_reason": USER_FACING_REASON_SCHEMA}, pre_hook=pre_hook
                )
            },
            alias_map={"probe": "skyvern_probe"},
            allowlist=frozenset({"skyvern_probe"}),
            context_provider=lambda: ctx,
        )
        await server.connect()
        advertised = await server.list_tools()
        tool = MCPUtil.to_function_tool(advertised[0], server, convert_schemas_to_strict=False)
    reason = "I will check this value before booking."
    assert tool.params_json_schema["properties"]["user_facing_reason"]["type"] == ["string", "null"]
    assert "user_facing_reason" in tool.params_json_schema["required"]
    dump = serialize_tool_surface([tool])
    assert dump.payload["tools"][0]["params_json_schema"] == tool.params_json_schema
    arguments = {"value": "ordinary", "user_facing_reason": reason}
    model = ScriptedModel([[scripted_call(tool.name, arguments)], [scripted_text("Done")]])
    result = Runner.run_streamed(
        Agent(name="controlled", model=model, tools=[tool], instructions=_build_system_prompt(tool_usage_guide="")),
        input="controlled",
        context=ctx,
        hooks=ObservedHooks(ctx),
        run_config=RunConfig(tracing_disabled=True),
    )
    pump = asyncio.create_task(stream_to_sse(result, stream, ctx))
    try:
        await asyncio.wait_for(entered.wait(), 3)
        assert model.served_instructions is not None
        assert "user_facing_reason" in model.served_instructions
        assert "progress and explanation channel" in model.served_instructions
        assert "substantive inspection, authoring and test actions" in model.served_instructions
        assert "Describe intent, not an outcome before results" in model.served_instructions
        served_reason = model.served_tools[0].params_json_schema["properties"]["user_facing_reason"]
        assert "displayed above this action while it runs" in served_reason["description"]
        assert "absence" not in served_reason["description"]
        assert "absence" not in model.served_instructions
        for _ in range(100):
            calls = [frame for frame in stream.sent if frame.type == WorkflowCopilotStreamMessageType.TOOL_CALL]
            if calls:
                break
            await asyncio.sleep(0.01)
        assert len(calls) == 1
        assert calls[0].tool_call_id == "call_held"
        assert calls[0].reason == reason
        assert calls[0].tool_input == {"value": "ordinary"}
        assert not any(frame.type == WorkflowCopilotStreamMessageType.TOOL_RESULT for frame in stream.sent)
        assert ctx.narrator_state.design_activity[0]["reason"] == reason
        release.set()
        await asyncio.wait_for(pump, 5)
        receipt = next(frame for frame in stream.sent if frame.type == WorkflowCopilotStreamMessageType.TOOL_RESULT)
        assert receipt.reason == reason
        assert receipt.activity_started_at == calls[0].timestamp
        assert seen == ["ordinary"]
        assert hook_call_ids == ["call_held"]
        if transport == "mcp":
            assert pre_hook_inputs == [{"value": "ordinary"}]
        capture_files = list((tmp_path / "actor-dispatch" / ctx.turn_id).glob("*.json"))
        assert len(capture_files) == 1
        written = capture_files[0].read_text()
        assert "ordinary" not in written and reason not in written
        capture = json.loads(written)
        assert capture["tool_call_id"] == calls[0].tool_call_id
        assert capture["arguments_shape"] == {"value": "str", "user_facing_reason": "str"}
        assert (
            capture["arguments_sha256"]
            == hashlib.sha256(json.dumps(arguments, ensure_ascii=False).encode()).hexdigest()
        )
        assert capture["activity_bucket"] == calls[0].activity_bucket
        assert capture["commit"]
    finally:
        release.set()
        if not pump.done():
            pump.cancel()
        await asyncio.gather(pump, return_exceptions=True)
        if server is not None:
            await server.cleanup()


@pytest.mark.asyncio
async def test_reason_custody_reversed_results_iteration_reset_secret_scrub_and_flush(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    ctx = _new_ctx()
    monkeypatch.setattr(streaming_adapter_module.settings, "ENV", "local")
    monkeypatch.setenv("COPILOT_DUMP_MODEL_INPUTS", str(tmp_path))
    stream = FakeCopilotStream()
    secret = "safe-test-secret/value"
    register_secret_scrub_value(ctx, secret)
    a_arguments = json.dumps({"expression": "a", "user_facing_reason": f"Read {secret} {quote(secret, safe='')}"})
    a = _tool_called_event("a", "evaluate", a_arguments)
    b = _tool_called_event(
        "b",
        "evaluate",
        json.dumps({"expression": "b", "user_facing_reason": "The booking has completed successfully."}),
    )
    first = MagicMock(stream_events=lambda: _stream_events_from(a, b))
    await stream_to_sse(first, stream, ctx)
    captures = {}
    for file in (tmp_path / "actor-dispatch" / ctx.turn_id).glob("*.json"):
        written = file.read_text()
        assert secret not in written and quote(secret, safe="") not in written
        capture = json.loads(written)
        assert capture["undeclared_argument_shapes"] == ["str", "str"]
        captures[capture["tool_call_id"]] = capture
    assert set(captures) == {"a", "b"}
    assert captures["a"]["arguments_sha256"] != hashlib.sha256(a_arguments.encode()).hexdigest()
    original_a = ctx.stream_tool_calls["a"]
    original_b = ctx.stream_tool_calls["b"]
    ctx.narrator_state.running_block_id = "wrb_later"
    ctx.narrator_state.running_block_label = "later"
    second = MagicMock(
        stream_events=lambda: _stream_events_from(_tool_output_event("b", ok=False), _tool_output_event("a"))
    )
    await stream_to_sse(second, stream, ctx)
    receipts = [frame for frame in stream.sent if frame.type == WorkflowCopilotStreamMessageType.TOOL_RESULT]
    assert [r.tool_call_id for r in receipts] == ["b", "a"]
    assert [r.reason for r in receipts] == [original_b.reason, original_a.reason]
    assert receipts[0].success is False
    assert all(r.activity_bucket == {"kind": "design"} for r in receipts)
    payload = _build_narrative_payload(ctx, terminal="response", terminal_message="Finished", narrative_summary=None)
    serialized = json.dumps(payload)
    assert secret not in serialized and quote(secret, safe="") not in serialized
    assert payload["designActivity"][-1]["reason"] == original_a.reason
    ctx.stream_tool_calls["a"] = original_a
    ctx.pending_stream_tool_call_ids.update({"a", "b"})
    ctx.goal_satisfied_tool_call_id = "a"
    ctx.goal_satisfied_tool_output = {"ok": True}
    ctx.in_flight_stream_tool_call = original_b
    await flush_goal_satisfied_tool_result(stream, ctx)
    assert stream.sent[-1].tool_call_id == "a"
    assert stream.sent[-1].reason == original_a.reason
    assert ctx.in_flight_stream_tool_call == original_b
    assert ctx.pending_stream_tool_call_ids == {"b"}


@pytest.mark.asyncio
@pytest.mark.parametrize("schema_source", ["native", "mcp"])
async def test_actor_dispatch_capture_writes_only_declared_names_and_no_model_authored_text(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    schema_source: str,
) -> None:
    async def leaf(
        expression: str, options: dict[str, Any] | None = None, parameters: dict[str, Any] | None = None
    ) -> str:
        return "{}"

    ctx = _new_ctx()
    ctx.api_key = "test-in-process-key"
    monkeypatch.setattr(streaming_adapter_module.settings, "ENV", "local")
    monkeypatch.setenv("COPILOT_DUMP_MODEL_INPUTS", str(tmp_path))
    server = None
    if schema_source == "native":
        tool = _with_action_reason(function_tool(leaf, name_override="probe", strict_mode=False))
        agent = Agent(name="capture", tools=[tool])
    else:
        probe = FastMCP("actor-capture-schema")
        probe.tool(name="skyvern_probe")(leaf)
        server = SkyvernOverlayMCPServer(
            transport=probe,
            overlays={"probe": SchemaOverlay(copilot_params={"user_facing_reason": USER_FACING_REASON_SCHEMA})},
            alias_map={"probe": "skyvern_probe"},
            allowlist=frozenset({"skyvern_probe"}),
            context_provider=lambda: ctx,
        )
        await server.connect()
        agent = Agent(name="capture", mcp_servers=[server])
    unregistered = "unregistered-test-password/Zx9"
    arguments = {
        "expression": f"document.querySelector('#password').value = '{unregistered}'",
        "options": {"timeout_ms": 500, "strict": True, "frame": None, "selectors": ["#user", unregistered]},
        "parameters": {unregistered: {"retries": 3}},
        unregistered: 7,
        "user_facing_reason": f"I will sign in with {unregistered} at the café.",
    }
    result = MagicMock(
        stream_events=lambda: _stream_events_from(_tool_called_event("typed", "probe", json.dumps(arguments), agent))
    )
    try:
        await stream_to_sse(result, FakeCopilotStream(), ctx)
    finally:
        if server is not None:
            await server.cleanup()
    capture_files = list((tmp_path / "actor-dispatch" / ctx.turn_id).glob("*.json"))
    assert len(capture_files) == 1
    written = capture_files[0].read_text()
    assert unregistered not in written
    capture = json.loads(written)
    assert capture["tool_call_id"] == "typed"
    assert capture["tool_name"] == "probe"
    assert capture["arguments_shape"] == {
        "expression": "str",
        "options": {"dict": ["int", "bool", "NoneType", ["str", "str"]]},
        "parameters": {"dict": [{"dict": ["int"]}]},
        "user_facing_reason": "str",
    }
    assert capture["undeclared_argument_shapes"] == ["int"]
    expected_bytes = json.dumps(arguments, ensure_ascii=False).encode()
    assert capture["arguments_sha256"] == hashlib.sha256(expected_bytes).hexdigest()


_FRAME_SIZE = (1400, 900)


def _stage_frame(ctx: CopilotContext, image_format: str = "PNG", color: str = "white") -> bool:
    buf = io.BytesIO()
    Image.new("RGB", _FRAME_SIZE, color).save(buf, format=image_format)
    return enqueue_screenshot(
        ctx,
        base64.b64encode(buf.getvalue()).decode(),
        provenance=ScreenshotProvenance.unknown(source_tool="click"),
    )


def _click_round_trip() -> list[RunItemStreamEvent]:
    return [_tool_called_event("c1", "click"), _tool_output_event("c1")]


def _saved_screenshots(ctx: CopilotContext) -> list[dict[str, str]] | None:
    """What a reloaded chat receives: the history model drops payload keys it does not declare."""
    reloaded = WorkflowCopilotChatHistoryMessage(
        sender=WorkflowCopilotChatSender.AI,
        content="Finished",
        created_at=datetime.now(UTC),
        narrative_payload=_build_narrative_payload(
            ctx, terminal="response", terminal_message="Finished", narrative_summary=None
        ),
    )
    return reloaded.model_dump(mode="json")["narrative_payload"].get("screenshots")


@pytest.fixture
def chat_screenshot_rows(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> list[dict[str, Any]]:
    """The real artifact manager over local storage; only the row insert is faked."""
    rows: list[dict[str, Any]] = []

    async def create_artifact(
        artifact_id: str, artifact_type: ArtifactType, uri: str, *, organization_id: str, **stamps: str | int | None
    ) -> Artifact:
        rows.append({"artifact_id": artifact_id, "uri": uri, "organization_id": organization_id, **stamps})
        now = datetime.now(UTC)
        return Artifact(
            artifact_id=artifact_id,
            artifact_type=artifact_type,
            uri=uri,
            organization_id=organization_id,
            created_at=now,
            modified_at=now,
        )

    monkeypatch.setattr(app, "ARTIFACT_MANAGER", ArtifactManager())
    monkeypatch.setattr(app, "STORAGE", LocalStorage(str(tmp_path)))
    monkeypatch.setattr(app.DATABASE.artifacts, "create_artifact", create_artifact)
    return rows


@pytest.mark.asyncio
@pytest.mark.parametrize(("source_format", "client_gone"), [("PNG", False), ("JPEG", True)])
async def test_a_frame_staged_for_the_model_is_stored_once_for_the_chat(
    chat_screenshot_rows: list[dict[str, Any]], source_format: str, client_gone: bool
) -> None:
    ctx = _test_copilot_context(workflow_copilot_chat_id="wcc_1")
    with capturing_tool_call("c1"):
        assert _stage_frame(ctx, source_format)
    assert _stage_frame(ctx, source_format)
    sent: list[Any] = []
    result = MagicMock(stream_events=lambda: _stream_events_from(*_click_round_trip()))

    with skyvern_context.scoped(SkyvernContext(run_id="pbs_other", workflow_run_id="wr_other", task_id="tsk_other")):
        await stream_to_sse(result, _sink_stream(sent, disconnected=client_gone), ctx)

    [row] = chat_screenshot_rows
    assert (row["organization_id"], row["workflow_run_id"], row["run_id"], row["task_id"]) == (
        "org_test",
        None,
        None,
        None,
    )
    assert "/logs/workflow_copilot_chat/wcc_1/" in row["uri"]
    with Image.open(parse_uri_to_path(row["uri"])) as stored:
        assert (stored.format, stored.size) == ("PNG", _FRAME_SIZE)
    [saved] = _saved_screenshots(ctx) or []
    assert (saved["artifactId"], saved["toolCallId"]) == (row["artifact_id"], "c1")
    assert datetime.fromisoformat(saved["capturedAt"]).utcoffset() == timedelta(0)

    if client_gone:
        assert sent == []
        return
    frames = [p for p in sent if p.type in ("screenshot", WorkflowCopilotStreamMessageType.TOOL_RESULT)]
    assert [p.type for p in frames] == ["screenshot", WorkflowCopilotStreamMessageType.TOOL_RESULT]
    wire = frames[0].model_dump(mode="json")
    assert wire == {
        "type": "screenshot",
        "artifact_id": row["artifact_id"],
        "captured_at": wire["captured_at"],
        "tool_call_id": "c1",
    }
    assert saved["capturedAt"] == frames[0].captured_at.isoformat()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "overrides",
    [{"workflow_copilot_chat_id": "wcc_1", "supports_vision": False}, {"workflow_copilot_chat_id": None}],
    ids=["model_has_no_vision", "turn_has_no_chat"],
)
async def test_no_frame_reaches_the_chat_without_vision_or_a_chat(
    chat_screenshot_rows: list[dict[str, Any]], overrides: dict[str, Any]
) -> None:
    ctx = _test_copilot_context(**overrides)
    _stage_frame(ctx)

    sent = await _drive(_click_round_trip(), ctx)

    assert chat_screenshot_rows == []
    assert "screenshot" not in [p.type for p in sent]
    assert _saved_screenshots(ctx) is None


@pytest.mark.asyncio
@pytest.mark.parametrize("storage_fault", ["refuses", "stalls"])
async def test_a_failed_screenshot_upload_leaves_the_tool_result_as_it_was(
    chat_screenshot_rows: list[dict[str, Any]], monkeypatch: pytest.MonkeyPatch, storage_fault: str
) -> None:
    dictation_upload = asyncio.create_task(asyncio.Event().wait())
    app.ARTIFACT_MANAGER.upload_aiotasks_map["wcc_1"].append(dictation_upload)
    dictation_waits: list[asyncio.Task[None]] = []
    save_budgets: list[asyncio.Timeout] = []
    save_budget_delays: list[float | None] = []

    def save_budget(delay: float | None) -> asyncio.Timeout:
        save_budget_delays.append(delay)
        save_budgets.append(asyncio.timeout(delay))
        return save_budgets[-1]

    async def refuse(artifact: Artifact, data: bytes) -> None:
        # A dictation request for the same chat starts waiting while this upload is in flight.
        dictation_waits.append(asyncio.create_task(app.ARTIFACT_MANAGER.wait_for_upload_aiotasks(["wcc_1"])))
        if storage_fault == "stalls":
            # Expire the save budget only once the upload is in flight; a short real budget let a slow
            # runner expire it before the upload started.
            save_budgets[-1].reschedule(asyncio.get_running_loop().time())
            await asyncio.Event().wait()
        raise RuntimeError("storage unavailable")

    monkeypatch.setattr(app.STORAGE, "store_artifact", refuse)
    monkeypatch.setattr(streaming_adapter_module, "asyncio", ScopedAsyncio(timeout=save_budget))

    def tool_results(sent: list[Any]) -> list[dict[str, Any]]:
        return [
            p.model_dump(exclude={"timestamp", "activity_started_at"})
            for p in sent
            if p.type == WorkflowCopilotStreamMessageType.TOOL_RESULT
        ]

    undisturbed = await _drive(_click_round_trip(), _test_copilot_context(workflow_copilot_chat_id="wcc_1"))
    ctx = _test_copilot_context(workflow_copilot_chat_id="wcc_1")
    assert _stage_frame(ctx)

    # Count only the saves made by the drive under test, not by the undisturbed drive above.
    save_budgets.clear()
    save_budget_delays.clear()
    try:
        sent = await _drive(_click_round_trip(), ctx)
        await asyncio.sleep(0)

        # Pins the production save budget, not just the constant's name.
        assert save_budget_delays == [5]
        assert [budget.expired() for budget in save_budgets] == [storage_fault == "stalls"]
        assert "screenshot" not in [p.type for p in sent]
        assert _saved_screenshots(ctx) is None
        assert len(tool_results(sent)) == 1
        assert tool_results(sent) == tool_results(undisturbed)
        every_upload = [u for uploads in app.ARTIFACT_MANAGER.upload_aiotasks_map.values() for u in uploads]
        assert [u for u in every_upload if not u.done()] == [dictation_upload]
        assert [wait.done() for wait in dictation_waits] == [False]
    finally:
        for task in [*dictation_waits, dictation_upload]:
            task.cancel()
        app.ARTIFACT_MANAGER.upload_aiotasks_map.pop("wcc_1", None)


@pytest.mark.asyncio
async def test_every_distinct_frame_one_tool_staged_reaches_the_chat_in_order(
    chat_screenshot_rows: list[dict[str, Any]],
) -> None:
    ctx = _test_copilot_context(workflow_copilot_chat_id="wcc_1")
    for color in ("white", "black", "red"):
        assert _stage_frame(ctx, color=color)

    sent = await _drive(_click_round_trip(), ctx)

    stored = [row["artifact_id"] for row in chat_screenshot_rows]
    assert len(stored) == 3
    assert [p.artifact_id for p in sent if p.type == "screenshot"] == stored
    assert [shot["artifactId"] for shot in _saved_screenshots(ctx) or []] == stored


@pytest.mark.asyncio
async def test_goal_satisfied_flush_stores_the_frame_its_tool_staged(
    chat_screenshot_rows: list[dict[str, Any]],
) -> None:
    ctx = _test_copilot_context(
        workflow_copilot_chat_id="wcc_1",
        in_flight_stream_tool_call=InFlightStreamToolCall(call_id="c9", tool_name="update_workflow", iteration=2),
        goal_satisfied_tool_output={"ok": True, "data": {"block_count": 1}},
    )
    _fixture_pending_identity(ctx)
    assert _stage_frame(ctx)
    sent: list[Any] = []

    await flush_goal_satisfied_tool_result(_sink_stream(sent), ctx)

    assert [p.type for p in sent] == ["screenshot", WorkflowCopilotStreamMessageType.TOOL_RESULT]
    assert [shot["artifactId"] for shot in _saved_screenshots(ctx) or []] == [chat_screenshot_rows[0]["artifact_id"]]
