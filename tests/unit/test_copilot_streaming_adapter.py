from __future__ import annotations

import asyncio
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

from skyvern.forge.sdk.copilot.streaming_adapter import _sanitize_input, stream_to_sse


def test_strips_workflow_yaml() -> None:
    result = _sanitize_input({"workflow_yaml": "title: x", "block_labels": ["a"]})
    assert "workflow_yaml" not in result
    assert result["block_labels"] == ["a"]


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

    ctx = SimpleNamespace()

    await stream_to_sse(result, stream, ctx)

    result.cancel.assert_not_called()
    stream.send.assert_not_called()


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

    ctx = SimpleNamespace()

    with pytest.raises(asyncio.CancelledError):
        await stream_to_sse(result, stream, ctx)

    result.cancel.assert_called_once()


@pytest.mark.asyncio
async def test_stream_to_sse_emits_narration_on_workflow_updated_transition(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """End-to-end: a completed update_workflow tool round-trip flips
    ctx.update_workflow_called, which should register as a workflow_updated
    transition on the narrator state and produce a NARRATION SSE payload in
    addition to the existing TOOL_CALL / TOOL_RESULT frames.
    """
    from agents.items import RunItem
    from agents.stream_events import RunItemStreamEvent

    from skyvern.forge.sdk.copilot import narration
    from skyvern.forge.sdk.copilot.context import CopilotContext
    from skyvern.forge.sdk.schemas.workflow_copilot import WorkflowCopilotStreamMessageType

    async def _handler(prompt: str, prompt_name: str, **kwargs: object) -> str:
        return "Revising the workflow draft."

    monkeypatch.setattr(narration, "_get_narrator_handler", lambda: _handler)

    # Tool input / output raw_items matching the shapes streaming_adapter reads.
    call_raw = {"call_id": "c-upd", "name": "update_workflow", "arguments": "{}"}
    call_item = MagicMock(spec=RunItem)
    call_item.raw_item = call_raw
    tool_call_event = RunItemStreamEvent(name="tool_called", item=call_item)

    # Shape the output so _update_enforcement_from_tool sets
    # update_workflow_called=True (needs ok=True + non-empty block_count).
    output_payload = [{"type": "text", "text": '{"ok": true, "data": {"block_count": 2}}'}]
    out_item = MagicMock(spec=RunItem)
    out_item.raw_item = {"call_id": "c-upd", "name": "update_workflow"}
    out_item.output = output_payload
    tool_output_event = RunItemStreamEvent(name="tool_output", item=out_item)

    # In production the SDK yields tool events slowly (LLM latency between
    # them), giving the background narration task ample time to finish before
    # stream_to_sse's finally cancels anything still in flight. Simulate that
    # by inserting explicit event-loop yields between synthetic events and
    # after the last one, so the narration task can run to completion under
    # the test's microsecond-fast loop.
    async def _slow_stream_events() -> Any:
        yield tool_call_event
        await asyncio.sleep(0)
        yield tool_output_event
        # Let the scheduled narration task complete before the async generator
        # exits and stream_to_sse's finally cancels any still-running task.
        for _ in range(10):
            await asyncio.sleep(0)

    result = MagicMock()
    result.stream_events = _slow_stream_events
    result.cancel = MagicMock()

    sent: list[Any] = []

    async def _send(payload: Any) -> bool:
        sent.append(payload)
        return True

    stream = MagicMock()
    stream.is_disconnected = AsyncMock(return_value=False)
    stream.send = _send

    ctx = CopilotContext(
        organization_id="org_test",
        workflow_id="wf_test",
        workflow_permanent_id="wpid_test",
        workflow_yaml="",
        browser_session_id=None,
        stream=None,  # type: ignore[arg-type]
        api_key=None,
        user_message="",
    )

    await stream_to_sse(result, stream, ctx)

    narration_payloads = [p for p in sent if getattr(p, "type", None) == WorkflowCopilotStreamMessageType.NARRATION]
    assert len(narration_payloads) == 1
    assert narration_payloads[0].narration == "Revising the workflow draft."
    # The tool round-trip also emitted TOOL_CALL + TOOL_RESULT.
    tool_types = [getattr(p, "type", None) for p in sent]
    assert WorkflowCopilotStreamMessageType.TOOL_CALL in tool_types
    assert WorkflowCopilotStreamMessageType.TOOL_RESULT in tool_types


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
