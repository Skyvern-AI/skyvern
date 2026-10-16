from __future__ import annotations

import asyncio
import json
import time
from datetime import UTC, datetime
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest
from agents import Agent, ModelSettings, RunContextWrapper, SQLiteSession, function_tool
from agents.exceptions import MaxTurnsExceeded
from agents.run_config import RunConfig
from litellm.exceptions import NotFoundError as LiteLLMNotFoundError
from litellm.exceptions import RateLimitError as LiteLLMRateLimitError
from litellm.exceptions import Timeout as LiteLLMTimeout

from skyvern.forge import app as forge_app
from skyvern.forge.sdk.copilot import agent as agent_module
from skyvern.forge.sdk.copilot import tools as tools_module
from skyvern.forge.sdk.copilot.agent import (
    FINAL_REPLY_UNAPPLIED_CHANGE_NOTE,
    _build_narrative_payload,
    _build_timeout_exit_result,
    _format_chat_history,
    _translate_to_agent_result,
)
from skyvern.forge.sdk.copilot.ask_user import QuestionInteraction, QuestionPart, QuestionResponse
from skyvern.forge.sdk.copilot.blocker_signal import CopilotToolBlockerSignal
from skyvern.forge.sdk.copilot.code_write_diff import CODE_WRITE_TOOL_NAMES
from skyvern.forge.sdk.copilot.config import CopilotConfig
from skyvern.forge.sdk.copilot.context import REPLY_TOOL_NAME, CopilotContext
from skyvern.forge.sdk.copilot.enforcement import (
    BUDGET_DRAIN_EXCLUDED_TOOLS,
    BUDGET_DRAIN_HEADROOM,
    EXPIRED_TURN_REPLY_SECONDS,
    HARD_BACKSTOP_ALLOWANCE_SECONDS,
    TOTAL_TIMEOUT_SECONDS,
    CopilotTotalTimeoutError,
    run_final_reply_drain,
    run_with_enforcement,
)
from skyvern.forge.sdk.copilot.hooks import CopilotRunHooks
from skyvern.forge.sdk.copilot.output_utils import extract_final_text, parse_final_response
from skyvern.forge.sdk.copilot.tools.run_execution import (
    _run_blocks_and_collect_debug,
    hydrate_prior_run_packet,
    recorded_open_step_runs,
)
from skyvern.forge.sdk.copilot.turn_outcome import CANCEL_TERMINAL_REASON
from skyvern.forge.sdk.schemas.copilot_turn_outcome import ResponseKind, TurnOutcome
from skyvern.forge.sdk.schemas.workflow_copilot import (
    WorkflowCopilotChatHistoryMessage,
    WorkflowCopilotChatRequest,
    WorkflowCopilotChatSender,
    WorkflowCopilotStreamMessageType,
)
from skyvern.forge.sdk.workflow.models.workflow import Workflow
from tests.unit.copilot_test_helpers import (
    HANDBACK_WORKFLOW_YAML,
    OWNED_RUN_ID,
    REPLY_ARGUMENTS,
    FakeCopilotStream,
    OwnedRunRow,
    ScriptedModel,
    install_run_blocks_harness,
    install_secret_carrying_run_registry,
    make_build_test_ctx,
    make_copilot_ctx,
    run_scripted_turn,
    scripted_call,
    scripted_text,
    settle_cancelled_tools,
    stub_copilot_agent_loop,
    tool_outputs_for,
)


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
            "staged draft remain available. Choose how to use these calls. This step offers no tools that change or "
            "run the workflow."
        ),
    }
    assert ctx.budget_expiry_state.drain_attempted is True
    assert ctx.budget_expiry_state.drain_fingerprint
    assert ctx.budget_expiry_state.report_produced is None


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
    prefix = "user question result: " if resolved else "user question request: "
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
async def test_expired_turn_reply_gets_its_own_allowance_past_the_hard_backstop(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    ctx = make_copilot_ctx()
    ctx.budget_expiry_state.source = "deadline"
    ctx.copilot_run_start_monotonic = time.monotonic() - 2 * (TOTAL_TIMEOUT_SECONDS + HARD_BACKSTOP_ALLOWANCE_SECONDS)
    allowances: list[float] = []

    async def record_allowance(_result: Any, _stream: Any, stream_ctx: Any) -> None:
        when = stream_ctx.model_stream_deadline.when()
        allowances.append(when - asyncio.get_running_loop().time())

    monkeypatch.setattr(
        "skyvern.forge.sdk.copilot.enforcement.Runner.run_streamed", lambda *_args, **_kwargs: _result("")
    )
    monkeypatch.setattr("skyvern.forge.sdk.copilot.streaming_adapter.stream_to_sse", record_allowance)

    await run_final_reply_drain(
        MagicMock(),
        ctx,
        MagicMock(),
        session=None,
        hooks=MagicMock(),
        run_config=RunConfig(model_settings=ModelSettings(), tracing_disabled=True),
    )

    [allowance] = allowances
    assert allowance > EXPIRED_TURN_REPLY_SECONDS - 5


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


@pytest.mark.asyncio
async def test_unknown_tool_on_the_last_allowed_turn_reaches_the_budget_drain_once() -> None:
    model = ScriptedModel(
        [[scripted_call("REPLY", {}, "call_made_up")], [scripted_call("reply", REPLY_ARGUMENTS, "call_reply")]]
    )
    result, ctx, _stream = await run_scripted_turn(model, max_turns=1)

    assert ctx.budget_expiry_state.drain_attempted
    assert len(model.inputs) == 2
    (unknown_result,) = tool_outputs_for(model.inputs[1], "call_made_up")
    assert "REPLY" in json.loads(unknown_result)["error"]
    assert parse_final_response(extract_final_text(result))["user_response"] == REPLY_ARGUMENTS["user_response"]


@pytest.mark.asyncio
async def test_unknown_tool_inside_the_budget_drain_gets_a_result_and_one_more_call() -> None:
    model = ScriptedModel(
        [
            [scripted_call("REPLY", {}, "call_first")],
            [scripted_call("REPLY", {}, "call_in_drain")],
            [scripted_call("reply", REPLY_ARGUMENTS, "call_reply")],
        ]
    )
    result, ctx, stream = await run_scripted_turn(model, max_turns=1)

    assert ctx.budget_expiry_state.drain_attempted
    assert len(model.inputs) == 3
    (drain_result,) = tool_outputs_for(model.inputs[2], "call_in_drain")
    assert json.loads(drain_result)["ok"] is False
    assert parse_final_response(extract_final_text(result))["user_response"] == REPLY_ARGUMENTS["user_response"]
    results = [f for f in stream.sent if f.type == WorkflowCopilotStreamMessageType.TOOL_RESULT]
    assert [(f.tool_call_id, f.success) for f in results] == [("call_first", False), ("call_in_drain", False)]


@pytest.mark.asyncio
async def test_unknown_tool_recovery_keeps_the_budget_drain_within_its_headroom() -> None:
    @function_tool
    async def probe() -> str:
        return json.dumps({"ok": True})

    model = ScriptedModel(
        [
            [scripted_call("REPLY", {}, "call_first")],
            [scripted_call("REPLY", {}, "call_in_drain")],
            *[[scripted_call("probe", {}, f"call_probe_{index}")] for index in range(BUDGET_DRAIN_HEADROOM - 1)],
            [scripted_call("reply", REPLY_ARGUMENTS, "call_past_headroom")],
        ]
    )

    with pytest.raises(MaxTurnsExceeded):
        await run_scripted_turn(model, tools=[probe], max_turns=1)

    assert len(model.inputs) == 1 + BUDGET_DRAIN_HEADROOM


_REPLACE_WORKFLOW_TEXT = json.dumps(
    {"type": "REPLACE_WORKFLOW", "user_response": "Saved a new draft.", "workflow_yaml": HANDBACK_WORKFLOW_YAML}
)


_DRAIN_KEPT_NATIVE_TOOLS = {
    "ask_user",
    "cancel_account_group",
    "delete_saved_credentials",
    "discover_workflow_entrypoint",
    "extend_browser_session",
    "fill_credential_field",
    "get_account_group_status",
    "get_organization_usage_quota",
    "get_run_results",
    "inspect_locator_matches",
    "inspect_page_for_composition",
    "list_credentials",
    "list_integrations",
    "read_google_sheet",
    "reply",
    "request_credential",
    "run_browser_code",
    "search_web",
    "set_work_plan",
    "solve_page_challenge",
    "start_fresh_browser",
    "upload_attached_file",
}


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("staged_before_expiry", "drain_script"),
    [
        (False, [scripted_call("update_workflow", {"workflow_yaml": "x"}, "call_stage"), REPLY_ARGUMENTS]),
        (True, [scripted_call("update_workflow", {"workflow_yaml": "x"}, "call_stage"), REPLY_ARGUMENTS]),
        (False, [scripted_text(_REPLACE_WORKFLOW_TEXT)]),
        (True, [scripted_text(_REPLACE_WORKFLOW_TEXT)]),
    ],
)
async def test_budget_drain_offers_no_tool_that_changes_or_runs_the_workflow(
    staged_before_expiry: bool, drain_script: list[Any]
) -> None:
    staged_writes: list[str] = []
    dispatched: list[list[str]] = []

    @function_tool
    async def probe() -> str:
        return json.dumps({"ok": True})

    @function_tool(name_override="update_workflow", strict_mode=False)
    async def stage_draft(ctx: RunContextWrapper[CopilotContext], workflow_yaml: str) -> str:
        staged_writes.append(workflow_yaml)
        ctx.context.staged_workflow = SimpleNamespace(workflow_id="wf_after_expiry")
        return json.dumps({"ok": True})

    @function_tool(name_override="run_blocks_and_collect_debug", strict_mode=False)
    async def dispatch_run(block_labels: list[str]) -> str:
        dispatched.append(block_labels)
        return json.dumps({"ok": True})

    stubbed = {stage_draft.name, dispatch_run.name, REPLY_TOOL_NAME}
    native_tools = [tool for tool in tools_module.NATIVE_TOOLS if tool.name not in stubbed]
    ctx = make_copilot_ctx(
        organization_id="org_test",
        workflow_permanent_id="wpid_test",
        workflow_id=None,
        workflow_yaml=None,
        stream=None,
        api_key="sk-copilot-org",
    )
    staged = SimpleNamespace(workflow_id="wf_draft", workflow_definition=SimpleNamespace(blocks=[]))
    if staged_before_expiry:
        ctx.staged_workflow = staged
    script = [
        [scripted_call("probe", {}, "call_probe")],
        *[[scripted_call("reply", item, "call_reply")] if item is REPLY_ARGUMENTS else [item] for item in drain_script],
    ]
    model = ScriptedModel(script)

    result, ctx, _stream = await run_scripted_turn(
        model,
        tools=[probe, stage_draft, dispatch_run, *native_tools],
        max_turns=1,
        ctx=ctx,
    )
    translated = await _translate_to_agent_result(result, ctx, None, _chat_request(), "org-1")

    first_call, *drain_calls = model.served_tool_names_by_call
    assert first_call - drain_calls[0] == first_call & BUDGET_DRAIN_EXCLUDED_TOOLS
    assert all(served == _DRAIN_KEPT_NATIVE_TOOLS | {"probe"} for served in drain_calls)
    assert CODE_WRITE_TOOL_NAMES <= BUDGET_DRAIN_EXCLUDED_TOOLS
    assert len(model.inputs) == len(script)
    for call_index, step in enumerate(drain_script[:-1], start=2):
        (not_run,) = tool_outputs_for(model.inputs[call_index], step.call_id)
        assert json.loads(not_run)["error"].startswith(f"No tool named {step.name} exists")
    assert staged_writes == []
    assert dispatched == []
    assert ctx.staged_workflow is (staged if staged_before_expiry else None)
    assert ctx.budget_expiry_state.staged_draft_id == ("wf_draft" if staged_before_expiry else None)
    assert translated.response_type == "REPLY"
    assert translated.turn_outcome is not None
    assert translated.turn_outcome.budget_expiry_report_produced is True
    assert translated.turn_outcome.budget_expiry_staged_draft_id == ("wf_draft" if staged_before_expiry else None)
    if drain_script[-1] is REPLY_ARGUMENTS:
        assert translated.user_response == REPLY_ARGUMENTS["user_response"]
    else:
        assert translated.updated_workflow is None
        assert translated.user_response.startswith("Saved a new draft.")
        assert FINAL_REPLY_UNAPPLIED_CHANGE_NOTE in translated.user_response


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("provider_error", "fallback_llm_key", "untested_retry"),
    [
        pytest.param(
            LiteLLMRateLimitError(message="rate limit", llm_provider="openai", model="m"),
            "SECONDARY",
            False,
            id="fallback_model",
        ),
        pytest.param(
            LiteLLMNotFoundError(message="missing model", llm_provider="azure", model="m"),
            None,
            True,
            id="model_lookup_retry",
        ),
    ],
)
async def test_provider_error_inside_the_budget_drain_never_reruns_with_write_tools(
    monkeypatch: pytest.MonkeyPatch,
    provider_error: Exception,
    fallback_llm_key: str | None,
    untested_retry: bool,
) -> None:
    offered: list[tuple[bool, set[str]]] = []
    contexts: list[CopilotContext] = []
    models: list[str] = []

    def run_streamed(agent: Agent, **kwargs: Any) -> MagicMock:
        ctx = kwargs["context"]
        contexts.append(ctx)
        models.append(str(agent.model))
        state = ctx.budget_expiry_state
        offered.append((state.drain_attempted, {tool.name for tool in agent.tools}))
        if state.drain_active:
            raise provider_error
        if not state.drain_attempted:
            raise MaxTurnsExceeded("cap")
        return _result(json.dumps({"type": "REPLY", "user_response": "Nothing was saved or tested this turn."}))

    surface_loop = agent_module._run_agent_loop_with_surface

    async def loop_with_untested_retry(**kwargs: Any) -> Any:
        return await surface_loop(**{**kwargs, "allow_untested_retry": untested_retry})

    stub_copilot_agent_loop(monkeypatch, run_with_enforcement)
    monkeypatch.setattr(agent_module, "_run_agent_loop_with_surface", loop_with_untested_retry)
    monkeypatch.setattr("skyvern.forge.sdk.copilot.enforcement.Runner.run_streamed", run_streamed)
    monkeypatch.setattr("skyvern.forge.sdk.copilot.streaming_adapter.stream_to_sse", AsyncMock(return_value=None))

    result = await agent_module.run_copilot_agent(
        stream=MagicMock(send=AsyncMock(return_value=True)),
        organization_id="org-1",
        chat_request=WorkflowCopilotChatRequest(
            message="build it",
            workflow_id="wf-1",
            workflow_permanent_id="wfp-1",
            workflow_copilot_chat_id="chat-1",
            workflow_yaml="",
        ),
        chat_history=[],
        global_llm_context=None,
        llm_api_handler=SimpleNamespace(llm_key="PRIMARY"),
        raw_secret_safety_handler=AsyncMock(
            return_value={"version": "1", "state": "clean", "handling": "none", "citations": []}
        ),
        api_key="sk-test",
        config=CopilotConfig(fallback_llm_key=fallback_llm_key),
    )

    (before_drain, *after_drain) = offered
    assert before_drain[0] is False and "update_workflow" in before_drain[1]
    assert after_drain and all(attempted for attempted, _ in after_drain)
    assert all(not served & {"update_workflow", "run_blocks_and_collect_debug"} for _, served in after_drain)
    assert after_drain[-1][1] == {REPLY_TOOL_NAME}
    assert (models[-1] != models[0]) is (fallback_llm_key is not None), models
    assert contexts[-1].budget_expiry_state.staged_draft_id is None
    assert result.response_type == "REPLY"
    assert result.user_response == "Nothing was saved or tested this turn."
    assert result.turn_outcome is not None
    assert result.turn_outcome.budget_expired is True
    assert result.turn_outcome.budget_expiry_staged_draft_id is None


async def _build_test_turn(
    monkeypatch: pytest.MonkeyPatch,
    *,
    dispatch_to_worker: bool,
    block_first_read: bool = False,
    on_chat_browser: bool = False,
) -> tuple[dict[str, Any], OwnedRunRow, CopilotContext, ScriptedModel]:
    harness = await install_run_blocks_harness(
        monkeypatch,
        workflow_yaml=HANDBACK_WORKFLOW_YAML,
        polled_status="running",
        dispatch_to_worker=dispatch_to_worker,
    )
    owned = OwnedRunRow(monkeypatch, block_first_read=block_first_read)
    monkeypatch.setattr(forge_app, "CACHE", MagicMock(get=AsyncMock(return_value=None)))
    monkeypatch.setattr("skyvern.forge.sdk.copilot.steer.STEER_POLL_SECONDS", 0.01)
    ctx = make_build_test_ctx(harness, monkeypatch, on_chat_browser=on_chat_browser, stream=FakeCopilotStream())
    ctx.workflow_copilot_chat_id = "chat-1"
    ctx.copilot_cancel_token = "cancel-token"
    model = ScriptedModel(
        [
            [scripted_call("run_blocks_and_collect_debug", {"block_labels": ["extract_heading"]}, "call_run")],
            [scripted_text("A model call after the build test was cancelled.")],
        ]
    )
    return harness, owned, ctx, model


@function_tool(name_override="run_blocks_and_collect_debug", strict_mode=False)
async def _build_test_tool(ctx: RunContextWrapper[CopilotContext], block_labels: list[str]) -> str:
    result = await _run_blocks_and_collect_debug({"block_labels": block_labels, "parameters": {}}, ctx.context)
    return json.dumps(result, default=str)


async def _run_build_test_turn(ctx: CopilotContext, model: ScriptedModel) -> None:
    session = SQLiteSession("build-test-turn")
    try:
        await run_with_enforcement(
            agent=Agent(
                name="build-test-turn",
                model=model,
                tools=[_build_test_tool, tools_module.reply_tool],
                tool_use_behavior=tools_module.reply_ends_turn,
            ),
            initial_input="Test the workflow.",
            ctx=ctx,
            stream=ctx.stream,
            session=session,
            hooks=CopilotRunHooks(ctx),
            run_config=RunConfig(tracing_disabled=True),
        )
    finally:
        session.close()


def _saved_left_run_ids(ctx: CopilotContext) -> list[str]:
    payload = _build_narrative_payload(ctx, terminal="response", terminal_message="", narrative_summary="")
    return payload.get("budgetExpiry", {}).get("leftRunIds", [])


def _expire_turn_soon(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("skyvern.forge.sdk.copilot.enforcement.TOTAL_TIMEOUT_SECONDS", 0.2)
    monkeypatch.setattr("skyvern.forge.sdk.copilot.enforcement.HARD_BACKSTOP_ALLOWANCE_SECONDS", 0.3)
    monkeypatch.setattr("skyvern.forge.sdk.copilot.enforcement.MIN_DEADLINE_REMAINING_SECONDS", 0.1)


def _make_prior_run_readable(monkeypatch: pytest.MonkeyPatch, workflow: Workflow) -> None:
    monkeypatch.setattr(forge_app.DATABASE.workflows, "get_workflow_for_workflow_run", AsyncMock(return_value=workflow))
    monkeypatch.setattr(forge_app.DATABASE.artifacts, "get_artifacts_for_run", AsyncMock(return_value=[]))
    monkeypatch.setattr(forge_app.DATABASE.self_heal, "get_heal_episodes_for_run", AsyncMock(return_value=[]))


@pytest.mark.asyncio
@pytest.mark.parametrize("worker_outcome", ["completed", "failed", None])
async def test_hard_backstop_leaves_the_active_dispatched_test_to_its_worker(
    monkeypatch: pytest.MonkeyPatch, worker_outcome: str | None
) -> None:
    harness, owned, ctx, model = await _build_test_turn(monkeypatch, dispatch_to_worker=True)
    _expire_turn_soon(monkeypatch)

    with pytest.raises(CopilotTotalTimeoutError):
        await _run_build_test_turn(ctx, model)
    left_run_ids = _saved_left_run_ids(ctx)
    reported = await recorded_open_step_runs(ctx)
    await settle_cancelled_tools()
    if worker_outcome is not None:
        owned.finish(worker_outcome)

    assert left_run_ids == [OWNED_RUN_ID]
    assert [(run.workflow_run_id, run.left_running) for run in reported] == [(OWNED_RUN_ID, True)]

    assert ctx.budget_expiry_state.hard_backstop_reached
    assert owned.polled.is_set()
    assert owned.cancel_writes == []
    assert len(model.inputs) == 1
    assert ctx.dispatched_run_ids_this_turn == {OWNED_RUN_ID}
    owned.draft_deletes.assert_not_awaited()
    harness["clear"].assert_not_awaited()
    _make_prior_run_readable(monkeypatch, harness["workflow"])
    packet = await hydrate_prior_run_packet(make_copilot_ctx(), workflow_run_id=OWNED_RUN_ID)
    assert packet is not None
    assert (packet["run"]["workflow_run_id"], packet["run"]["status"]) == (
        OWNED_RUN_ID,
        worker_outcome or "running",
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("run", ["on_chat_browser", "owes_secrets", "carries_registered_secrets"])
async def test_hard_backstop_still_cancels_a_test_it_cannot_leave_running(
    monkeypatch: pytest.MonkeyPatch, run: str
) -> None:
    harness, owned, ctx, model = await _build_test_turn(
        monkeypatch, dispatch_to_worker=True, on_chat_browser=run == "on_chat_browser"
    )
    if run != "on_chat_browser":
        install_secret_carrying_run_registry(monkeypatch, all_registered=run == "carries_registered_secrets")
    _expire_turn_soon(monkeypatch)

    with pytest.raises(CopilotTotalTimeoutError):
        await _run_build_test_turn(ctx, model)
    left_run_ids = _saved_left_run_ids(ctx)
    reported = await recorded_open_step_runs(ctx)
    await settle_cancelled_tools()

    assert left_run_ids == []
    assert [(run.workflow_run_id, run.left_running) for run in reported] == [(OWNED_RUN_ID, False)]
    assert ctx.budget_expiry_state.hard_backstop_reached
    assert owned.cancel_writes == [OWNED_RUN_ID]
    assert owned.row.status == "canceled"
    assert len(model.inputs) == 1
    assert harness["clear"].await_count == (0 if run == "on_chat_browser" else 1)


@pytest.mark.asyncio
@pytest.mark.parametrize("dispatch_to_worker", [True, False])
@pytest.mark.parametrize("elapsed_at_stop", [300.0, 950.0])
@pytest.mark.parametrize("block_first_read", [False, True])
async def test_stop_still_cancels_the_active_build_test(
    monkeypatch: pytest.MonkeyPatch, dispatch_to_worker: bool, elapsed_at_stop: float, block_first_read: bool
) -> None:
    harness, owned, ctx, model = await _build_test_turn(
        monkeypatch, dispatch_to_worker=dispatch_to_worker, block_first_read=block_first_read
    )
    monkeypatch.setattr("skyvern.forge.sdk.copilot.enforcement._elapsed_run_seconds", lambda *_: elapsed_at_stop)

    turn = asyncio.create_task(_run_build_test_turn(ctx, model))
    await asyncio.wait_for(owned.polled.wait(), timeout=5)
    turn.cancel()
    with pytest.raises(asyncio.CancelledError):
        await turn
    left_run_ids = _saved_left_run_ids(ctx)
    saved = agent_module._build_cancelled_exit_result(ctx, None)
    await settle_cancelled_tools()

    assert left_run_ids == []
    assert saved.cancelled is True
    assert saved.turn_outcome is not None
    assert saved.turn_outcome.terminal_reason == CANCEL_TERMINAL_REASON
    assert not saved.turn_outcome.budget_expired
    assert owned.cancel_writes == [OWNED_RUN_ID]
    assert owned.row.status == "canceled"
    assert harness["executor_cancelled"] is not dispatch_to_worker
    harness["clear"].assert_awaited_once()


@pytest.mark.asyncio
@pytest.mark.parametrize("seconds_at_cut", [300.0, TOTAL_TIMEOUT_SECONDS + 50.0])
async def test_a_model_call_cut_mid_turn_leaves_the_fallback_on_the_turns_clock(
    monkeypatch: pytest.MonkeyPatch, seconds_at_cut: float
) -> None:
    offset = 0.0
    attempts: list[dict[str, Any]] = []

    def run_streamed(agent: Agent, **_kwargs: Any) -> MagicMock:
        attempts.append({"model": str(agent.model), "tools": {tool.name for tool in agent.tools}})
        return _result(json.dumps({"type": "REPLY", "user_response": "Here is where things stand."}))

    async def stream_to_sse(_result: Any, _stream: Any, ctx: Any) -> None:
        nonlocal offset
        attempts[-1]["drained"] = ctx.budget_expiry_state.drain_active
        attempts[-1]["allowance"] = ctx.model_stream_deadline.when() - asyncio.get_running_loop().time()
        if len(attempts) == 1:
            offset = seconds_at_cut
            raise LiteLLMTimeout("Copilot model stream made no progress", model="m", llm_provider="anthropic")

    stub_copilot_agent_loop(monkeypatch, run_with_enforcement)
    monkeypatch.setattr(
        "skyvern.forge.sdk.copilot.enforcement.time", SimpleNamespace(monotonic=lambda: time.monotonic() + offset)
    )
    monkeypatch.setattr("skyvern.forge.sdk.copilot.enforcement.Runner.run_streamed", run_streamed)
    monkeypatch.setattr("skyvern.forge.sdk.copilot.streaming_adapter.stream_to_sse", stream_to_sse)

    result = await agent_module.run_copilot_agent(
        stream=MagicMock(send=AsyncMock(return_value=True)),
        organization_id="org-1",
        chat_request=WorkflowCopilotChatRequest(
            message="build it",
            workflow_id="wf-1",
            workflow_permanent_id="wfp-1",
            workflow_copilot_chat_id="chat-1",
            workflow_yaml="",
        ),
        chat_history=[],
        global_llm_context=None,
        llm_api_handler=SimpleNamespace(llm_key="PRIMARY"),
        raw_secret_safety_handler=AsyncMock(
            return_value={"version": "1", "state": "clean", "handling": "none", "citations": []}
        ),
        api_key="sk-test",
        config=CopilotConfig(fallback_llm_key="SECONDARY"),
    )

    budget_spent = seconds_at_cut >= TOTAL_TIMEOUT_SECONDS
    (_, fallback) = attempts
    assert fallback["model"] == "model-SECONDARY"
    assert fallback["drained"] is budget_spent
    assert ("update_workflow" in fallback["tools"]) is not budget_spent
    assert fallback["allowance"] < TOTAL_TIMEOUT_SECONDS + HARD_BACKSTOP_ALLOWANCE_SECONDS - seconds_at_cut + 5
    assert result.user_response == "Here is where things stand."
    assert result.turn_outcome is not None
    assert result.turn_outcome.budget_expired is budget_spent
