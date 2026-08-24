"""Unit tests for the Task V3 agent tool-loop.

A scripted fake ``LLMCaller`` emits queued tool_calls (in the same dict shape
``LLMCaller.call(raw_response=True)`` returns) so we can assert the loop's
behavior — on-demand perception, action batching, terminal finish, budget caps,
and error handling — without any real LLM or browser.
"""

from __future__ import annotations

import asyncio
import copy
import hashlib
import json
import random
import time
from typing import Any

import pytest
from structlog.testing import capture_logs

from skyvern.exceptions import SkyvernContextWindowExceededError
from skyvern.forge.sdk.api.llm.exceptions import LLMProviderErrorRetryableTask
from skyvern.forge.sdk.core import skyvern_context
from skyvern.forge.sdk.core.skyvern_context import SkyvernContext
from skyvern.forge.taskv3.loop import (
    FAILURE_EVIDENCE_MIN_TOOL_CALLS,
    FAILURE_EVIDENCE_MIN_TURNS,
    NO_TOOL_CALL_NUDGE,
    PERCEPTION_STALL_NUDGE_AFTER,
    PERCEPTION_STALL_REASON_PREFIX,
    PERCEPTION_STALL_SHADOW_EVENT,
    PERCEPTION_STALL_SUPPRESSED_EVENT,
    PERCEPTION_STALL_TERMINATE_AFTER,
    ActivityRecency,
    LoopOutcome,
    SubmitWatch,
    ToolHandler,
    ToolResult,
    ToolSpec,
    _canonical_perception_content,
    _PerceptionLedger,
    make_finish_tool,
    run_agent_tool_loop,
)


class _ScriptedCaller:
    """Emits one queued turn per ``call``. Each turn is a list of (tool_name, args)."""

    def __init__(self, script: list[list[tuple[str, dict[str, Any]]]]) -> None:
        self._script = script
        self.calls = 0
        self.message_history: list[dict[str, Any]] = []
        self.sent_tools: list[dict[str, Any]] | None = None

    def supports_tool_choice(self) -> bool:
        return True

    async def call(
        self,
        *,
        prompt: str | None = None,
        prompt_name: str | None = None,
        organization_id: str | None = None,
        tools: list[dict[str, Any]] | None = None,
        use_message_history: bool = False,
        raw_response: bool = False,
        tool_choice: str | None = None,
    ) -> dict[str, Any]:
        self.sent_tools = tools
        turn = self._script[self.calls] if self.calls < len(self._script) else []
        self.calls += 1
        message: dict[str, Any] = {"content": "reasoning..."}
        if turn:
            message["tool_calls"] = [
                {"id": f"call_{i}", "type": "function", "function": {"name": name, "arguments": json.dumps(args)}}
                for i, (name, args) in enumerate(turn)
            ]
        return {"choices": [{"message": message}], "usage": {"prompt_tokens": 10, "completion_tokens": 5}}


def _recording_tool(name: str, sink: list[tuple[str, dict[str, Any]]], *, raises: bool = False) -> ToolSpec:
    async def handler(args: dict[str, Any]) -> ToolResult:
        sink.append((name, args))
        if raises:
            raise RuntimeError("boom")
        return ToolResult.ok(f"{name} done")

    return ToolSpec(name=name, description=name, parameters={"type": "object", "properties": {}}, handler=handler)


async def _run(script: list[list[tuple[str, dict[str, Any]]]], tools: list[ToolSpec], **kwargs: Any):
    caller = _ScriptedCaller(script)
    defaults = {"max_turns": 20, "max_tool_calls": 100}
    defaults.update(kwargs)
    outcome = await run_agent_tool_loop(
        llm_caller=caller,
        system_prompt="sys",
        user_prompt="goal",
        tools=tools,
        **defaults,
    )
    return outcome, caller


@pytest.mark.asyncio
async def test_finish_terminates_with_status_and_output() -> None:
    observe_calls: list[tuple[str, dict[str, Any]]] = []
    tools = [_recording_tool("observe", observe_calls), make_finish_tool()]
    script = [
        [("observe", {})],
        [("finish", {"status": "completed", "reason": "goal met", "extracted_output": {"x": 1}})],
    ]
    outcome, _ = await _run(script, tools)

    assert outcome.status == "completed"
    assert outcome.reason == "goal met"
    assert outcome.extracted_output == {"x": 1}
    assert outcome.turns == 2
    assert outcome.tool_calls == 2
    assert observe_calls == [("observe", {})]


@pytest.mark.asyncio
async def test_perception_is_on_demand_never_injected() -> None:
    observe_calls: list[tuple[str, dict[str, Any]]] = []
    click_calls: list[tuple[str, dict[str, Any]]] = []
    tools = [_recording_tool("observe", observe_calls), _recording_tool("click", click_calls), make_finish_tool()]
    script = [
        [("click", {"i": 1})],
        [("click", {"i": 2})],
        [("finish", {"status": "completed", "reason": "ok"})],
    ]
    outcome, _ = await _run(script, tools)

    assert outcome.status == "completed"
    # The loop never perceives on its own — observe fires only when the model asks.
    assert observe_calls == []
    assert len(click_calls) == 2


@pytest.mark.asyncio
async def test_action_batching_multiple_tool_calls_one_turn() -> None:
    click_calls: list[tuple[str, dict[str, Any]]] = []
    type_calls: list[tuple[str, dict[str, Any]]] = []
    tools = [_recording_tool("click", click_calls), _recording_tool("type", type_calls), make_finish_tool()]
    script = [
        [("click", {"i": 1}), ("type", {"t": "a"}), ("click", {"i": 2})],
        [("finish", {"status": "completed", "reason": "ok"})],
    ]
    outcome, _ = await _run(script, tools)

    assert outcome.status == "completed"
    assert outcome.turns == 2  # one batched action turn + the finish turn
    assert outcome.tool_calls == 4
    assert [args["i"] for args in [c[1] for c in click_calls]] == [1, 2]
    assert type_calls == [("type", {"t": "a"})]


@pytest.mark.asyncio
async def test_max_turns_budget_exhausted() -> None:
    click_calls: list[tuple[str, dict[str, Any]]] = []
    tools = [_recording_tool("click", click_calls), make_finish_tool()]
    script = [[("click", {})]] * 10  # never finishes
    outcome, _ = await _run(script, tools, max_turns=3)

    assert outcome.status == "budget_exhausted"
    assert "max_turns" in outcome.reason
    assert outcome.turns == 3


@pytest.mark.asyncio
async def test_max_tool_calls_budget_exhausted() -> None:
    click_calls: list[tuple[str, dict[str, Any]]] = []
    tools = [_recording_tool("click", click_calls), make_finish_tool()]
    script = [[("click", {}), ("click", {})]] * 10
    outcome, _ = await _run(script, tools, max_tool_calls=2)

    assert outcome.status == "budget_exhausted"
    assert "max_tool_calls" in outcome.reason
    assert outcome.tool_calls == 2


@pytest.mark.asyncio
async def test_no_tool_call_triggers_nudge_and_continues() -> None:
    tools = [make_finish_tool()]
    script = [[], [("finish", {"status": "completed", "reason": "ok"})]]
    outcome, _ = await _run(script, tools)

    assert outcome.status == "completed"
    assert outcome.turns == 2
    nudges = [m for m in outcome.messages if m.get("role") == "user" and m.get("content") == NO_TOOL_CALL_NUDGE]
    assert len(nudges) == 1


@pytest.mark.asyncio
async def test_unknown_tool_is_reported_and_loop_continues() -> None:
    tools = [make_finish_tool()]
    script = [[("does_not_exist", {})], [("finish", {"status": "completed", "reason": "ok"})]]
    outcome, _ = await _run(script, tools)

    assert outcome.status == "completed"
    tool_messages = [m for m in outcome.messages if m.get("role") == "tool"]
    assert any("unknown_tool: does_not_exist" in m["content"] for m in tool_messages)


@pytest.mark.asyncio
async def test_tool_handler_exception_becomes_error_result() -> None:
    boom_calls: list[tuple[str, dict[str, Any]]] = []
    tools = [_recording_tool("click", boom_calls, raises=True), make_finish_tool()]
    script = [[("click", {})], [("finish", {"status": "completed", "reason": "recovered"})]]
    outcome, _ = await _run(script, tools)

    assert outcome.status == "completed"
    tool_messages = [m for m in outcome.messages if m.get("role") == "tool"]
    assert any("tool_error: RuntimeError" in m["content"] for m in tool_messages)


@pytest.mark.asyncio
async def test_terminal_finish_with_failed_status() -> None:
    tools = [make_finish_tool()]
    script = [[("finish", {"status": "failed", "reason": "blocked by captcha"})]]
    outcome, _ = await _run(script, tools)

    assert outcome.status == "failed"
    assert outcome.reason == "blocked by captcha"


@pytest.mark.asyncio
async def test_tools_are_forwarded_in_openai_shape() -> None:
    tools = [make_finish_tool()]
    script = [[("finish", {"status": "completed", "reason": "ok"})]]
    _, caller = await _run(script, tools)

    assert caller.sent_tools is not None
    finish_schema = next(t for t in caller.sent_tools if t["function"]["name"] == "finish")
    assert finish_schema["type"] == "function"
    assert "status" in finish_schema["function"]["parameters"]["properties"]


class _FlakyCaller(_ScriptedCaller):
    """Raises `exc` on the first `fail_times` calls, then behaves like the scripted caller.

    A failed attempt raises before consuming a script turn, so only successful calls advance
    the script — mirroring a transient provider error on an otherwise-valid turn.
    """

    def __init__(self, script: list[list[tuple[str, dict[str, Any]]]], *, fail_times: int, exc: BaseException) -> None:
        super().__init__(script)
        self._fail_times = fail_times
        self._exc = exc
        self.attempts = 0

    async def call(self, **kwargs: Any) -> dict[str, Any]:
        if self.attempts < self._fail_times:
            self.attempts += 1
            raise self._exc
        return await super().call(**kwargs)


@pytest.mark.asyncio
async def test_transient_call_error_is_retried_then_succeeds() -> None:
    from skyvern.forge.sdk.api.llm.exceptions import LLMProviderErrorRetryableTask

    caller = _FlakyCaller(
        [[("finish", {"status": "completed", "reason": "ok"})]],
        fail_times=2,
        exc=LLMProviderErrorRetryableTask("test-key"),
    )
    outcome = await run_agent_tool_loop(
        llm_caller=caller,
        system_prompt="sys",
        user_prompt="goal",
        tools=[make_finish_tool()],
        max_turns=5,
        max_tool_calls=10,
        retryable_call_exceptions=(LLMProviderErrorRetryableTask,),
        max_call_retries=2,
        call_retry_base_delay=0.0,
    )
    assert outcome.status == "completed"
    assert caller.attempts == 2  # failed twice, third attempt succeeded


@pytest.mark.asyncio
async def test_transient_call_error_exhausts_retries_to_loop_error() -> None:
    from skyvern.forge.sdk.api.llm.exceptions import LLMProviderErrorRetryableTask

    caller = _FlakyCaller(
        [[("finish", {"status": "completed", "reason": "ok"})]],
        fail_times=99,
        exc=LLMProviderErrorRetryableTask("test-key"),
    )
    outcome = await run_agent_tool_loop(
        llm_caller=caller,
        system_prompt="sys",
        user_prompt="goal",
        tools=[make_finish_tool()],
        max_turns=5,
        max_tool_calls=10,
        retryable_call_exceptions=(LLMProviderErrorRetryableTask,),
        max_call_retries=2,
        call_retry_base_delay=0.0,
    )
    assert outcome.status == "loop_error"
    assert "llm_call_failed" in outcome.reason


@pytest.mark.asyncio
async def test_non_retryable_call_error_is_not_retried() -> None:
    caller = _FlakyCaller(
        [[("finish", {"status": "completed", "reason": "ok"})]],
        fail_times=99,
        exc=RuntimeError("boom"),
    )
    outcome = await run_agent_tool_loop(
        llm_caller=caller,
        system_prompt="sys",
        user_prompt="goal",
        tools=[make_finish_tool()],
        max_turns=5,
        max_tool_calls=10,
        retryable_call_exceptions=(ValueError,),  # RuntimeError is not in here
        max_call_retries=2,
        call_retry_base_delay=0.0,
    )
    assert outcome.status == "loop_error"
    assert caller.attempts == 1  # raised once, no retry


class _Clock:
    """Deterministic monotonic clock: returns each queued value once, then repeats the last."""

    def __init__(self, values: list[float]) -> None:
        self._values = values
        self._i = 0

    def __call__(self) -> float:
        v = self._values[min(self._i, len(self._values) - 1)]
        self._i += 1
        return v


@pytest.mark.asyncio
async def test_should_cancel_between_turns_yields_canceled() -> None:
    # Cancel signal arrives after the first turn; the loop must stop with status "canceled",
    # not run to a finish or a budget cap.
    seen = {"n": 0}

    async def _cancel() -> bool:
        seen["n"] += 1
        return seen["n"] > 1  # False on the first (pre-turn) check, True on the next

    caller = _ScriptedCaller([[("observe", {})], [("observe", {})], [("observe", {})]])
    outcome = await run_agent_tool_loop(
        llm_caller=caller,
        system_prompt="sys",
        user_prompt="goal",
        tools=[_recording_tool("observe", []), make_finish_tool()],
        max_turns=20,
        max_tool_calls=100,
        should_cancel=_cancel,
    )
    assert outcome.status == "canceled"
    assert outcome.turns == 1


@pytest.mark.asyncio
async def test_deadline_seconds_exhausts_budget(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("skyvern.forge.taskv3.loop.time.monotonic", _Clock([0.0, 10_000.0]))
    caller = _ScriptedCaller([[("observe", {})]])
    outcome = await run_agent_tool_loop(
        llm_caller=caller,
        system_prompt="sys",
        user_prompt="goal",
        tools=[_recording_tool("observe", []), make_finish_tool()],
        max_turns=20,
        max_tool_calls=100,
        deadline_seconds=60.0,
    )
    assert outcome.status == "budget_exhausted"
    assert "deadline" in outcome.reason
    assert caller.calls == 0  # tripped before any LLM call


@pytest.mark.asyncio
async def test_max_tokens_exhausts_budget() -> None:
    # The scripted caller reports 15 tokens/turn; a 10-token budget trips after one turn.
    caller = _ScriptedCaller([[("observe", {})], [("observe", {})]])
    outcome = await run_agent_tool_loop(
        llm_caller=caller,
        system_prompt="sys",
        user_prompt="goal",
        tools=[_recording_tool("observe", []), make_finish_tool()],
        max_turns=20,
        max_tool_calls=100,
        max_tokens=10,
    )
    assert outcome.status == "budget_exhausted"
    assert "max_tokens" in outcome.reason
    assert caller.calls == 1  # one turn ran, then the token budget stopped it


@pytest.mark.asyncio
async def test_should_cancel_mid_batch_stops_before_next_tool() -> None:
    # A cancel that arrives partway through a batched turn must stop before the next tool runs.
    # Distinct from the between-turns check: this pins the mid-batch poll specifically.
    seen = {"n": 0}

    async def _cancel() -> bool:
        seen["n"] += 1
        return seen["n"] > 2  # False for top-of-loop + first tool; True before the second tool

    observe_calls: list[tuple[str, dict[str, Any]]] = []
    tools = [_recording_tool("observe", observe_calls), make_finish_tool()]
    outcome = await run_agent_tool_loop(
        llm_caller=_ScriptedCaller([[("observe", {}), ("observe", {})]]),  # one turn, two tool calls
        system_prompt="sys",
        user_prompt="goal",
        tools=tools,
        max_turns=20,
        max_tool_calls=100,
        should_cancel=_cancel,
    )
    assert outcome.status == "canceled"
    assert len(observe_calls) == 1  # cancel fired before the second tool in the batch


@pytest.mark.asyncio
async def test_tool_call_cap_stops_mid_batch() -> None:
    # A single batched turn cannot overrun the tool-call cap: the per-dispatch check stops it
    # after the cap is reached, mid-batch.
    observe_calls: list[tuple[str, dict[str, Any]]] = []
    tools = [_recording_tool("observe", observe_calls), make_finish_tool()]
    outcome = await run_agent_tool_loop(
        llm_caller=_ScriptedCaller([[("observe", {}), ("observe", {})]]),  # two tool calls in one turn
        system_prompt="sys",
        user_prompt="goal",
        tools=tools,
        max_turns=20,
        max_tool_calls=1,  # only one dispatch allowed
    )
    assert outcome.status == "budget_exhausted"
    assert len(observe_calls) == 1  # the cap stopped the batch after the first dispatch


@pytest.mark.asyncio
async def test_tool_error_stops_batch_and_skips_remaining() -> None:
    # A failed call mid-batch must stop the rest of the batch (so a later write can't run against a
    # page a failed earlier call left in a bad state) and answer the skipped calls, so the next turn
    # sees a valid transcript and re-plans from the error.
    click_calls: list[tuple[str, dict[str, Any]]] = []
    type_calls: list[tuple[str, dict[str, Any]]] = []
    tools = [
        _recording_tool("click", click_calls),
        _recording_tool("boom", [], raises=True),
        _recording_tool("type", type_calls),
        make_finish_tool(),
    ]
    script = [
        [("click", {}), ("boom", {}), ("type", {"selector": "#x"})],  # one batched turn
        [("finish", {"status": "completed", "reason": "recovered"})],
    ]
    outcome, _ = await _run(script, tools)

    assert outcome.status == "completed"  # loop recovered on the next turn
    assert len(click_calls) == 1  # the call before the error ran
    assert len(type_calls) == 0  # the call after the error was skipped, not executed
    turn1_tool_msgs = [m for m in outcome.messages if m.get("role") == "tool"]
    assert any(m.get("name") == "type" and "skipped" in m["content"] for m in turn1_tool_msgs)
    assert any("tool_error: RuntimeError" in m["content"] for m in turn1_tool_msgs)


@pytest.mark.asyncio
async def test_action_step_budget_ignores_perception_rounds() -> None:
    # A v3 "step" is an action round; perception rounds (observe/get_html) must not consume the
    # caller's step budget, or a tight budget starves the engine before it can act.
    obs_calls: list[tuple[str, dict[str, Any]]] = []
    click_calls: list[tuple[str, dict[str, Any]]] = []
    observe = _recording_tool("observe", obs_calls)  # not billable = perception
    click = _recording_tool("click", click_calls)
    click.billable = True
    script = [
        [("observe", {})],
        [("observe", {})],
        [("observe", {})],
        [("observe", {})],
        [("click", {})],
        [("finish", {"status": "completed", "reason": "ok"})],
    ]
    outcome, _ = await _run(script, [observe, click, make_finish_tool()], max_action_steps=2, max_turns=20)
    assert outcome.status == "completed"  # 4 perception rounds did not burn the 2-action-step budget
    assert len(obs_calls) == 4 and len(click_calls) == 1
    assert outcome.action_steps == 1  # only the single action round counted


@pytest.mark.asyncio
async def test_action_step_budget_counts_rounds_not_individual_actions() -> None:
    # A batched action round (many actions in one turn) is ONE step, matching a step-engine step.
    click_calls: list[tuple[str, dict[str, Any]]] = []
    type_calls: list[tuple[str, dict[str, Any]]] = []
    click = _recording_tool("click", click_calls)
    click.billable = True
    type_ = _recording_tool("type", type_calls)
    type_.billable = True
    script = [
        [("click", {}), ("type", {"t": "a"})],  # action round 1 (2 actions)
        [("click", {}), ("type", {"t": "b"})],  # action round 2 (2 actions)
        [("click", {}), ("type", {"t": "c"})],  # round 3 -> blocked by the 2-step budget
    ]
    outcome, _ = await _run(script, [click, type_, make_finish_tool()], max_action_steps=2, max_turns=20)
    assert outcome.status == "budget_exhausted"
    assert "maximum steps (2)" in outcome.reason
    assert outcome.action_steps == 2  # two action rounds counted, exposed on the outcome
    # 2 rounds (4 actions) ran; the 3rd was blocked at the top -> per-round, not per-action, counting.
    assert len(click_calls) == 2 and len(type_calls) == 2


@pytest.mark.asyncio
async def test_action_step_budget_allows_finish_after_last_action_round() -> None:
    # Regression: at the boundary the model must still be able to re-observe and finish (a separate
    # turn per the system prompt). The cap bounds new action rounds, not the completion signal.
    obs_calls: list[tuple[str, dict[str, Any]]] = []
    click_calls: list[tuple[str, dict[str, Any]]] = []
    observe = _recording_tool("observe", obs_calls)
    click = _recording_tool("click", click_calls)
    click.billable = True
    script = [
        [("observe", {})],
        [("click", {})],  # action round 1 == cap
        [("observe", {})],  # perception after the last action must NOT be blocked
        [("finish", {"status": "completed", "reason": "done", "extracted_output": {"ok": True}})],
    ]
    outcome, _ = await _run(script, [observe, click, make_finish_tool()], max_action_steps=1, max_turns=20)
    assert outcome.status == "completed"  # not budget_exhausted
    assert outcome.extracted_output == {"ok": True}  # output not dropped
    assert outcome.action_steps == 1
    assert len(click_calls) == 1 and len(obs_calls) == 2


@pytest.mark.asyncio
async def test_action_step_budget_terminates_only_on_action_beyond_budget() -> None:
    click_calls: list[tuple[str, dict[str, Any]]] = []
    click = _recording_tool("click", click_calls)
    click.billable = True
    script = [[("click", {})], [("click", {})]]  # 2nd click is the (cap+1)th action round
    outcome, _ = await _run(script, [click, make_finish_tool()], max_action_steps=1, max_turns=20)
    assert outcome.status == "budget_exhausted"
    assert "maximum steps (1)" in outcome.reason
    assert outcome.action_steps == 1
    assert len(click_calls) == 1  # the over-budget action was refused, not executed


@pytest.mark.asyncio
async def test_action_step_budget_counts_failed_action_rounds() -> None:
    # A dispatched page action consumes a step even if it errors (it may mutate before failing),
    # so a run cannot exceed the budget by repeatedly failing a mutating tool.
    click_calls: list[tuple[str, dict[str, Any]]] = []
    click = _recording_tool("click", click_calls, raises=True)
    click.billable = True
    script = [[("click", {})], [("click", {})]]
    outcome, _ = await _run(script, [click, make_finish_tool()], max_action_steps=1, max_turns=20)
    assert outcome.status == "budget_exhausted"
    assert outcome.action_steps == 1  # the failed 1st round still consumed the budget
    assert len(click_calls) == 1  # 2nd round refused at the budget gate


@pytest.mark.asyncio
async def test_on_action_round_fires_once_per_action_round() -> None:
    # The callback fires once per action ROUND (a turn with >=1 successful billable action), not per
    # tool and not on perception-only turns, and receives that round's (name, args) list.
    rounds: list[list[tuple[str, dict[str, Any]]]] = []

    async def _on_round(actions: list[tuple[str, dict[str, Any]]]) -> None:
        rounds.append(actions)

    obs, clk, typ = [], [], []
    observe = _recording_tool("observe", obs)  # perception, not billable
    click = _recording_tool("click", clk)
    click.billable = True
    type_ = _recording_tool("type", typ)
    type_.billable = True
    script = [
        [("observe", {})],  # perception-only -> no callback
        [("click", {"selector": "#a"}), ("type", {"selector": "#b", "text": "x"})],  # 1 round, 2 tools -> 1 call
        [("finish", {"status": "completed", "reason": "ok"})],
    ]
    outcome, _ = await _run(script, [observe, click, type_, make_finish_tool()], on_action_round=_on_round)
    assert outcome.status == "completed"
    assert len(rounds) == 1
    assert rounds[0] == [("click", {"selector": "#a"}, True), ("type", {"selector": "#b", "text": "x"}, True)]


@pytest.mark.asyncio
async def test_on_action_round_fires_for_all_failed_round_with_failure_flag() -> None:
    # A dispatched billable round consumes budget even when every call errors; it must reach the
    # callback (flagged unsuccessful) so the round persists into the workflow-run step budget.
    rounds: list[list[tuple[str, dict[str, Any], bool]]] = []

    async def _on_round(actions: list[tuple[str, dict[str, Any], bool]]) -> None:
        rounds.append(actions)

    clk: list[tuple[str, dict[str, Any]]] = []
    click = _recording_tool("click", clk, raises=True)  # dispatched (consumes a step) but errors
    click.billable = True
    script = [[("click", {})], [("finish", {"status": "completed", "reason": "ok"})]]
    outcome, _ = await _run(script, [click, make_finish_tool()], on_action_round=_on_round)
    assert outcome.status == "completed"
    assert rounds == [[("click", {}, False)]]
    assert outcome.billable_actions == []  # billing still counts successes only


@pytest.mark.asyncio
async def test_on_action_round_failure_does_not_abort_run() -> None:
    async def _boom(actions: list[tuple[str, dict[str, Any]]]) -> None:
        raise RuntimeError("persist boom")

    clk: list[tuple[str, dict[str, Any]]] = []
    click = _recording_tool("click", clk)
    click.billable = True
    script = [[("click", {})], [("finish", {"status": "completed", "reason": "ok"})]]
    outcome, _ = await _run(script, [click, make_finish_tool()], on_action_round=_boom)
    assert outcome.status == "completed"  # callback error contained; run still completes
    assert len(clk) == 1


def _tool_msg(tool_call_id: str, name: str, content: str) -> dict[str, Any]:
    return {"role": "tool", "tool_call_id": tool_call_id, "name": name, "content": content}


def _assistant_turn(*ids: str) -> dict[str, Any]:
    return {"role": "assistant", "content": None, "tool_calls": [{"id": i} for i in ids]}


def test_compact_transcript_elides_superseded_perception() -> None:
    # Keep the newest snapshot of each tracked tool; elide older ones' content (never remove the message),
    # and leave untracked results untouched. Round 2 (after the last assistant) supersedes round 1's
    # observe/get_html. `snapshot_indices` names the successful-perception message indices the loop records.
    from skyvern.forge.taskv3.loop import _compact_transcript

    messages = [
        {"role": "system", "content": "sys"},
        {"role": "user", "content": "goal"},
        _assistant_turn("a", "b", "c"),  # round 1
        _tool_msg("a", "observe", "OBSERVE_1 " + "x" * 300),  # idx 3
        _tool_msg("b", "get_html", "HTML_1 " + "y" * 300),  # idx 4
        _tool_msg("c", "click", "clicked #x"),  # idx 5 (not a snapshot)
        _assistant_turn("d", "e"),  # round 2 (latest)
        _tool_msg("d", "observe", "OBSERVE_2 latest " + "z" * 300),  # idx 7
        _tool_msg("e", "get_html", "HTML_2 latest " + "w" * 300),  # idx 8
    ]
    snapshots = {3, 4, 7, 8}  # the observe/get_html successes; the click (5) is not a snapshot
    _compact_transcript(messages, snapshots)
    by_id = {m["tool_call_id"]: m["content"] for m in messages if m.get("role") == "tool"}
    assert by_id["a"].startswith("[superseded observe")  # older observe elided
    assert by_id["b"].startswith("[superseded get_html")  # older get_html elided
    assert by_id["c"] == "clicked #x"  # untracked result untouched
    assert by_id["d"].startswith("OBSERVE_2 latest")  # newest observe kept intact
    assert by_id["e"].startswith("HTML_2 latest")  # newest get_html kept intact
    assert snapshots == {7, 8}  # elided indices are dropped so a re-run can't re-anchor them

    # Idempotent: a second pass over the (now-reduced) index set changes nothing.
    snapshot = [m.get("content") for m in messages]
    _compact_transcript(messages, snapshots)
    assert [m.get("content") for m in messages] == snapshot


def test_compact_transcript_keeps_unread_latest_round() -> None:
    # A single turn can batch several perception calls; compaction runs before the model reads them, so
    # the latest round must be kept intact even when it repeats a compactable tool (would otherwise drop
    # a result the model requested but never saw).
    from skyvern.forge.taskv3.loop import _compact_transcript

    messages = [
        {"role": "user", "content": "goal"},
        _assistant_turn("a", "b"),
        _tool_msg("a", "get_html", "HTML_A " + "a" * 300),  # idx 2
        _tool_msg("b", "get_html", "HTML_B " + "b" * 300),  # idx 3
    ]
    _compact_transcript(messages, {2, 3})
    assert messages[2]["content"].startswith("HTML_A")  # both unread → neither elided
    assert messages[3]["content"].startswith("HTML_B")


def test_compact_transcript_skip_stub_does_not_shadow_real_snapshot() -> None:
    # A skipped/errored perception result is never recorded as a snapshot, so it can't shadow the real
    # observe from an earlier round — else a failed batch would leave the agent with no page view. The
    # skip stub (idx 4) is simply absent from the index set regardless of its content.
    from skyvern.forge.taskv3.loop import _compact_transcript

    messages = [
        _assistant_turn("o1"),
        _tool_msg("o1", "observe", "REAL_OBSERVE " + "p" * 300),  # idx 1 (the only real snapshot)
        _assistant_turn("c1", "o2"),  # latest round: a click that failed, so the batched observe was skipped
        _tool_msg("c1", "click", "tool_error: TimeoutError: click failed"),  # idx 3
        _tool_msg("o2", "observe", "skipped: earlier tool call in this batch failed"),  # idx 4 (not tracked)
    ]
    _compact_transcript(messages, {1})
    assert messages[1]["content"].startswith("REAL_OBSERVE")  # real snapshot preserved as the live view
    assert messages[4]["content"].startswith("skipped:")  # skip stub left as-is, never elided or promoted


def test_compact_transcript_noop_without_tracked_snapshots() -> None:
    from skyvern.forge.taskv3.loop import _compact_transcript

    messages = [_assistant_turn("a"), _tool_msg("a", "observe", "big " + "x" * 500)]
    _compact_transcript(messages, set())
    assert messages[1]["content"].startswith("big ")  # nothing elided when nothing is tracked


def _observe_tool(handler: ToolHandler) -> ToolSpec:
    spec = ToolSpec(
        name="observe", description="observe", parameters={"type": "object", "properties": {}}, handler=handler
    )
    spec.compactable = True
    return spec


def _big_observe_tool() -> ToolSpec:
    async def handler(_args: dict[str, Any]) -> ToolResult:
        return ToolResult.ok("OBSERVE " + "x" * 300)  # a real, snapshot-sized perception result

    return _observe_tool(handler)


@pytest.mark.asyncio
async def test_loop_compacts_superseded_observe_snapshots() -> None:
    # Across a multi-observe run the loop keeps only the latest snapshot in the re-sent transcript,
    # eliding earlier ones — this is what bounds context growth on perception-heavy pages.
    script = [
        [("observe", {})],
        [("observe", {})],
        [("observe", {})],
        [("finish", {"status": "completed", "reason": "ok"})],
    ]
    outcome, _ = await _run(script, [_big_observe_tool(), make_finish_tool()])
    assert outcome.status == "completed"
    obs_msgs = [m for m in outcome.messages if m.get("role") == "tool" and m.get("name") == "observe"]
    assert len(obs_msgs) == 3
    elided = [m for m in obs_msgs if m["content"].startswith("[superseded ")]
    intact = [m for m in obs_msgs if m["content"].startswith("OBSERVE ")]
    assert len(elided) == 2 and len(intact) == 1  # only the most-recent observe snapshot survives


@pytest.mark.asyncio
async def test_loop_elides_superseded_short_snapshots() -> None:
    # A genuine but tiny snapshot is still elided once superseded: snapshots are tracked by success
    # status, not size, so perception-light pages compact too (the old length heuristic missed these).
    async def handler(_args: dict[str, Any]) -> ToolResult:
        return ToolResult.ok("OBS")  # 3 chars — well under any size threshold, but a real snapshot

    script = [[("observe", {})], [("observe", {})], [("finish", {"status": "completed", "reason": "ok"})]]
    outcome, _ = await _run(script, [_observe_tool(handler), make_finish_tool()])
    obs = [m for m in outcome.messages if m.get("role") == "tool" and m.get("name") == "observe"]
    assert len(obs) == 2
    assert obs[0]["content"].startswith("[superseded ")  # older short snapshot elided
    assert obs[1]["content"] == "OBS"  # newest kept


@pytest.mark.asyncio
async def test_loop_verbose_error_never_shadows_real_snapshot() -> None:
    # A verbose tool error (e.g. a multi-line Playwright timeout, well over any length threshold) must
    # never be treated as the live snapshot: it has error status, so it is never tracked and the last
    # good snapshot survives. This is the failure a size-based heuristic would have gotten wrong.
    calls = {"n": 0}

    async def handler(_args: dict[str, Any]) -> ToolResult:
        calls["n"] += 1
        if calls["n"] == 1:
            return ToolResult.ok("OBSERVE " + "x" * 300)  # real snapshot
        return ToolResult.error("tool_error: TimeoutError: " + "waiting for selector\n" * 40)  # long error

    script = [[("observe", {})], [("observe", {})], [("finish", {"status": "completed", "reason": "ok"})]]
    outcome, _ = await _run(script, [_observe_tool(handler), make_finish_tool()])
    obs = [m for m in outcome.messages if m.get("role") == "tool" and m.get("name") == "observe"]
    assert len(obs) == 2
    assert obs[0]["content"].startswith("OBSERVE ")  # last good snapshot preserved, not shadowed by the error
    assert obs[1]["content"].startswith("tool_error:")  # the long error left intact, never promoted or elided


@pytest.mark.asyncio
async def test_finish_completed_defers_until_page_settles() -> None:
    # A finish(completed) on a still-rendering page (delayed data load) is deferred so the model
    # re-verifies against the settled state; the deferral is an ordinary tool error, and the
    # follow-up finish on the settled page is terminal. The fingerprints model a same-shape content
    # swap: equal-length samples still differ, so the swap cannot read as settled.
    samples = iter(["fp-a", "fp-b", "fp-b", "fp-b"])

    async def fingerprint() -> str | None:
        return next(samples)

    script = [
        [("finish", {"status": "completed", "reason": "looks done"})],
        [("finish", {"status": "completed", "reason": "confirmed on settled page"})],
    ]
    outcome, _ = await _run(script, [make_finish_tool(page_fingerprint=fingerprint, settle_wait_seconds=0.0)])
    assert outcome.status == "completed"
    assert outcome.reason == "confirmed on settled page"
    deferral_messages = [
        m for m in outcome.messages if m.get("role") == "tool" and "still rendering" in str(m.get("content"))
    ]
    assert len(deferral_messages) == 1


@pytest.mark.asyncio
async def test_finish_settle_deferrals_are_bounded_and_scoped_to_completed() -> None:
    # A permanently-unsettled page cannot livelock the run: after the deferral cap the verdict is
    # accepted. terminated/failed finishes never consult the page.
    counter = iter(range(1000))

    async def never_settled() -> str | None:
        return f"fp-{next(counter)}"

    script = [
        [("finish", {"status": "completed", "reason": "try 1"})],
        [("finish", {"status": "completed", "reason": "try 2"})],
        [("finish", {"status": "completed", "reason": "try 3"})],
    ]
    outcome, _ = await _run(
        script,
        [make_finish_tool(page_fingerprint=never_settled, max_settle_deferrals=2, settle_wait_seconds=0.0)],
    )
    assert outcome.status == "completed"
    assert outcome.reason == "try 3"

    sample_calls = 0

    async def counting_fingerprint() -> str | None:
        nonlocal sample_calls
        sample_calls += 1
        return "fp"

    script = [[("finish", {"status": "terminated", "reason": "blocked"})]]
    outcome, _ = await _run(script, [make_finish_tool(page_fingerprint=counting_fingerprint, settle_wait_seconds=0.0)])
    assert outcome.status == "terminated"
    assert sample_calls == 0


@pytest.mark.asyncio
async def test_finish_settle_probe_error_fails_closed_and_defers() -> None:
    # A raising probe (e.g. execution context destroyed mid-navigation) is evidence of nothing, not
    # of stability: the verdict is deferred for re-verification, still bounded by the deferral cap
    # so a permanently-broken probe cannot livelock the run.
    async def exploding_fingerprint() -> str | None:
        raise RuntimeError("execution context was destroyed")

    script = [
        [("finish", {"status": "completed", "reason": "try 1"})],
        [("finish", {"status": "completed", "reason": "try 2"})],
        [("finish", {"status": "completed", "reason": "try 3"})],
    ]
    outcome, _ = await _run(script, [make_finish_tool(page_fingerprint=exploding_fingerprint, settle_wait_seconds=0.0)])
    assert outcome.status == "completed"
    assert outcome.reason == "try 3"
    deferral_messages = [
        m for m in outcome.messages if m.get("role") == "tool" and "verified as settled" in str(m.get("content"))
    ]
    assert len(deferral_messages) == 2


@pytest.mark.asyncio
async def test_finish_settle_wait_aborts_on_cancellation() -> None:
    # A cancellation arriving while the probe waits between samples abandons the probe: the second
    # sample is never taken, the verdict defers, and the loop ends the run as canceled instead of
    # letting a completion land after cancellation was requested.
    canceled = False
    sample_calls = 0

    async def fingerprint() -> str | None:
        nonlocal canceled, sample_calls
        sample_calls += 1
        canceled = True
        return "stable"

    async def should_cancel() -> bool:
        return canceled

    script = [[("finish", {"status": "completed", "reason": "done"})]]
    outcome, _ = await _run(
        script,
        [make_finish_tool(page_fingerprint=fingerprint, should_cancel=should_cancel, settle_wait_seconds=0.0)],
        should_cancel=should_cancel,
    )
    assert outcome.status == "canceled"
    assert sample_calls == 1


@pytest.mark.asyncio
async def test_finish_settle_wait_is_capped_by_the_deadline() -> None:
    # The wait between samples cannot overrun the loop deadline: with the deadline already past, the
    # probe resamples without sleeping instead of blocking for settle_wait_seconds.
    async def fingerprint() -> str | None:
        return "stable"

    script = [[("finish", {"status": "completed", "reason": "done"})]]
    started = time.monotonic()
    outcome, _ = await _run(
        script,
        [make_finish_tool(page_fingerprint=fingerprint, deadline_at=started - 1.0, settle_wait_seconds=30.0)],
    )
    assert outcome.status == "completed"
    assert time.monotonic() - started < 5.0


@pytest.mark.asyncio
async def test_no_tool_call_turn_is_counted_and_nudged() -> None:
    tools = [make_finish_tool()]
    script = [[], [("finish", {"status": "completed", "reason": "ok"})]]
    outcome, _ = await _run(script, tools)

    assert outcome.status == "completed"
    assert outcome.no_tool_call_turns == 1
    nudges = [m for m in outcome.messages if m.get("role") == "user" and m.get("content") == NO_TOOL_CALL_NUDGE]
    assert len(nudges) == 1


class _ToolChoiceSensitiveCaller(_ScriptedCaller):
    """Rejects any call carrying ``tool_choice``, as a provider that does not accept it would."""

    def __init__(self, script: list[list[tuple[str, dict[str, Any]]]]) -> None:
        super().__init__(script)
        self.tool_choice_per_call: list[str | None] = []

    async def call(self, **kwargs: Any) -> dict[str, Any]:
        self.tool_choice_per_call.append(kwargs.get("tool_choice"))
        if kwargs.get("tool_choice") is not None:
            # The LLM layer maps a provider 400 onto the retryable type, so that -- not a bare
            # exception -- is what the loop actually has to degrade from.
            raise LLMProviderErrorRetryableTask("TEST_KEY")
        return await super().call(**kwargs)


@pytest.mark.asyncio
async def test_loop_drops_tool_choice_and_retries_the_turn_after_a_call_failure() -> None:
    caller = _ToolChoiceSensitiveCaller([[("finish", {"status": "completed", "reason": "ok"})]])
    outcome = await run_agent_tool_loop(
        llm_caller=caller,
        system_prompt="sys",
        user_prompt="goal",
        tools=[make_finish_tool()],
        max_turns=5,
        max_tool_calls=10,
        call_kwargs={"tool_choice": "required"},
        retryable_call_exceptions=(LLMProviderErrorRetryableTask,),
        max_call_retries=2,
        call_retry_base_delay=0.0,
    )

    assert outcome.status == "completed"
    assert outcome.tool_choice_in_effect is False
    # The transient budget is spent first, then the parameter is dropped and the turn re-issued.
    assert caller.tool_choice_per_call == ["required", "required", "required", None]


@pytest.mark.asyncio
async def test_loop_does_not_blame_tool_choice_for_a_context_window_overflow() -> None:
    # Dropping a parameter cannot shrink a transcript, so re-issuing would burn a second oversized
    # request and mislabel the failure.
    class _OverflowingCaller(_ScriptedCaller):
        async def call(self, **kwargs: Any) -> dict[str, Any]:
            self.calls += 1
            raise SkyvernContextWindowExceededError(model="test-model")

    caller = _OverflowingCaller([])
    outcome = await run_agent_tool_loop(
        llm_caller=caller,
        system_prompt="sys",
        user_prompt="goal",
        tools=[make_finish_tool()],
        max_turns=5,
        max_tool_calls=10,
        call_kwargs={"tool_choice": "required"},
    )

    assert outcome.status == "loop_error"
    assert caller.calls == 1


@pytest.mark.asyncio
async def test_every_executed_tool_call_emits_one_timing_record() -> None:
    # Tool execution is the largest unmeasured block of a v3 run's wall-clock, so the guarantee
    # this asserts is coverage: one record per call that actually ran, none for calls skipped
    # after a failure, and a duration that tracks real handler time.
    async def slow_handler(args: dict[str, Any]) -> ToolResult:
        await asyncio.sleep(0.02)
        return ToolResult.ok("observe done")

    async def raising_handler(args: dict[str, Any]) -> ToolResult:
        raise RuntimeError("boom")

    click_calls: list[tuple[str, dict[str, Any]]] = []
    tools = [
        ToolSpec(name="observe", description="o", parameters={}, handler=slow_handler, compactable=True),
        _recording_tool("click", click_calls),
        ToolSpec(name="boom", description="b", parameters={}, handler=raising_handler, billable=True),
        make_finish_tool(),
    ]
    script = [
        # A null selector is the case that matters: the tools fall back to scanning the whole page,
        # so it must read as absent even though the key is present.
        [("observe", {"selector": "sel"}), ("click", {"selector": None})],
        [("boom", {}), ("click", {})],  # boom fails, so the trailing click is skipped, not executed
        [("nope", {})],  # unknown tool
        [("finish", {"status": "completed", "reason": "ok"})],
    ]
    with capture_logs() as logs:
        outcome, _ = await _run(script, tools)

    records = [entry for entry in logs if entry["event"] == "taskv3 tool call finished"]

    assert outcome.status == "completed"
    # observe, click, boom, unknown, finish — the click skipped behind boom's failure is absent.
    # The hallucinated name "nope" is reported as the sentinel, keeping the field's values bounded
    # to the registered tools however the model misbehaves.
    assert [entry["tool"] for entry in records] == ["observe", "click", "boom", "unknown_tool", "finish"]
    assert [entry["tool_status"] for entry in records] == ["ok", "ok", "error", "error", "ok"]
    assert [entry["batch_size"] for entry in records] == [2, 2, 2, 1, 1]
    assert [entry["batch_index"] for entry in records] == [0, 1, 0, 0, 0]
    assert [entry["selector_present"] for entry in records] == [True, False, False, False, False]
    assert [entry["billable"] for entry in records] == [False, False, True, False, False]
    assert [entry["turn"] for entry in records] == [1, 1, 2, 3, 4]

    observe_record = records[0]
    assert observe_record["result_chars"] == len("observe done")
    assert observe_record["duration_seconds"] >= 0.02
    assert records[2]["result_chars"] > 0  # the error text the model is handed back
    assert outcome.tool_seconds >= observe_record["duration_seconds"]


_OBSERVE_SUMMARY_FIELDS = (
    "text_dropped",
    "hidden_listed",
    "iframes_in_component_roots",
    "undiscovered_roots",
    "omitted_unnameable",
    "invalid_fields",
    "markers_minted",
    "markers_reused",
)


async def _run_observe_then_click(summary: dict[str, int]) -> dict[str, dict[str, Any]]:
    """One observe and one click, both handing back the same summary data; records keyed by tool."""

    async def observe_handler(args: dict[str, Any]) -> ToolResult:
        return ToolResult.ok("observe done", data={"count": 3, "summary": summary})

    click_calls: list[tuple[str, dict[str, Any]]] = []
    tools = [
        ToolSpec(name="observe", description="o", parameters={}, handler=observe_handler, compactable=True),
        _billable_tool("click", click_calls, data={"summary": summary}),
        make_finish_tool(),
    ]
    script: list[list[tuple[str, dict[str, Any]]]] = [
        [("observe", {}), ("click", {})],
        [("finish", {"status": "completed", "reason": "ok"})],
    ]
    with capture_logs() as logs:
        outcome, _ = await _run(script, tools)
    assert outcome.status == "completed"
    return {entry["tool"]: entry for entry in logs if entry["event"] == "taskv3 tool call finished"}


@pytest.mark.asyncio
async def test_observe_summary_counters_land_on_the_tool_call_record() -> None:
    # Six of nine perception fixes change only page-evaluated JS whose result reaches only the tool
    # content, which the per-call record deliberately never carries. The counters observe computes
    # anyway are the one mechanism signal those fixes can leave, so they ride the existing record.
    summary = {field: index + 1 for index, field in enumerate(_OBSERVE_SUMMARY_FIELDS)}

    records = await _run_observe_then_click(summary)

    assert {field: records["observe"][field] for field in _OBSERVE_SUMMARY_FIELDS} == summary
    # Counts only: nothing the page rendered can reach an indexed field through the summary.
    assert all(isinstance(records["observe"][field], int) for field in _OBSERVE_SUMMARY_FIELDS)


@pytest.mark.asyncio
async def test_non_observe_tool_call_records_gain_no_summary_fields() -> None:
    # The gate is the tool, not the payload: a click handing back the same data shape must leave
    # its record byte-identical to today's, so existing queries and dashboards keep working.
    summary = {field: index + 1 for index, field in enumerate(_OBSERVE_SUMMARY_FIELDS)}

    records = await _run_observe_then_click(summary)

    # Without this the test is vacuous on a build that logs the summary nowhere at all.
    assert set(_OBSERVE_SUMMARY_FIELDS) <= set(records["observe"])
    for entry in (records["click"], records["finish"]):
        assert not set(_OBSERVE_SUMMARY_FIELDS) & set(entry)


def _perception_tool(name: str, contents: str | list[str]) -> ToolSpec:
    """Compactable perception fake: returns contents[i] per call (last one repeats)."""
    seq = [contents] if isinstance(contents, str) else contents
    calls = {"n": 0}

    async def handler(args: dict[str, Any]) -> ToolResult:
        content = seq[min(calls["n"], len(seq) - 1)]
        calls["n"] += 1
        return ToolResult.ok(content)

    return ToolSpec(
        name=name, description=name, parameters={"type": "object", "properties": {}}, handler=handler, compactable=True
    )


@pytest.mark.asyncio
async def test_perception_stall_terminates_with_bounded_verdict() -> None:
    # The production failure signature: a page frozen behind a gate the agent cannot perceive
    # produced ~90 byte-identical observes over ~30 minutes until the budget died, with no usable
    # reason. N identical snapshots of an unchanging page must instead end the run with a bounded
    # verdict carrying the real situation.
    script = [[("observe", {})] for _ in range(90)]
    tools = [_perception_tool("observe", "url=x (0 elements)"), make_finish_tool()]
    outcome, caller = await _run(script, tools, max_turns=200, max_tool_calls=500)
    assert outcome.status == "terminated"
    assert "identical" in outcome.reason and "observe" in outcome.reason
    assert caller.calls <= 20  # bounded well below the 90-observe runaway


@pytest.mark.asyncio
async def test_perception_stall_nudges_before_terminating() -> None:
    # Before the loop takes the verdict out of the model's hands it warns once, so a model that can
    # act on the information (finish with the real reason, or change approach) gets the chance.
    script = [[("observe", {})] for _ in range(90)]
    tools = [_perception_tool("observe", "url=x (0 elements)"), make_finish_tool()]
    outcome, _ = await _run(script, tools, max_turns=200, max_tool_calls=500)
    nudges = [m for m in outcome.messages if m.get("role") == "user" and "not changing" in str(m.get("content"))]
    assert len(nudges) == 1
    terminate_idx = len(outcome.messages) - 1
    assert outcome.messages.index(nudges[0]) < terminate_idx  # warned before the verdict


@pytest.mark.asyncio
async def test_perception_stall_resets_when_content_changes() -> None:
    # A progressing run (each action changes the page, so each observe differs) must never trip the
    # stall policy, however long it runs.
    contents = [f"url=x step={i}" for i in range(30)]
    script = [[("observe", {})] for _ in range(30)] + [[("finish", {"status": "completed", "reason": "done"})]]
    tools = [_perception_tool("observe", contents), make_finish_tool()]
    outcome, _ = await _run(script, tools, max_turns=200, max_tool_calls=500)
    assert outcome.status == "completed"


@pytest.mark.asyncio
async def test_identical_results_from_non_perception_tools_never_trip_the_stall_policy() -> None:
    # wait/click legitimately return the same string every call ("waited", "clicked #x — now at
    # url"); only perception snapshots (compactable tools) can witness "the page is not changing".
    script = [[("wait", {})] for _ in range(40)] + [[("finish", {"status": "completed", "reason": "done"})]]
    waits: list[tuple[str, dict[str, Any]]] = []
    tools = [_recording_tool("wait", waits), make_finish_tool()]
    outcome, _ = await _run(script, tools, max_turns=200, max_tool_calls=500)
    assert outcome.status == "completed"
    assert len(waits) == 40


@pytest.mark.asyncio
async def test_stall_nudge_names_available_unblockers_and_all_stalled_tools() -> None:
    # The models that stall are the ones that cannot see the gate — a generic "you appear stuck"
    # leaves solve_captcha undiscovered. The single warning must name every stalled perception tool
    # and the unblockers this run actually offers.
    script = [[("observe", {}), ("get_html", {})] for _ in range(8)]
    solve_calls: list[tuple[str, dict[str, Any]]] = []
    tools = [
        _perception_tool("observe", "url=x frozen"),
        _perception_tool("get_html", "<div>frozen</div>"),
        _recording_tool("solve_captcha", solve_calls),
        make_finish_tool(),
    ]
    outcome, _ = await _run(script, tools, max_turns=200, max_tool_calls=500)
    nudges = [m for m in outcome.messages if m.get("role") == "user" and "not changing" in str(m.get("content"))]
    assert len(nudges) == 1  # both tools stall in the same turn: one combined warning, not zero, not two
    content = str(nudges[0]["content"])
    assert "observe" in content and "get_html" in content
    assert "solve_captcha" in content  # offered tool is named as an unblocker
    assert "finish" in content


@pytest.mark.asyncio
async def test_stall_nudge_omits_solve_captcha_when_not_offered() -> None:
    script = [[("observe", {})] for _ in range(7)]
    tools = [_perception_tool("observe", "url=x frozen"), make_finish_tool()]
    outcome, _ = await _run(script, tools, max_turns=200, max_tool_calls=500)
    nudges = [m for m in outcome.messages if m.get("role") == "user" and "not changing" in str(m.get("content"))]
    assert len(nudges) == 1
    assert "solve_captcha" not in str(nudges[0]["content"])


@pytest.mark.asyncio
async def test_completed_static_page_with_confirmatory_reobserves_is_not_misclassified() -> None:
    # After an async submit swaps the form for a static confirmation banner, a careful model may
    # re-observe the unchanged page several times before finishing. That must stay a completed
    # verdict — the stall policy exists for runs that never finish, not for double-checking.
    script = [[("observe", {})] for _ in range(10)] + [
        [("finish", {"status": "completed", "reason": "confirmation banner present"})]
    ]
    tools = [_perception_tool("observe", "url=x text: 'Success — application received'"), make_finish_tool()]
    outcome, _ = await _run(script, tools, max_turns=200, max_tool_calls=500)
    assert outcome.status == "completed"


@pytest.mark.asyncio
async def test_perception_stall_counter_resets_and_reclimbs_without_tripping() -> None:
    # Exercise the actual reset transition: 10 identical, a change, 10 identical again — neither
    # streak reaches the threshold, so the run must complete normally.
    contents = ["url=x page1"] * 10 + ["url=x page2"] * 10
    script = [[("observe", {})] for _ in range(20)] + [[("finish", {"status": "completed", "reason": "done"})]]
    tools = [_perception_tool("observe", contents), make_finish_tool()]
    outcome, _ = await _run(script, tools, max_turns=200, max_tool_calls=500)
    assert outcome.status == "completed"


@pytest.mark.asyncio
async def test_perception_stall_verdict_reason_carries_facetable_prefix() -> None:
    # Telemetry counts policy firings by this prefix; a bounded verdict nobody can query is a
    # silent policy.
    from skyvern.forge.taskv3.loop import PERCEPTION_STALL_REASON_PREFIX

    script = [[("observe", {})] for _ in range(20)]
    tools = [_perception_tool("observe", "url=x frozen"), make_finish_tool()]
    outcome, _ = await _run(script, tools, max_turns=200, max_tool_calls=500)
    assert outcome.status == "terminated"
    assert outcome.reason.startswith(PERCEPTION_STALL_REASON_PREFIX)


def _billable_tool(
    name: str, sink: list[tuple[str, dict[str, Any]]], *, data: dict[str, Any] | None = None
) -> ToolSpec:
    async def handler(args: dict[str, Any]) -> ToolResult:
        sink.append((name, args))
        return ToolResult.ok(f"{name} done", data=data)

    return ToolSpec(
        name=name, description=name, parameters={"type": "object", "properties": {}}, handler=handler, billable=True
    )


_REJECTION_OBSERVE = "url=x text: 'We couldn't submit your application. Please submit your application again.'"


def _resubmit_script(submits: int) -> list[list[tuple[str, dict[str, Any]]]]:
    """The live 12-resubmit signature: same submit click, re-observe shows the same rejection."""
    script: list[list[tuple[str, dict[str, Any]]]] = []
    for _ in range(submits):
        script.append([("click", {"selector": "#submit"})])
        script.append([("observe", {})])
    return script


@pytest.mark.asyncio
async def test_action_loop_terminates_with_bounded_verdict_on_resubmit_signature() -> None:
    # The live failure shape: a rejection banner saying "please submit again" drove 12 re-clicks of
    # the same submit button (132 tool calls) while every observe showed the same unchanged banner.
    # The perception stream varied enough (interleaved actions) that the stall policy never fired.
    # Repeating the same action against unchanged observed state must end with a bounded verdict.
    from skyvern.forge.taskv3.loop import ACTION_LOOP_REASON_PREFIX

    clicks: list[tuple[str, dict[str, Any]]] = []
    tools = [_billable_tool("click", clicks), _perception_tool("observe", _REJECTION_OBSERVE), make_finish_tool()]
    outcome, caller = await _run(_resubmit_script(12), tools, max_turns=200, max_tool_calls=500)
    assert outcome.status == "terminated"
    assert outcome.reason.startswith(ACTION_LOOP_REASON_PREFIX)
    assert "click" in outcome.reason and "#submit" in outcome.reason
    assert len(clicks) < 12  # bounded well below the live 12-resubmit runaway
    assert caller.calls <= 15


@pytest.mark.asyncio
async def test_action_loop_warns_once_naming_action_count_and_unchanged_state() -> None:
    # Compaction elides superseded observes, so the model cannot see its own repetition in the
    # transcript. The warn is that lost memory: WHICH action, HOW MANY times, and that the observed
    # state did not change — specific enough that the model can self-correct instead of dying at
    # the terminate backstop.
    clicks: list[tuple[str, dict[str, Any]]] = []
    tools = [_billable_tool("click", clicks), _perception_tool("observe", _REJECTION_OBSERVE), make_finish_tool()]
    outcome, _ = await _run(_resubmit_script(12), tools, max_turns=200, max_tool_calls=500)
    warns = [m for m in outcome.messages if m.get("role") == "user" and "#submit" in str(m.get("content"))]
    assert len(warns) == 1
    content = str(warns[0]["content"])
    assert "click" in content and "3" in content
    assert "unchanged" in content
    assert "finish" in content
    assert outcome.messages.index(warns[0]) < len(outcome.messages) - 1  # warned before the verdict


@pytest.mark.asyncio
async def test_action_loop_warn_recovery_keeps_verdict_with_the_model() -> None:
    # The warn is the primary deliverable, the terminate only a backstop: a model that acts on the
    # warning (finishes honestly with the real rejection) must keep its own verdict — the guard
    # never takes over.
    script = _resubmit_script(3) + [[("finish", {"status": "failed", "reason": "submission rejected by the site"})]]
    clicks: list[tuple[str, dict[str, Any]]] = []
    tools = [_billable_tool("click", clicks), _perception_tool("observe", _REJECTION_OBSERVE), make_finish_tool()]
    outcome, _ = await _run(script, tools, max_turns=200, max_tool_calls=500)
    assert outcome.status == "failed"
    assert outcome.reason == "submission rejected by the site"
    assert len(clicks) == 3


@pytest.mark.asyncio
async def test_action_loop_catches_varied_probe_evasion() -> None:
    # The second live signature's SHAPE, extended past where the production run's token budget
    # killed it (the real trace reached 4 identical tail clicks — warn territory): fresh-selector
    # get_html probes each return different content, so every probe resets the per-content stall
    # streak, while the same click keeps repeating. A first-time probe is evidence of nothing (no
    # baseline), so it must NOT reset the action counter, and the cap must land.
    from skyvern.forge.taskv3.loop import ACTION_LOOP_REASON_PREFIX, PERCEPTION_STALL_REASON_PREFIX

    probe_contents = [f"<div>fragment {i}</div>" for i in range(12)]
    script: list[list[tuple[str, dict[str, Any]]]] = []
    for i in range(12):
        script.append([("click", {"selector": "#continue"})])
        script.append([("get_html", {"selector": f"#probe{i}"})])
    clicks: list[tuple[str, dict[str, Any]]] = []
    tools = [_billable_tool("click", clicks), _perception_tool("get_html", probe_contents), make_finish_tool()]
    outcome, caller = await _run(script, tools, max_turns=200, max_tool_calls=500)
    assert outcome.status == "terminated"
    assert outcome.reason.startswith(ACTION_LOOP_REASON_PREFIX)
    assert not outcome.reason.startswith(PERCEPTION_STALL_REASON_PREFIX)
    assert len(clicks) < 12
    assert caller.calls <= 15


@pytest.mark.asyncio
async def test_pagination_with_changing_page_content_never_trips_action_loop() -> None:
    # Healthy pagination clicks the same Next selector many times, but each page's observe differs —
    # a repeated probe returning different content is evidence of progress and must clear the guard.
    contents = [f"url=x page{i} rows for page {i}" for i in range(10)]
    script: list[list[tuple[str, dict[str, Any]]]] = []
    for _ in range(10):
        script.append([("click", {"selector": "#next"})])
        script.append([("observe", {})])
    script.append([("finish", {"status": "completed", "reason": "all pages read"})])
    clicks: list[tuple[str, dict[str, Any]]] = []
    tools = [_billable_tool("click", clicks), _perception_tool("observe", contents), make_finish_tool()]
    outcome, _ = await _run(script, tools, max_turns=200, max_tool_calls=500)
    assert outcome.status == "completed"
    assert len(clicks) == 10
    assert not any(m.get("role") == "user" and "#next" in str(m.get("content")) for m in outcome.messages)


@pytest.mark.asyncio
async def test_retry_after_fixing_a_field_never_trips_action_loop() -> None:
    # Legitimate multi-submit: each retry follows a fix that visibly changed the page (validation
    # summary shrinks, field value appears), so the guard's state evidence resets between attempts.
    contents = [
        "url=x text: 'Error: field A is required' [#a] input ''",
        "url=x text: 'Error: field A is required' [#a] input value='v1'",
        "url=x text: 'Error: field B is required' [#a] input value='v1'",
        "url=x text: 'Error: field B is required' [#b] input value='v2'",
        "url=x text: 'Application received'",
    ]
    script = [
        [("click", {"selector": "#submit"})],
        [("observe", {})],
        [("type", {"selector": "#a", "text": "v1"})],
        [("observe", {})],
        [("click", {"selector": "#submit"})],
        [("observe", {})],
        [("type", {"selector": "#b", "text": "v2"})],
        [("observe", {})],
        [("click", {"selector": "#submit"})],
        [("observe", {})],
        [("finish", {"status": "completed", "reason": "application received"})],
    ]
    clicks: list[tuple[str, dict[str, Any]]] = []
    types: list[tuple[str, dict[str, Any]]] = []
    tools = [
        _billable_tool("click", clicks),
        _billable_tool("type", types),
        _perception_tool("observe", contents),
        make_finish_tool(),
    ]
    outcome, _ = await _run(script, tools, max_turns=200, max_tool_calls=500)
    assert outcome.status == "completed"
    assert len(clicks) == 3


@pytest.mark.asyncio
async def test_repeated_download_clicks_never_trip_action_loop() -> None:
    # A "download next file" flow legitimately clicks the same selector many times against a page
    # that never changes; each click's download notice is the progress evidence.
    script = [[("click", {"selector": "#download-next"})] for _ in range(10)] + [
        [("finish", {"status": "completed", "reason": "all files downloaded"})]
    ]
    clicks: list[tuple[str, dict[str, Any]]] = []
    tools = [_billable_tool("click", clicks, data={"download_notice": True}), make_finish_tool()]
    outcome, _ = await _run(script, tools, max_turns=200, max_tool_calls=500)
    assert outcome.status == "completed"
    assert len(clicks) == 10


@pytest.mark.asyncio
async def test_double_submit_never_trips_or_warns() -> None:
    # One retry is within policy ("at most one retry, then finish honestly") — two identical
    # submits against an unchanged banner must produce neither a warning nor a verdict.
    script = _resubmit_script(2) + [[("finish", {"status": "failed", "reason": "rejected twice, reporting honestly"})]]
    clicks: list[tuple[str, dict[str, Any]]] = []
    tools = [_billable_tool("click", clicks), _perception_tool("observe", _REJECTION_OBSERVE), make_finish_tool()]
    outcome, _ = await _run(script, tools, max_turns=200, max_tool_calls=500)
    assert outcome.status == "failed"
    assert len(clicks) == 2
    assert not any(m.get("role") == "user" and "#submit" in str(m.get("content")) for m in outcome.messages)


@pytest.mark.asyncio
async def test_action_loop_warn_and_terminate_emit_facetable_logs() -> None:
    # "Warns followed by recovery" is the metric that proves the guard improves runs rather than
    # capping them — both the warn and the verdict must be queryable events, like the stall policy's.
    clicks: list[tuple[str, dict[str, Any]]] = []
    tools = [_billable_tool("click", clicks), _perception_tool("observe", _REJECTION_OBSERVE), make_finish_tool()]
    with capture_logs() as logs:
        outcome, _ = await _run(_resubmit_script(12), tools, max_turns=200, max_tool_calls=500)
    assert outcome.status == "terminated"
    warned = [entry for entry in logs if entry["event"] == "taskv3 loop action repeat nudged"]
    terminated = [entry for entry in logs if entry["event"] == "taskv3 loop action repeated"]
    assert len(warned) == 1 and warned[0]["tool"] == "click" and warned[0]["repeat_count"] == 3
    assert len(terminated) == 1 and terminated[0]["tool"] == "click" and terminated[0]["repeat_count"] == 6


@pytest.mark.asyncio
async def test_interleaved_changing_probe_keeps_stall_policy_from_firing() -> None:
    # Pins the stall policy's per-tool CONSECUTIVE comparison, which the nine #15621 tests cannot
    # (they never vary a probe's args): re-reading a static region interleaved with a sibling probe
    # that changes every read is a LIVE page, and the stall verdict must never fire on it —
    # accounting keyed per (tool, args) would accumulate the static region to the threshold.
    calls = {"n": 0}

    async def handler(args: dict[str, Any]) -> ToolResult:
        calls["n"] += 1
        if args.get("selector") == "#status":
            return ToolResult.ok("<div>processing</div>")
        return ToolResult.ok(f"<div>log line {calls['n']}</div>")

    probe = ToolSpec(
        name="get_html",
        description="g",
        parameters={"type": "object", "properties": {}},
        handler=handler,
        compactable=True,
    )
    script: list[list[tuple[str, dict[str, Any]]]] = []
    for _ in range(20):
        script.append([("get_html", {"selector": "#status"})])
        script.append([("get_html", {"selector": "#log"})])
    script.append([("finish", {"status": "completed", "reason": "job finished"})])
    outcome, _ = await _run(script, [probe, make_finish_tool()], max_turns=200, max_tool_calls=500)
    assert outcome.status == "completed"


@pytest.mark.asyncio
async def test_interleaved_changing_probe_is_reported_as_a_would_fire_shadow() -> None:
    # Same fixture as the spec above, which stays unmodified. A static region re-read alongside a
    # ticking sibling is indistinguishable, byte for byte, from a frozen page whose sibling ticks
    # (a clock, a log tail): a cross-probe clear that let the sibling's progress reset this streak
    # kept a byte-frozen page alive to the budget cap where the per-tool counter had ended it at
    # the threshold. So the per-probe streak is NOT cleared by the sibling; it trips here and is
    # reported as a shadow false positive — the precision the rollout measures — never acted on.
    calls = {"n": 0}

    async def handler(args: dict[str, Any]) -> ToolResult:
        calls["n"] += 1
        if args.get("selector") == "#status":
            return ToolResult.ok("<div>processing</div>")
        return ToolResult.ok(f"<div>log line {calls['n']}</div>")

    probe = ToolSpec(
        name="get_html",
        description="g",
        parameters={"type": "object", "properties": {}},
        handler=handler,
        compactable=True,
    )
    script: list[list[tuple[str, dict[str, Any]]]] = []
    for _ in range(20):
        script.append([("get_html", {"selector": "#status"})])
        script.append([("get_html", {"selector": "#log"})])
    script.append([("finish", {"status": "completed", "reason": "job finished"})])
    with capture_logs() as logs:
        outcome, _ = await _run(script, [probe, make_finish_tool()], max_turns=200, max_tool_calls=500)
    assert outcome.status == "completed"
    assert [entry for entry in logs if entry.get("event") == PERCEPTION_STALL_SHADOW_EVENT]


@pytest.mark.asyncio
async def test_frozen_observe_with_a_ticking_sibling_probe_still_terminates_at_the_threshold() -> None:
    # A page frozen behind a gate, read through an argument-free observe, while the run also reads
    # a region that changes on every call. The nudge text itself asks for "one targeted get_html
    # look", so this is the shape the guard's own advice produces. The per-tool counter ended it at
    # the threshold; letting the sibling's progress clear the observe streak ran it to the budget
    # cap with no nudge and no verdict.
    calls = {"n": 0}

    async def ticking(args: dict[str, Any]) -> ToolResult:
        calls["n"] += 1
        return ToolResult.ok(f"<div>00:{calls['n']:02d}</div>")

    sibling = ToolSpec(
        name="get_html",
        description="g",
        parameters={"type": "object", "properties": {}},
        handler=ticking,
        compactable=True,
    )
    tools = [_perception_tool("observe", "url=x frozen behind a gate"), sibling, make_finish_tool()]
    script: list[list[tuple[str, dict[str, Any]]]] = []
    for _ in range(40):
        script.append([("observe", {})])
        script.append([("get_html", {"selector": "#clock"})])
    script.append([("finish", {"status": "completed", "reason": "done"})])
    outcome, caller = await _run(script, tools, max_turns=200, max_tool_calls=500)
    assert outcome.status == "terminated"
    assert outcome.reason.startswith(PERCEPTION_STALL_REASON_PREFIX)
    assert caller.calls <= 2 * PERCEPTION_STALL_TERMINATE_AFTER


def _main_would_fire(sequence: list[tuple[str, str, str]], threshold: int) -> bool:
    """The argument-blind per-tool counter the guard shipped with: identical runs per tool name."""
    counts: dict[str, tuple[str, int]] = {}
    for tool, _args, content in sequence:
        prev = counts.get(tool)
        count = prev[1] + 1 if prev is not None and prev[0] == content else 1
        counts[tool] = (content, count)
        if count >= threshold:
            return True
    return False


@pytest.mark.parametrize("seed", range(200))
def test_live_stall_firing_is_a_subset_of_the_per_tool_counter(seed: int) -> None:
    # The central claim of keying per probe: the loop never terminates a run the per-tool counter
    # would have let live. Enumerated over random probe sequences, because seven hand-picked
    # shapes cannot guard a future loosening of the conjunct.
    rng = random.Random(seed)
    threshold = 4
    sequence = [
        (rng.choice(["observe", "get_html"]), rng.choice(["a", "b", "c"]), rng.choice(["X", "Y"]))
        for _ in range(rng.randint(1, 40))
    ]
    ledger = _PerceptionLedger()
    for index, (tool, args, content) in enumerate(sequence):
        if ledger.record((tool, args), content).live >= threshold:
            assert _main_would_fire(sequence[: index + 1], threshold)
            break


@pytest.mark.parametrize("seed", range(200))
def test_imminent_is_exactly_whether_one_more_read_of_some_probe_trips_live(seed: int) -> None:
    # ``next_snapshot_can_trip`` is pinned by simulation, not by restating its formula: after every
    # prefix, it must equal "re-reading SOME probe once, returning what it last returned, reaches
    # the threshold live". A dormant probe whose tool has since moved on to other content must
    # therefore read False, or the failure-evidence deferral is disarmed by a trip that cannot come.
    rng = random.Random(seed)
    threshold = rng.choice([2, 3, 4])
    sequence = [
        (rng.choice(["observe", "get_html"]), rng.choice(["a", "b", "c"]), rng.choice(["X", "Y"]))
        for _ in range(rng.randint(1, 40))
    ]
    ledger = _PerceptionLedger()
    for tool, args, content in sequence:
        ledger.record((tool, args), content)
        trips_on_one_more_read = False
        for key, probe in ledger._probes.items():
            trial = copy.deepcopy(ledger)
            if trial.record(key, probe.history[-1]).live >= threshold:
                trips_on_one_more_read = True
                break
        assert ledger.next_snapshot_can_trip(threshold) == trips_on_one_more_read


@pytest.mark.asyncio
async def test_dormant_probe_at_the_edge_does_not_disarm_the_deferral_once_its_tool_moved_on() -> None:
    # One probe read three times at the edge of a 4-snapshot terminator, then never again; the tool
    # then reads three DISTINCT dropdowns that all say "Select One". No probe can trip on its next
    # read — the dormant one's content is no longer the tool's, the fresh ones have no streak — so
    # the failure-evidence gate must still hold the verdict for its one evidence turn.
    async def handler(args: dict[str, Any]) -> ToolResult:
        return ToolResult.ok("frozen region" if args.get("selector") == "#a" else "Select One")

    probe = ToolSpec(
        name="get_html",
        description="g",
        parameters={"type": "object", "properties": {}},
        handler=handler,
        compactable=True,
    )
    activity = ActivityRecency()
    fingerprint, fp_calls = _fingerprint_seq(["fp"])
    clicks: list[tuple[str, dict[str, Any]]] = []
    tools = [
        probe,
        _billable_tool("click", clicks),
        make_finish_tool(page_fingerprint=fingerprint, activity=activity, settle_wait_seconds=0.0),
    ]
    script: list[list[tuple[str, dict[str, Any]]]] = [[("get_html", {"selector": "#a"})] for _ in range(3)]
    script += [[("get_html", {"selector": f"#dd-{i}"})] for i in range(3)]
    script += [
        [("click", {"selector": "#submit"})],
        [("finish", {"status": "failed", "reason": "could not submit"})],
        [("finish", {"status": "failed", "reason": "still could not submit"})],
    ]
    outcome, _ = await _run(script, tools, activity=activity, stall_terminate_after=4, stall_nudge_after=2)
    assert outcome.status == "failed"
    assert outcome.reason == "still could not submit"
    assert fp_calls["n"] == 1
    assert not activity.perception_stall_imminent


@pytest.mark.asyncio
async def test_distinct_probes_returning_the_same_string_are_not_a_frozen_page() -> None:
    # A run read 15 DISTINCT dropdowns, 14 of them different selectors, each returning the same
    # 10-byte "Select One" — and was terminated at step 4 of 7 with a responsive dropdown on
    # screen. Keying on tool name alone cannot tell "this probe saw the same thing again" from
    # "a different probe happened to return the same string", so reading a form full of
    # not-yet-chosen dropdowns is indistinguishable from a page that stopped responding.
    async def handler(args: dict[str, Any]) -> ToolResult:
        return ToolResult.ok("Select One")

    probe = ToolSpec(
        name="get_html",
        description="g",
        parameters={"type": "object", "properties": {}},
        handler=handler,
        compactable=True,
    )
    script: list[list[tuple[str, dict[str, Any]]]] = [[("get_html", {"selector": f"#dd-{i}"})] for i in range(20)]
    script.append([("finish", {"status": "completed", "reason": "read every dropdown"})])
    with capture_logs() as logs:
        outcome, _ = await _run(script, [probe, make_finish_tool()], max_turns=200, max_tool_calls=500)
    assert outcome.status == "completed"
    # The per-tool counter did trip here; the event is how often this shape occurs in production.
    suppressed = [entry for entry in logs if entry.get("event") == PERCEPTION_STALL_SUPPRESSED_EVENT]
    assert [entry["tool"] for entry in suppressed] == ["get_html"]


@pytest.mark.asyncio
async def test_double_reading_many_regions_is_neither_a_stall_nor_a_disarmed_deferral() -> None:
    # A careful run re-reads each of fourteen regions once to confirm it. No probe repeats more than
    # twice and every region differs, so there is no streak to sum: the run must not terminate, and
    # the failure-evidence gate must still hold the later verdict for its one evidence turn.
    async def handler(args: dict[str, Any]) -> ToolResult:
        return ToolResult.ok(f"region {args.get('selector')}")

    probe = ToolSpec(
        name="get_html",
        description="g",
        parameters={"type": "object", "properties": {}},
        handler=handler,
        compactable=True,
    )
    activity = ActivityRecency()
    fingerprint, fp_calls = _fingerprint_seq(["fp"])
    clicks: list[tuple[str, dict[str, Any]]] = []
    tools = [
        probe,
        _billable_tool("click", clicks),
        make_finish_tool(page_fingerprint=fingerprint, activity=activity, settle_wait_seconds=0.0),
    ]
    script: list[list[tuple[str, dict[str, Any]]]] = []
    for region in range(14):
        script.append([("get_html", {"selector": f"#r{region}"})])
        script.append([("get_html", {"selector": f"#r{region}"})])
    script += [
        [("click", {"selector": "#submit"})],
        [("finish", {"status": "failed", "reason": "could not submit"})],
        [("finish", {"status": "failed", "reason": "still could not submit"})],
    ]
    outcome, _ = await _run(script, tools, activity=activity, max_turns=200, max_tool_calls=500)
    assert outcome.status == "failed"
    assert outcome.reason == "still could not submit"
    assert fp_calls["n"] == 1
    assert not activity.perception_stall_imminent


@pytest.mark.asyncio
async def test_period_two_oscillation_is_a_stall_even_though_no_two_calls_match() -> None:
    # A control toggled open and closed under the run's own clicks: observe alternated strictly
    # between two states for 7 consecutive calls while the same click repeated 21 times. No two
    # CONSECUTIVE results are identical, so a counter that resets on any difference never reaches
    # 2 — let alone the threshold — and the run spends its whole budget going nowhere. Returning
    # to a state already seen is not evidence of progress.
    # Reported, not acted on: terminating here is NEW firing against a population nobody has
    # measured, and the step engine's tripwires earn that right by publishing this event first.
    contents = ["state-A", "state-B"] * 30
    script = [[("observe", {})] for _ in range(60)]
    script.append([("finish", {"status": "completed", "reason": "done"})])
    tools = [_perception_tool("observe", contents), make_finish_tool()]
    with capture_logs() as logs:
        outcome, _ = await _run(script, tools, max_turns=200, max_tool_calls=500)
    would_fire = [entry for entry in logs if entry.get("event") == PERCEPTION_STALL_SHADOW_EVENT]
    assert len(would_fire) == 1
    assert would_fire[0]["snapshots"] >= PERCEPTION_STALL_TERMINATE_AFTER
    assert outcome.status == "completed"
    assert not outcome.reason.startswith(PERCEPTION_STALL_REASON_PREFIX)


@pytest.mark.asyncio
async def test_stall_nudge_is_delivered_once_per_streak_not_once_per_turn() -> None:
    # The nudge tells the model to stop re-observing and take one targeted look instead — advice
    # that stops the streak advancing. Re-sending it every turn afterwards fills the transcript with
    # copies of a warning the model already obeyed, each asserting a count from an earlier turn.
    clicks: list[tuple[str, dict[str, Any]]] = []
    tools = [
        _perception_tool("observe", "url=x frozen behind a gate"),
        _billable_tool("click", clicks),
        make_finish_tool(),
    ]
    # Reach the threshold, then do what the nudge asks: stop re-observing and act instead. Those
    # rounds carry no repeated perception, so the count stands still at exactly the threshold.
    script: list[list[tuple[str, dict[str, Any]]]] = [[("observe", {})] for _ in range(PERCEPTION_STALL_NUDGE_AFTER)]
    script += [[("click", {"selector": "#retry"})] for _ in range(6)]
    script.append([("finish", {"status": "completed", "reason": "done"})])
    outcome, _ = await _run(script, tools, max_turns=200, max_tool_calls=500)
    nudges = [
        message
        for message in outcome.messages
        if message.get("role") == "user" and "identical output" in str(message.get("content"))
    ]
    assert len(nudges) == 1


def _stall_warnings(outcome: LoopOutcome) -> list[dict[str, Any]]:
    return [
        message
        for message in outcome.messages
        if message.get("role") == "user" and "identical output" in str(message.get("content"))
    ]


@pytest.mark.asyncio
async def test_stall_verdict_is_preceded_by_exactly_one_warning_even_when_live_skips_the_threshold() -> None:
    # ``live`` is a min of two counters, not a by-one counter: a single region with content resets
    # the tool counter while a frozen region's own streak keeps climbing, and re-reading other empty
    # regions then lifts the tool counter back past the threshold — so ``live`` jumps 1 → 7 and an
    # equality test never sees the threshold. The verdict must not arrive with zero warnings.
    async def handler(args: dict[str, Any]) -> ToolResult:
        return ToolResult.ok("<div>Please wait</div>" if args.get("selector") == "#status" else "<div></div>")

    probe = ToolSpec(
        name="get_html",
        description="g",
        parameters={"type": "object", "properties": {}},
        handler=handler,
        compactable=True,
    )
    selectors = ["#form"] * 5 + ["#status"] + ["#form"] + ["#other"] * 5 + ["#form"] * 10
    script: list[list[tuple[str, dict[str, Any]]]] = [[("get_html", {"selector": s})] for s in selectors]
    script.append([("finish", {"status": "failed", "reason": "blocked"})])
    outcome, _ = await _run(script, [probe, make_finish_tool()], max_turns=200, max_tool_calls=500)
    assert outcome.status == "terminated"
    assert outcome.reason.startswith(PERCEPTION_STALL_REASON_PREFIX)
    assert len(_stall_warnings(outcome)) == 1


@pytest.mark.asyncio
async def test_one_warning_per_stall_not_one_per_probe() -> None:
    # Two selectors alternating over one frozen string each cross the threshold on their own
    # streak; the page stalled once, so the model is told once, as the argument-blind counter did.
    async def handler(args: dict[str, Any]) -> ToolResult:
        return ToolResult.ok("frozen")

    probe = ToolSpec(
        name="get_html",
        description="g",
        parameters={"type": "object", "properties": {}},
        handler=handler,
        compactable=True,
    )
    script: list[list[tuple[str, dict[str, Any]]]] = [
        [("get_html", {"selector": selector})] for _ in range(20) for selector in ("#a", "#b")
    ]
    script.append([("finish", {"status": "failed", "reason": "blocked"})])
    outcome, _ = await _run(script, [probe, make_finish_tool()], max_turns=200, max_tool_calls=500)
    assert outcome.status == "terminated"
    assert len(_stall_warnings(outcome)) == 1


@pytest.mark.asyncio
async def test_a_turn_that_batches_identical_probes_counts_every_one_of_them() -> None:
    # The threshold is denominated in snapshots and the system prompt commands batching. Counting a
    # five-probe turn as one snapshot would let a batching run burn several times the budget the
    # threshold exists to bound, while the verdict rate falls for reasons unrelated to any page.
    tools = [_perception_tool("observe", "url=x frozen behind a gate"), make_finish_tool()]
    script = [[("observe", {})] * 5 for _ in range(10)]
    script.append([("finish", {"status": "completed", "reason": "done"})])
    outcome, caller = await _run(script, tools, max_turns=200, max_tool_calls=500)
    assert outcome.status == "terminated"
    assert caller.calls <= 4


@pytest.mark.asyncio
async def test_an_action_batched_with_the_stall_verdict_never_executes() -> None:
    # The verdict means the run is over. A submit sitting after the observe in the same batch must
    # not still reach the page — it would meter a step and mutate a site on a run already ended.
    clicks: list[tuple[str, dict[str, Any]]] = []
    tools = [
        _perception_tool("observe", "url=x frozen behind a gate"),
        _billable_tool("click", clicks),
        make_finish_tool(),
    ]
    script = [[("observe", {}), ("click", {"selector": "#submit"})] for _ in range(10)]
    script.append([("finish", {"status": "completed", "reason": "done"})])
    outcome, _ = await _run(script, tools, max_turns=200, max_tool_calls=500, stall_terminate_after=3)
    assert outcome.status == "terminated"
    assert len(clicks) == 2


@pytest.mark.asyncio
async def test_batched_identical_clicks_with_in_batch_change_never_trip_or_warn() -> None:
    # The system prompt commands batching ("never spend a separate turn on each click"), so eight
    # date-picker arrow clicks in ONE turn are healthy. The streak must not terminate within a
    # single turn, and the batched observe that shows the change must also retract the queued warn.
    contents = ["url=x month=January", "url=x month=September"]
    script = [
        [("observe", {})],
        [("click", {"selector": "#next-month"})] * 8 + [("observe", {})],
        [("finish", {"status": "completed", "reason": "date reached"})],
    ]
    clicks: list[tuple[str, dict[str, Any]]] = []
    tools = [_billable_tool("click", clicks), _perception_tool("observe", contents), make_finish_tool()]
    outcome, _ = await _run(script, tools, max_turns=200, max_tool_calls=500)
    assert outcome.status == "completed"
    assert len(clicks) == 8
    assert not any(m.get("role") == "user" and "#next-month" in str(m.get("content")) for m in outcome.messages)


@pytest.mark.asyncio
async def test_single_batch_repeats_without_feedback_never_warn() -> None:
    # Five identical clicks batched in ONE turn with no probe at all (a stepper spammed blind) get
    # no warning either — the warn text claims "the state you last observed is unchanged", which is
    # false when nothing was observed between attempts. The streak stays armed for later turns.
    script = [
        [("click", {"selector": "#add-row"})] * 5,
        [("finish", {"status": "completed", "reason": "rows added"})],
    ]
    clicks: list[tuple[str, dict[str, Any]]] = []
    tools = [_billable_tool("click", clicks), make_finish_tool()]
    outcome, _ = await _run(script, tools, max_turns=200, max_tool_calls=500)
    assert outcome.status == "completed"
    assert len(clicks) == 5
    assert not any(m.get("role") == "user" and "#add-row" in str(m.get("content")) for m in outcome.messages)


@pytest.mark.asyncio
async def test_warn_always_precedes_terminate_even_after_single_batch_burst() -> None:
    # A burst that crosses the terminate threshold before any warning could be delivered must not
    # be terminated on the spot: the verdict waits until the model has seen the warning and
    # repeated anyway.
    from skyvern.forge.taskv3.loop import ACTION_LOOP_REASON_PREFIX

    script = [
        [("click", {"selector": "#submit"})] * 5,
        [("click", {"selector": "#submit"})],
        [("click", {"selector": "#submit"})],
    ]
    clicks: list[tuple[str, dict[str, Any]]] = []
    tools = [_billable_tool("click", clicks), make_finish_tool()]
    outcome, _ = await _run(script, tools, max_turns=200, max_tool_calls=500)
    assert outcome.status == "terminated"
    assert outcome.reason.startswith(ACTION_LOOP_REASON_PREFIX)
    assert len(clicks) == 7  # 5 burst + 1 post-warn-queue + 1 post-warn-delivery
    warns = [m for m in outcome.messages if m.get("role") == "user" and "#submit" in str(m.get("content"))]
    assert len(warns) == 1
    assert outcome.messages.index(warns[0]) < len(outcome.messages) - 1


@pytest.mark.asyncio
async def test_action_loop_counts_errored_attempts() -> None:
    # A submit whose click errors on every attempt burns budget exactly like one that returns ok —
    # and a dispatched error already consumes the action-step budget, so the guard counts it too.
    from skyvern.forge.taskv3.loop import ACTION_LOOP_REASON_PREFIX

    clicks: list[tuple[str, dict[str, Any]]] = []
    click = _recording_tool("click", clicks, raises=True)
    click.billable = True
    script = [[("click", {"selector": "#dead"})] for _ in range(10)]
    outcome, _ = await _run(script, [click, make_finish_tool()], max_turns=200, max_tool_calls=500)
    assert outcome.status == "terminated"
    assert outcome.reason.startswith(ACTION_LOOP_REASON_PREFIX)
    assert len(clicks) == 6


@pytest.mark.asyncio
async def test_navigate_resets_action_counters() -> None:
    # A retry AFTER navigating is a fresh attempt against a fresh page (the live trace's re-fill
    # bursts), not a continuation of the old streak.
    async def nav_handler(args: dict[str, Any]) -> ToolResult:
        return ToolResult.ok("navigated", data={"page_state_changed": True})

    nav = ToolSpec(
        name="navigate", description="n", parameters={"type": "object", "properties": {}}, handler=nav_handler
    )
    clicks: list[tuple[str, dict[str, Any]]] = []
    script = (
        [[("click", {"selector": "#submit"})] for _ in range(5)]
        + [[("navigate", {"url": "https://forms.example.test/apply"})]]
        + [[("click", {"selector": "#submit"})] for _ in range(5)]
        + [[("finish", {"status": "completed", "reason": "second attempt accepted"})]]
    )
    tools = [_billable_tool("click", clicks), nav, make_finish_tool()]
    outcome, _ = await _run(script, tools, max_turns=200, max_tool_calls=500)
    assert outcome.status == "completed"
    assert len(clicks) == 10


def _captcha_tool(results: list[str]) -> ToolSpec:
    """solve_captcha fake: recordable, non-billable, returns each result as a tool ERROR (the
    tri-state's not-solved arm) — the arm the false-negative verdicts followed in production."""
    calls = {"n": 0}

    async def handler(args: dict[str, Any]) -> ToolResult:
        content = results[min(calls["n"], len(results) - 1)]
        calls["n"] += 1
        return ToolResult.error(content)

    return ToolSpec(
        name="solve_captcha",
        description="solve_captcha",
        parameters={"type": "object", "properties": {}},
        handler=handler,
        recordable=True,
    )


def _fingerprint_seq(samples: list[str | None]):
    """Fingerprint fake returning each sample in order, repeating the last forever."""
    calls = {"n": 0}

    async def fingerprint() -> str | None:
        sample = samples[min(calls["n"], len(samples) - 1)]
        calls["n"] += 1
        return sample

    return fingerprint, calls


@pytest.mark.asyncio
async def test_finish_failed_after_captcha_defers_for_evidence_then_corrected_verdict() -> None:
    # The production false-negative shape: solve_captcha reports not-solved, the model immediately
    # calls finish(failed) — but the captcha protocol completes asynchronously and the submission
    # lands. The verdict must be held for one evidence turn; the fresh observe shows the
    # confirmation banner and the corrected verdict is completed.
    activity = ActivityRecency()
    fingerprint, fp_calls = _fingerprint_seq(["mid-flight", "submitted", "submitted"])
    tools = [
        _captcha_tool(["a captcha challenge is present but could not be solved this attempt"]),
        _perception_tool("observe", "url=x text: 'Application submitted!'"),
        make_finish_tool(page_fingerprint=fingerprint, activity=activity, settle_wait_seconds=0.01),
    ]
    script = [
        [("solve_captcha", {})],
        [("finish", {"status": "failed", "reason": "could_not_pass_captcha"})],
        [("observe", {})],
        [("finish", {"status": "completed", "reason": "the page shows the application was submitted"})],
    ]
    outcome, _ = await _run(script, tools, activity=activity)
    assert outcome.status == "completed"
    assert outcome.reason == "the page shows the application was submitted"
    deferrals = [
        m
        for m in outcome.messages
        if m.get("role") == "tool" and "held for one evidence check" in str(m.get("content"))
    ]
    assert len(deferrals) == 1
    # The quiescence wait must actually compare sample pairs: mid-flight vs submitted (mutating),
    # then submitted twice (stable exit) = 3 samples; the corrected finish(completed)'s own settle
    # probe adds 2 more.
    assert fp_calls["n"] == 5


@pytest.mark.asyncio
async def test_failure_gate_still_fires_when_completed_side_settle_is_disabled() -> None:
    # The bare-task configuration (SKY-14598): the sampler is supplied so the failure-evidence gate
    # can run, while max_settle_deferrals=0 keeps the completed-side settle probe off. Disabling one
    # gate must not disable the other — they share only the sampler.
    activity = ActivityRecency()
    fingerprint, _ = _fingerprint_seq(["mid-flight", "submitted", "submitted"])
    tools = [
        _captcha_tool(["a captcha challenge is present but could not be solved this attempt"]),
        _perception_tool("observe", "url=x text: 'Application submitted!'"),
        make_finish_tool(
            page_fingerprint=fingerprint,
            max_settle_deferrals=0,
            activity=activity,
            settle_wait_seconds=0.01,
        ),
    ]
    script = [
        [("solve_captcha", {})],
        [("finish", {"status": "failed", "reason": "could_not_pass_captcha"})],
        [("observe", {})],
        [("finish", {"status": "completed", "reason": "the page shows the application was submitted"})],
    ]
    outcome, _ = await _run(script, tools, activity=activity)
    assert outcome.status == "completed"
    deferrals = [
        m
        for m in outcome.messages
        if m.get("role") == "tool" and "held for one evidence check" in str(m.get("content"))
    ]
    assert len(deferrals) == 1


@pytest.mark.asyncio
async def test_completed_verdict_never_probes_when_settle_is_disabled() -> None:
    # The other half of the same scoping claim: with max_settle_deferrals=0 a completed verdict is
    # accepted as-is and the page is never probed for it. Asserting the sampler is untouched (not
    # merely that no deferral message appeared) is what makes "the completed path is unchanged for
    # bare tasks" checkable rather than asserted.
    activity = ActivityRecency()
    fingerprint, fp_calls = _fingerprint_seq(["mid-flight", "settled", "settled"])
    tools = [
        _recording_tool("click", []),
        make_finish_tool(
            page_fingerprint=fingerprint,
            max_settle_deferrals=0,
            activity=activity,
            settle_wait_seconds=0.01,
        ),
    ]
    script = [
        [("click", {"selector": "#submit"})],
        [("finish", {"status": "completed", "reason": "done"})],
    ]
    outcome, _ = await _run(script, tools, activity=activity)
    assert outcome.status == "completed"
    assert outcome.reason == "done"
    assert fp_calls["n"] == 0


@pytest.mark.asyncio
async def test_finish_failed_after_submit_click_defers_once_then_stands() -> None:
    # An honest blocked failure after a submit attempt (e.g. a persistent anti-spam banner) costs
    # exactly one evidence observe: the re-observe shows the same blocked state, the re-issued
    # failure is accepted unchanged.
    activity = ActivityRecency()
    fingerprint, _ = _fingerprint_seq(["fp"])
    clicks: list[tuple[str, dict[str, Any]]] = []
    tools = [
        _billable_tool("click", clicks),
        _perception_tool("observe", _REJECTION_OBSERVE),
        make_finish_tool(page_fingerprint=fingerprint, activity=activity, settle_wait_seconds=0.0),
    ]
    script = [
        [("click", {"selector": "#btn-submit"})],
        [("finish", {"status": "failed", "reason": "submission rejected"})],
        [("observe", {})],
        [("finish", {"status": "failed", "reason": "submission still rejected after re-observe"})],
    ]
    outcome, _ = await _run(script, tools, activity=activity)
    assert outcome.status == "failed"
    assert outcome.reason == "submission still rejected after re-observe"
    deferrals = [
        m
        for m in outcome.messages
        if m.get("role") == "tool" and "held for one evidence check" in str(m.get("content"))
    ]
    assert len(deferrals) == 1


@pytest.mark.asyncio
async def test_dead_posting_real_trace_shape_gains_at_most_one_observe() -> None:
    # Conservative dead-posting variant: the replayed dead-posting traces contain no trigger
    # actions at all (zero cost, pinned by the no-recent-trigger test); this pins the worst case
    # where a probing click lands in-window — the gate fires and the accepted cost is exactly one
    # deferral cycle, never more, and the verdict stands.
    activity = ActivityRecency()
    fingerprint, _ = _fingerprint_seq(["dead-page"])
    clicks: list[tuple[str, dict[str, Any]]] = []
    tools = [
        _perception_tool("get_html", "<h1>Page not found</h1>"),
        _perception_tool("observe", ["url=x text: 'The page you requested was not found'"] * 4),
        _billable_tool("click", clicks),
        make_finish_tool(page_fingerprint=fingerprint, activity=activity, settle_wait_seconds=0.0),
    ]
    script = [
        [("get_html", {})],
        [("observe", {})],
        [("click", {"selector": "#try-anyway"})],
        [("observe", {})],
        [("finish", {"status": "failed", "reason": "posting no longer exists"})],
        [("observe", {})],
        [("finish", {"status": "failed", "reason": "posting no longer exists (re-verified)"})],
    ]
    outcome, _ = await _run(script, tools, activity=activity)
    assert outcome.status == "failed"
    assert outcome.reason == "posting no longer exists (re-verified)"
    deferrals = [
        m
        for m in outcome.messages
        if m.get("role") == "tool" and "held for one evidence check" in str(m.get("content"))
    ]
    assert len(deferrals) == 1


@pytest.mark.asyncio
async def test_finish_failed_without_recent_trigger_is_not_gated() -> None:
    # A failure with no recent submit-class or captcha activity (missing input data, dead page
    # never interacted with) needs no page evidence: accepted immediately, page never sampled.
    activity = ActivityRecency()
    fingerprint, fp_calls = _fingerprint_seq(["fp"])
    tools = [
        _perception_tool("observe", "url=x text: 'Job not found'"),
        make_finish_tool(page_fingerprint=fingerprint, activity=activity, settle_wait_seconds=0.0),
    ]
    script = [
        [("observe", {})],
        [("finish", {"status": "failed", "reason": "the posting does not exist"})],
    ]
    outcome, _ = await _run(script, tools, activity=activity)
    assert outcome.status == "failed"
    assert outcome.turns == 2
    assert fp_calls["n"] == 0


@pytest.mark.asyncio
async def test_recency_window_boundary_arms_at_five_turns_and_expires_at_six() -> None:
    # Trigger activity expires exactly at the window edge: a click 6 turns back is not gated; the
    # same click 5 turns back still is. Pins FAILURE_EVIDENCE_WINDOW_TURNS in both directions.
    contents = [f"url=x step={i}" for i in range(10)]

    activity = ActivityRecency()
    fingerprint, fp_calls = _fingerprint_seq(["fp"])
    clicks: list[tuple[str, dict[str, Any]]] = []
    tools = [
        _billable_tool("click", clicks),
        _perception_tool("observe", contents),
        make_finish_tool(page_fingerprint=fingerprint, activity=activity, settle_wait_seconds=0.0),
    ]
    script = (
        [[("click", {"selector": "#expand"})]]
        + [[("observe", {})] for _ in range(5)]
        + [[("finish", {"status": "failed", "reason": "blocked"})]]
    )
    outcome, _ = await _run(script, tools, activity=activity)
    assert outcome.status == "failed"
    assert fp_calls["n"] == 0

    activity = ActivityRecency()
    fingerprint, fp_calls = _fingerprint_seq(["fp"])
    tools = [
        _billable_tool("click", clicks),
        _perception_tool("observe", contents),
        make_finish_tool(page_fingerprint=fingerprint, activity=activity, settle_wait_seconds=0.0),
    ]
    script = (
        [[("click", {"selector": "#expand"})]]
        + [[("observe", {})] for _ in range(4)]
        + [
            [("finish", {"status": "failed", "reason": "blocked"})],
            [("observe", {})],
            [("finish", {"status": "failed", "reason": "blocked (re-verified)"})],
        ]
    )
    outcome, _ = await _run(script, tools, activity=activity)
    assert outcome.status == "failed"
    assert outcome.reason == "blocked (re-verified)"
    assert fp_calls["n"] > 0


@pytest.mark.asyncio
async def test_errored_click_does_not_arm_the_evidence_gate() -> None:
    # A click that never dispatched cannot have an async tail; only successful clicks (or any
    # solve_captcha attempt) arm the gate.
    activity = ActivityRecency()
    fingerprint, fp_calls = _fingerprint_seq(["fp"])
    sink: list[tuple[str, dict[str, Any]]] = []
    click = _recording_tool("click", sink, raises=True)
    click.billable = True
    tools = [click, make_finish_tool(page_fingerprint=fingerprint, activity=activity, settle_wait_seconds=0.0)]
    script = [
        [("click", {"selector": "#submit"})],
        [("finish", {"status": "failed", "reason": "could not interact with the page"})],
    ]
    outcome, _ = await _run(script, tools, activity=activity)
    assert outcome.status == "failed"
    assert fp_calls["n"] == 0


@pytest.mark.asyncio
async def test_finish_failed_gate_skipped_when_no_page() -> None:
    # No page to observe (fingerprint samples None): a deferral would burn a turn on an observe
    # that cannot succeed, so the verdict is accepted as-is.
    activity = ActivityRecency()
    fingerprint, _ = _fingerprint_seq([None])
    clicks: list[tuple[str, dict[str, Any]]] = []
    tools = [
        _billable_tool("click", clicks),
        make_finish_tool(page_fingerprint=fingerprint, activity=activity, settle_wait_seconds=0.0),
    ]
    script = [
        [("click", {"selector": "#submit"})],
        [("finish", {"status": "failed", "reason": "page lost"})],
    ]
    outcome, _ = await _run(script, tools, activity=activity)
    assert outcome.status == "failed"
    assert outcome.reason == "page lost"


@pytest.mark.asyncio
async def test_finish_failed_gate_respects_remaining_turn_budget() -> None:
    # The corrected-verdict turn needs two turns (observe + re-finish). With no turn budget left
    # the gate must accept the honest verdict rather than convert it into budget_exhausted.
    activity = ActivityRecency()
    fingerprint, fp_calls = _fingerprint_seq(["fp"])
    clicks: list[tuple[str, dict[str, Any]]] = []
    tools = [
        _billable_tool("click", clicks),
        make_finish_tool(page_fingerprint=fingerprint, activity=activity, settle_wait_seconds=0.0),
    ]
    script = [
        [("click", {"selector": "#submit"})],
        [("finish", {"status": "failed", "reason": "blocked at the buzzer"})],
    ]
    outcome, _ = await _run(script, tools, activity=activity, max_turns=2)
    assert outcome.status == "failed"
    assert outcome.reason == "blocked at the buzzer"
    assert fp_calls["n"] == 0


@pytest.mark.asyncio
async def test_failure_settle_wait_is_bounded_on_a_never_quiet_page() -> None:
    # A page that never stops mutating cannot pin the settle wait: the quiescence loop gives up at
    # its cap and the deferral proceeds, still bounded to one evidence turn overall.
    activity = ActivityRecency()
    counter = iter(range(1000))

    async def never_quiet() -> str | None:
        return f"fp-{next(counter)}"

    clicks: list[tuple[str, dict[str, Any]]] = []
    tools = [
        _billable_tool("click", clicks),
        _perception_tool("observe", _REJECTION_OBSERVE),
        make_finish_tool(
            page_fingerprint=never_quiet,
            activity=activity,
            settle_wait_seconds=0.01,
            failure_settle_max_seconds=0.05,
        ),
    ]
    script = [
        [("click", {"selector": "#submit"})],
        [("finish", {"status": "failed", "reason": "rejected"})],
        [("observe", {})],
        [("finish", {"status": "failed", "reason": "rejected (re-verified)"})],
    ]
    started = time.monotonic()
    outcome, _ = await _run(script, tools, activity=activity)
    assert outcome.status == "failed"
    assert outcome.reason == "rejected (re-verified)"
    assert time.monotonic() - started < 5.0


@pytest.mark.asyncio
async def test_finish_terminated_after_click_is_never_gated() -> None:
    # terminate_criterion verdicts stay cheap: terminated never consults the page, even with
    # trigger activity in the window.
    activity = ActivityRecency()
    fingerprint, fp_calls = _fingerprint_seq(["fp"])
    clicks: list[tuple[str, dict[str, Any]]] = []
    tools = [
        _billable_tool("click", clicks),
        make_finish_tool(page_fingerprint=fingerprint, activity=activity, settle_wait_seconds=0.0),
    ]
    script = [
        [("click", {"selector": "#submit"})],
        [("finish", {"status": "terminated", "reason": "terminate criterion met"})],
    ]
    outcome, _ = await _run(script, tools, activity=activity)
    assert outcome.status == "terminated"
    assert fp_calls["n"] == 0


@pytest.mark.asyncio
async def test_failure_gate_off_without_activity_tracker() -> None:
    # Fenced like the completed-side probe: without an activity tracker (bare callers) the failure
    # path keeps its pre-gate behavior — first finish(failed) accepted, page never sampled.
    fingerprint, fp_calls = _fingerprint_seq(["fp"])
    clicks: list[tuple[str, dict[str, Any]]] = []
    tools = [
        _billable_tool("click", clicks),
        make_finish_tool(page_fingerprint=fingerprint, settle_wait_seconds=0.0),
    ]
    script = [
        [("click", {"selector": "#submit"})],
        [("finish", {"status": "failed", "reason": "blocked"})],
    ]
    outcome, _ = await _run(script, tools)
    assert outcome.status == "failed"
    assert fp_calls["n"] == 0


@pytest.mark.asyncio
async def test_failure_gate_skips_when_deadline_headroom_is_short() -> None:
    # A deferral issued near the run deadline cannot complete its observe + re-finish cycle: the
    # loop would convert the honest verdict into budget_exhausted. With thin deadline headroom the
    # gate accepts the verdict as-is.
    activity = ActivityRecency()
    fingerprint, fp_calls = _fingerprint_seq(["fp"])
    clicks: list[tuple[str, dict[str, Any]]] = []
    tools = [
        _billable_tool("click", clicks),
        make_finish_tool(
            page_fingerprint=fingerprint,
            activity=activity,
            settle_wait_seconds=0.0,
            deadline_at=time.monotonic() + 5.0,
        ),
    ]
    script = [
        [("click", {"selector": "#submit"})],
        [("finish", {"status": "failed", "reason": "blocked near the deadline"})],
    ]
    outcome, _ = await _run(script, tools, activity=activity)
    assert outcome.status == "failed"
    assert outcome.reason == "blocked near the deadline"
    assert fp_calls["n"] == 0


@pytest.mark.asyncio
async def test_failure_gate_skips_when_tool_call_budget_is_short() -> None:
    # Same conversion risk on the tool-call cap: the deferral cycle needs the finish + observe +
    # re-finish calls, so with fewer remaining the verdict is accepted as-is.
    activity = ActivityRecency()
    fingerprint, fp_calls = _fingerprint_seq(["fp"])
    clicks: list[tuple[str, dict[str, Any]]] = []
    tools = [
        _billable_tool("click", clicks),
        make_finish_tool(page_fingerprint=fingerprint, activity=activity, settle_wait_seconds=0.0),
    ]
    script = [
        [("click", {"selector": "#submit"})],
        [("finish", {"status": "failed", "reason": "blocked with two calls left"})],
    ]
    outcome, _ = await _run(script, tools, activity=activity, max_tool_calls=3)
    assert outcome.status == "failed"
    assert outcome.reason == "blocked with two calls left"
    assert fp_calls["n"] == 0


@pytest.mark.asyncio
async def test_failure_gate_never_trips_the_perception_stall_terminator() -> None:
    # A deferral-forced observe must never be the identical snapshot that trips the stall
    # terminator — that would replace the model's accurate failure reason with a generic stall
    # termination. With the streak one short of the terminator the gate accepts the verdict.
    activity = ActivityRecency()
    fingerprint, fp_calls = _fingerprint_seq(["fp"])
    clicks: list[tuple[str, dict[str, Any]]] = []
    tools = [
        _billable_tool("click", clicks),
        _perception_tool("observe", "url=x frozen behind a gate"),
        make_finish_tool(page_fingerprint=fingerprint, activity=activity, settle_wait_seconds=0.0),
    ]
    script = [
        [("observe", {})],
        [("observe", {})],
        [("click", {"selector": "#submit"})],
        [("finish", {"status": "failed", "reason": "page is frozen behind a gate"})],
    ]
    outcome, _ = await _run(script, tools, activity=activity, stall_terminate_after=3, stall_nudge_after=2)
    assert outcome.status == "failed"
    assert outcome.reason == "page is frozen behind a gate"
    assert fp_calls["n"] == 0


@pytest.mark.asyncio
async def test_deferral_forced_single_reobserve_keeps_the_honest_verdict_at_the_streak_edge() -> None:
    # Two short of the terminator the gate still defers, and the deferral asks for ONE re-observe.
    # Counting per result means that one identical probe lands exactly one short, so the model's
    # re-issued failure verdict is read instead of being replaced by a stall termination. (Answering
    # the deferral with two identical probes in one batch does cross the threshold — that shape is
    # off-instruction and behaves the same before this change.)
    activity = ActivityRecency()
    fingerprint, fp_calls = _fingerprint_seq(["fp"])
    clicks: list[tuple[str, dict[str, Any]]] = []
    tools = [
        _billable_tool("click", clicks),
        _perception_tool("observe", "url=x frozen behind a gate"),
        make_finish_tool(page_fingerprint=fingerprint, activity=activity, settle_wait_seconds=0.0),
    ]
    script = [
        [("observe", {})],
        [("observe", {})],
        [("click", {"selector": "#submit"})],
        [("finish", {"status": "failed", "reason": "page is frozen behind a gate"})],
        [("observe", {})],
        [("finish", {"status": "failed", "reason": "still frozen"})],
    ]
    outcome, _ = await _run(script, tools, activity=activity, stall_terminate_after=4, stall_nudge_after=2)
    assert outcome.status == "failed"
    assert outcome.reason == "still frozen"
    assert fp_calls["n"] == 1


@pytest.mark.asyncio
async def test_failure_gate_reads_tool_call_budget_per_call_not_per_turn() -> None:
    # A batched action+finish turn consumes calls after the turn-start snapshot; the gate must read
    # the refreshed counter or its deferral converts the honest verdict into budget_exhausted.
    activity = ActivityRecency()
    fingerprint, fp_calls = _fingerprint_seq(["fp"])
    clicks: list[tuple[str, dict[str, Any]]] = []
    tools = [
        _billable_tool("click", clicks),
        make_finish_tool(page_fingerprint=fingerprint, activity=activity, settle_wait_seconds=0.0),
    ]
    script = [
        [("click", {"selector": "#a"}), ("click", {"selector": "#b"})],
        [("click", {"selector": "#submit"}), ("finish", {"status": "failed", "reason": "blocked at the call cap"})],
    ]
    outcome, _ = await _run(script, tools, activity=activity, max_tool_calls=5)
    assert outcome.status == "failed"
    assert outcome.reason == "blocked at the call cap"
    assert fp_calls["n"] == 0


@pytest.mark.asyncio
async def test_failure_gate_skips_when_token_budget_is_short() -> None:
    # Near the token ceiling the deferral cycle cannot fund its two extra turns; the verdict is
    # accepted rather than converted into budget_exhausted (max_tokens).
    activity = ActivityRecency()
    fingerprint, fp_calls = _fingerprint_seq(["fp"])
    clicks: list[tuple[str, dict[str, Any]]] = []
    tools = [
        _billable_tool("click", clicks),
        make_finish_tool(page_fingerprint=fingerprint, activity=activity, settle_wait_seconds=0.0),
    ]
    script = [
        [("click", {"selector": "#submit"})],
        [("finish", {"status": "failed", "reason": "blocked near the token ceiling"})],
    ]
    outcome, _ = await _run(script, tools, activity=activity, max_tokens=35)
    assert outcome.status == "failed"
    assert outcome.reason == "blocked near the token ceiling"
    assert fp_calls["n"] == 0


@pytest.mark.asyncio
async def test_enter_typed_submission_arms_the_evidence_gate_but_plain_typing_does_not() -> None:
    # An Enter-typed submission (press_enter=true) carries the same async tail as a click; a
    # plain field fill does not and must stay ungated.
    activity = ActivityRecency()
    fingerprint, _ = _fingerprint_seq(["fp"])
    typed: list[tuple[str, dict[str, Any]]] = []
    tools = [
        _billable_tool("type", typed),
        _perception_tool("observe", _REJECTION_OBSERVE),
        make_finish_tool(page_fingerprint=fingerprint, activity=activity, settle_wait_seconds=0.0),
    ]
    script = [
        [("type", {"selector": "#q", "text": "answer", "press_enter": True})],
        [("finish", {"status": "failed", "reason": "rejected"})],
        [("observe", {})],
        [("finish", {"status": "failed", "reason": "rejected (re-verified)"})],
    ]
    outcome, _ = await _run(script, tools, activity=activity)
    assert outcome.status == "failed"
    assert outcome.reason == "rejected (re-verified)"

    activity = ActivityRecency()
    fingerprint, fp_calls = _fingerprint_seq(["fp"])
    tools = [
        _billable_tool("type", typed),
        make_finish_tool(page_fingerprint=fingerprint, activity=activity, settle_wait_seconds=0.0),
    ]
    script = [
        [("type", {"selector": "#q", "text": "answer"})],
        [("finish", {"status": "failed", "reason": "missing required data"})],
    ]
    outcome, _ = await _run(script, tools, activity=activity)
    assert outcome.status == "failed"
    assert fp_calls["n"] == 0


@pytest.mark.asyncio
async def test_press_key_enter_arms_the_gate_but_escape_does_not() -> None:
    # Only submit-shaped key presses carry an async submission tail; Escape/Tab/arrows are
    # navigation and must not buy an evidence check.
    activity = ActivityRecency()
    fingerprint, _ = _fingerprint_seq(["fp"])
    presses: list[tuple[str, dict[str, Any]]] = []
    tools = [
        _billable_tool("press_key", presses),
        _perception_tool("observe", _REJECTION_OBSERVE),
        make_finish_tool(page_fingerprint=fingerprint, activity=activity, settle_wait_seconds=0.0),
    ]
    script = [
        [("press_key", {"key": "Enter"})],
        [("finish", {"status": "failed", "reason": "rejected"})],
        [("observe", {})],
        [("finish", {"status": "failed", "reason": "rejected (re-verified)"})],
    ]
    outcome, _ = await _run(script, tools, activity=activity)
    assert outcome.status == "failed"
    assert outcome.reason == "rejected (re-verified)"

    activity = ActivityRecency()
    fingerprint, fp_calls = _fingerprint_seq(["fp"])
    tools = [
        _billable_tool("press_key", presses),
        make_finish_tool(page_fingerprint=fingerprint, activity=activity, settle_wait_seconds=0.0),
    ]
    script = [
        [("press_key", {"key": "Escape"})],
        [("finish", {"status": "failed", "reason": "modal would not close"})],
    ]
    outcome, _ = await _run(script, tools, activity=activity)
    assert outcome.status == "failed"
    assert fp_calls["n"] == 0


@pytest.mark.asyncio
async def test_deferral_cycle_with_wait_fits_the_reserved_budget() -> None:
    # The deferral message invites an optional wait before the evidence observe, so the reserved
    # cycle is wait + observe + re-finish: a deferral granted at the reservation edge must let all
    # three calls run instead of converting the verdict into budget_exhausted at the cap.
    activity = ActivityRecency()
    fingerprint, _ = _fingerprint_seq(["fp"])
    clicks: list[tuple[str, dict[str, Any]]] = []
    waits: list[tuple[str, dict[str, Any]]] = []
    tools = [
        _billable_tool("click", clicks),
        _recording_tool("wait", waits),
        _perception_tool("observe", _REJECTION_OBSERVE),
        make_finish_tool(page_fingerprint=fingerprint, activity=activity, settle_wait_seconds=0.0),
    ]
    script = [
        [("click", {"selector": "#submit"})],
        [("finish", {"status": "failed", "reason": "rejected"})],
        [("wait", {"seconds": 3})],
        [("observe", {})],
        [("finish", {"status": "failed", "reason": "rejected (re-verified after wait)"})],
    ]
    outcome, _ = await _run(script, tools, activity=activity, max_tool_calls=5)
    assert outcome.status == "failed"
    assert outcome.reason == "rejected (re-verified after wait)"
    assert len(waits) == 1


_PENDING = "the submit control still reads 'Submitting…'"
_HELD = "still shows a submission in flight"


def _pending_probe(results: str | None | list[str | None]):
    """Pending-marker fake returning each marker in order (last repeats), recording every selector it
    was asked about."""
    seq = results if isinstance(results, list) else [results]
    asked: list[str] = []

    async def probe(selector: str) -> str | None:
        asked.append(selector)
        return seq[min(len(asked) - 1, len(seq) - 1)]

    return probe, asked


def _held_messages(outcome) -> list[dict[str, Any]]:
    return [m for m in outcome.messages if m.get("role") == "tool" and _HELD in str(m.get("content"))]


@pytest.mark.asyncio
async def test_a_frozen_control_holds_the_verdict_once_and_then_gets_out_of_the_way() -> None:
    # The measured shape: submit clicked, page frozen at "Submitting…", DOM static. The settle probe
    # cannot object to that — a frozen page is maximally stable — so a verdict taken after a click has
    # to be gated on what the page still SHOWS, not on whether it moved. The hold buys the model the
    # one look it never took; the verdict it then insists on is the verdict the run reports, even
    # with the control still frozen. One deferral, one probe, no verdict of the gate's own.
    probe, asked = _pending_probe(_PENDING)
    watch = SubmitWatch(selector="#submit")
    script = [
        [("finish", {"status": "completed", "reason": "submitted the application"})],
        [("finish", {"status": "completed", "reason": "still looks submitted to me"})],
    ]
    outcome, _ = await _run(script, [make_finish_tool(pending_marker=probe, submit_watch=watch)])
    assert outcome.status == "completed", outcome.status
    assert outcome.reason == "still looks submitted to me"
    assert len(_held_messages(outcome)) == 1, outcome.messages
    assert asked == ["#submit"]
    assert watch.deferred is True


@pytest.mark.asyncio
async def test_a_confirmed_page_completes_even_though_a_submit_just_fired() -> None:
    # The control, and the direction that decides whether the gate is safe: a run whose clicked
    # control shows nothing pending must complete unimpeded and never consult the page twice.
    probe, asked = _pending_probe(None)
    watch = SubmitWatch(selector="#submit")
    script = [[("finish", {"status": "completed", "reason": "confirmation shown"})]]
    outcome, _ = await _run(script, [make_finish_tool(pending_marker=probe, submit_watch=watch)])
    assert outcome.status == "completed"
    assert outcome.reason == "confirmation shown"
    assert asked == ["#submit"]


@pytest.mark.asyncio
async def test_a_pending_marker_does_not_gate_a_verdict_no_submit_preceded() -> None:
    # Scoped to verdicts that follow a click on a control. A page that merely happens to render a
    # spinner somewhere must not hold up a run that never acted on one — that would be the gate
    # over-firing into the mirror defect.
    probe, asked = _pending_probe(_PENDING)
    script = [[("finish", {"status": "completed", "reason": "read the page, nothing to submit"})]]
    outcome, _ = await _run(script, [make_finish_tool(pending_marker=probe, submit_watch=SubmitWatch())])
    assert outcome.status == "completed"
    assert asked == []


@pytest.mark.asyncio
async def test_the_pending_gate_asks_about_the_control_that_was_acted_on() -> None:
    # "Is anything on this page busy?" strands a finished run on an unrelated widget or a stale modal
    # the app left in the DOM. The gate is handed the selector the click named, and a probe that
    # finds nothing pending THERE lets the verdict stand however busy the rest of the page looks.
    asked: list[str] = []

    async def marker_for(selector: str) -> str | None:
        asked.append(selector)
        return _PENDING if selector == "#submit" else None

    outcome, _ = await _run(
        [[("finish", {"status": "completed", "reason": "submitted"})]],
        [make_finish_tool(pending_marker=marker_for, submit_watch=SubmitWatch(selector="#dismiss-banner"))],
    )
    assert outcome.status == "completed"
    assert asked == ["#dismiss-banner"]


@pytest.mark.asyncio
async def test_the_pending_gate_runs_before_the_settle_probe() -> None:
    # A page frozen mid-submit is maximally stable AND never settles for the sampler; whichever gate
    # runs first owns the first answer the model gets, and the settle message ("wait for it to
    # settle, re-observe") is the one that produced the false completion.
    counter = iter(range(100))

    async def never_settles() -> str | None:
        return f"fp-{next(counter)}"

    probe, asked = _pending_probe(_PENDING)
    watch = SubmitWatch(selector="#submit")
    script = [[("finish", {"status": "completed", "reason": f"try {i}"})] for i in range(4)]
    outcome, _ = await _run(
        script,
        [
            make_finish_tool(
                page_fingerprint=never_settles,
                pending_marker=probe,
                submit_watch=watch,
                settle_wait_seconds=0.0,
            )
        ],
        max_turns=6,
    )
    finish_results = [m for m in outcome.messages if m.get("role") == "tool" and m.get("name") == "finish"]
    assert len(finish_results) >= 2, outcome.messages
    assert _HELD in str(finish_results[0]["content"]), finish_results[0]
    assert "still rendering" in str(finish_results[1]["content"]), finish_results[1]
    assert outcome.status == "completed", outcome.status
    assert asked == ["#submit"]


@pytest.mark.asyncio
async def test_a_captcha_solve_after_the_submit_click_keeps_the_gate_on_that_click() -> None:
    # The live sequence: click submit, the page raises a challenge, solve_captcha runs, the model
    # calls finish while the submit is still frozen. A captcha dispatch names no control, so letting
    # it overwrite the record leaves the gate asking about nothing and the frozen submit sails
    # through unheld.
    probe, asked = _pending_probe(_PENDING)
    watch = SubmitWatch()
    activity = ActivityRecency()
    clicks: list[tuple[str, dict[str, Any]]] = []
    tools = [
        _billable_tool("click", clicks),
        _captcha_tool(["the captcha was not solved"]),
        make_finish_tool(pending_marker=probe, submit_watch=watch, activity=activity),
    ]
    script = [
        [("click", {"selector": "#submit"})],
        [("solve_captcha", {})],
        [("finish", {"status": "completed", "reason": "submitted the application"})],
        [("finish", {"status": "completed", "reason": "still looks submitted to me"})],
    ]
    outcome, _ = await _run(script, tools, activity=activity, submit_watch=watch)
    assert outcome.status == "completed", outcome.status
    assert len(_held_messages(outcome)) == 1, outcome.messages
    assert asked == ["#submit"]


@pytest.mark.asyncio
async def test_the_loop_records_the_clicked_control_for_the_pending_gate() -> None:
    # The gate only ever fires on what the loop recorded, so the record has to be written by the
    # loop's own action path — a gate wired to a watch nothing ever writes is a gate that never runs.
    probe, asked = _pending_probe(None)
    watch = SubmitWatch()
    clicks: list[tuple[str, dict[str, Any]]] = []
    tools = [_billable_tool("click", clicks), make_finish_tool(pending_marker=probe, submit_watch=watch)]
    script = [
        [("click", {"selector": "#submit"})],
        [("finish", {"status": "completed", "reason": "submitted"})],
    ]
    outcome, _ = await _run(script, tools, submit_watch=watch)
    assert outcome.status == "completed"
    assert asked == ["#submit"]


@pytest.mark.asyncio
async def test_the_pending_gate_survives_a_long_wait_after_the_click() -> None:
    # Waiting for a slow submission is exactly what the gate asks the model to do, so a turn window
    # would expire precisely on the runs that obeyed it. The probe is the arbiter: however many
    # observes and waits separate the click from the verdict, the control is still the subject.
    probe, asked = _pending_probe(_PENDING)
    watch = SubmitWatch()
    activity = ActivityRecency()
    clicks: list[tuple[str, dict[str, Any]]] = []
    waits: list[tuple[str, dict[str, Any]]] = []
    tools = [
        _billable_tool("click", clicks),
        _recording_tool("wait", waits),
        _perception_tool("observe", [f"url=x poll={i}" for i in range(5)]),
        make_finish_tool(pending_marker=probe, submit_watch=watch, activity=activity),
    ]
    script = [
        [("click", {"selector": "#submit"})],
        [("observe", {})],
        [("wait", {"time_ms": 2000})],
        [("observe", {})],
        [("wait", {"time_ms": 2000})],
        [("observe", {})],
        [("finish", {"status": "completed", "reason": "submitted"})],
        [("finish", {"status": "completed", "reason": "still looks submitted to me"})],
    ]
    outcome, _ = await _run(script, tools, activity=activity, submit_watch=watch)
    assert outcome.status == "completed", outcome.status
    assert len(_held_messages(outcome)) == 1, outcome.messages
    assert asked == ["#submit"]


@pytest.mark.asyncio
async def test_re_clicking_after_a_hold_cannot_burn_the_run_out_of_budget() -> None:
    # A click re-arms the watch, so a model that answers each hold by clicking again gets held again
    # — the only way this gate spends more than one turn on a page. It must still land on the
    # model's own verdict: with the last hold taken on the turn the run needed to answer it, the
    # outcome would be budget_exhausted, which is unmapped and lands on failed — a false failure
    # invented by the gate. The headroom reservation is what stops the last one being taken.
    probe, asked = _pending_probe(_PENDING)
    watch = SubmitWatch()
    activity = ActivityRecency()
    clicks: list[tuple[str, dict[str, Any]]] = []
    tools = [
        _billable_tool("click", clicks),
        make_finish_tool(pending_marker=probe, submit_watch=watch, activity=activity),
    ]
    script = [
        [("click", {"selector": "#submit"})],
        [("finish", {"status": "completed", "reason": "submitted"})],
    ] * 6
    outcome, _ = await _run(script, tools, activity=activity, submit_watch=watch, max_turns=9)
    assert outcome.status == "completed", (outcome.status, outcome.reason)
    assert len(_held_messages(outcome)) >= 1, outcome.messages
    assert asked, asked


@pytest.mark.asyncio
async def test_actions_that_name_no_control_never_arm_the_pending_gate() -> None:
    # An Enter press and a type-that-pressed-Enter submit through a control they do not name, and a
    # captcha dispatch names none at all: the selector they carry is a text field whose value is the
    # model's own typed text, which would read as a marker the page never rendered.
    sink: list[tuple[str, dict[str, Any]]] = []
    cases: list[tuple[str, dict[str, Any], ToolSpec]] = [
        (
            "type",
            {"selector": "#search", "text": "Processing Engineer", "press_enter": True},
            _billable_tool("type", sink),
        ),
        ("press_key", {"key": "Enter", "selector": "#search"}, _billable_tool("press_key", sink)),
        ("solve_captcha", {}, _captcha_tool(["the captcha was not solved"])),
    ]
    for tool_name, args, action_tool in cases:
        probe, asked = _pending_probe(_PENDING)
        watch = SubmitWatch()
        tools = [action_tool, make_finish_tool(pending_marker=probe, submit_watch=watch)]
        script = [[(tool_name, args)], [("finish", {"status": "completed", "reason": "done"})]]
        outcome, _ = await _run(script, tools, submit_watch=watch)
        assert outcome.status == "completed", (tool_name, outcome.status)
        assert asked == [], (tool_name, asked)


@pytest.mark.asyncio
async def test_navigating_away_clears_the_recorded_control() -> None:
    # The run left the page deliberately; the control it clicked went with it, so a marker found at
    # that selector on the new page belongs to something the run never submitted. `navigate` is
    # neither billable nor recordable in the production tool set, so the clear has to be reachable
    # from a plain tool.
    probe, asked = _pending_probe(_PENDING)
    watch = SubmitWatch()
    clicks: list[tuple[str, dict[str, Any]]] = []
    navigations: list[tuple[str, dict[str, Any]]] = []
    tools = [
        _billable_tool("click", clicks),
        _recording_tool("navigate", navigations),
        make_finish_tool(pending_marker=probe, submit_watch=watch),
    ]
    script = [
        [("click", {"selector": "#submit"})],
        [("navigate", {"url": "https://example.test/next"})],
        [("finish", {"status": "completed", "reason": "read the next page"})],
    ]
    outcome, _ = await _run(script, tools, submit_watch=watch)
    assert outcome.status == "completed", outcome.status
    assert asked == []


@pytest.mark.asyncio
async def test_a_pending_verdict_is_not_held_without_the_turns_to_resolve_it() -> None:
    # Holding the verdict costs a turn. With no turn left to spend, the deferral does not buy a
    # re-verification — it ends the run budget_exhausted, which is unmapped and lands on failed,
    # turning an honest hold into the false failure this gate exists to avoid.
    probe, asked = _pending_probe(_PENDING)
    watch = SubmitWatch(selector="#submit")
    activity = ActivityRecency(turn=2, turns_remaining=1, tool_calls_remaining=10)
    script = [[("finish", {"status": "completed", "reason": "submitted"})]]
    outcome, _ = await _run(script, [make_finish_tool(pending_marker=probe, submit_watch=watch, activity=activity)])
    assert outcome.status == "completed", outcome.status
    assert asked == []


@pytest.mark.asyncio
async def test_a_pending_verdict_is_not_held_without_the_tool_calls_to_resolve_it() -> None:
    # Same floor on the other axis: the hold's re-observe cycle has no calls to run in, so the
    # deferral would end the run budget_exhausted instead of buying the look it asks for.
    probe, asked = _pending_probe(_PENDING)
    watch = SubmitWatch()
    activity = ActivityRecency()
    clicks: list[tuple[str, dict[str, Any]]] = []
    tools = [
        _billable_tool("click", clicks),
        make_finish_tool(pending_marker=probe, submit_watch=watch, activity=activity),
    ]
    script = [
        [("click", {"selector": "#submit"})],
        [("finish", {"status": "completed", "reason": "submitted with two calls left"})],
    ]
    outcome, _ = await _run(script, tools, activity=activity, submit_watch=watch, max_tool_calls=3)
    assert activity.tool_calls_remaining is not None and activity.tool_calls_remaining < FAILURE_EVIDENCE_MIN_TOOL_CALLS
    assert outcome.status == "completed", outcome.status
    assert asked == []


@pytest.mark.asyncio
async def test_a_pending_verdict_is_not_held_near_the_token_ceiling() -> None:
    # Near the token ceiling the hold cannot fund the re-verification it asks for; the run would end
    # budget_exhausted, which lands on failed — the false failure this gate exists to avoid.
    probe, asked = _pending_probe(_PENDING)
    watch = SubmitWatch()
    activity = ActivityRecency()
    clicks: list[tuple[str, dict[str, Any]]] = []
    tools = [
        _billable_tool("click", clicks),
        make_finish_tool(pending_marker=probe, submit_watch=watch, activity=activity),
    ]
    script = [
        [("click", {"selector": "#submit"})],
        [("finish", {"status": "completed", "reason": "submitted near the token ceiling"})],
    ]
    outcome, _ = await _run(script, tools, activity=activity, submit_watch=watch, max_tokens=35)
    assert activity.tokens_remaining is not None
    assert activity.tokens_remaining < FAILURE_EVIDENCE_MIN_TURNS * activity.last_turn_tokens
    assert outcome.status == "completed", outcome.status
    assert outcome.reason == "submitted near the token ceiling"
    assert asked == []


@pytest.mark.asyncio
async def test_a_pending_verdict_is_not_held_when_a_perception_stall_is_imminent() -> None:
    # With the identical-snapshot streak one short of the stall terminator, the observe the hold asks
    # for is the snapshot that trips it — replacing this gate's verdict with a generic stall
    # termination. The verdict stands instead.
    probe, asked = _pending_probe(_PENDING)
    watch = SubmitWatch()
    activity = ActivityRecency()
    clicks: list[tuple[str, dict[str, Any]]] = []
    tools = [
        _billable_tool("click", clicks),
        _perception_tool("observe", "url=x frozen behind a gate"),
        make_finish_tool(pending_marker=probe, submit_watch=watch, activity=activity),
    ]
    script = [
        [("observe", {})],
        [("observe", {})],
        [("click", {"selector": "#submit"})],
        [("finish", {"status": "completed", "reason": "submitted"})],
    ]
    outcome, _ = await _run(
        script, tools, activity=activity, submit_watch=watch, stall_terminate_after=3, stall_nudge_after=2
    )
    assert activity.perception_stall_imminent is True
    assert outcome.status == "completed", outcome.status
    assert outcome.reason == "submitted"
    assert asked == []


@pytest.mark.asyncio
async def test_a_pending_verdict_is_not_held_without_the_deadline_headroom_to_resolve_it() -> None:
    # Thin deadline headroom: the verdict stands rather than becoming a deferral the run has no time
    # to answer.
    probe, asked = _pending_probe(_PENDING)
    finish = make_finish_tool(
        pending_marker=probe,
        submit_watch=SubmitWatch(selector="#submit"),
        deadline_at=time.monotonic() + 5.0,
    )
    result = await finish.handler({"status": "completed", "reason": "submitted near the deadline"})
    assert (result.data or {}).get("status") == "completed", result
    assert asked == []


@pytest.mark.asyncio
async def test_a_failing_pending_probe_fails_open() -> None:
    # A positive observation gates the verdict; a probe that blew up observed nothing, and nothing is
    # not evidence of pending. Holding runs on probe flakiness is the mirror defect.
    calls = {"n": 0}

    async def broken(selector: str) -> str | None:
        calls["n"] += 1
        raise RuntimeError("probe blew up")

    script = [[("finish", {"status": "completed", "reason": "submitted"})]]
    with capture_logs() as logs:
        outcome, _ = await _run(
            script, [make_finish_tool(pending_marker=broken, submit_watch=SubmitWatch(selector="#submit"))]
        )
    assert outcome.status == "completed", outcome.status
    assert calls["n"] == 1
    assert any(log.get("log_level") == "warning" and "pending-marker" in str(log.get("event")) for log in logs), logs


@pytest.mark.asyncio
@pytest.mark.parametrize("seed", range(200))
async def test_every_live_verdict_is_preceded_by_exactly_one_warning_on_its_tool_streak(seed: int) -> None:
    # The warning reads the per-tool counter because that counter moves by exactly one per read:
    # any live verdict needs tool_identical >= terminate > nudge on the same streak, so the streak
    # passed the nudge threshold exactly once, and the loop must have delivered that warning.
    # ``live`` is a min of two counters and jumps, so an equality on it warns zero or several
    # times. Driven through the loop, one probe per turn, against an independent by-one count.
    rng = random.Random(seed)
    nudge_after, terminate_after = 2, 4
    sequence = [(rng.choice(["#a", "#b", "#c"]), rng.choice(["X", "Y"])) for _ in range(rng.randint(1, 40))]
    contents = iter(content for _, content in sequence)

    async def handler(args: dict[str, Any]) -> ToolResult:
        return ToolResult.ok(next(contents))

    probe = ToolSpec(
        name="get_html",
        description="g",
        parameters={"type": "object", "properties": {}},
        handler=handler,
        compactable=True,
    )
    script: list[list[tuple[str, dict[str, Any]]]] = [[("get_html", {"selector": s})] for s, _ in sequence]
    script.append([("finish", {"status": "failed", "reason": "blocked"})])
    outcome, _ = await _run(
        script,
        [probe, make_finish_tool()],
        max_turns=200,
        max_tool_calls=500,
        stall_nudge_after=nudge_after,
        stall_terminate_after=terminate_after,
    )

    crossings_on_current_streak = 0
    crossings = 0
    previous: str | None = None
    count = 0
    ledger = _PerceptionLedger()
    for selector, content in sequence:
        count = count + 1 if content == previous else 1
        previous = content
        if count == 1:
            crossings_on_current_streak = 0
        if count == nudge_after:
            crossings += 1
            crossings_on_current_streak += 1
        if ledger.record(("get_html", selector), content).live >= terminate_after:
            assert outcome.status == "terminated"
            assert crossings_on_current_streak == 1
            break
    assert len(_stall_warnings(outcome)) == crossings


@pytest.mark.asyncio
async def test_stall_verdict_on_a_live_jump_past_both_thresholds_still_carries_one_warning() -> None:
    # A frozen region re-read between reads of a live sibling pins the tool counter at 1 while the
    # region's own streak climbs; 14 reads of other regions returning the frozen bytes then lift the
    # tool counter to 14, and the next read of the region takes ``live`` from 4 to 15 in ONE
    # snapshot. A warning keyed on ``live`` never sees the nudge threshold; one keyed on the
    # by-one tool counter was delivered eight snapshots earlier.
    async def handler(args: dict[str, Any]) -> ToolResult:
        return ToolResult.ok(
            "<div>live sibling</div>" if args.get("selector") == "#sibling" else "<div>Please wait</div>"
        )

    probe = ToolSpec(
        name="get_html",
        description="g",
        parameters={"type": "object", "properties": {}},
        handler=handler,
        compactable=True,
    )
    selectors: list[str] = []
    for _ in range(14):
        selectors += ["#main", "#sibling"]
    selectors += ["#a"] * 5 + ["#b"] * 5 + ["#c"] * 4 + ["#main"]
    script: list[list[tuple[str, dict[str, Any]]]] = [[("get_html", {"selector": s})] for s in selectors]
    script.append([("finish", {"status": "failed", "reason": "blocked"})])
    outcome, _ = await _run(script, [probe, make_finish_tool()], max_turns=200, max_tool_calls=500)
    assert outcome.status == "terminated"
    assert outcome.reason.startswith(PERCEPTION_STALL_REASON_PREFIX)
    assert len(_stall_warnings(outcome)) == 1


@pytest.mark.asyncio
async def test_suppressed_main_fire_is_reported_once_per_run_like_the_shadow_event() -> None:
    # The argument-blind counter would have ENDED the run at its first trip, so a second trip after
    # the streak resets and re-climbs is not a second spared run. Both measurement streams count
    # runs, or their rates cannot be compared.
    async def handler(args: dict[str, Any]) -> ToolResult:
        return ToolResult.ok("changed" if args.get("selector") == "#break" else "Select One")

    probe = ToolSpec(
        name="get_html",
        description="g",
        parameters={"type": "object", "properties": {}},
        handler=handler,
        compactable=True,
    )
    selectors = [f"#dd-{i}" for i in range(4)] + ["#break"] + [f"#dd-{i}" for i in range(4)]
    script: list[list[tuple[str, dict[str, Any]]]] = [[("get_html", {"selector": s})] for s in selectors]
    script.append([("finish", {"status": "completed", "reason": "done"})])
    with capture_logs() as logs:
        outcome, _ = await _run(script, [probe, make_finish_tool()], stall_terminate_after=4, stall_nudge_after=2)
    assert outcome.status == "completed"
    assert len([entry for entry in logs if entry.get("event") == PERCEPTION_STALL_SUPPRESSED_EVENT]) == 1


@pytest.mark.asyncio
async def test_shadow_event_fires_at_the_configured_threshold() -> None:
    contents = ["state-A", "state-B"] * 10
    script = [[("observe", {})] for _ in range(20)]
    script.append([("finish", {"status": "completed", "reason": "done"})])
    tools = [_perception_tool("observe", contents), make_finish_tool()]
    with capture_logs() as logs:
        await _run(script, tools, stall_terminate_after=4)
    would_fire = [entry for entry in logs if entry.get("event") == PERCEPTION_STALL_SHADOW_EVENT]
    assert [entry["turn"] for entry in would_fire] == [5]


@pytest.mark.asyncio
async def test_disabling_the_stall_guard_silences_its_measurement_streams_too() -> None:
    contents = ["state-A", "state-B"] * 30
    script = [[("observe", {})] for _ in range(60)]
    script.append([("finish", {"status": "completed", "reason": "done"})])
    tools = [_perception_tool("observe", contents), make_finish_tool()]
    with capture_logs() as logs:
        outcome, _ = await _run(script, tools, max_turns=200, max_tool_calls=500, stall_terminate_after=None)
    assert outcome.status == "completed"
    events = {PERCEPTION_STALL_SHADOW_EVENT, PERCEPTION_STALL_SUPPRESSED_EVENT}
    assert not [entry for entry in logs if entry.get("event") in events]


@pytest.mark.asyncio
async def test_model_hidden_secret_values_are_scrubbed_from_the_tool_message_the_model_sees() -> None:
    """A model-hidden value (a magic sign-in link URL and its bare token) must never reach the
    LLM's view of a tool result — including a tool_error raised by a handler — while a
    registered-but-not-hidden secret (a TOTP code) still passes through untouched."""
    hidden_url = "https://example.test/magic?token=synthetictoken0123"
    hidden_token = "synthetictoken0123"
    visible_code = "123456"

    async def clean_handler(_args: dict[str, Any]) -> ToolResult:
        return ToolResult.ok("nothing sensitive here")

    async def linky_handler(_args: dict[str, Any]) -> ToolResult:
        return ToolResult.ok(f"now at {hidden_url} code {visible_code}")

    async def boom_handler(_args: dict[str, Any]) -> ToolResult:
        raise ValueError(f"failed at {hidden_url}")

    tools = [
        ToolSpec(name="clean", description="c", parameters={"type": "object", "properties": {}}, handler=clean_handler),
        ToolSpec(name="linky", description="l", parameters={"type": "object", "properties": {}}, handler=linky_handler),
        ToolSpec(name="boom", description="b", parameters={"type": "object", "properties": {}}, handler=boom_handler),
        make_finish_tool(),
    ]
    script = [
        [("clean", {}), ("linky", {}), ("boom", {})],
        [("finish", {"status": "completed", "reason": "done"})],
    ]

    ctx = SkyvernContext(task_id="tsk_hidden_link")
    ctx.register_secret_value(hidden_url, hide_from_model=True)
    ctx.register_secret_value(hidden_token, hide_from_model=True)
    ctx.register_secret_value(visible_code)
    skyvern_context.set(ctx)
    try:
        outcome, caller = await _run(script, tools)
    finally:
        skyvern_context.reset()

    assert outcome.status == "completed"
    tool_messages = {m["name"]: m["content"] for m in caller.message_history if m.get("role") == "tool"}
    assert tool_messages["clean"] == "nothing sensitive here"
    assert tool_messages["linky"] == f"now at [withheld: sign-in link] code {visible_code}"
    assert tool_messages["boom"] == "tool_error: ValueError: failed at [withheld: sign-in link]"

    assert ctx.runtime_secret_values == {hidden_url, hidden_token, visible_code}
    assert ctx.model_hidden_values == {hidden_url, hidden_token}


@pytest.mark.asyncio
async def test_on_pre_action_fires_before_dispatch_only_for_submit_shaped_actions() -> None:
    # The pre-action hook fires BEFORE the handler runs (after it the page may be the confirmation
    # page) and only for the loop's own submit-shaped predicate: any click, an Enter press, a type
    # that presses Enter. Perception, a plain type, a non-Enter key and solve_captcha never fire it.
    events: list[str] = []

    async def _pre(tool_name: str, args: dict[str, Any]) -> None:
        events.append(f"pre:{tool_name}")

    def _tool(name: str) -> ToolSpec:
        async def handler(args: dict[str, Any]) -> ToolResult:
            events.append(f"run:{name}")
            return ToolResult.ok("ok")

        spec = ToolSpec(name=name, description=name, parameters={"type": "object", "properties": {}}, handler=handler)
        spec.billable = name != "observe"
        return spec

    tools = [_tool(n) for n in ("observe", "click", "type", "press_key", "solve_captcha")]
    script: list[list[tuple[str, dict[str, Any]]]] = [
        [("observe", {})],
        [("type", {"selector": "#a", "text": "x"}), ("type", {"selector": "#b", "text": "y", "press_enter": True})],
        [("press_key", {"key": "Tab"}), ("press_key", {"key": "Enter"})],
        [("solve_captcha", {}), ("click", {"selector": "#submit"})],
        [("finish", {"status": "completed", "reason": "ok"})],
    ]
    outcome, _ = await _run(script, tools + [make_finish_tool()], on_pre_action=_pre)
    assert outcome.status == "completed"
    assert events == [
        "run:observe",
        "run:type",
        "pre:type",
        "run:type",
        "run:press_key",
        "pre:press_key",
        "run:press_key",
        "run:solve_captcha",
        "pre:click",
        "run:click",
    ]


@pytest.mark.asyncio
async def test_on_pre_action_failure_does_not_abort_the_action() -> None:
    async def _boom(tool_name: str, args: dict[str, Any]) -> None:
        raise RuntimeError("capture boom")

    clk: list[tuple[str, dict[str, Any]]] = []
    click = _recording_tool("click", clk)
    click.billable = True
    script: list[list[tuple[str, dict[str, Any]]]] = [
        [("click", {})],
        [("finish", {"status": "completed", "reason": "ok"})],
    ]
    outcome, _ = await _run(script, [click, make_finish_tool()], on_pre_action=_boom)
    assert outcome.status == "completed"
    assert len(clk) == 1


_SALT = "a" * 32
_SECRET_TEXT = "Boston-Zip-02134-sentinel"


def _record_dump(logs: list[dict[str, Any]]) -> str:
    return json.dumps(logs, default=str, sort_keys=True)


@pytest.mark.asyncio
async def test_tool_call_record_carries_a_stable_action_key_hash_and_never_the_value() -> None:
    # Same (tool, canonical args) → same hash within a run; a changed arg → a different hash. This is
    # what lets a Datadog query tell "the guard's key repeated N times" from "the text varied".
    calls: list[tuple[str, dict[str, Any]]] = []
    tools = [_recording_tool("type", calls), make_finish_tool()]
    script = [
        [("type", {"selector": "#city", "text": _SECRET_TEXT})],
        [("type", {"text": _SECRET_TEXT, "selector": "#city"})],  # key order differs, key identical
        [("type", {"selector": "#city", "text": _SECRET_TEXT + "x"})],
        [("finish", {"status": "completed", "reason": "ok"})],
    ]
    with capture_logs() as logs:
        await _run(script, tools, telemetry_salt=_SALT)
    records = [entry for entry in logs if entry["event"] == "taskv3 tool call finished"]
    hashes = [entry["action_key_hash"] for entry in records]
    assert len(hashes) == 4 and all(len(h) == 16 for h in hashes)
    assert hashes[0] == hashes[1] != hashes[2]
    canonical = json.dumps({"selector": "#city", "text": _SECRET_TEXT}, sort_keys=True)
    expected = hashlib.sha256(f"{_SALT}\x1ftype\x1f{canonical}".encode()).hexdigest()[:16]
    assert hashes[0] == expected
    dump = _record_dump(logs)
    assert _SECRET_TEXT not in dump and "#city" not in dump and _SALT not in dump


@pytest.mark.asyncio
async def test_observe_summary_cannot_shadow_the_attribution_fields() -> None:
    # A summary key named like a fixed field would otherwise raise at the log call on every observe.
    async def handler(args: dict[str, Any]) -> ToolResult:
        return ToolResult.ok("snap", data={"summary": {"probe_first_time": 7, "snapshot_digest": 1, "text_dropped": 2}})

    tools = [ToolSpec(name="observe", description="o", parameters={}, handler=handler, compactable=True)]
    script = [[("observe", {})], [("finish", {"status": "completed", "reason": "ok"})]]
    with capture_logs() as logs:
        outcome, _ = await _run(script, [*tools, make_finish_tool()], telemetry_salt=_SALT)
    assert outcome.status == "completed"
    record = [e for e in logs if e["event"] == "taskv3 tool call finished" and e["tool"] == "observe"][0]
    assert record["probe_first_time"] is True and len(record["snapshot_digest"]) == 16
    assert record["text_dropped"] == 2


@pytest.mark.asyncio
async def test_action_repeated_verdict_carries_the_key_hash_it_counted() -> None:
    calls: list[tuple[str, dict[str, Any]]] = []
    click = _recording_tool("click", calls)
    click.billable = True
    script = [[("click", {"selector": "#go"})] for _ in range(6)]
    with capture_logs() as logs:
        outcome, _ = await _run(
            script, [click, make_finish_tool()], action_nudge_after=2, action_terminate_after=3, telemetry_salt=_SALT
        )
    assert outcome.status == "terminated"
    repeated = [e for e in logs if e["event"] == "taskv3 loop action repeated"][0]
    finished = [e for e in logs if e["event"] == "taskv3 tool call finished"][0]
    assert repeated["action_key_hash"] == finished["action_key_hash"]


@pytest.mark.asyncio
async def test_action_key_hash_differs_across_runs_without_an_injected_salt() -> None:
    calls: list[tuple[str, dict[str, Any]]] = []
    script = [[("type", {"selector": "#city", "text": "x"})], [("finish", {"status": "completed", "reason": "ok"})]]
    seen = []
    for _ in range(2):
        with capture_logs() as logs:
            await _run(script, [_recording_tool("type", calls), make_finish_tool()])
        seen.append([e["action_key_hash"] for e in logs if e["event"] == "taskv3 tool call finished"][0])
    assert seen[0] != seen[1]


@pytest.mark.asyncio
async def test_perception_records_carry_snapshot_digest_and_first_time_flag() -> None:
    contents = ["page-A " + _SECRET_TEXT, "page-A " + _SECRET_TEXT, "page-B " + _SECRET_TEXT]
    tools = [_perception_tool("observe", contents), _recording_tool("click", []), make_finish_tool()]
    script = [
        [("observe", {"selector": "#a"})],
        [("click", {"selector": "#btn"})],
        [("observe", {"selector": "#a"})],
        [("observe", {"selector": "#b"})],
        [("finish", {"status": "completed", "reason": "ok"})],
    ]
    with capture_logs() as logs:
        await _run(script, tools, telemetry_salt=_SALT)
    records = [entry for entry in logs if entry["event"] == "taskv3 tool call finished"]
    observes = [r for r in records if r["tool"] == "observe"]
    assert [r["probe_first_time"] for r in observes] == [True, False, True]
    digests = [r["snapshot_digest"] for r in observes]
    assert digests[0] == digests[1] != digests[2] and all(len(d) == 16 for d in digests)
    content_sha = hashlib.sha256(contents[0].encode()).hexdigest()
    assert digests[0] == hashlib.sha256(f"{_SALT}\x1f{content_sha}".encode()).hexdigest()[:16]
    # Action tools never enter the ledger, so they carry neither field — today's record shape holds.
    click = [r for r in records if r["tool"] == "click"][0]
    assert "snapshot_digest" not in click and "probe_first_time" not in click
    dump = _record_dump(logs)
    assert _SECRET_TEXT not in dump and _SALT not in dump


@pytest.mark.asyncio
async def test_stall_firing_lines_carry_the_compared_digest_and_key_hash() -> None:
    async def handler(args: dict[str, Any]) -> ToolResult:
        return ToolResult.ok("frozen " + _SECRET_TEXT)

    probe = ToolSpec(name="get_html", description="g", parameters={}, handler=handler, compactable=True)
    script: list[list[tuple[str, dict[str, Any]]]] = [[("get_html", {"selector": "#x"})] for _ in range(6)]
    with capture_logs() as logs:
        outcome, _ = await _run(script, [probe, make_finish_tool()], stall_terminate_after=4, telemetry_salt=_SALT)
    assert outcome.status == "terminated"
    stalled = [e for e in logs if e["event"] == "taskv3 loop perception stalled"][0]
    finished = [e for e in logs if e["event"] == "taskv3 tool call finished"][0]
    assert stalled["snapshot_digest"] == finished["snapshot_digest"]
    assert stalled["action_key_hash"] == finished["action_key_hash"]
    assert _SECRET_TEXT not in _record_dump(logs) and _SALT not in _record_dump(logs)


@pytest.mark.asyncio
async def test_shadow_and_suppressed_lines_carry_the_hash_fields() -> None:
    async def handler(args: dict[str, Any]) -> ToolResult:
        return ToolResult.ok("changed" if args.get("selector") == "#break" else "Select One")

    probe = ToolSpec(name="get_html", description="g", parameters={}, handler=handler, compactable=True)
    selectors = [f"#dd-{i}" for i in range(4)]
    script: list[list[tuple[str, dict[str, Any]]]] = [[("get_html", {"selector": s})] for s in selectors]
    script.append([("finish", {"status": "completed", "reason": "done"})])
    with capture_logs() as logs:
        await _run(
            script, [probe, make_finish_tool()], stall_terminate_after=4, stall_nudge_after=2, telemetry_salt=_SALT
        )
    suppressed = [e for e in logs if e["event"] == PERCEPTION_STALL_SUPPRESSED_EVENT][0]
    assert len(suppressed["snapshot_digest"]) == 16 and len(suppressed["action_key_hash"]) == 16

    contents = ["state-A", "state-B"] * 10
    script = [[("observe", {})] for _ in range(20)]
    script.append([("finish", {"status": "completed", "reason": "done"})])
    with capture_logs() as logs:
        await _run(
            script,
            [_perception_tool("observe", contents), make_finish_tool()],
            stall_terminate_after=4,
            telemetry_salt=_SALT,
        )
    shadow = [e for e in logs if e["event"] == PERCEPTION_STALL_SHADOW_EVENT][0]
    assert len(shadow["snapshot_digest"]) == 16 and len(shadow["action_key_hash"]) == 16


def _replaced_node_observe(counter: int) -> str:
    # A node-replacing framework loses data-tv3 with each rebuilt node, so observe re-mints values
    # from the monotonic counter; every semantic byte below is frozen across calls.
    return (
        "url=https://site.test/form title='Form' (2 interactive elements)\n"
        f"[[data-tv3=\"t{counter}\"]] input/text 'First name'\n"
        f"[[data-tv3=\"t{counter + 1}-1\"]] button 'Continue' *required"
    )


@pytest.mark.asyncio
async def test_marker_churn_on_a_frozen_page_still_trips_the_stall_guard_through_observe() -> None:
    # SKY-14658 Direction B mode 2: the re-minted marker values are the only bytes that change, so
    # byte-identity on the raw payload can never form a streak — the digest must be computed on
    # marker-canonicalized content for the guard to do its primary job on a re-rendering page.
    contents = [_replaced_node_observe(5 * i) for i in range(20)]
    script = [[("observe", {})] for _ in range(20)]
    script.append([("finish", {"status": "completed", "reason": "done"})])
    tools = [_perception_tool("observe", contents), make_finish_tool()]
    outcome, _ = await _run(script, tools, max_turns=200, max_tool_calls=500)
    assert outcome.status == "terminated"
    assert outcome.reason.startswith(PERCEPTION_STALL_REASON_PREFIX)


@pytest.mark.asyncio
async def test_marker_churn_on_a_frozen_page_still_trips_the_stall_guard_through_get_html() -> None:
    contents = [
        f'<form><input data-tv3="t{7 * i}" name="q"><button data-tv3="t{7 * i + 3}-2">Go</button></form>'
        for i in range(20)
    ]
    script = [[("get_html", {})] for _ in range(20)]
    script.append([("finish", {"status": "completed", "reason": "done"})])
    tools = [_perception_tool("get_html", contents), make_finish_tool()]
    outcome, _ = await _run(script, tools, max_turns=200, max_tool_calls=500)
    assert outcome.status == "terminated"
    assert outcome.reason.startswith(PERCEPTION_STALL_REASON_PREFIX)


@pytest.mark.asyncio
async def test_semantic_change_under_marker_churn_still_reads_as_progress() -> None:
    # The canonicalization may only merge snapshots whose every semantic byte matches: when the
    # page genuinely changes call over call (and markers churn too), the streak must keep resetting.
    contents = [_replaced_node_observe(5 * i).replace("'Form'", f"'Form step {i}'") for i in range(20)]
    script = [[("observe", {})] for _ in range(20)]
    script.append([("finish", {"status": "completed", "reason": "done"})])
    tools = [_perception_tool("observe", contents), make_finish_tool()]
    outcome, _ = await _run(script, tools, max_turns=200, max_tool_calls=500)
    assert outcome.status == "completed"
    assert not outcome.reason.startswith(PERCEPTION_STALL_REASON_PREFIX)


def test_canonicalization_normalizes_only_engine_minted_marker_values() -> None:
    # Minted values (t<counter>, optionally -<n> disambiguated) are identity handles, not page
    # semantics: both shapes normalize, in observe rendering and raw HTML alike.
    assert _canonical_perception_content('[[data-tv3="t12"]] input') == _canonical_perception_content(
        '[[data-tv3="t9004"]] input'
    )
    assert _canonical_perception_content('<a data-tv3="t3-1">x</a>') == _canonical_perception_content(
        '<a data-tv3="t77">x</a>'
    )
    # A page-authored data-tv3 value is page content like any other attribute — left alone.
    assert _canonical_perception_content('<a data-tv3="decoy">x</a>') != _canonical_perception_content(
        '<a data-tv3="other">x</a>'
    )
    # The positional menu markers are stable on a frozen page and stay significant.
    assert _canonical_perception_content('[[data-tv3-menu="2"]] row') != _canonical_perception_content(
        '[[data-tv3-menu="3"]] row'
    )


@pytest.mark.asyncio
async def test_a_marker_cut_open_by_the_get_html_truncation_does_not_leak_churn() -> None:
    # get_html truncates at a fixed byte budget BEFORE the loop hashes, so a marker straddling the
    # cut has no closing quote and its churning digits were the one leak canonicalization missed.
    frozen_prefix = "<form>" + "<input name=q>" * 10 + '<button data-tv3="t'
    contents = [f"{frozen_prefix}{100 + i}…[truncated at 20000 chars]" for i in range(20)]
    script = [[("get_html", {})] for _ in range(20)]
    script.append([("finish", {"status": "completed", "reason": "done"})])
    tools = [_perception_tool("get_html", contents), make_finish_tool()]
    outcome, _ = await _run(script, tools, max_turns=200, max_tool_calls=500)
    assert outcome.status == "terminated"
    assert outcome.reason.startswith(PERCEPTION_STALL_REASON_PREFIX)


def test_canonicalization_covers_a_marker_fragment_left_open_at_the_tail() -> None:
    # The cut can land anywhere in the value — after the digits, mid-digits, or before them.
    assert _canonical_perception_content(
        '<a data-tv3="t12…[truncated at 20000 chars]'
    ) == _canonical_perception_content('<a data-tv3="t907-3…[truncated at 20000 chars]')
    assert _canonical_perception_content('x data-tv3="t') == _canonical_perception_content('x data-tv3="t44-')
    # A closed marker earlier in the content does not shield the open tail fragment, and a closed
    # tail marker is not double-rewritten.
    assert _canonical_perception_content('<a data-tv3="t1">y</a><b data-tv3="t2') == _canonical_perception_content(
        '<a data-tv3="t9">y</a><b data-tv3="t8'
    )
    assert _canonical_perception_content('tail closed data-tv3="t5"') == 'tail closed data-tv3="*"'
