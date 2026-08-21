"""Unit tests for the Task V3 agent tool-loop.

A scripted fake ``LLMCaller`` emits queued tool_calls (in the same dict shape
``LLMCaller.call(raw_response=True)`` returns) so we can assert the loop's
behavior — on-demand perception, action batching, terminal finish, budget caps,
and error handling — without any real LLM or browser.
"""

from __future__ import annotations

import asyncio
import json
import time
from typing import Any

import pytest
from structlog.testing import capture_logs

from skyvern.exceptions import SkyvernContextWindowExceededError
from skyvern.forge.sdk.api.llm.exceptions import LLMProviderErrorRetryableTask
from skyvern.forge.taskv3.loop import (
    NO_TOOL_CALL_NUDGE,
    ActivityRecency,
    ToolHandler,
    ToolResult,
    ToolSpec,
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
