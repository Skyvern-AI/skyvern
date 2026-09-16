from __future__ import annotations

import json
from datetime import UTC, datetime
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest
from agents.exceptions import MaxTurnsExceeded

from skyvern.forge.sdk.copilot import tools as tools_module
from skyvern.forge.sdk.copilot.agent import (
    _build_timeout_exit_result,
    _format_chat_history,
    _translate_to_agent_result,
)
from skyvern.forge.sdk.copilot.ask_user import QuestionInteraction, QuestionPart, QuestionResponse
from skyvern.forge.sdk.copilot.blocker_signal import CopilotToolBlockerSignal
from skyvern.forge.sdk.copilot.enforcement import run_with_enforcement
from skyvern.forge.sdk.copilot.tools.run_execution import _run_blocks_and_collect_debug
from skyvern.forge.sdk.schemas.copilot_turn_outcome import ResponseKind, TurnOutcome
from skyvern.forge.sdk.schemas.workflow_copilot import (
    WorkflowCopilotChatHistoryMessage,
    WorkflowCopilotChatSender,
)
from tests.unit.copilot_test_helpers import make_copilot_ctx


def _result(final_output: str) -> MagicMock:
    result = MagicMock()
    result.final_output = final_output
    result.new_items = []
    result.raw_responses = []
    result.to_input_list.return_value = []
    return result


@pytest.mark.asyncio
async def test_timeout_finishes_active_call_then_drains_once_in_same_session(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr("skyvern.forge.sdk.copilot.enforcement.TOTAL_TIMEOUT_SECONDS", 0)
    first = _result('{"type":"REPLY","user_response":"unfinished"}')
    report = _result('{"type":"REPLY","user_response":"Saved the draft."}')
    calls: list[dict[str, Any]] = []

    def run_streamed(*_args: Any, **kwargs: Any) -> MagicMock:
        calls.append(kwargs)
        return first if len(calls) == 1 else report

    monkeypatch.setattr("skyvern.forge.sdk.copilot.enforcement.Runner.run_streamed", run_streamed)
    monkeypatch.setattr(
        "skyvern.forge.sdk.copilot.streaming_adapter.stream_to_sse",
        AsyncMock(return_value=None),
    )
    session = object()
    ctx = make_copilot_ctx()

    returned = await run_with_enforcement(
        agent=MagicMock(),
        initial_input="build it",
        ctx=ctx,
        stream=MagicMock(),
        session=session,
    )

    assert returned is report
    assert len(calls) == 2
    assert calls[0]["session"] is session
    assert calls[1]["session"] is session
    assert calls[1]["max_turns"] > 0
    observation = json.loads(calls[1]["input"])
    assert observation == {
        "budget_expired": True,
        "source": "deadline",
        "headroom": calls[1]["max_turns"],
        "staged_draft": None,
        "message": (
            "The normal turn budget has expired. headroom is the maximum number of model calls available to finish "
            "this turn, including this call; the hard time limit may stop it sooner. Your session history and any "
            "staged draft remain available. Choose how to use these calls. New build-test runs cannot be dispatched."
        ),
    }
    assert ctx.budget_expiry_state.drain_attempted is True
    assert ctx.budget_expiry_state.drain_fingerprint
    assert ctx.budget_expiry_state.report_produced is None


@pytest.mark.asyncio
async def test_drain_denies_new_run_before_dispatch(monkeypatch: pytest.MonkeyPatch) -> None:
    ctx = make_copilot_ctx()
    ctx.budget_expiry_state.source = "max_turns"
    ctx.budget_expiry_state.drain_active = True
    database_lookup = AsyncMock()
    monkeypatch.setattr(
        "skyvern.forge.sdk.copilot.tools.run_execution.app.DATABASE.workflows.get_workflow_by_permanent_id",
        database_lookup,
    )

    result = await _run_blocks_and_collect_debug({"block_labels": ["step"]}, ctx)

    assert result == {
        "ok": False,
        "data": {
            "budget_expired": True,
            "run_dispatched": False,
            "source": "max_turns",
        },
    }
    database_lookup.assert_not_awaited()


@pytest.mark.asyncio
async def test_composite_tool_stages_edit_before_drain_denies_run(monkeypatch: pytest.MonkeyPatch) -> None:
    from skyvern.forge.sdk.routes.workflow_copilot import _process_workflow_yaml

    monkeypatch.setattr(
        "skyvern.forge.sdk.copilot.workflow_yaml.app.WORKFLOW_SERVICE.get_workflow_by_permanent_id",
        AsyncMock(return_value=None),
    )
    workflow_yaml = """
title: Drain draft
workflow_definition:
  parameters: []
  blocks:
    - block_type: navigation
      label: staged_step
      navigation_goal: Open the staged page.
"""
    ctx = make_copilot_ctx()
    ctx.budget_expiry_state.source = "deadline"
    ctx.budget_expiry_state.drain_active = True

    async def fake_update_workflow(payload: dict[str, Any], copilot_ctx: Any, **_kwargs: Any) -> dict[str, Any]:
        copilot_ctx.workflow_yaml = payload["workflow_yaml"]
        workflow = await _process_workflow_yaml(
            settings_fallback_yaml="",
            workflow_id=copilot_ctx.workflow_id,
            workflow_permanent_id=copilot_ctx.workflow_permanent_id,
            organization_id=copilot_ctx.organization_id,
            workflow_yaml=payload["workflow_yaml"],
        )
        copilot_ctx.staged_workflow = workflow
        copilot_ctx.staged_workflow_yaml = payload["workflow_yaml"]
        copilot_ctx.has_staged_proposal = True
        return {"ok": True, "_workflow": workflow, "data": {"block_count": 1}}

    monkeypatch.setattr(tools_module, "_update_and_run_requires_skipped_run", lambda *_args: False)
    monkeypatch.setattr(tools_module, "_authority_tool_error", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(tools_module, "_get_prior_workflow_definition", AsyncMock(return_value=None))
    monkeypatch.setattr(tools_module, "_update_workflow", fake_update_workflow)
    monkeypatch.setattr(tools_module, "_frontier_runtime_page_url", AsyncMock(return_value=None))
    monkeypatch.setattr(
        tools_module,
        "_plan_frontier",
        lambda *_args: (["staged_step"], {}, "staged_step", "initial"),
    )
    monkeypatch.setattr(tools_module, "_verify_and_record_run_blocks_result", AsyncMock(return_value=None))
    monkeypatch.setattr(tools_module, "_carry_unresolved_failure_into_result", lambda *_args: None)
    monkeypatch.setattr(tools_module, "record_tool_step_result_for_ctx", lambda *_args: None)
    monkeypatch.setattr(tools_module, "finalize_build_test_result", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(tools_module, "enqueue_screenshot_from_result", lambda *_args, **_kwargs: None)

    result = await tools_module.update_and_run_blocks_tool.on_invoke_tool(
        SimpleNamespace(context=ctx, tool_name="update_and_run_blocks"),
        json.dumps({"workflow_yaml": workflow_yaml, "block_labels": ["staged_step"], "parameters": {}}),
    )

    assert ctx.staged_workflow_yaml == workflow_yaml
    assert ctx.has_staged_proposal is True
    assert json.loads(result) == {
        "ok": False,
        "data": {
            "budget_expired": True,
            "run_dispatched": False,
            "source": "deadline",
        },
    }


@pytest.mark.parametrize("resolved", [False, True])
def test_prior_expiry_and_question_are_serialized_into_next_model_history(resolved: bool) -> None:
    interaction = QuestionInteraction(
        interaction_id="question-1",
        turn_id="turn-1",
        tool_call_id="call-1",
        parts=[QuestionPart(part_id="part-1", prompt="Which report?")],
        status="resolved" if resolved else "pending",
        response=QuestionResponse(text="The weekly report.") if resolved else None,
    )
    outcome = TurnOutcome(
        response_kind=ResponseKind.BUILD,
        terminal_reason="timeout",
        budget_expired=True,
        budget_expiry_source="deadline",
        budget_expiry_report_produced=True,
        budget_expiry_staged_draft_id="wf_draft",
        drain_fingerprint="drain-1",
    )
    history = _format_chat_history(
        [
            WorkflowCopilotChatHistoryMessage(
                sender=WorkflowCopilotChatSender.AI,
                content="I saved the draft.",
                narrative_payload={
                    "turnId": "turn-1",
                    "turnIndex": 0,
                    "questionInteractions": [interaction.model_dump(mode="json")],
                    "designStarted": False,
                    "designEnded": False,
                    "draft": None,
                    "blocks": [],
                    "terminal": "complete",
                    "terminalMessage": None,
                    "narrativeSummary": None,
                    "priorBlockCount": None,
                    "designActivity": [],
                    "startedAt": None,
                    "endedAt": None,
                },
                turn_outcome=outcome,
                created_at=datetime.now(UTC),
            )
        ]
    )

    question_line, message_line, _ = history.splitlines()
    prefix = "ask_user result: " if resolved else "ask_user request: "
    expected = interaction.tool_result() if resolved else interaction.model_dump(mode="json")
    assert json.loads(question_line.removeprefix(prefix)) == expected
    assert message_line == "ai: I saved the draft."
    typed_line = next(line for line in history.splitlines() if line.startswith("turn_outcome: "))
    assert json.loads(typed_line.removeprefix("turn_outcome: ")) == {
        "budget_expired": True,
        "source": "deadline",
        "report_produced": True,
        "staged_draft": "wf_draft",
        "drain_fingerprint": "drain-1",
    }


@pytest.mark.asyncio
async def test_max_turns_gets_one_drain_and_second_cap_escapes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[dict[str, Any]] = []

    def run_streamed(*_args: Any, **kwargs: Any) -> MagicMock:
        calls.append(kwargs)
        raise MaxTurnsExceeded("cap")

    monkeypatch.setattr("skyvern.forge.sdk.copilot.enforcement.Runner.run_streamed", run_streamed)
    ctx = make_copilot_ctx()
    session = object()

    with pytest.raises(MaxTurnsExceeded):
        await run_with_enforcement(
            agent=MagicMock(),
            initial_input="build it",
            ctx=ctx,
            stream=MagicMock(),
            session=session,
        )

    assert len(calls) == 2
    assert calls[1]["session"] is session
    assert ctx.budget_expiry_state.source == "max_turns"
    assert ctx.budget_expiry_state.drain_attempted is True
    assert ctx.budget_expiry_state.drain_active is False


def _chat_request() -> SimpleNamespace:
    return SimpleNamespace(
        workflow_id="wf-1",
        workflow_permanent_id="wfp-1",
        workflow_copilot_chat_id="chat-1",
        workflow_yaml="",
        product_action=None,
    )


@pytest.mark.asyncio
async def test_drain_translation_keeps_safe_authored_text_unchanged(monkeypatch: pytest.MonkeyPatch) -> None:
    ctx = make_copilot_ctx()
    ctx.budget_expiry_state.source = "deadline"
    ctx.budget_expiry_state.drain_attempted = True
    ctx.budget_expiry_state.drain_fingerprint = "drain-1"
    ctx.staged_workflow = SimpleNamespace(
        workflow_id="wf_draft",
        workflow_definition=SimpleNamespace(blocks=[]),
    )
    ctx.blocker_signal = CopilotToolBlockerSignal(
        blocker_kind="authority_denied",
        agent_steering_text="Report the blocker without another run.",
        user_facing_reason="A backend-owned fallback must not replace the drain report.",
        recovery_hint="report_blocker_to_user",
        internal_reason_code="budget_drain_test",
        blocked_tool="run_workflow",
        classifier_mode="build",
    )
    authored = "I kept the useful draft and stopped before another run."
    unresolved_detector = MagicMock(side_effect=AssertionError("budget drain must not append backend prose"))
    monkeypatch.setattr(
        "skyvern.forge.sdk.copilot.agent.unresolved_runtime_block_failure_with_disposition",
        unresolved_detector,
    )

    translated = await _translate_to_agent_result(
        _result(json.dumps({"type": "REPLY", "user_response": authored})),
        ctx,
        None,
        _chat_request(),
        "org-1",
    )

    assert translated.user_response == authored
    assert translated.turn_outcome is not None
    assert translated.turn_outcome.terminal_reason == "timeout"
    assert translated.turn_outcome.drain_fingerprint == "drain-1"
    assert translated.turn_outcome.budget_expiry_report_produced is True
    assert translated.turn_outcome.budget_expiry_staged_draft_id == "wf_draft"
    assert translated.narrative_payload is not None
    assert translated.narrative_payload["budgetExpiry"]["stagedDraftId"] == "wf_draft"
    unresolved_detector.assert_not_called()


@pytest.mark.asyncio
async def test_empty_drain_translation_persists_no_report_without_fallback() -> None:
    ctx = make_copilot_ctx()
    ctx.budget_expiry_state.source = "max_turns"
    ctx.budget_expiry_state.drain_attempted = True

    translated = await _translate_to_agent_result(
        _result(""),
        ctx,
        None,
        _chat_request(),
        "org-1",
    )

    assert translated.user_response == ""
    assert translated.turn_outcome is not None
    assert translated.turn_outcome.terminal_reason == "max_turns"
    assert translated.turn_outcome.budget_expiry_report_produced is False


def test_hard_backstop_preserves_staged_proposal_without_fallback() -> None:
    ctx = make_copilot_ctx()
    staged = SimpleNamespace(
        workflow_id="wf_draft",
        workflow_definition=SimpleNamespace(blocks=[]),
    )
    ctx.staged_workflow = staged
    ctx.staged_workflow_yaml = "workflow_definition:\n  blocks: []\n"
    ctx.has_staged_proposal = True

    result = _build_timeout_exit_result(ctx, global_llm_context=None)

    assert result.user_response == ""
    assert result.updated_workflow is staged
    assert result.proposal_disposition == "review_untested"
    assert result.turn_outcome is not None
    assert result.turn_outcome.budget_expiry_staged_draft_id == "wf_draft"


@pytest.mark.asyncio
@pytest.mark.parametrize("expiry_boundary", ["model", "tool"])
async def test_sdk_soft_deadline_preserves_pending_tool_and_drains_before_next_model(
    monkeypatch: pytest.MonkeyPatch, expiry_boundary: str
) -> None:
    from agents import Agent, Model, RunConfig, SQLiteSession, function_tool
    from agents.items import ModelResponse
    from openai.types.responses import (
        Response,
        ResponseCompletedEvent,
        ResponseFunctionToolCall,
        ResponseOutputMessage,
        ResponseOutputText,
    )

    from skyvern.forge.sdk.copilot.hooks import CopilotRunHooks

    elapsed = 0.0
    monkeypatch.setattr("skyvern.forge.sdk.copilot.enforcement.TOTAL_TIMEOUT_SECONDS", 10)
    monkeypatch.setattr("skyvern.forge.sdk.copilot.enforcement._elapsed_run_seconds", lambda *_: elapsed)
    ctx = make_copilot_ctx()
    tool_completed = False
    model_inputs: list[list[Any]] = []
    drain_at_model_call: list[bool] = []

    @function_tool
    async def finish_active_work() -> str:
        nonlocal elapsed, tool_completed
        if expiry_boundary == "tool":
            elapsed = 11.0
        tool_completed = True
        return '{"ok":true,"data":{"retained":"finished-active-work"}}'

    class BoundaryModel(Model):
        async def get_response(self, *args: Any, **kwargs: Any) -> ModelResponse:
            raise AssertionError("The production runner must stream")

        async def stream_response(self, *args: Any, **kwargs: Any):
            nonlocal elapsed
            model_inputs.append(kwargs["input"] if "input" in kwargs else args[1])
            drain_at_model_call.append(ctx.budget_expiry_state.drain_active)
            if len(model_inputs) == 1:
                if expiry_boundary == "model":
                    elapsed = 11.0
                output = [
                    ResponseFunctionToolCall(
                        type="function_call", name="finish_active_work", call_id="call_active", arguments="{}"
                    )
                ]
            else:
                output = [
                    ResponseOutputMessage(
                        id="msg_report",
                        type="message",
                        role="assistant",
                        status="completed",
                        content=[ResponseOutputText(type="output_text", text="Saved the draft.", annotations=[])],
                    )
                ]
            response = Response(
                id=f"resp_{len(model_inputs)}",
                created_at=0.0,
                model="boundary-test",
                object="response",
                output=output,
                parallel_tool_calls=True,
                tool_choice="auto",
                tools=[],
                status="completed",
            )
            yield ResponseCompletedEvent(response=response, sequence_number=0, type="response.completed")

    async def consume_sdk_stream(result: Any, *_args: Any) -> None:
        async for _event in result.stream_events():
            pass

    monkeypatch.setattr("skyvern.forge.sdk.copilot.streaming_adapter.stream_to_sse", consume_sdk_stream)
    session = SQLiteSession("deadline-boundary")
    try:
        result = await run_with_enforcement(
            agent=Agent(name="boundary-test", model=BoundaryModel(), tools=[finish_active_work]),
            initial_input="build it",
            ctx=ctx,
            stream=MagicMock(),
            session=session,
            hooks=CopilotRunHooks(ctx),
            run_config=RunConfig(tracing_disabled=True),
        )
        assert tool_completed
        assert drain_at_model_call == [False, True]
        assert result.final_output == "Saved the draft."
        drain_input = model_inputs[1]
        assert any(
            item.get("type") == "function_call_output" and "finished-active-work" in item["output"]
            for item in drain_input
        )
        assert any(
            item.get("role") == "user" and '"budget_expired":true' in item.get("content", "").replace(" ", "")
            for item in drain_input
        )
        persisted = await session.get_items()
        assert any(item.get("type") == "function_call_output" for item in persisted)
        assert ctx.budget_expiry_state.drain_attempted
    finally:
        session.close()


@pytest.mark.asyncio
async def test_sdk_overlapping_human_waits_resume_with_both_answers_without_expiry(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import asyncio

    from agents import Agent, Model, RunConfig, SQLiteSession, function_tool
    from agents.items import ModelResponse
    from openai.types.responses import (
        Response,
        ResponseCompletedEvent,
        ResponseFunctionToolCall,
        ResponseOutputMessage,
        ResponseOutputText,
    )

    from skyvern.forge.sdk.copilot.enforcement import _elapsed_run_seconds
    from skyvern.forge.sdk.copilot.hooks import CopilotRunHooks
    from skyvern.forge.sdk.copilot.human_input_wait import pause_human_input

    now = 0.0
    clock = SimpleNamespace(monotonic=lambda: now)
    monkeypatch.setattr("skyvern.forge.sdk.copilot.enforcement.time", clock)
    monkeypatch.setattr("skyvern.forge.sdk.copilot.human_input_wait.time", clock)
    monkeypatch.setattr("skyvern.forge.sdk.copilot.enforcement.TOTAL_TIMEOUT_SECONDS", 10)
    ctx = make_copilot_ctx()
    second_wait_started = asyncio.Event()
    first_hook_completed = asyncio.Event()
    model_inputs: list[list[Any]] = []
    drain_at_model_call: list[bool] = []

    @function_tool
    async def ask_human(question: int) -> str:
        nonlocal now
        with pause_human_input(ctx, "question"):
            if question == 1:
                await second_wait_started.wait()
                now = 11.0
            else:
                second_wait_started.set()
                await first_hook_completed.wait()
                now = 12.0
        return f"answer-{question}"

    class OverlapHooks(CopilotRunHooks):
        async def on_tool_end(self, context: Any, agent: Any, tool: Any, result: Any) -> None:
            await super().on_tool_end(context, agent, tool, result)
            if result == "answer-1":
                assert ctx.human_input_wait.count == 1
                first_hook_completed.set()

    class OverlapModel(Model):
        async def get_response(self, *args: Any, **kwargs: Any) -> ModelResponse:
            raise AssertionError("The production runner must stream")

        async def stream_response(self, *args: Any, **kwargs: Any):
            model_inputs.append(kwargs["input"] if "input" in kwargs else args[1])
            drain_at_model_call.append(ctx.budget_expiry_state.drain_active)
            if len(model_inputs) == 1:
                output = [
                    ResponseFunctionToolCall(
                        type="function_call",
                        name="ask_human",
                        call_id=f"call_{question}",
                        arguments=json.dumps({"question": question}),
                    )
                    for question in (1, 2)
                ]
            else:
                output = [
                    ResponseOutputMessage(
                        id="msg_answers",
                        type="message",
                        role="assistant",
                        status="completed",
                        content=[
                            ResponseOutputText(
                                type="output_text",
                                annotations=[],
                                text='{"type":"REPLY","user_response":"Both answered."}',
                            )
                        ],
                    )
                ]
            yield ResponseCompletedEvent(
                sequence_number=0,
                type="response.completed",
                response=Response(
                    id=f"resp_{len(model_inputs)}",
                    created_at=0.0,
                    model="overlap-test",
                    object="response",
                    output=output,
                    parallel_tool_calls=True,
                    tool_choice="auto",
                    tools=[],
                    status="completed",
                ),
            )

    async def consume_sdk_stream(result: Any, *_args: Any) -> None:
        async for _event in result.stream_events():
            pass

    monkeypatch.setattr("skyvern.forge.sdk.copilot.streaming_adapter.stream_to_sse", consume_sdk_stream)
    session = SQLiteSession("overlapping-human-waits")
    try:
        result = await run_with_enforcement(
            agent=Agent(name="overlap-test", model=OverlapModel(), tools=[ask_human]),
            initial_input="Ask both questions",
            ctx=ctx,
            stream=MagicMock(),
            session=session,
            hooks=OverlapHooks(ctx),
            run_config=RunConfig(tracing_disabled=True),
        )
        assert drain_at_model_call == [False, False]
        assert json.loads(result.final_output)["user_response"] == "Both answered."
        assert {item["output"] for item in model_inputs[1] if item.get("type") == "function_call_output"} == {
            "answer-1",
            "answer-2",
        }
        assert ctx.copilot_question_pause_seconds == 12.0
        assert ctx.human_input_wait.count == 0
        assert _elapsed_run_seconds(ctx, 0.0) == 0.0
        assert not ctx.budget_expiry_state.drain_attempted
    finally:
        session.close()
